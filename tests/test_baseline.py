from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from spectrace.baseline import (
    ANSWER_KEY_MARKERS,
    dataset_hash,
    prompt_hash,
    render_baseline_prompt,
    run_baseline,
)
from spectrace.config import LLMSettings
from spectrace.llm import (
    RawGeneration,
    RetryExhaustedError,
    TransientLLMError,
    generate_prediction_with_retry,
)


ROOT = Path(__file__).parents[1]
DEMO_PACK = ROOT / "data" / "synthetic" / "demo_project"
PROMPT_PATH = ROOT / "prompts" / "baseline.md"


def _prediction_json(request_id: str = "CR-001") -> str:
    return json.dumps(
        {
            "request_id": request_id,
            "classification": "IN_SCOPE",
            "supporting_evidence_ids": ["SOW-SCP-003"],
            "conflicting_evidence_ids": [],
            "requires_clarification": False,
            "clarification_questions": [],
            "dependencies": ["Existing listing data"],
            "reasoning_summary": "The requested listing content is approved.",
            "cumulative_drift_detected": False,
            "cumulative_related_request_ids": [],
            "cumulative_related_decision_ids": [],
        }
    )


class SequenceClient:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.calls: list[str] = []

    def generate(self, prompt: str) -> RawGeneration:
        self.calls.append(prompt)
        outcome = self.outcomes[len(self.calls) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        assert isinstance(outcome, RawGeneration)
        return outcome


def test_cr001_excludes_future_requests_and_decisions() -> None:
    rendered = render_baseline_prompt(DEMO_PACK, "CR-001", prompt_path=PROMPT_PATH)
    assert rendered.included_request_ids == ("CR-001",)
    assert rendered.included_decision_ids == ("DEC-001", "DEC-002", "DEC-003", "DEC-004")
    assert '"request_id": "CR-002"' not in rendered.text
    assert "## DEC-005" not in rendered.text
    assert "## DEC-006" not in rendered.text


def test_cr007_includes_dec005_but_not_dec006() -> None:
    rendered = render_baseline_prompt(DEMO_PACK, "CR-007", prompt_path=PROMPT_PATH)
    assert rendered.included_request_ids == tuple(f"CR-{number:03d}" for number in range(1, 8))
    assert "DEC-005" in rendered.included_decision_ids
    assert "DEC-006" not in rendered.included_decision_ids
    assert "## DEC-005" in rendered.text
    assert "## DEC-006" not in rendered.text
    assert '"request_id": "CR-008"' not in rendered.text


def test_cr010_includes_both_chronological_decisions() -> None:
    rendered = render_baseline_prompt(DEMO_PACK, "CR-010", prompt_path=PROMPT_PATH)
    assert rendered.included_request_ids == tuple(f"CR-{number:03d}" for number in range(1, 11))
    assert rendered.included_decision_ids[-2:] == ("DEC-005", "DEC-006")
    assert "## DEC-005" in rendered.text
    assert "## DEC-006" in rendered.text


def test_answer_key_content_is_absent_from_every_prompt() -> None:
    for number in range(1, 11):
        text = render_baseline_prompt(
            DEMO_PACK, f"CR-{number:03d}", prompt_path=PROMPT_PATH
        ).text.lower()
        assert all(marker.lower() not in text for marker in ANSWER_KEY_MARKERS)
        assert "confirm_in_scope" not in text
        assert "cum-full-session-001" not in text


def test_each_render_is_independent_and_does_not_mutate_prior_prompt() -> None:
    first = render_baseline_prompt(DEMO_PACK, "CR-001", prompt_path=PROMPT_PATH)
    later = render_baseline_prompt(DEMO_PACK, "CR-007", prompt_path=PROMPT_PATH)
    first_again = render_baseline_prompt(DEMO_PACK, "CR-001", prompt_path=PROMPT_PATH)
    assert first == first_again
    assert first.text != later.text
    assert "## DEC-005" not in first_again.text


def test_one_request_run_makes_one_independent_call_and_records_file_hashes(
    tmp_path: Path,
) -> None:
    client = SequenceClient([RawGeneration(_prediction_json())])
    settings = LLMSettings(
        provider="google",
        model="fake-model",
        api_key="unit-test-secret-value",
    )
    run_directory = run_baseline(
        DEMO_PACK,
        ["CR-001"],
        client=client,
        settings=settings,
        seed=7,
        max_output_tokens=2048,
        max_attempts=2,
        results_root=tmp_path,
    )

    assert len(client.calls) == 1
    assert '"request_id": "CR-002"' not in client.calls[0]
    manifest = json.loads((run_directory / "manifest.json").read_text(encoding="utf-8"))
    assembled_hash = hashlib.sha256(client.calls[0].encode("utf-8")).hexdigest()
    assert manifest["request_count"] == 1
    assert manifest["dataset_hash"] == dataset_hash(DEMO_PACK)
    assert manifest["prompt_hash"] == prompt_hash(PROMPT_PATH)
    assert manifest["assembled_prompt_hashes"] == {"CR-001": assembled_hash}
    assert "unit-test-secret-value" not in json.dumps(manifest)


def test_prompt_hash_is_stable_and_hashes_exact_file_bytes() -> None:
    import hashlib

    expected = hashlib.sha256(PROMPT_PATH.read_bytes()).hexdigest()
    assert prompt_hash(PROMPT_PATH) == expected
    assert render_baseline_prompt(DEMO_PACK, "CR-001", prompt_path=PROMPT_PATH).prompt_hash == expected


def test_structured_prediction_parsing_succeeds() -> None:
    client = SequenceClient([RawGeneration(_prediction_json(), {"total_token_count": 42})])
    result = generate_prediction_with_retry(client, "standalone prompt", max_attempts=3)
    assert result.prediction.request_id == "CR-001"
    assert result.attempt_count == 1
    assert result.raw_responses == (_prediction_json(),)
    assert result.attempt_errors == ()
    assert result.usage == {"total_token_count": 42}


def test_invalid_structured_responses_fail_clearly_and_preserve_raw_text() -> None:
    client = SequenceClient([RawGeneration("not-json"), RawGeneration("{}")])
    with pytest.raises(RetryExhaustedError) as captured:
        generate_prediction_with_retry(client, "standalone prompt", max_attempts=2)
    assert len(client.calls) == 2
    assert captured.value.raw_responses == ["not-json", "{}"]
    assert "ModelPrediction validation failed" in str(captured.value)


def test_wrong_request_id_is_retried_and_preserved() -> None:
    wrong = _prediction_json("CR-999")
    client = SequenceClient([RawGeneration(wrong), RawGeneration(wrong)])
    with pytest.raises(RetryExhaustedError) as captured:
        generate_prediction_with_retry(
            client,
            "standalone prompt",
            max_attempts=2,
            expected_request_id="CR-001",
        )
    assert len(client.calls) == 2
    assert captured.value.raw_responses == [wrong, wrong]
    assert "request_id does not match" in str(captured.value)


def test_transient_retry_is_bounded_with_fake_client() -> None:
    client = SequenceClient(
        [
            TransientLLMError("temporary one"),
            TransientLLMError("temporary two"),
            TransientLLMError("temporary three"),
        ]
    )
    with pytest.raises(RetryExhaustedError, match="after 3 attempts"):
        generate_prediction_with_retry(client, "standalone prompt", max_attempts=3)
    assert len(client.calls) == 3


def test_transient_failure_can_recover_without_network() -> None:
    client = SequenceClient(
        [TransientLLMError("temporary"), RawGeneration(_prediction_json())]
    )
    result = generate_prediction_with_retry(client, "standalone prompt", max_attempts=3)
    assert result.attempt_count == 2
    assert len(client.calls) == 2
    assert result.attempt_errors == ("temporary",)
    assert result.raw_responses == (_prediction_json(),)
