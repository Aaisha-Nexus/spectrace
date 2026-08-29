from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from spectrace.baseline import (
    ANSWER_KEY_MARKERS,
    dataset_hash,
    main,
    prompt_hash,
    render_baseline_prompt,
    run_baseline,
)
from spectrace.config import LLMSettings
from spectrace.llm import (
    ProviderErrorCategory,
    ProviderErrorDiagnostic,
    ProviderLLMError,
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


def _provider_failure(
    *,
    status_code: int = 503,
    category: ProviderErrorCategory = ProviderErrorCategory.TRANSIENT_PROVIDER_ERROR,
    retryable: bool = True,
) -> ProviderLLMError:
    provider_statuses = {
        ProviderErrorCategory.AUTHENTICATION: "UNAUTHENTICATED",
        ProviderErrorCategory.PERMISSION: "PERMISSION_DENIED",
        ProviderErrorCategory.QUOTA_OR_RATE_LIMIT: "RESOURCE_EXHAUSTED",
        ProviderErrorCategory.MODEL_NOT_FOUND: "NOT_FOUND",
        ProviderErrorCategory.INVALID_REQUEST_OR_SCHEMA: "INVALID_ARGUMENT",
        ProviderErrorCategory.TRANSIENT_PROVIDER_ERROR: "UNAVAILABLE",
        ProviderErrorCategory.UNKNOWN_PROVIDER_ERROR: "UNKNOWN",
    }
    diagnostic = ProviderErrorDiagnostic(
        provider="google",
        exception_type="FakeProviderError",
        status_code=status_code,
        provider_status=provider_statuses[category],
        category=category,
        sanitized_provider_message="fake provider failure",
        retryable=retryable,
    )
    return TransientLLMError(diagnostic) if retryable else ProviderLLMError(diagnostic)


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
    scores = json.loads((run_directory / "scores.json").read_text(encoding="utf-8"))
    assert scores["status"] == "COMPLETE_UNCURATED"
    assert scores["official_benchmark_result"] is False


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
            _provider_failure(),
            _provider_failure(),
            _provider_failure(),
        ]
    )
    with pytest.raises(RetryExhaustedError, match="after 3 attempts"):
        generate_prediction_with_retry(client, "standalone prompt", max_attempts=3)
    assert len(client.calls) == 3


def test_transient_failure_can_recover_without_network() -> None:
    client = SequenceClient(
        [_provider_failure(), RawGeneration(_prediction_json())]
    )
    result = generate_prediction_with_retry(client, "standalone prompt", max_attempts=3)
    assert result.attempt_count == 2
    assert len(client.calls) == 2
    assert result.attempt_errors == (
        "TRANSIENT_PROVIDER_ERROR: fake provider failure",
    )
    assert result.raw_responses == (_prediction_json(),)
    assert result.provider_errors[0].attempt_number == 1


@pytest.mark.parametrize(
    ("status_code", "category"),
    [
        (400, ProviderErrorCategory.INVALID_REQUEST_OR_SCHEMA),
        (401, ProviderErrorCategory.AUTHENTICATION),
        (403, ProviderErrorCategory.PERMISSION),
        (404, ProviderErrorCategory.MODEL_NOT_FOUND),
        (429, ProviderErrorCategory.QUOTA_OR_RATE_LIMIT),
    ],
)
def test_nonretryable_provider_failure_stops_after_one_attempt(
    status_code: int, category: ProviderErrorCategory
) -> None:
    client = SequenceClient(
        [
            _provider_failure(
                status_code=status_code,
                category=category,
                retryable=False,
            ),
            RawGeneration(_prediction_json()),
        ]
    )
    with pytest.raises(RetryExhaustedError) as captured:
        generate_prediction_with_retry(
            client,
            "standalone prompt",
            max_attempts=3,
            expected_request_id="CR-001",
        )
    assert len(client.calls) == 1
    assert captured.value.attempt_count == 1
    assert captured.value.retry_exhausted is False
    diagnostic = captured.value.provider_errors[0]
    assert diagnostic.request_id == "CR-001"
    assert diagnostic.attempt_number == 1
    assert diagnostic.retryable is False


def test_failed_run_preserves_structured_error_and_marks_scores_incomplete(
    tmp_path: Path,
) -> None:
    client = SequenceClient(
        [
            _provider_failure(
                status_code=401,
                category=ProviderErrorCategory.AUTHENTICATION,
                retryable=False,
            )
        ]
    )
    settings = LLMSettings("google", "fake-model", "unit-test-secret-value")
    run_directory = run_baseline(
        DEMO_PACK,
        ["CR-001"],
        client=client,
        settings=settings,
        seed=None,
        max_output_tokens=2048,
        max_attempts=3,
        results_root=tmp_path,
    )

    error = json.loads(
        (run_directory / "errors.jsonl").read_text(encoding="utf-8").strip()
    )
    diagnostic = error["provider_errors"][0]
    assert error["attempt_count"] == 1
    assert error["retry_exhausted"] is False
    assert diagnostic == {
        "provider": "google",
        "exception_type": "FakeProviderError",
        "status_code": 401,
        "provider_status": "UNAUTHENTICATED",
        "category": "AUTHENTICATION",
        "sanitized_provider_message": "fake provider failure",
        "request_id": "CR-001",
        "attempt_number": 1,
        "retryable": False,
    }
    scores = json.loads((run_directory / "scores.json").read_text(encoding="utf-8"))
    assert scores["status"] == "INCOMPLETE_NOT_OFFICIAL"
    assert scores["official_benchmark_result"] is False
    assert scores["metrics"] is None
    manifest = json.loads((run_directory / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["run_status"] == "INCOMPLETE"


@pytest.mark.parametrize(
    ("command", "successes", "failures", "expected_request_count"),
    [
        ("run", 0, 1, 1),
        ("run-all", 9, 1, 10),
    ],
)
def test_cli_returns_nonzero_when_any_requested_case_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    command: str,
    successes: int,
    failures: int,
    expected_request_count: int,
) -> None:
    settings = LLMSettings("google", "fake-model", "unit-test-secret-value")
    monkeypatch.setattr("spectrace.baseline.load_llm_settings", lambda: settings)
    monkeypatch.setattr("spectrace.baseline.GoogleGenAIClient", lambda *args, **kwargs: object())

    def fake_run(*args, **kwargs):
        request_ids = args[1]
        assert len(request_ids) == expected_request_count
        run_directory = tmp_path / command
        run_directory.mkdir()
        (run_directory / "manifest.json").write_text(
            json.dumps(
                {
                    "request_count": expected_request_count,
                    "successful_request_count": successes,
                    "failed_request_count": failures,
                    "run_status": "INCOMPLETE",
                }
            ),
            encoding="utf-8",
        )
        return run_directory

    monkeypatch.setattr("spectrace.baseline.run_baseline", fake_run)
    argv = ["--project-pack", str(DEMO_PACK), command, "--confirm-api-call"]
    if command == "run":
        argv.extend(["--request-id", "CR-001"])
    assert main(argv) == 1


def test_cli_returns_zero_only_when_every_requested_case_succeeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = LLMSettings("google", "fake-model", "unit-test-secret-value")
    monkeypatch.setattr("spectrace.baseline.load_llm_settings", lambda: settings)
    monkeypatch.setattr("spectrace.baseline.GoogleGenAIClient", lambda *args, **kwargs: object())

    def fake_run(*args, **kwargs):
        run_directory = tmp_path / "complete"
        run_directory.mkdir()
        (run_directory / "manifest.json").write_text(
            json.dumps(
                {
                    "request_count": 1,
                    "successful_request_count": 1,
                    "failed_request_count": 0,
                    "run_status": "COMPLETE",
                }
            ),
            encoding="utf-8",
        )
        return run_directory

    monkeypatch.setattr("spectrace.baseline.run_baseline", fake_run)
    assert main(
        [
            "--project-pack",
            str(DEMO_PACK),
            "run",
            "--confirm-api-call",
            "--request-id",
            "CR-001",
        ]
    ) == 0
