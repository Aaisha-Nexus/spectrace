from __future__ import annotations

from pathlib import Path

from spectrace.advanced_models import (
    CapabilitySignature,
    ConflictAssessment,
    DriftSeverity,
    DriftThresholds,
    TemporalStatus,
    SufficiencyAssessment,
)
from spectrace.analysis_tools import (
    assess_sufficiency,
    calculate_cumulative_drift,
    find_effective_conflicts,
    reconcile_classification,
)
from spectrace.dataset import validate_project_pack
from spectrace.models import Classification
from spectrace.retrieval import retrieve_evidence
from spectrace.scope_anchor import build_scope_anchor


PACK = Path(__file__).parents[1] / "data" / "synthetic" / "demo_project"


def _case(request_id: str):
    pack = validate_project_pack(PACK)
    request = next(item for item in pack.requests if item.request_id == request_id)
    anchor = build_scope_anchor(PACK)
    retrieval = retrieve_evidence(anchor, PACK, request.message, request.evidence_available_through)
    return request, retrieval, anchor


def test_undefined_actor_and_vague_target_are_blocking_but_acceptance_detail_is_not() -> None:
    helper, retrieval, anchor = _case("CR-003")
    assert not assess_sufficiency(helper, retrieval, anchor).sufficient_for_classification
    instant, retrieval, anchor = _case("CR-004")
    assert not assess_sufficiency(instant, retrieval, anchor).sufficient_for_classification
    cancellation, retrieval, anchor = _case("CR-002")
    result = assess_sufficiency(cancellation, retrieval, anchor)
    assert result.sufficient_for_classification
    assert any(not finding.blocking for finding in result.findings)


def test_specific_rejection_beats_other_signals_and_neutral_boundary_is_not_conflict() -> None:
    ceramic, retrieval, anchor = _case("CR-008")
    conflicts = find_effective_conflicts(ceramic, retrieval, anchor)
    assert conflicts.proven_specific_contradiction
    assert "DEC-003" in conflicts.conflicting_evidence_ids

    queue, retrieval, anchor = _case("CR-007")
    neutral = find_effective_conflicts(queue, retrieval, anchor)
    assert not neutral.proven_specific_contradiction
    assert "DEC-005" in neutral.neutral_boundary_evidence_ids


def test_frozen_advanced_precedence_is_deterministic() -> None:
    sufficient = SufficiencyAssessment(sufficient_for_classification=True, rationale="enough")
    ambiguous = SufficiencyAssessment(
        sufficient_for_classification=False,
        findings=({"kind": "UNDEFINED_ACTOR", "description": "unknown", "blocking": True, "heuristic": True, "clarification_question": "Who?"},),
        clarification_questions=("Who?",),
        rationale="blocked",
    )
    contradiction = ConflictAssessment(proven_specific_contradiction=True, conflicting_evidence_ids=("DEC-003",), rationale="specific rejection")
    none = ConflictAssessment(proven_specific_contradiction=False, rationale="none")
    assert reconcile_classification(Classification.IN_SCOPE, ambiguous, contradiction) == Classification.CONTRADICTS_APPROVED_DECISION
    assert reconcile_classification(Classification.IN_SCOPE, ambiguous, none) == Classification.AMBIGUOUS
    approved = ConflictAssessment(proven_specific_contradiction=False, approved_evidence_ids=("SOW-SCP-001",), exclusion_evidence_ids=("SOW-EXC-001",), rationale="both")
    assert reconcile_classification(Classification.OUT_OF_SCOPE, sufficient, approved) == Classification.IN_SCOPE


def test_subsystem_drift_requires_related_approved_history_and_distinct_increment() -> None:
    prior = (
        CapabilitySignature(domain_terms=("queue", "join"), actions=("join",), objects=("queue",), facets=("workflow",), source_request_ids=("CR-006",), source_decision_ids=("DEC-005",)),
        CapabilitySignature(domain_terms=("queue", "order"), actions=("order",), objects=("queue",), facets=("ordering", "persistence"), source_request_ids=("CR-007",), source_decision_ids=("DEC-006",)),
    )
    current = CapabilitySignature(domain_terms=("queue", "assign", "capacity"), actions=("assign",), objects=("queue", "capacity"), facets=("automation", "capacity"), dependency_terms=("queue",), source_request_ids=("CR-010",))
    result = calculate_cumulative_drift(current, prior)
    assert result.severity == DriftSeverity.SUBSYSTEM
    assert result.cumulative_drift_detected
    unrelated = CapabilitySignature(domain_terms=("invoice",), objects=("invoice",), facets=("financial",), source_request_ids=("CR-009",))
    assert calculate_cumulative_drift(unrelated, prior).severity == DriftSeverity.NONE


def test_one_approved_change_is_at_most_emerging_and_thresholds_are_configurable() -> None:
    prior = CapabilitySignature(domain_terms=("queue", "join"), actions=("join",), objects=("queue",), facets=("ordering",), source_request_ids=("CR-006",), source_decision_ids=("DEC-005",))
    current = CapabilitySignature(domain_terms=("queue", "notify"), actions=("notify",), objects=("queue",), facets=("notification",), dependency_terms=("queue",), source_request_ids=("CR-007",))
    assert calculate_cumulative_drift(current, (prior,)).severity == DriftSeverity.EMERGING
    strict = DriftThresholds(subsystem_prior_approved_changes=3, subsystem_total_increments=4, subsystem_minimum_facets=4)
    assert calculate_cumulative_drift(current, (prior,), thresholds=strict).severity != DriftSeverity.SUBSYSTEM


def test_partial_supersession_keeps_decision_usable_but_neutral_facet_does_not_approve() -> None:
    request, retrieval, anchor = _case("CR-010")
    decision = next(item.evidence for item in retrieval.items if item.evidence.evidence_id == "DEC-005")
    assert decision.temporal_status == TemporalStatus.PARTIALLY_SUPERSEDED
    conflicts = find_effective_conflicts(request, retrieval, anchor)
    assert not conflicts.proven_specific_contradiction
    assert "DEC-005" in conflicts.neutral_boundary_evidence_ids
    assert "DEC-005" not in conflicts.approved_evidence_ids
