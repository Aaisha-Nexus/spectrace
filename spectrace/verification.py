"""Deterministic verification gates for advanced assessments."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable

from spectrace.advanced_models import (
    AdvancedAssessment,
    ConflictAssessment,
    DriftAssessment,
    EvidenceCategory,
    RetrievalBundle,
    ScopeAnchor,
    SufficiencyAssessment,
    TemporalStatus,
    VerificationIssue,
    VerificationResult,
)
from spectrace.analysis_tools import reconcile_classification
from spectrace.models import Classification


APPROVAL_CLAIM_RE = re.compile(
    r"\b(?:request|capability|change)\s+(?:is|has been)\s+approved\b", re.IGNORECASE
)


def verify_assessment(
    assessment: AdvancedAssessment,
    retrieval: RetrievalBundle,
    anchor: ScopeAnchor,
    sufficiency: SufficiencyAssessment,
    conflicts: ConflictAssessment,
    drift: DriftAssessment,
    *,
    eligible_drift_request_ids: Iterable[str] = (),
    eligible_drift_decision_ids: Iterable[str] = (),
) -> VerificationResult:
    """Verify evidence placement, temporal validity, precedence, and drift provenance."""

    issues: list[VerificationIssue] = []
    anchor_by_id = {item.evidence_id: item for item in anchor.items}
    retrieved_by_id = {item.evidence.evidence_id: item.evidence for item in retrieval.items}
    supporting = set(assessment.supporting_evidence_ids)
    conflicting = set(assessment.conflicting_evidence_ids)

    for evidence_id in sorted(supporting | conflicting):
        evidence = anchor_by_id.get(evidence_id)
        if evidence is None:
            issues.append(VerificationIssue(code="UNKNOWN_CITATION", message="Citation does not exist in the scope anchor.", evidence_ids=(evidence_id,), repairable=True))
        elif evidence_id not in retrieved_by_id:
            issues.append(VerificationIssue(code="UNRETRIEVED_CITATION", message="Citation was not supplied to the analysis model.", evidence_ids=(evidence_id,), repairable=True))
        elif retrieved_by_id[evidence_id].temporal_status in {TemporalStatus.FUTURE, TemporalStatus.SUPERSEDED}:
            issues.append(VerificationIssue(code="INACTIVE_CITATION", message="Citation is future or fully superseded at this cutoff.", evidence_ids=(evidence_id,), repairable=True))

    misplaced_conflicts = sorted(
        evidence_id
        for evidence_id in conflicting
        if evidence_id not in set(conflicts.conflicting_evidence_ids)
    )
    if misplaced_conflicts:
        issues.append(VerificationIssue(code="MISPLACED_CONFLICT", message="Conflicting citations must be current specific rejection evidence.", evidence_ids=tuple(misplaced_conflicts), repairable=True))

    if assessment.classification == Classification.CONTRADICTS_APPROVED_DECISION:
        required = set(conflicts.conflicting_evidence_ids)
        if not required or not (conflicting & required):
            issues.append(VerificationIssue(code="MISSING_CONTRADICTION_EVIDENCE", message="A contradiction requires a current specific rejection in the conflicting field.", evidence_ids=tuple(sorted(required)), repairable=True))
    elif conflicting:
        issues.append(VerificationIssue(code="CONFLICT_FIELD_FOR_NON_CONTRADICTION", message="Only contradiction classification may retain conflicting evidence.", evidence_ids=tuple(sorted(conflicting)), repairable=True))
    if supporting & conflicting:
        duplicate = tuple(sorted(supporting & conflicting))
        issues.append(VerificationIssue(code="DUPLICATE_CITATION_PLACEMENT", message="A citation cannot appear in both evidence fields.", evidence_ids=duplicate, repairable=True))

    if assessment.classification == Classification.IN_SCOPE and not (
        supporting & set(conflicts.approved_evidence_ids)
    ):
        issues.append(VerificationIssue(code="MISSING_IN_SCOPE_SUPPORT", message="IN_SCOPE requires current approved support.", evidence_ids=conflicts.approved_evidence_ids, repairable=True))
    if assessment.classification == Classification.OUT_OF_SCOPE and not (
        supporting & set(conflicts.exclusion_evidence_ids)
    ):
        issues.append(VerificationIssue(code="MISSING_EXCLUSION_SUPPORT", message="OUT_OF_SCOPE requires a current exclusion citation.", evidence_ids=conflicts.exclusion_evidence_ids, repairable=True))

    expected = reconcile_classification(
        assessment.model_recommendation,
        sufficiency,
        conflicts,
    )
    if assessment.classification != expected:
        issues.append(VerificationIssue(code="PRECEDENCE_MISMATCH", message=f"Classification must reconcile to {expected.value}.", repairable=True))

    if assessment.requires_clarification != (not sufficiency.sufficient_for_classification):
        issues.append(VerificationIssue(code="CLARIFICATION_MISMATCH", message="Clarification state conflicts with deterministic sufficiency.", repairable=True))

    if assessment.classification == Classification.IN_SCOPE and supporting:
        substantive = [
            evidence_id for evidence_id in supporting
            if retrieved_by_id.get(evidence_id)
            and retrieved_by_id[evidence_id].category
            not in {EvidenceCategory.ASSUMPTION, EvidenceCategory.UNRESOLVED_QUESTION}
        ]
        if not substantive:
            issues.append(VerificationIssue(code="NONAUTHORITATIVE_IN_SCOPE_SUPPORT", message="Assumptions and unresolved questions cannot be the sole support for IN_SCOPE.", evidence_ids=tuple(sorted(supporting)), repairable=True))

    if APPROVAL_CLAIM_RE.search(assessment.rationale):
        issues.append(VerificationIssue(code="UNAUTHORIZED_APPROVAL_CLAIM", message="The assessment may not claim that a request or change is approved.", repairable=False))

    allowed_requests = set(eligible_drift_request_ids)
    allowed_decisions = set(eligible_drift_decision_ids)
    bad_requests = sorted(set(drift.related_request_ids) - allowed_requests)
    bad_decisions = sorted(set(drift.related_decision_ids) - allowed_decisions)
    if bad_requests or bad_decisions:
        issues.append(VerificationIssue(code="INELIGIBLE_DRIFT_HISTORY", message="Drift may use only human-approved scope-change history.", repairable=False))
    if drift.cumulative_drift_detected != (drift.severity.value == "SUBSYSTEM"):
        issues.append(VerificationIssue(code="DRIFT_BOOLEAN_MISMATCH", message="The drift boolean is true only at SUBSYSTEM severity.", repairable=False))

    return VerificationResult(passed=not issues, issues=tuple(issues))


def verify_with_optional_repair(
    assessment: AdvancedAssessment,
    verifier: Callable[[AdvancedAssessment], VerificationResult],
    *,
    repairer: Callable[[AdvancedAssessment, VerificationResult], AdvancedAssessment] | None = None,
) -> tuple[AdvancedAssessment, VerificationResult]:
    """Allow no more than one bounded repair; otherwise fail closed."""

    first = verifier(assessment)
    if first.passed or repairer is None or any(not issue.repairable for issue in first.issues):
        return assessment, first
    repaired = repairer(assessment, first)
    second = verifier(repaired)
    result = VerificationResult(
        passed=second.passed,
        issues=second.issues,
        repair_attempted=True,
        repair_succeeded=second.passed,
    )
    return repaired, result
