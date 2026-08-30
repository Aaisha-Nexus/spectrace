from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from spectrace.advanced_models import AdvancedAssessment, DriftAssessment, DriftSeverity, HumanAction, HumanDecisionPayload, HumanReview, LedgerEntryEffect, VerificationResult
from spectrace.analysis_tools import build_capability_signature
from spectrace.change_package import build_change_impact_package
from spectrace.dataset import validate_project_pack
from spectrace.models import Classification
from spectrace.retrieval import retrieve_evidence
from spectrace.scope_anchor import build_scope_anchor


PACK = Path(__file__).parents[1] / "data" / "synthetic" / "demo_project"


def _values():
    request = validate_project_pack(PACK).requests[5]
    anchor = build_scope_anchor(PACK)
    retrieval = retrieve_evidence(anchor, PACK, request.message, request.evidence_available_through)
    assessment = AdvancedAssessment(request_id=request.request_id, model_recommendation=Classification.POTENTIAL_SCOPE_CHANGE, classification=Classification.POTENTIAL_SCOPE_CHANGE, supporting_evidence_ids=("SOW-CON-001",), requires_clarification=False, rationale="A distinct capability needs review.", capability_signature=build_capability_signature(request.message, evidence_ids=("SOW-CON-001",), request_ids=(request.request_id,)))
    drift = DriftAssessment(severity=DriftSeverity.EMERGING, cumulative_drift_detected=False, approved_change_count=1, rationale="related")
    verification = VerificationResult(passed=True)
    return request, retrieval, assessment, drift, verification


def test_defer_builds_review_memo_not_change_package() -> None:
    request, retrieval, assessment, drift, verification = _values()
    review = HumanReview(review_id="HR-DEFER", project_id="studiolane", request_id=request.request_id, assessment_id=f"ASMNT-{request.request_id}", action=HumanAction.DEFER, reviewer_id="human", reviewed_at=datetime.now(UTC))
    package = build_change_impact_package(request, assessment, drift, verification, review, retrieval)
    assert package.is_review_memo
    assert not package.acceptance_criteria


def test_explicit_approved_scope_change_builds_evidence_linked_draft() -> None:
    request, retrieval, assessment, drift, verification = _values()
    payload = HumanDecisionPayload(decision_id="DEC-007", effective_date=date(2026, 5, 27), effect=LedgerEntryEffect.APPROVE_CAPABILITY, decision_text="Approve an opt-in email availability alert.", evidence_ids=("SOW-CON-001",), changes_approved_scope=True, approves_requested_capability=True)
    review = HumanReview(review_id="HR-APPROVE", project_id="studiolane", request_id=request.request_id, assessment_id=f"ASMNT-{request.request_id}", action=HumanAction.APPROVE, reviewer_id="human", reviewed_at=datetime.now(UTC), final_classification=Classification.POTENTIAL_SCOPE_CHANGE, decision_payload=payload)
    package = build_change_impact_package(request, assessment, drift, verification, review, retrieval)
    assert not package.is_review_memo
    assert package.acceptance_criteria[0].status == "DRAFT"
    assert package.acceptance_criteria[0].evidence_ids
    assert "cost" in package.non_goals[0].lower()
