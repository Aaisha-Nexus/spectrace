from __future__ import annotations

from pathlib import Path

from spectrace.advanced_models import AdvancedAssessment, DriftAssessment, DriftSeverity
from spectrace.analysis_tools import assess_sufficiency, build_capability_signature, find_effective_conflicts, reconcile_classification
from spectrace.dataset import validate_project_pack
from spectrace.models import Classification
from spectrace.retrieval import retrieve_evidence
from spectrace.scope_anchor import build_scope_anchor
from spectrace.verification import verify_assessment, verify_with_optional_repair


PACK = Path(__file__).parents[1] / "data" / "synthetic" / "demo_project"


def _context(request_id: str):
    pack = validate_project_pack(PACK)
    request = next(item for item in pack.requests if item.request_id == request_id)
    anchor = build_scope_anchor(PACK)
    retrieval = retrieve_evidence(anchor, PACK, request.message, request.evidence_available_through)
    sufficiency = assess_sufficiency(request, retrieval, anchor)
    conflicts = find_effective_conflicts(request, retrieval, anchor)
    return request, anchor, retrieval, sufficiency, conflicts


def test_verification_rejects_misplaced_contradiction_and_unauthorized_approval_claim() -> None:
    request, anchor, retrieval, sufficiency, conflicts = _context("CR-008")
    assessment = AdvancedAssessment(
        request_id=request.request_id,
        model_recommendation=Classification.IN_SCOPE,
        classification=reconcile_classification(Classification.IN_SCOPE, sufficiency, conflicts),
        supporting_evidence_ids=("DEC-003",),
        requires_clarification=False,
        rationale="The request is approved.",
        capability_signature=build_capability_signature(request.message, request_ids=(request.request_id,)),
    )
    drift = DriftAssessment(severity=DriftSeverity.NONE, cumulative_drift_detected=False, approved_change_count=0, rationale="none")
    result = verify_assessment(assessment, retrieval, anchor, sufficiency, conflicts, drift)
    assert not result.passed
    assert {issue.code for issue in result.issues} >= {"MISSING_CONTRADICTION_EVIDENCE", "UNAUTHORIZED_APPROVAL_CLAIM"}


def test_optional_repair_is_bounded_to_one_attempt() -> None:
    request, anchor, retrieval, sufficiency, conflicts = _context("CR-005")
    assessment = AdvancedAssessment(
        request_id=request.request_id,
        model_recommendation=Classification.OUT_OF_SCOPE,
        classification=Classification.OUT_OF_SCOPE,
        requires_clarification=False,
        rationale="Excluded capability.",
        capability_signature=build_capability_signature(request.message, request_ids=(request.request_id,)),
    )
    drift = DriftAssessment(severity=DriftSeverity.NONE, cumulative_drift_detected=False, approved_change_count=0, rationale="none")
    verifier = lambda candidate: verify_assessment(candidate, retrieval, anchor, sufficiency, conflicts, drift)
    calls = []
    repaired, result = verify_with_optional_repair(
        assessment,
        verifier,
        repairer=lambda candidate, first: calls.append(first) or candidate.model_copy(update={"supporting_evidence_ids": conflicts.exclusion_evidence_ids}),
    )
    assert len(calls) == 1
    assert result.repair_attempted and result.repair_succeeded and result.passed
    assert repaired.supporting_evidence_ids


def test_verification_rejects_unretrieved_citation_false_ambiguity_and_false_drift() -> None:
    request, anchor, retrieval, sufficiency, conflicts = _context("CR-001")
    assessment = AdvancedAssessment(
        request_id=request.request_id,
        model_recommendation=Classification.AMBIGUOUS,
        classification=Classification.AMBIGUOUS,
        supporting_evidence_ids=("DEC-006", "DEC-999"),
        requires_clarification=True,
        clarification_questions=("What is unclear?",),
        rationale="A human should review this heuristic ambiguity.",
        capability_signature=build_capability_signature(request.message, request_ids=(request.request_id,)),
    )
    drift = DriftAssessment(severity=DriftSeverity.RELATED, cumulative_drift_detected=False, related_request_ids=("CR-007",), related_decision_ids=("DEC-006",), approved_change_count=1, rationale="claimed relation")
    result = verify_assessment(assessment, retrieval, anchor, sufficiency, conflicts, drift)
    codes = {issue.code for issue in result.issues}
    assert {"UNKNOWN_CITATION", "UNRETRIEVED_CITATION", "PRECEDENCE_MISMATCH", "CLARIFICATION_MISMATCH", "INELIGIBLE_DRIFT_HISTORY"} <= codes
