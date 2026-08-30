"""Resumable advanced-analysis state machine with a mandatory human gate."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from spectrace.advanced_models import (
    AdvancedAssessment,
    AdvancedModelOutput,
    AdvancedRunState,
    AgentNode,
    AgentStatus,
    HumanAction,
    HumanReview,
    HumanReviewRecommendation,
    RetrievalLimits,
    TrajectoryEvent,
)
from spectrace.analysis_tools import (
    assess_sufficiency,
    build_capability_signature,
    calculate_cumulative_drift,
    find_effective_conflicts,
    reconcile_classification,
)
from spectrace.change_package import build_change_impact_package
from spectrace.ledger import LedgerError, LedgerStore
from spectrace.llm import StructuredGenerationClient, generate_structured_with_retry
from spectrace.models import Classification, IncomingRequest
from spectrace.retrieval import retrieve_evidence
from spectrace.scope_anchor import build_scope_anchor
from spectrace.verification import verify_assessment, verify_with_optional_repair


DEFAULT_ADVANCED_PROMPT = Path(__file__).resolve().parents[1] / "prompts" / "advanced.md"


class AdvancedAgentError(RuntimeError):
    """Raised when the advanced run cannot safely proceed."""


def _hash(value: object) -> str:
    if isinstance(value, str):
        payload = value
    else:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def advanced_prompt_hash(prompt_path: str | Path = DEFAULT_ADVANCED_PROMPT) -> str:
    return hashlib.sha256(Path(prompt_path).read_bytes()).hexdigest()


def render_advanced_prompt(
    request: IncomingRequest,
    retrieval: object,
    sufficiency: object,
    conflicts: object,
    ledger_summary: object = (),
    prompt_path: str | Path = DEFAULT_ADVANCED_PROMPT,
) -> str:
    template = Path(prompt_path).read_text(encoding="utf-8")
    evidence_json = json.dumps(retrieval.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, indent=2)
    tool_json = json.dumps(
        {
            "sufficiency": sufficiency.model_dump(mode="json"),
            "conflicts": conflicts.model_dump(mode="json"),
        },
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    )
    return (
        template.replace("{{REQUEST_JSON}}", request.model_dump_json(indent=2))
        .replace("{{EVIDENCE_JSON}}", evidence_json)
        .replace("{{TOOL_JSON}}", tool_json)
        .replace("{{LEDGER_JSON}}", json.dumps(ledger_summary, ensure_ascii=False, sort_keys=True, indent=2))
    )


def _append_event(
    state: AdvancedRunState,
    ledger: LedgerStore,
    node: AgentNode,
    *,
    tool: str | None,
    input_ids: tuple[str, ...],
    input_value: object,
    summary: str,
    started: float,
    verification: str | None = None,
    human_state: str | None = None,
    error: str | None = None,
) -> None:
    event = TrajectoryEvent(
        sequence=len(state.trajectory) + 1,
        node=node,
        tool=tool,
        input_ids=input_ids,
        input_hash=_hash(input_value),
        result_summary=summary,
        verification=verification,
        duration_ms=max(0, int((time.perf_counter() - started) * 1000)),
        human_state=human_state,
        error=error,
    )
    state.current_node = node
    state.trajectory = (*state.trajectory, event)
    ledger.record_trajectory_event(state.project_id, state.request.request_id, event)


def _ensure_seeded(ledger: LedgerStore, state: AdvancedRunState, anchor: object) -> None:
    try:
        ledger.snapshot(state.project_id)
    except LedgerError:
        ledger.seed_anchor(
            anchor,
            state.project_pack_path,
            approved_through=state.request.evidence_available_through,
        )


def _deterministic_repair(
    assessment: AdvancedAssessment,
    _result: object,
    *,
    sufficiency: object,
    conflicts: object,
) -> AdvancedAssessment:
    classification = reconcile_classification(
        assessment.model_recommendation, sufficiency, conflicts
    )
    if classification == Classification.CONTRADICTS_APPROVED_DECISION:
        supporting: tuple[str, ...] = ()
        conflicting = conflicts.conflicting_evidence_ids
    elif classification == Classification.IN_SCOPE:
        supporting = conflicts.approved_evidence_ids
        conflicting = ()
    elif classification == Classification.OUT_OF_SCOPE:
        supporting = conflicts.exclusion_evidence_ids
        conflicting = ()
    elif classification == Classification.AMBIGUOUS:
        supporting = tuple(sorted({evidence_id for finding in sufficiency.findings for evidence_id in finding.evidence_ids}))
        conflicting = ()
    else:
        supporting = assessment.supporting_evidence_ids
        conflicting = ()
    return assessment.model_copy(
        update={
            "classification": classification,
            "supporting_evidence_ids": supporting,
            "conflicting_evidence_ids": conflicting,
            "requires_clarification": not sufficiency.sufficient_for_classification,
            "clarification_questions": sufficiency.clarification_questions,
        }
    )


def new_run_state(
    project_pack_path: str | Path,
    project_id: str,
    request: IncomingRequest,
    *,
    run_id: str | None = None,
) -> AdvancedRunState:
    return AdvancedRunState(
        run_id=run_id or f"advanced-{request.request_id.lower()}",
        project_pack_path=str(Path(project_pack_path)),
        project_id=project_id,
        request=request,
    )


def run_until_human_review(
    state: AdvancedRunState,
    ledger: LedgerStore,
    client: StructuredGenerationClient,
    *,
    limits: RetrievalLimits | None = None,
    max_attempts: int = 2,
    prompt_path: str | Path = DEFAULT_ADVANCED_PROMPT,
) -> AdvancedRunState:
    """Run once to the mandatory human-review pause without mutating approved memory."""

    if state.status not in {AgentStatus.NEW, AgentStatus.FAILED}:
        raise AdvancedAgentError(f"cannot start from state {state.status.value}")
    state.status = AgentStatus.RUNNING

    started = time.perf_counter()
    anchor = build_scope_anchor(state.project_pack_path)
    if anchor.project_id != state.project_id:
        raise AdvancedAgentError("state project_id does not match the scope anchor")
    _ensure_seeded(ledger, state, anchor)
    ledger.record_request(state.project_id, state.request)
    state.anchor_hash = anchor.anchor_hash
    _append_event(state, ledger, AgentNode.LOAD_SCOPE_ANCHOR, tool="build_scope_anchor", input_ids=(state.project_id,), input_value=state.project_pack_path, summary=f"Loaded {len(anchor.items)} evidence items.", started=started)

    started = time.perf_counter()
    retrieval = retrieve_evidence(anchor, state.project_pack_path, state.request.message, state.request.evidence_available_through, limits)
    state.retrieval = retrieval
    evidence_ids = tuple(item.evidence.evidence_id for item in retrieval.items)
    _append_event(state, ledger, AgentNode.RETRIEVE_EVIDENCE, tool="retrieve_evidence", input_ids=(state.request.request_id, *evidence_ids), input_value={"message": state.request.message, "cutoff": state.request.evidence_available_through}, summary=f"Retrieved {len(evidence_ids)} cutoff-safe evidence items.", started=started)

    started = time.perf_counter()
    sufficiency = assess_sufficiency(state.request, retrieval, anchor)
    state.sufficiency = sufficiency
    _append_event(state, ledger, AgentNode.ASSESS_SUFFICIENCY, tool="assess_sufficiency", input_ids=(state.request.request_id,), input_value=sufficiency.model_dump(mode="json"), summary="Classification sufficiency assessed.", started=started)

    started = time.perf_counter()
    conflicts = find_effective_conflicts(state.request, retrieval, anchor)
    state.conflicts = conflicts
    _append_event(state, ledger, AgentNode.CHECK_CONTRADICTIONS, tool="find_effective_conflicts", input_ids=(state.request.request_id, *conflicts.conflicting_evidence_ids), input_value=conflicts.model_dump(mode="json"), summary="Current effective conflicts and boundaries checked.", started=started)

    started = time.perf_counter()
    records = ledger.approved_scope_change_records(state.project_id)
    ledger_summary = tuple(
        {
            "decision_id": record["decision_id"],
            "request_id": record["request_id"],
            "decision_text": record["decision_text"],
            "evidence_ids": record["evidence_ids"],
            "effective_date": record["effective_date"],
        }
        for record in records
    )
    rendered = render_advanced_prompt(
        state.request,
        retrieval,
        sufficiency,
        conflicts,
        ledger_summary,
        prompt_path,
    )
    state.prompt_hash = advanced_prompt_hash(prompt_path)
    state.assembled_prompt_hash = _hash(rendered)
    attempt = generate_structured_with_retry(client, rendered, AdvancedModelOutput, max_attempts=max_attempts, expected_request_id=state.request.request_id)
    state.raw_response_hash = _hash(attempt.raw_response)
    state.token_usage = attempt.usage
    model_output = attempt.output
    signature = build_capability_signature(
        state.request.message,
        evidence_ids=tuple(sorted(set(model_output.supporting_evidence_ids) | set(model_output.conflicting_evidence_ids))),
        request_ids=(state.request.request_id,),
        known_actors={actor for item in anchor.items for actor in item.actor_terms},
        dependencies=model_output.dependencies,
    )
    classification = reconcile_classification(model_output.recommended_classification, sufficiency, conflicts)
    assessment = AdvancedAssessment(
        request_id=state.request.request_id,
        model_recommendation=model_output.recommended_classification,
        classification=classification,
        supporting_evidence_ids=model_output.supporting_evidence_ids,
        conflicting_evidence_ids=model_output.conflicting_evidence_ids,
        requires_clarification=not sufficiency.sufficient_for_classification,
        clarification_questions=sufficiency.clarification_questions,
        dependencies=model_output.dependencies,
        rationale=model_output.rationale,
        capability_signature=signature,
    )
    state.assessment = assessment
    _append_event(state, ledger, AgentNode.CLASSIFY_REQUEST, tool="structured_generation+reconcile_classification", input_ids=(state.request.request_id, *evidence_ids), input_value=state.assembled_prompt_hash, summary=f"Recommended {model_output.recommended_classification.value}; reconciled {classification.value}.", started=started)

    started = time.perf_counter()
    prior_signatures = tuple(
        build_capability_signature(
            record["decision_text"],
            evidence_ids=record["evidence_ids"],
            request_ids=(record["request_id"],),
            decision_ids=(record["decision_id"],),
            known_actors={actor for item in anchor.items for actor in item.actor_terms},
        )
        for record in records
    )
    drift = calculate_cumulative_drift(signature, prior_signatures)
    state.drift = drift
    _append_event(state, ledger, AgentNode.CALCULATE_CUMULATIVE_DRIFT, tool="calculate_cumulative_drift", input_ids=(state.request.request_id, *drift.related_decision_ids), input_value=[record["entry_hash"] for record in records], summary=f"Drift severity: {drift.severity.value}.", started=started)

    started = time.perf_counter()
    eligible_requests = (
        *(record["request_id"] for record in records),
        state.request.request_id,
    )
    eligible_decisions = tuple(record["decision_id"] for record in records)
    verifier = lambda candidate: verify_assessment(candidate, retrieval, anchor, sufficiency, conflicts, drift, eligible_drift_request_ids=eligible_requests, eligible_drift_decision_ids=eligible_decisions)
    assessment, verification = verify_with_optional_repair(
        assessment,
        verifier,
        repairer=lambda candidate, result: _deterministic_repair(candidate, result, sufficiency=sufficiency, conflicts=conflicts),
    )
    state.assessment = assessment
    state.verification = verification
    _append_event(state, ledger, AgentNode.VERIFY_ASSESSMENT, tool="verify_assessment", input_ids=(state.request.request_id, *assessment.supporting_evidence_ids, *assessment.conflicting_evidence_ids), input_value=assessment.model_dump(mode="json"), summary="Assessment verification passed." if verification.passed else "Assessment verification failed closed.", started=started, verification="PASS" if verification.passed else "FAIL")
    assessment_id = f"ASMNT-{state.request.request_id}"
    known_evidence_ids = {item.evidence_id for item in anchor.items}
    recorded_evidence_ids = tuple(
        sorted(
            (set(assessment.supporting_evidence_ids) | set(assessment.conflicting_evidence_ids))
            & known_evidence_ids
        )
    )
    ledger.record_assessment(state.project_id, assessment_id, state.request.request_id, classification=assessment.classification, evidence_ids=recorded_evidence_ids, assessment=assessment.model_dump(mode="json"))

    started = time.perf_counter()
    action = (
        HumanAction.DEFER
        if not verification.passed
        else HumanAction.NEEDS_CLARIFICATION
        if assessment.requires_clarification
        else HumanAction.DEFER
    )
    recommendation = HumanReviewRecommendation(
        request_id=state.request.request_id,
        action=action,
        classification=assessment.classification,
        summary=(
            "Verification failed closed; a human must review the issues."
            if not verification.passed
            else "Review the evidence-grounded assessment; only an explicit human transaction may change approved memory."
        ),
        evidence_ids=tuple(sorted(set(assessment.supporting_evidence_ids) | set(assessment.conflicting_evidence_ids))),
        clarification_questions=assessment.clarification_questions,
        drift_severity=drift.severity,
    )
    state.recommendation = recommendation
    _append_event(state, ledger, AgentNode.PREPARE_RECOMMENDATION, tool="prepare_human_recommendation", input_ids=(state.request.request_id,), input_value=recommendation.model_dump(mode="json"), summary=f"Prepared {action.value} recommendation.", started=started)

    state.pause_snapshot_hash = ledger.snapshot(state.project_id).snapshot_hash
    state.status = AgentStatus.AWAITING_HUMAN_REVIEW
    started = time.perf_counter()
    _append_event(state, ledger, AgentNode.AWAIT_HUMAN_REVIEW, tool=None, input_ids=(state.request.request_id, assessment_id), input_value=state.pause_snapshot_hash, summary="Paused before consequential ledger mutation.", started=started, human_state=state.status.value)
    return state


def resume_after_human_review(
    state: AdvancedRunState,
    ledger: LedgerStore,
    review: HumanReview,
) -> AdvancedRunState:
    """Validate the pause boundary, atomically apply review, and complete output."""

    if state.status != AgentStatus.AWAITING_HUMAN_REVIEW:
        raise AdvancedAgentError("run is not awaiting human review")
    if review.project_id != state.project_id or review.request_id != state.request.request_id:
        raise AdvancedAgentError("human review does not match the paused run")
    if review.assessment_id != f"ASMNT-{state.request.request_id}":
        raise AdvancedAgentError("human review references the wrong assessment")
    anchor = build_scope_anchor(state.project_pack_path)
    if anchor.anchor_hash != state.anchor_hash:
        raise AdvancedAgentError("scope anchor changed while the run was paused")
    snapshot = ledger.snapshot(state.project_id)
    if snapshot.snapshot_hash != state.pause_snapshot_hash:
        raise AdvancedAgentError("ledger snapshot changed while the run was paused")
    if not all((state.assessment, state.drift, state.verification, state.retrieval)):
        raise AdvancedAgentError("paused state is incomplete")

    started = time.perf_counter()
    update = ledger.apply_human_review(review)
    state.status = AgentStatus.REVIEWED
    state.human_review = review
    state.ledger_update = update
    _append_event(state, ledger, AgentNode.APPLY_HUMAN_DECISION, tool="apply_human_review", input_ids=(review.review_id, review.request_id), input_value=review.model_dump(mode="json"), summary="Applied explicit human review transaction.", started=started, human_state=review.action.value)

    started = time.perf_counter()
    package = build_change_impact_package(
        state.request,
        state.assessment,
        state.drift,
        state.verification,
        review,
        state.retrieval,
        source_hash_by_id={item.evidence_id: item.source_hash for item in anchor.items},
    )
    state.change_package = package
    _append_event(state, ledger, AgentNode.BUILD_CHANGE_IMPACT_PACKAGE, tool="build_change_impact_package", input_ids=(review.review_id, state.request.request_id), input_value=package.model_dump(mode="json"), summary="Built full change package." if not package.is_review_memo else "Built review memo without an authorized scope change.", started=started)

    state.status = AgentStatus.COMPLETE
    started = time.perf_counter()
    _append_event(state, ledger, AgentNode.COMPLETE, tool=None, input_ids=(state.request.request_id,), input_value=ledger.snapshot(state.project_id).snapshot_hash, summary="Advanced run completed after human review.", started=started, human_state=state.status.value)
    return state
