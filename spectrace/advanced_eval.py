"""Reproducible evaluator-only runner for the verified advanced state machine.

The generation path deliberately loads requests without constructing a
``ProjectPack`` because that validation object also contains the frozen answer
key. One ground-truth record is loaded only after its request has reached the
mandatory human-review pause. No expected classification, citation, or drift
field is passed back into advanced-agent production logic.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, time as datetime_time
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from spectrace.advanced import (
    AdvancedAgentError,
    advanced_prompt_hash,
    new_run_state,
    resume_after_human_review,
    run_until_human_review,
)
from spectrace.advanced_models import (
    AdvancedAssessment,
    AdvancedModelOutput,
    AgentStatus,
    HumanAction,
    HumanDecisionPayload,
    HumanReview,
    LedgerEntryEffect,
    ScopeAnchor,
)
from spectrace.baseline import dataset_hash, prompt_hash as baseline_prompt_hash
from spectrace.config import ConfigurationError, load_llm_settings
from spectrace.ledger import LedgerStore
from spectrace.llm import (
    GoogleGenAIClient,
    RawGeneration,
    RetryExhaustedError,
    StructuredGenerationClient,
    gemini_schema_for_model,
)
from spectrace.models import GroundTruthRecord, IncomingRequest, ModelPrediction
from spectrace.scope_anchor import build_scope_anchor, resolve_anchor_at_cutoff
from spectrace.scoring import score_predictions


DEFAULT_PROJECT_PACK = Path("data/synthetic/demo_project")
DEFAULT_RESULTS_ROOT = Path("results")
EXPECTED_REQUEST_IDS = tuple(f"CR-{index:03d}" for index in range(1, 11))
MANDATED_DECISIONS = {"CR-006": "DEC-005", "CR-007": "DEC-006"}
TEMPERATURE = 0.0
DEFAULT_MAX_OUTPUT_TOKENS = 2048
DEFAULT_MAX_ATTEMPTS = 2
ARTIFACT_NAMES = frozenset(
    {
        "assessments.json",
        "errors.jsonl",
        "human_reviews.json",
        "ledger.sqlite",
        "ledger_snapshots.json",
        "manifest.json",
        "predictions.json",
        "raw_responses.jsonl",
        "scores.json",
        "trajectories",
    }
)


class AdvancedEvaluationError(RuntimeError):
    """Raised when an advanced evaluation cannot satisfy its isolation contract."""


class EvaluationMode(str, Enum):
    FAKE_REPLAY = "FAKE_REPLAY"
    REAL_SMOKE = "REAL_SMOKE"
    REAL_FULL = "REAL_FULL"


ClientFactory = Callable[[IncomingRequest], StructuredGenerationClient]
TruthLoader = Callable[[Path, str], GroundTruthRecord]


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, records: Sequence[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )


def _git_commit() -> str | None:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def _run_id() -> str:
    return "advanced-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")


def load_generation_requests(project_pack_path: str | Path) -> tuple[IncomingRequest, ...]:
    """Load chronology without opening the answer-key file."""

    path = Path(project_pack_path) / "requests.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdvancedEvaluationError(f"cannot load generation requests: {exc}") from exc
    if not isinstance(raw, list):
        raise AdvancedEvaluationError("requests.json must contain an array")
    try:
        requests = tuple(IncomingRequest.model_validate(item) for item in raw)
    except ValidationError as exc:
        raise AdvancedEvaluationError(f"invalid generation request: {exc}") from exc
    actual_ids = tuple(request.request_id for request in requests)
    if actual_ids != EXPECTED_REQUEST_IDS:
        raise AdvancedEvaluationError(
            f"advanced evaluation requires frozen chronological IDs {EXPECTED_REQUEST_IDS}; got {actual_ids}"
        )
    return requests


def _load_truth_record(project_pack_path: Path, request_id: str) -> GroundTruthRecord:
    """Evaluator-only answer-key read; callers must first prove the pause state."""

    path = project_pack_path / "ground_truth.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdvancedEvaluationError(f"cannot load evaluator truth: {exc}") from exc
    if not isinstance(raw, list):
        raise AdvancedEvaluationError("ground_truth.json must contain an array")
    matching = [item for item in raw if item.get("request_id") == request_id]
    if len(matching) != 1:
        raise AdvancedEvaluationError(
            f"expected exactly one evaluator record for {request_id}; got {len(matching)}"
        )
    return GroundTruthRecord.model_validate(matching[0])


def assessment_to_prediction(
    assessment: AdvancedAssessment,
    *,
    cumulative_drift_detected: bool,
    related_request_ids: Sequence[str],
    related_decision_ids: Sequence[str],
) -> ModelPrediction:
    """Adapt a verified advanced result to the frozen scoring contract."""

    return ModelPrediction(
        request_id=assessment.request_id,
        classification=assessment.classification,
        supporting_evidence_ids=list(assessment.supporting_evidence_ids),
        conflicting_evidence_ids=list(assessment.conflicting_evidence_ids),
        requires_clarification=assessment.requires_clarification,
        clarification_questions=list(assessment.clarification_questions),
        dependencies=list(assessment.dependencies),
        reasoning_summary=assessment.rationale,
        cumulative_drift_detected=cumulative_drift_detected,
        cumulative_related_request_ids=list(related_request_ids),
        cumulative_related_decision_ids=list(related_decision_ids),
    )


class RecordingClient:
    """Capture every returned provider response while preserving the client boundary."""

    def __init__(self, client: StructuredGenerationClient) -> None:
        self.client = client
        self.generations: list[RawGeneration] = []
        self.calls = 0

    def generate(self, prompt: str) -> RawGeneration:
        self.calls += 1
        generation = self.client.generate(prompt)
        self.generations.append(generation)
        return generation


class OfflineFakeClient:
    """One-call fake output with no answer-key or network dependency."""

    def __init__(self, request_id: str) -> None:
        self.request_id = request_id
        self.called = False

    def generate(self, prompt: str) -> RawGeneration:
        if self.called:
            raise AdvancedEvaluationError("offline fake client is single-use")
        self.called = True
        forbidden = ("ground_truth", "expected_classification", "expected_human_action")
        if any(marker in prompt.lower() for marker in forbidden):
            raise AdvancedEvaluationError("answer-key marker reached fake generation")
        payload = {
            "request_id": self.request_id,
            "recommended_classification": "POTENTIAL_SCOPE_CHANGE",
            "supporting_evidence_ids": [],
            "conflicting_evidence_ids": [],
            "requires_clarification": False,
            "clarification_questions": [],
            "dependencies": [],
            "rationale": "Offline fake recommendation for deterministic verification and human review.",
            "capability_signature": {"heuristic": True},
        }
        return RawGeneration(
            text=json.dumps(payload, sort_keys=True),
            usage={"fake_generation_count": 1},
        )


def offline_fake_client_factory(request: IncomingRequest) -> StructuredGenerationClient:
    return OfflineFakeClient(request.request_id)


def _source_dataset_hash(project_pack_path: Path) -> str:
    digest = hashlib.sha256()
    for name in ("sow.md", "decisions.md", "requests.json"):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update((project_pack_path / name).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _tool_hashes() -> dict[str, str]:
    package = Path(__file__).resolve().parent
    return {
        name: _hash_file(package / name)
        for name in (
            "advanced.py",
            "advanced_eval.py",
            "advanced_models.py",
            "analysis_tools.py",
            "change_package.py",
            "ledger.py",
            "llm.py",
            "retrieval.py",
            "scoring.py",
            "scope_anchor.py",
            "verification.py",
        )
    }


def _schema_hashes() -> dict[str, str]:
    return {
        "local_advanced_output": _hash_json(AdvancedModelOutput.model_json_schema()),
        "provider_advanced_output": _hash_json(gemini_schema_for_model(AdvancedModelOutput)),
        "model_prediction": _hash_json(ModelPrediction.model_json_schema()),
    }


def _decision_payload(anchor: ScopeAnchor, request_id: str) -> HumanDecisionPayload:
    decision_id = MANDATED_DECISIONS[request_id]
    item = next((item for item in anchor.items if item.evidence_id == decision_id), None)
    if item is None or item.effective_date is None:
        raise AdvancedEvaluationError(f"missing dated source decision {decision_id}")
    known_ids = {candidate.evidence_id for candidate in anchor.items}
    evidence_ids = tuple(
        sorted(
            evidence_id
            for evidence_id in set(item.supersedes_ids)
            | set(item.superseded_by_ids)
            | set(_evidence_ids_in_text(item.source_text))
            if evidence_id in known_ids and evidence_id != decision_id
        )
    )
    if not evidence_ids:
        raise AdvancedEvaluationError(f"source decision {decision_id} has no linked evidence")
    return HumanDecisionPayload(
        decision_id=decision_id,
        effective_date=item.effective_date,
        effect=LedgerEntryEffect.APPROVE_CAPABILITY,
        decision_text=_approved_decision_text(item.source_text, decision_id),
        evidence_ids=evidence_ids,
        changes_approved_scope=True,
        approves_requested_capability=True,
    )


def _evidence_ids_in_text(text: str) -> tuple[str, ...]:
    return tuple(sorted(set(re.findall(r"\b(?:SOW-[A-Z]{3}-\d{3}|DEC-\d{3})\b", text))))


def _approved_decision_text(source_text: str, decision_id: str) -> str:
    match = re.search(
        r"^- \*\*Approves:\*\* ([\s\S]*?)(?=^- \*\*[A-Za-z ]+(?: and [A-Za-z ]+)?:\*\*|\Z)",
        source_text,
        re.MULTILINE,
    )
    if not match:
        raise AdvancedEvaluationError(f"source decision {decision_id} has no approved clause")
    return " ".join(match.group(1).split())


def _review_action(expected_human_action: str) -> HumanAction:
    if expected_human_action == "REQUEST_CLARIFICATION":
        return HumanAction.NEEDS_CLARIFICATION
    if expected_human_action == "DEFER_FOR_FORMAL_SCOPE_REVIEW":
        return HumanAction.DEFER
    if expected_human_action in {
        "APPROVE_LIMITED_SCOPE_CHANGE",
        "CONFIRM_IN_SCOPE",
        "UPHOLD_APPROVED_DECISION",
        "UPHOLD_APPROVED_REJECTION",
        "UPHOLD_EXPLICIT_EXCLUSION",
    }:
        return HumanAction.APPROVE
    raise AdvancedEvaluationError(f"unsupported frozen human action: {expected_human_action}")


def _human_review(
    anchor: ScopeAnchor,
    request: IncomingRequest,
    expected_human_action: str,
) -> HumanReview:
    action = _review_action(expected_human_action)
    payload = (
        _decision_payload(anchor, request.request_id)
        if request.request_id in MANDATED_DECISIONS
        else None
    )
    if payload and expected_human_action != "APPROVE_LIMITED_SCOPE_CHANGE":
        raise AdvancedEvaluationError(
            f"{request.request_id} cannot apply {payload.decision_id} for {expected_human_action}"
        )
    reviewed_at = datetime.combine(request.date, datetime_time(12, 0), tzinfo=UTC)
    return HumanReview(
        review_id=f"HR-EVAL-{request.request_id}",
        project_id=anchor.project_id,
        request_id=request.request_id,
        assessment_id=f"ASMNT-{request.request_id}",
        action=action,
        reviewer_id="frozen-synthetic-evaluator",
        reviewed_at=reviewed_at,
        reason=f"Frozen evaluator action: {expected_human_action}",
        decision_payload=payload,
    )


def _snapshot_record(ledger: LedgerStore, project_id: str, phase: str, request_id: str) -> dict[str, Any]:
    snapshot = ledger.snapshot(project_id)
    return {
        "request_id": request_id,
        "phase": phase,
        **snapshot.model_dump(mode="json"),
    }


def _raw_record(request_id: str, client: RecordingClient, state: Any) -> dict[str, Any]:
    generations = [
        {
            "text": generation.text,
            "response_hash": hashlib.sha256(generation.text.encode("utf-8")).hexdigest(),
            "usage": generation.usage,
        }
        for generation in client.generations
    ]
    return {
        "request_id": request_id,
        "call_count": client.calls,
        "assembled_prompt_hash": state.assembled_prompt_hash,
        "raw_response_hash": state.raw_response_hash,
        "generations": generations,
    }


def _error_record(request_id: str, exc: Exception, client: RecordingClient) -> dict[str, Any]:
    record: dict[str, Any] = {
        "request_id": request_id,
        "error_type": type(exc).__name__,
        "error": str(exc),
        "call_count": client.calls,
        "raw_responses": [generation.text for generation in client.generations],
    }
    if isinstance(exc, RetryExhaustedError):
        record.update(
            {
                "attempt_count": exc.attempt_count,
                "attempt_errors": list(exc.errors),
                "provider_errors": [item.as_dict() for item in exc.provider_errors],
                "raw_responses": list(exc.raw_responses),
                "retry_exhausted": exc.retry_exhausted,
            }
        )
    return record


def _requested_sequence(
    requests: tuple[IncomingRequest, ...],
    request_ids: Sequence[str],
    mode: EvaluationMode,
) -> tuple[IncomingRequest, ...]:
    if len(request_ids) != len(set(request_ids)):
        raise AdvancedEvaluationError("request IDs must be unique")
    by_id = {request.request_id: request for request in requests}
    unknown = sorted(set(request_ids) - set(by_id))
    if unknown:
        raise AdvancedEvaluationError(f"unknown request IDs: {unknown}")
    if mode in {EvaluationMode.FAKE_REPLAY, EvaluationMode.REAL_FULL}:
        if tuple(request_ids) != EXPECTED_REQUEST_IDS:
            raise AdvancedEvaluationError("full replay requires CR-001 through CR-010 in order")
    elif len(request_ids) != 1:
        raise AdvancedEvaluationError("real smoke mode requires exactly one specified request")
    return tuple(by_id[request_id] for request_id in request_ids)


def run_advanced_evaluation(
    project_pack_path: str | Path,
    request_ids: Sequence[str],
    *,
    mode: EvaluationMode,
    client_factory: ClientFactory,
    provider: str,
    model: str,
    seed: int | None = None,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    results_root: str | Path = DEFAULT_RESULTS_ROOT,
    truth_loader: TruthLoader = _load_truth_record,
) -> Path:
    """Run an isolated sequential evaluation and preserve a complete audit trail."""

    project_path = Path(project_pack_path)
    all_requests = load_generation_requests(project_path)
    requests = _requested_sequence(all_requests, request_ids, mode)
    anchor = build_scope_anchor(project_path)
    source_hash = _source_dataset_hash(project_path)
    generation_commit = _git_commit()
    started_at = datetime.now(UTC)
    started_clock = time.perf_counter()
    run_directory = Path(results_root) / _run_id()
    run_directory.mkdir(parents=True, exist_ok=False)
    trajectories_directory = run_directory / "trajectories"
    trajectories_directory.mkdir()
    database_path = run_directory / "ledger.sqlite"

    predictions: list[ModelPrediction] = []
    truth_records: list[GroundTruthRecord] = []
    raw_records: list[dict[str, Any]] = []
    error_records: list[dict[str, Any]] = []
    assessment_records: list[dict[str, Any]] = []
    review_records: list[dict[str, Any]] = []
    snapshot_records: list[dict[str, Any]] = []
    request_runtime: dict[str, float] = {}
    total_usage: dict[str, int] = {}
    assembled_prompt_hashes: dict[str, str] = {}
    final_snapshot_hash: str | None = None

    with LedgerStore(database_path) as ledger:
        for request in requests:
            request_started = time.perf_counter()
            state = new_run_state(
                project_path,
                anchor.project_id,
                request,
                run_id=f"{run_directory.name}-{request.request_id}",
            )
            try:
                recording = RecordingClient(client_factory(request))
            except Exception as exc:
                error_records.append(
                    {
                        "request_id": request.request_id,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "call_count": 0,
                        "raw_responses": [],
                    }
                )
                raw_records.append(
                    {
                        "request_id": request.request_id,
                        "call_count": 0,
                        "assembled_prompt_hash": None,
                        "raw_response_hash": None,
                        "generations": [],
                    }
                )
                request_runtime[request.request_id] = time.perf_counter() - request_started
                _write_json(trajectories_directory / f"{request.request_id}.json", [])
                break
            if snapshot_records:
                snapshot_records.append(
                    _snapshot_record(ledger, anchor.project_id, "BEFORE_REQUEST", request.request_id)
                )
            try:
                state = run_until_human_review(
                    state,
                    ledger,
                    recording,
                    max_attempts=max_attempts,
                )
                if state.status != AgentStatus.AWAITING_HUMAN_REVIEW:
                    raise AdvancedEvaluationError(
                        f"{request.request_id} did not reach the human-review isolation boundary"
                    )
                snapshot_records.append(
                    _snapshot_record(ledger, anchor.project_id, "AT_HUMAN_PAUSE", request.request_id)
                )
                assembled_prompt_hashes[request.request_id] = state.assembled_prompt_hash or ""
                raw_records.append(_raw_record(request.request_id, recording, state))
                if not state.verification or not state.verification.passed:
                    raise AdvancedEvaluationError(
                        f"{request.request_id} failed verification and cannot become a prediction"
                    )

                # This is the first answer-key access for the current request.
                truth = truth_loader(project_path, request.request_id)
                review = _human_review(anchor, request, truth.expected_human_action)
                state = resume_after_human_review(state, ledger, review)
                if state.status != AgentStatus.COMPLETE:
                    raise AdvancedEvaluationError(f"{request.request_id} did not complete review")
                snapshot_records.append(
                    _snapshot_record(ledger, anchor.project_id, "AFTER_HUMAN_REVIEW", request.request_id)
                )
                assert state.assessment is not None
                assert state.drift is not None
                prediction = assessment_to_prediction(
                    state.assessment,
                    cumulative_drift_detected=state.drift.cumulative_drift_detected,
                    related_request_ids=state.drift.related_request_ids,
                    related_decision_ids=state.drift.related_decision_ids,
                )
                predictions.append(prediction)
                truth_records.append(truth)
                assessment_records.append(
                    {
                        "request_id": request.request_id,
                        "assessment": state.assessment.model_dump(mode="json"),
                        "sufficiency": state.sufficiency.model_dump(mode="json") if state.sufficiency else None,
                        "conflicts": state.conflicts.model_dump(mode="json") if state.conflicts else None,
                        "drift": state.drift.model_dump(mode="json"),
                        "verification": state.verification.model_dump(mode="json") if state.verification else None,
                        "recommendation": state.recommendation.model_dump(mode="json") if state.recommendation else None,
                        "change_package": state.change_package.model_dump(mode="json") if state.change_package else None,
                    }
                )
                review_records.append(
                    {
                        "expected_human_action": truth.expected_human_action,
                        "review": review.model_dump(mode="json"),
                        "ledger_update": state.ledger_update.model_dump(mode="json") if state.ledger_update else None,
                    }
                )
                if state.token_usage:
                    for key, value in state.token_usage.items():
                        if isinstance(value, int):
                            total_usage[key] = total_usage.get(key, 0) + value
            except Exception as exc:
                if not raw_records or raw_records[-1].get("request_id") != request.request_id:
                    raw_records.append(
                        {
                            "request_id": request.request_id,
                            "call_count": recording.calls,
                            "assembled_prompt_hash": state.assembled_prompt_hash,
                            "raw_response_hash": state.raw_response_hash,
                            "generations": [
                                {
                                    "text": generation.text,
                                    "response_hash": hashlib.sha256(generation.text.encode("utf-8")).hexdigest(),
                                    "usage": generation.usage,
                                }
                                for generation in recording.generations
                            ],
                        }
                    )
                error_records.append(_error_record(request.request_id, exc, recording))
            finally:
                request_runtime[request.request_id] = time.perf_counter() - request_started
                _write_json(
                    trajectories_directory / f"{request.request_id}.json",
                    [event.model_dump(mode="json") for event in state.trajectory],
                )
            if error_records:
                break
        try:
            final_snapshot_hash = ledger.snapshot(anchor.project_id).snapshot_hash
        except Exception:
            final_snapshot_hash = None

    complete = len(predictions) == len(requests) and not error_records
    availability = {
        request.request_id: frozenset(
            item.evidence_id
            for item in resolve_anchor_at_cutoff(
                anchor, project_path, request.evidence_available_through
            )
            if item.temporal_status.value != "FUTURE"
        )
        for request in requests
    }
    if complete:
        scores = score_predictions(
            predictions,
            truth_records,
            {item.evidence_id for item in anchor.items},
            availability,
        )
        score_payload: dict[str, Any] = {
            "status": "COMPLETE_UNCURATED",
            "official_benchmark_result": False,
            "requested_case_count": len(requests),
            "successful_case_count": len(predictions),
            "failed_case_count": 0,
            "metrics": scores.model_dump(mode="json"),
        }
    else:
        score_payload = {
            "status": "INCOMPLETE_NOT_OFFICIAL",
            "official_benchmark_result": False,
            "requested_case_count": len(requests),
            "successful_case_count": len(predictions),
            "failed_case_count": len(error_records),
            "metrics": None,
            "reason": "one or more requested cases failed; no replacement prediction was created",
        }

    _write_json(run_directory / "predictions.json", [item.model_dump(mode="json") for item in predictions])
    _write_json(run_directory / "assessments.json", assessment_records)
    _write_json(run_directory / "human_reviews.json", review_records)
    _write_json(run_directory / "ledger_snapshots.json", snapshot_records)
    _write_jsonl(run_directory / "raw_responses.jsonl", raw_records)
    _write_jsonl(run_directory / "errors.jsonl", error_records)
    _write_json(run_directory / "scores.json", score_payload)

    scoring_commit = _git_commit()
    evaluation_hash = dataset_hash(project_path) if truth_records else None
    manifest = {
        "timestamp": started_at.isoformat(),
        "mode": mode.value,
        "generation_commit": generation_commit,
        "scoring_commit": scoring_commit,
        "dataset_identity": project_path.as_posix(),
        "source_dataset_hash": source_hash,
        "evaluation_dataset_hash": evaluation_hash,
        "anchor_hash": anchor.anchor_hash,
        "advanced_prompt_hash": advanced_prompt_hash(),
        "baseline_prompt_hash": baseline_prompt_hash(),
        "assembled_prompt_hashes": assembled_prompt_hashes,
        "tool_hashes": _tool_hashes(),
        "schema_hashes": _schema_hashes(),
        "provider": provider,
        "model": model,
        "temperature": TEMPERATURE,
        "seed": seed,
        "max_output_tokens": max_output_tokens,
        "max_attempts": max_attempts,
        "independent_assessment_per_request": True,
        "conversation_history_used": False,
        "sequential_context_complete": tuple(request_ids) == EXPECTED_REQUEST_IDS,
        "request_ids": list(request_ids),
        "request_count": len(requests),
        "successful_request_count": len(predictions),
        "failed_request_count": len(error_records),
        "run_status": "COMPLETE" if complete else "INCOMPLETE",
        "official_benchmark_result": False,
        "curation_status": "UNCURATED",
        "runtime_seconds": time.perf_counter() - started_clock,
        "request_runtime_seconds": request_runtime,
        "token_usage": total_usage or None,
        "final_ledger_snapshot_hash": final_snapshot_hash,
        "ledger_database_hash": _hash_file(database_path),
        "applied_human_review_count": len(review_records),
        "artifact_names": sorted(ARTIFACT_NAMES),
    }
    _write_json(run_directory / "manifest.json", manifest)
    return run_directory


def run_completed_successfully(run_directory: str | Path) -> bool:
    manifest = json.loads((Path(run_directory) / "manifest.json").read_text(encoding="utf-8"))
    return bool(
        manifest.get("request_count")
        and manifest.get("successful_request_count") == manifest.get("request_count")
        and manifest.get("failed_request_count") == 0
        and manifest.get("run_status") == "COMPLETE"
    )


def run_exit_code(run_directory: str | Path) -> int:
    return 0 if run_completed_successfully(run_directory) else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SpecTrace advanced evaluation runner")
    parser.add_argument("--project-pack", type=Path, default=DEFAULT_PROJECT_PACK)
    subparsers = parser.add_subparsers(dest="command", required=True)

    fake = subparsers.add_parser("fake-replay", help="offline ten-request replay")
    fake.add_argument("--results-root", type=Path, default=DEFAULT_RESULTS_ROOT)

    smoke = subparsers.add_parser("smoke", help="exactly one explicitly confirmed real request")
    smoke.add_argument("--request-id", required=True)
    smoke.add_argument("--confirm-api-call", action="store_true", required=True)

    full = subparsers.add_parser("run-all", help="explicitly confirmed real ten-request run")
    full.add_argument("--confirm-api-call", action="store_true", required=True)
    full.add_argument("--confirm-full-advanced-run", action="store_true", required=True)

    for command in (smoke, full):
        command.add_argument("--results-root", type=Path, default=DEFAULT_RESULTS_ROOT)
        command.add_argument("--seed", type=int)
        command.add_argument("--max-output-tokens", type=int, default=DEFAULT_MAX_OUTPUT_TOKENS)
        command.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        requests = load_generation_requests(args.project_pack)
        if args.command == "fake-replay":
            run_directory = run_advanced_evaluation(
                args.project_pack,
                EXPECTED_REQUEST_IDS,
                mode=EvaluationMode.FAKE_REPLAY,
                client_factory=offline_fake_client_factory,
                provider="offline-fake",
                model="deterministic-fixture",
                results_root=args.results_root,
            )
        else:
            settings = load_llm_settings()

            def real_factory(_request: IncomingRequest) -> StructuredGenerationClient:
                return GoogleGenAIClient(
                    settings,
                    temperature=TEMPERATURE,
                    seed=args.seed,
                    max_output_tokens=args.max_output_tokens,
                    output_model=AdvancedModelOutput,
                )

            request_ids = (
                (args.request_id,)
                if args.command == "smoke"
                else tuple(request.request_id for request in requests)
            )
            mode = EvaluationMode.REAL_SMOKE if args.command == "smoke" else EvaluationMode.REAL_FULL
            run_directory = run_advanced_evaluation(
                args.project_pack,
                request_ids,
                mode=mode,
                client_factory=real_factory,
                provider=settings.provider,
                model=settings.model,
                seed=args.seed,
                max_output_tokens=args.max_output_tokens,
                max_attempts=args.max_attempts,
                results_root=args.results_root,
            )
        if run_completed_successfully(run_directory):
            print(f"Advanced evaluation completed uncurated at {run_directory}")
            return 0
        print(f"Advanced evaluation incomplete at {run_directory}", file=sys.stderr)
        return 1
    except (
        AdvancedAgentError,
        AdvancedEvaluationError,
        ConfigurationError,
        OSError,
        ValueError,
    ) as exc:
        print(f"Advanced evaluation error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
