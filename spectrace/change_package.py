"""Evidence-linked change-impact packages produced only after human review."""

from __future__ import annotations

import hashlib
import json

from spectrace.advanced_models import (
    AdvancedAssessment,
    ChangeImpactPackage,
    DraftAcceptanceCriterion,
    DriftAssessment,
    HumanAction,
    HumanReview,
    RetrievalBundle,
    VerificationResult,
)
from spectrace.models import Classification, IncomingRequest


def _hash(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_change_impact_package(
    request: IncomingRequest,
    assessment: AdvancedAssessment,
    drift: DriftAssessment,
    verification: VerificationResult,
    review: HumanReview,
    retrieval: RetrievalBundle,
    *,
    source_hash_by_id: dict[str, str] | None = None,
) -> ChangeImpactPackage:
    """Build a full package only for an explicitly approved scope-changing payload."""

    payload = review.decision_payload
    full_change = bool(
        review.action in {HumanAction.APPROVE, HumanAction.OVERRIDE}
        and payload
        and payload.changes_approved_scope
        and payload.approves_requested_capability
    )
    final_classification = review.final_classification or assessment.classification
    cited = tuple(sorted(set(assessment.supporting_evidence_ids) | set(assessment.conflicting_evidence_ids)))
    retrieved = {item.evidence.evidence_id: item.evidence for item in retrieval.items}
    hashes = {evidence_id: evidence.source_hash for evidence_id, evidence in retrieved.items()}
    hashes.update(source_hash_by_id or {})
    payload_evidence = payload.evidence_ids if payload else ()
    source_ids = set(cited) | set(payload_evidence)
    source_hashes = tuple(sorted({hashes[evidence_id] for evidence_id in source_ids if evidence_id in hashes}))
    verification_hash = _hash(verification.model_dump(mode="json"))

    if not full_change:
        return ChangeImpactPackage(
            request_id=request.request_id,
            review_id=review.review_id,
            is_review_memo=True,
            approval_state=review.action,
            agent_classification=assessment.classification,
            final_classification=final_classification,
            summary="Human review recorded without an approved scope-changing capability; no change package was authorized.",
            supporting_evidence_ids=assessment.supporting_evidence_ids,
            conflicting_evidence_ids=assessment.conflicting_evidence_ids,
            open_questions=assessment.clarification_questions,
            dependencies=assessment.dependencies,
            drift_severity=drift.severity,
            drift_pattern=drift.pattern_key,
            non_goals=("No implementation commitment, estimate, contract amendment, or client communication is implied.",),
            unknowns=assessment.clarification_questions,
            verification_hash=verification_hash,
            source_hashes=source_hashes,
        )

    criterion_evidence = tuple(sorted(set(payload.evidence_ids) | set(cited)))
    criteria = (
        DraftAcceptanceCriterion(
            criterion_id=f"DRAFT-{request.request_id}-001",
            text=f"DRAFT: Verify the approved behavior described by: {request.message}",
            evidence_ids=criterion_evidence,
        ),
    ) if criterion_evidence else ()
    signature = assessment.capability_signature
    return ChangeImpactPackage(
        request_id=request.request_id,
        review_id=review.review_id,
        is_review_memo=False,
        approval_state=review.action,
        agent_classification=assessment.classification,
        final_classification=final_classification,
        summary="Human-approved scope change package. Acceptance criteria remain draft pending normal project governance.",
        supporting_evidence_ids=assessment.supporting_evidence_ids,
        conflicting_evidence_ids=assessment.conflicting_evidence_ids,
        added_requirements=(payload.decision_text,),
        affected_actors=signature.actors,
        affected_components=signature.objects,
        affected_data_state=tuple(facet for facet in signature.facets if facet in {"persistence", "capacity"}),
        affected_integrations=tuple(term for term in signature.domain_terms if term in {"api", "calendar", "email", "integration", "sms"}),
        dependencies=assessment.dependencies,
        workflow_steps=("Record the approved decision in the ledger.", "Refine the evidence-linked draft acceptance criterion with the human reviewer."),
        open_questions=assessment.clarification_questions,
        acceptance_criteria=criteria,
        drift_severity=drift.severity,
        drift_pattern=drift.pattern_key,
        non_goals=("No cost, legal, contract, schedule, or delivery commitment is generated.",),
        unknowns=assessment.clarification_questions,
        verification_hash=verification_hash,
        source_hashes=source_hashes,
    )
