from __future__ import annotations

import json
from pathlib import Path

import pytest

import spectrace.advanced_eval as advanced_eval
from spectrace.advanced_models import (
    AdvancedAssessment,
    AgentNode,
    AgentStatus,
    CapabilitySignature,
)
from spectrace.baseline import prompt_hash
from spectrace.models import Classification, ModelPrediction


PACK = Path(__file__).parents[1] / "data" / "synthetic" / "demo_project"
BASELINE_HASH = "369b16540e18ac3592867bdcde4a9d37e156ef8ee726371e1782380edb48a687"


def _read_json(run: Path, name: str):
    return json.loads((run / name).read_text(encoding="utf-8"))


def _read_jsonl(run: Path, name: str):
    return [
        json.loads(line)
        for line in (run / name).read_text(encoding="utf-8").splitlines()
        if line
    ]


@pytest.fixture
def fake_replay(tmp_path: Path) -> Path:
    return advanced_eval.run_advanced_evaluation(
        PACK,
        advanced_eval.EXPECTED_REQUEST_IDS,
        mode=advanced_eval.EvaluationMode.FAKE_REPLAY,
        client_factory=advanced_eval.offline_fake_client_factory,
        provider="offline-fake",
        model="deterministic-fixture",
        results_root=tmp_path,
    )


def test_complete_sequential_fake_replay(fake_replay: Path) -> None:
    manifest = _read_json(fake_replay, "manifest.json")
    scores = _read_json(fake_replay, "scores.json")
    predictions = _read_json(fake_replay, "predictions.json")
    raw = _read_jsonl(fake_replay, "raw_responses.jsonl")
    assert manifest["run_status"] == "COMPLETE"
    assert manifest["official_benchmark_result"] is False
    assert manifest["curation_status"] == "UNCURATED"
    assert manifest["request_ids"] == list(advanced_eval.EXPECTED_REQUEST_IDS)
    assert manifest["independent_assessment_per_request"] is True
    assert manifest["conversation_history_used"] is False
    assert len(predictions) == len(raw) == 10
    assert all(record["call_count"] == 1 for record in raw)
    assert scores["status"] == "COMPLETE_UNCURATED"


def test_ground_truth_is_loaded_only_after_each_pause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paused: set[str] = set()
    truth_reads: list[str] = []
    original_run = advanced_eval.run_until_human_review
    original_truth = advanced_eval._load_truth_record
    original_read_text = Path.read_text
    original_read_bytes = Path.read_bytes
    answer_key_accesses: list[str] = []

    def tracked_run(*args, **kwargs):
        state = original_run(*args, **kwargs)
        assert state.status == AgentStatus.AWAITING_HUMAN_REVIEW
        paused.add(state.request.request_id)
        return state

    def guarded_truth(path: Path, request_id: str):
        assert request_id in paused
        truth_reads.append(request_id)
        return original_truth(path, request_id)

    def guarded_read_text(path: Path, *args, **kwargs):
        if path.name == "ground_truth.json":
            assert paused
            answer_key_accesses.append("text")
        return original_read_text(path, *args, **kwargs)

    def guarded_read_bytes(path: Path, *args, **kwargs):
        if path.name == "ground_truth.json":
            assert paused
            answer_key_accesses.append("bytes")
        return original_read_bytes(path, *args, **kwargs)

    monkeypatch.setattr(advanced_eval, "run_until_human_review", tracked_run)
    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)
    advanced_eval.run_advanced_evaluation(
        PACK,
        advanced_eval.EXPECTED_REQUEST_IDS,
        mode=advanced_eval.EvaluationMode.FAKE_REPLAY,
        client_factory=advanced_eval.offline_fake_client_factory,
        provider="offline-fake",
        model="fixture",
        results_root=tmp_path,
        truth_loader=guarded_truth,
    )
    assert truth_reads == list(advanced_eval.EXPECTED_REQUEST_IDS)
    assert answer_key_accesses == ["text"] * 10 + ["bytes"]


def test_mandated_decisions_are_applied_in_order_and_persist(fake_replay: Path) -> None:
    reviews = _read_json(fake_replay, "human_reviews.json")
    applied = [
        record["review"]["decision_payload"]["decision_id"]
        for record in reviews
        if record["review"]["decision_payload"]
    ]
    assert applied == ["DEC-005", "DEC-006"]
    assert reviews[5]["review"]["request_id"] == "CR-006"
    assert reviews[6]["review"]["request_id"] == "CR-007"

    snapshots = _read_json(fake_replay, "ledger_snapshots.json")
    before_cr007 = next(
        item
        for item in snapshots
        if item["request_id"] == "CR-007" and item["phase"] == "BEFORE_REQUEST"
    )
    before_cr008 = next(
        item
        for item in snapshots
        if item["request_id"] == "CR-008" and item["phase"] == "BEFORE_REQUEST"
    )
    assert "DEC-005" in before_cr007["approved_evidence_ids"]
    assert "HUMAN-DEC-005" in before_cr007["ledger_entry_ids"]
    assert {"DEC-005", "DEC-006"} <= set(before_cr008["approved_evidence_ids"])


def test_raw_requests_do_not_count_toward_drift(fake_replay: Path) -> None:
    assessments = {
        record["request_id"]: record
        for record in _read_json(fake_replay, "assessments.json")
    }
    assert assessments["CR-006"]["drift"]["approved_change_count"] == 0
    assert assessments["CR-006"]["drift"]["cumulative_drift_detected"] is False
    assert assessments["CR-007"]["drift"]["approved_change_count"] == 1
    assert assessments["CR-010"]["drift"]["cumulative_drift_detected"] is True
    assert assessments["CR-010"]["drift"]["related_decision_ids"] == [
        "DEC-005",
        "DEC-006",
    ]


def test_fresh_database_and_deterministic_artifact_structure(tmp_path: Path) -> None:
    runs = [
        advanced_eval.run_advanced_evaluation(
            PACK,
            advanced_eval.EXPECTED_REQUEST_IDS,
            mode=advanced_eval.EvaluationMode.FAKE_REPLAY,
            client_factory=advanced_eval.offline_fake_client_factory,
            provider="offline-fake",
            model="fixture",
            results_root=tmp_path,
        )
        for _ in range(2)
    ]
    assert runs[0] != runs[1]
    for run in runs:
        assert {path.name for path in run.iterdir()} == advanced_eval.ARTIFACT_NAMES
        assert (run / "ledger.sqlite").is_file()
        first_pause = next(
            item
            for item in _read_json(run, "ledger_snapshots.json")
            if item["request_id"] == "CR-001" and item["phase"] == "AT_HUMAN_PAUSE"
        )
        assert first_pause["request_ids"] == ["CR-001"]
        assert first_pause["ledger_entry_ids"] == []


def test_prediction_conversion_preserves_verified_fields() -> None:
    assessment = AdvancedAssessment(
        request_id="CR-010",
        model_recommendation=Classification.POTENTIAL_SCOPE_CHANGE,
        classification=Classification.POTENTIAL_SCOPE_CHANGE,
        supporting_evidence_ids=("DEC-006",),
        requires_clarification=False,
        dependencies=("Approved queue",),
        rationale="A distinct proposed increment requires review.",
        capability_signature=CapabilitySignature(heuristic=True),
    )
    prediction = advanced_eval.assessment_to_prediction(
        assessment,
        cumulative_drift_detected=True,
        related_request_ids=("CR-006", "CR-007", "CR-010"),
        related_decision_ids=("DEC-005", "DEC-006"),
    )
    assert isinstance(prediction, ModelPrediction)
    assert prediction.reasoning_summary == assessment.rationale
    assert prediction.supporting_evidence_ids == ["DEC-006"]
    assert prediction.cumulative_drift_detected


def test_per_request_trajectory_order_is_preserved(fake_replay: Path) -> None:
    expected = [node.value for node in AgentNode]
    for request_id in advanced_eval.EXPECTED_REQUEST_IDS:
        trajectory = _read_json(fake_replay / "trajectories", f"{request_id}.json")
        assert [event["node"] for event in trajectory] == expected
        assert [event["sequence"] for event in trajectory] == list(
            range(1, len(expected) + 1)
        )


def test_failed_request_is_incomplete_and_never_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FailingClient:
        def generate(self, prompt: str):
            raise RuntimeError("offline injected failure")

    def factory(request):
        if request.request_id == "CR-003":
            return FailingClient()
        return advanced_eval.OfflineFakeClient(request.request_id)

    run = advanced_eval.run_advanced_evaluation(
        PACK,
        advanced_eval.EXPECTED_REQUEST_IDS,
        mode=advanced_eval.EvaluationMode.FAKE_REPLAY,
        client_factory=factory,
        provider="offline-fake",
        model="injected-failure",
        results_root=tmp_path,
    )
    manifest = _read_json(run, "manifest.json")
    assert manifest["run_status"] == "INCOMPLETE"
    assert manifest["successful_request_count"] == 2
    assert manifest["failed_request_count"] == 1
    assert len(_read_json(run, "predictions.json")) == 2
    assert _read_json(run, "scores.json")["metrics"] is None
    assert _read_jsonl(run, "errors.jsonl")[0]["request_id"] == "CR-003"
    assert advanced_eval.run_exit_code(run) == 1
    assert not (run / "trajectories" / "CR-004.json").exists()
    monkeypatch.setattr(advanced_eval, "run_advanced_evaluation", lambda *args, **kwargs: run)
    assert advanced_eval.main(
        [
            "--project-pack",
            str(PACK),
            "fake-replay",
            "--results-root",
            str(tmp_path),
        ]
    ) == 1


def test_single_request_smoke_mode_is_structurally_supported(tmp_path: Path) -> None:
    run = advanced_eval.run_advanced_evaluation(
        PACK,
        ("CR-001",),
        mode=advanced_eval.EvaluationMode.REAL_SMOKE,
        client_factory=advanced_eval.offline_fake_client_factory,
        provider="offline-injected",
        model="smoke-fixture",
        results_root=tmp_path,
    )
    manifest = _read_json(run, "manifest.json")
    assert manifest["request_count"] == 1
    assert manifest["sequential_context_complete"] is False
    assert advanced_eval.run_exit_code(run) == 0
    with pytest.raises(advanced_eval.AdvancedEvaluationError, match="exactly one"):
        advanced_eval.run_advanced_evaluation(
            PACK,
            ("CR-001", "CR-002"),
            mode=advanced_eval.EvaluationMode.REAL_SMOKE,
            client_factory=advanced_eval.offline_fake_client_factory,
            provider="offline",
            model="invalid-smoke",
            results_root=tmp_path,
        )


def test_client_construction_failure_is_preserved_without_answer_key_access(
    tmp_path: Path,
) -> None:
    def failing_factory(request):
        raise RuntimeError("offline client construction failure")

    def forbidden_truth(path: Path, request_id: str):
        pytest.fail("answer key read before a human-review pause")

    run = advanced_eval.run_advanced_evaluation(
        PACK,
        ("CR-001",),
        mode=advanced_eval.EvaluationMode.REAL_SMOKE,
        client_factory=failing_factory,
        provider="offline-injected",
        model="construction-failure",
        results_root=tmp_path,
        truth_loader=forbidden_truth,
    )
    assert advanced_eval.run_exit_code(run) == 1
    assert _read_json(run, "predictions.json") == []
    error = _read_jsonl(run, "errors.jsonl")[0]
    assert error["call_count"] == 0
    assert error["request_id"] == "CR-001"
    assert _read_json(run, "manifest.json")["evaluation_dataset_hash"] is None


def test_real_cli_modes_require_explicit_confirmation_flags() -> None:
    parser = advanced_eval._parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["smoke", "--request-id", "CR-001"])
    with pytest.raises(SystemExit):
        parser.parse_args(["run-all", "--confirm-api-call"])
    args = parser.parse_args(
        ["run-all", "--confirm-api-call", "--confirm-full-advanced-run"]
    )
    assert args.command == "run-all"


def test_fake_cli_never_constructs_network_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        advanced_eval,
        "GoogleGenAIClient",
        lambda *args, **kwargs: pytest.fail("network adapter constructed"),
    )
    exit_code = advanced_eval.main(
        [
            "--project-pack",
            str(PACK),
            "fake-replay",
            "--results-root",
            str(tmp_path),
        ]
    )
    assert exit_code == 0


def test_baseline_prompt_regression_remains_frozen() -> None:
    assert prompt_hash() == BASELINE_HASH
