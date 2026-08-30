"""Strict contracts for the deterministic advanced-agent foundation.

These models deliberately contain no model predictions or benchmark answer-key
fields.  They describe source evidence, retrieval, and human-controlled memory.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from enum import Enum
from typing import Annotated, Any

from pydantic import ConfigDict, Field, field_validator, model_validator

from spectrace.models import (
    Classification,
    DECISION_ID_PATTERN,
    EVIDENCE_ID_PATTERN,
    REQUEST_ID_PATTERN,
    IncomingRequest,
    StrictModel,
)


SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
ANCHOR_VERSION_PATTERN = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
REVIEW_ID_PATTERN = re.compile(r"^HR-[A-Z0-9][A-Z0-9_-]*$")
ASSESSMENT_ID_PATTERN = re.compile(r"^ASMNT-[A-Z0-9][A-Z0-9_-]*$")

NonEmptyText = Annotated[str, Field(min_length=1)]
Sha256 = Annotated[str, Field(pattern=SHA256_PATTERN.pattern)]


class EvidenceCategory(str, Enum):
    APPROVED_SCOPE = "APPROVED_SCOPE"
    CONSTRAINT = "CONSTRAINT"
    EXCLUSION = "EXCLUSION"
    ASSUMPTION = "ASSUMPTION"
    UNRESOLVED_QUESTION = "UNRESOLVED_QUESTION"
    DECISION = "DECISION"


class DecisionPolarity(str, Enum):
    APPROVES = "APPROVES"
    REJECTS = "REJECTS"
    APPROVES_WITH_OPEN_DETAILS = "APPROVES_WITH_OPEN_DETAILS"
    DOES_NOT_APPROVE_OR_REJECT = "DOES_NOT_APPROVE_OR_REJECT"


class TemporalStatus(str, Enum):
    CURRENT = "CURRENT"
    PARTIALLY_SUPERSEDED = "PARTIALLY_SUPERSEDED"
    SUPERSEDED = "SUPERSEDED"
    FUTURE = "FUTURE"


class HumanAction(str, Enum):
    APPROVE = "APPROVE"
    OVERRIDE = "OVERRIDE"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    DEFER = "DEFER"


class LedgerEntryEffect(str, Enum):
    APPROVE_CAPABILITY = "APPROVE_CAPABILITY"
    UPHOLD_EXISTING_SCOPE = "UPHOLD_EXISTING_SCOPE"
    RECORD_REJECTION = "RECORD_REJECTION"


class EvidenceItem(StrictModel):
    evidence_id: str
    category: EvidenceCategory
    source_text: NonEmptyText
    source_path: NonEmptyText
    source_location: NonEmptyText
    source_hash: Sha256
    effective_date: date | None = None
    decision_polarity: DecisionPolarity | None = None
    temporal_status: TemporalStatus = TemporalStatus.CURRENT
    actor_terms: tuple[str, ...] = ()
    action_terms: tuple[str, ...] = ()
    object_terms: tuple[str, ...] = ()
    domain_terms: tuple[str, ...] = ()
    facet_terms: tuple[str, ...] = ()
    supersedes_ids: tuple[str, ...] = ()
    superseded_by_ids: tuple[str, ...] = ()
    triggering_request_id: str | None = None

    @field_validator("evidence_id")
    @classmethod
    def validate_evidence_id(cls, value: str) -> str:
        if not EVIDENCE_ID_PATTERN.fullmatch(value):
            raise ValueError(f"invalid evidence ID: {value!r}")
        return value

    @field_validator("supersedes_ids", "superseded_by_ids")
    @classmethod
    def validate_related_evidence_ids(
        cls, values: tuple[str, ...], info: object
    ) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError(f"{info.field_name} must not contain duplicates")
        for value in values:
            if not EVIDENCE_ID_PATTERN.fullmatch(value):
                raise ValueError(f"invalid evidence ID: {value!r}")
        return values

    @field_validator(
        "actor_terms", "action_terms", "object_terms", "domain_terms", "facet_terms"
    )
    @classmethod
    def validate_metadata_terms(
        cls, values: tuple[str, ...], info: object
    ) -> tuple[str, ...]:
        normalized = tuple(value.strip().lower() for value in values)
        if any(not value for value in normalized):
            raise ValueError(f"{info.field_name} cannot contain empty values")
        if normalized != tuple(sorted(set(normalized))):
            raise ValueError(f"{info.field_name} must be sorted and unique")
        return normalized

    @field_validator("triggering_request_id")
    @classmethod
    def validate_triggering_request_id(cls, value: str | None) -> str | None:
        if value is not None and not REQUEST_ID_PATTERN.fullmatch(value):
            raise ValueError(f"invalid request ID: {value!r}")
        return value

    @model_validator(mode="after")
    def validate_category_fields(self) -> EvidenceItem:
        is_decision = self.category == EvidenceCategory.DECISION
        if is_decision and (self.effective_date is None or self.decision_polarity is None):
            raise ValueError("decision evidence requires an effective date and polarity")
        if not is_decision and self.decision_polarity is not None:
            raise ValueError("non-decision evidence cannot have decision polarity")
        return self


class SupersessionEdge(StrictModel):
    superseding_id: str
    superseded_id: str
    facet: NonEmptyText
    is_partial: bool
    source_hash: Sha256

    @field_validator("superseding_id", "superseded_id")
    @classmethod
    def validate_evidence_id(cls, value: str) -> str:
        if not EVIDENCE_ID_PATTERN.fullmatch(value):
            raise ValueError(f"invalid evidence ID: {value!r}")
        return value

    @model_validator(mode="after")
    def prevent_self_edge(self) -> SupersessionEdge:
        if self.superseding_id == self.superseded_id:
            raise ValueError("evidence cannot supersede itself")
        return self


class ScopeAnchor(StrictModel):
    project_id: NonEmptyText
    version: NonEmptyText
    source_root: NonEmptyText
    items: tuple[EvidenceItem, ...]
    supersession_edges: tuple[SupersessionEdge, ...] = ()
    anchor_hash: Sha256

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        if not ANCHOR_VERSION_PATTERN.fullmatch(value):
            raise ValueError("anchor version must be a stable lowercase identifier")
        return value

    @model_validator(mode="after")
    def validate_unique_ids(self) -> ScopeAnchor:
        ids = [item.evidence_id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("scope anchor contains duplicate evidence IDs")
        known = set(ids)
        for edge in self.supersession_edges:
            if edge.superseding_id not in known or edge.superseded_id not in known:
                raise ValueError("supersession edge references unknown evidence")
        return self


def _default_quotas() -> dict[EvidenceCategory, int]:
    return {category: 2 for category in EvidenceCategory}


class RetrievalLimits(StrictModel):
    max_total: Annotated[int, Field(ge=1)] = 12
    category_quotas: dict[EvidenceCategory, Annotated[int, Field(ge=0)]] = Field(
        default_factory=_default_quotas
    )
    expanded_max_total: Annotated[int, Field(ge=1)] = 18
    minimum_category_coverage: Annotated[int, Field(ge=1)] = 4

    @model_validator(mode="after")
    def validate_expansion(self) -> RetrievalLimits:
        if self.expanded_max_total < self.max_total:
            raise ValueError("expanded_max_total must be at least max_total")
        return self


class RetrievedEvidence(StrictModel):
    evidence: EvidenceItem
    score: Annotated[float, Field(ge=0)]
    score_components: dict[str, float]
    rank: Annotated[int, Field(ge=1)]


class RetrievalBundle(StrictModel):
    query_text: NonEmptyText
    evidence_cutoff: str
    anchor_hash: Sha256
    items: tuple[RetrievedEvidence, ...]
    category_coverage: tuple[EvidenceCategory, ...]
    expanded: bool

    @field_validator("evidence_cutoff")
    @classmethod
    def validate_cutoff(cls, value: str) -> str:
        if not (
            REQUEST_ID_PATTERN.fullmatch(value) or DECISION_ID_PATTERN.fullmatch(value)
        ):
            raise ValueError(f"invalid evidence cutoff: {value!r}")
        return value


class HumanDecisionPayload(StrictModel):
    decision_id: str
    effective_date: date
    effect: LedgerEntryEffect
    decision_text: NonEmptyText
    evidence_ids: tuple[str, ...]
    changes_approved_scope: bool
    approves_requested_capability: bool

    @field_validator("decision_id")
    @classmethod
    def validate_decision_id(cls, value: str) -> str:
        if not DECISION_ID_PATTERN.fullmatch(value):
            raise ValueError(f"invalid decision ID: {value!r}")
        return value

    @field_validator("evidence_ids")
    @classmethod
    def validate_evidence_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values:
            raise ValueError("decision payload requires evidence IDs")
        if len(values) != len(set(values)):
            raise ValueError("decision evidence IDs must be unique")
        for value in values:
            if not EVIDENCE_ID_PATTERN.fullmatch(value):
                raise ValueError(f"invalid evidence ID: {value!r}")
        return values

    @model_validator(mode="after")
    def validate_effect_consistency(self) -> HumanDecisionPayload:
        if self.effect == LedgerEntryEffect.APPROVE_CAPABILITY and not (
            self.changes_approved_scope and self.approves_requested_capability
        ):
            raise ValueError(
                "APPROVE_CAPABILITY must change scope and approve the requested capability"
            )
        if self.effect == LedgerEntryEffect.UPHOLD_EXISTING_SCOPE and (
            self.changes_approved_scope or self.approves_requested_capability
        ):
            raise ValueError(
                "UPHOLD_EXISTING_SCOPE cannot change scope or approve the request"
            )
        if (
            self.effect == LedgerEntryEffect.RECORD_REJECTION
            and self.approves_requested_capability
        ):
            raise ValueError("RECORD_REJECTION cannot approve the requested capability")
        return self


class HumanReview(StrictModel):
    review_id: str
    project_id: NonEmptyText
    request_id: str
    assessment_id: str
    action: HumanAction
    reviewer_id: NonEmptyText
    reviewed_at: datetime
    final_classification: Classification | None = None
    reason: str | None = None
    evidence_ids: tuple[str, ...] = ()
    decision_payload: HumanDecisionPayload | None = None

    @field_validator("review_id")
    @classmethod
    def validate_review_id(cls, value: str) -> str:
        if not REVIEW_ID_PATTERN.fullmatch(value):
            raise ValueError(f"invalid review ID: {value!r}")
        return value

    @field_validator("request_id")
    @classmethod
    def validate_request_id(cls, value: str) -> str:
        if not REQUEST_ID_PATTERN.fullmatch(value):
            raise ValueError(f"invalid request ID: {value!r}")
        return value

    @field_validator("assessment_id")
    @classmethod
    def validate_assessment_id(cls, value: str) -> str:
        if not ASSESSMENT_ID_PATTERN.fullmatch(value):
            raise ValueError(f"invalid assessment ID: {value!r}")
        return value

    @field_validator("evidence_ids")
    @classmethod
    def validate_review_evidence(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("review evidence IDs must be unique")
        for value in values:
            if not EVIDENCE_ID_PATTERN.fullmatch(value):
                raise ValueError(f"invalid evidence ID: {value!r}")
        return values

    @model_validator(mode="after")
    def validate_action_requirements(self) -> HumanReview:
        if self.action == HumanAction.OVERRIDE:
            if self.final_classification is None or not self.reason or not self.evidence_ids:
                raise ValueError(
                    "OVERRIDE requires classification, reason, and evidence"
                )
        if self.action in {HumanAction.NEEDS_CLARIFICATION, HumanAction.DEFER}:
            if self.decision_payload is not None:
                raise ValueError(f"{self.action.value} cannot include a decision payload")
        payload = self.decision_payload
        if payload and payload.changes_approved_scope and self.action not in {
            HumanAction.APPROVE,
            HumanAction.OVERRIDE,
        }:
            raise ValueError("only APPROVE or OVERRIDE can change approved scope")
        if payload and self.final_classification in {
            Classification.OUT_OF_SCOPE,
            Classification.CONTRADICTS_APPROVED_DECISION,
        } and payload.approves_requested_capability:
            raise ValueError(
                "upholding an exclusion or contradiction cannot approve the request"
            )
        return self


class LedgerSnapshot(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: NonEmptyText
    anchor_hash: Sha256
    approved_evidence_ids: tuple[str, ...]
    ledger_entry_ids: tuple[str, ...]
    request_ids: tuple[str, ...]
    assessment_ids: tuple[str, ...]
    review_ids: tuple[str, ...]
    snapshot_hash: Sha256


class LedgerUpdateResult(StrictModel):
    review_id: str
    action: HumanAction
    ledger_changed: bool
    ledger_entry_id: str | None = None
    before_snapshot_hash: Sha256
    after_snapshot_hash: Sha256


class AmbiguityKind(str, Enum):
    UNDEFINED_ACTOR = "UNDEFINED_ACTOR"
    VAGUE_QUALITATIVE_TARGET = "VAGUE_QUALITATIVE_TARGET"
    MISSING_BEHAVIOR = "MISSING_BEHAVIOR"
    ACCEPTANCE_DETAIL = "ACCEPTANCE_DETAIL"


class AmbiguityFinding(StrictModel):
    kind: AmbiguityKind
    description: NonEmptyText
    evidence_ids: tuple[str, ...] = ()
    blocking: bool
    heuristic: bool
    clarification_question: str | None = None

    @field_validator("evidence_ids")
    @classmethod
    def validate_evidence_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _validated_unique_ids(values, EVIDENCE_ID_PATTERN, "evidence ID")

    @model_validator(mode="after")
    def validate_blocking_question(self) -> AmbiguityFinding:
        if self.blocking and not self.clarification_question:
            raise ValueError("blocking ambiguity requires a clarification question")
        if not self.evidence_ids and not self.heuristic:
            raise ValueError("an unevidenced finding must be marked heuristic")
        return self


class SufficiencyAssessment(StrictModel):
    sufficient_for_classification: bool
    findings: tuple[AmbiguityFinding, ...] = ()
    clarification_questions: tuple[NonEmptyText, ...] = ()
    rationale: NonEmptyText

    @model_validator(mode="after")
    def validate_consistency(self) -> SufficiencyAssessment:
        blocking = any(finding.blocking for finding in self.findings)
        if self.sufficient_for_classification == blocking:
            raise ValueError("sufficiency must be false exactly when ambiguity is blocking")
        if blocking and not self.clarification_questions:
            raise ValueError("blocking ambiguity requires clarification questions")
        return self


class ConflictFinding(StrictModel):
    evidence_id: str
    polarity: DecisionPolarity
    description: NonEmptyText
    facet_terms: tuple[str, ...] = ()
    specific: bool
    active: bool
    heuristic: bool = False

    @field_validator("evidence_id")
    @classmethod
    def validate_evidence_id(cls, value: str) -> str:
        if not EVIDENCE_ID_PATTERN.fullmatch(value):
            raise ValueError(f"invalid evidence ID: {value!r}")
        return value


class ConflictAssessment(StrictModel):
    findings: tuple[ConflictFinding, ...] = ()
    proven_specific_contradiction: bool
    conflicting_evidence_ids: tuple[str, ...] = ()
    approved_evidence_ids: tuple[str, ...] = ()
    exclusion_evidence_ids: tuple[str, ...] = ()
    neutral_boundary_evidence_ids: tuple[str, ...] = ()
    rationale: NonEmptyText

    @field_validator(
        "conflicting_evidence_ids",
        "approved_evidence_ids",
        "exclusion_evidence_ids",
        "neutral_boundary_evidence_ids",
    )
    @classmethod
    def validate_evidence_ids(
        cls, values: tuple[str, ...], info: object
    ) -> tuple[str, ...]:
        return _validated_unique_ids(values, EVIDENCE_ID_PATTERN, info.field_name)


class CapabilitySignature(StrictModel):
    domain_terms: tuple[str, ...] = ()
    actors: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    objects: tuple[str, ...] = ()
    facets: tuple[str, ...] = ()
    dependency_terms: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    source_request_ids: tuple[str, ...] = ()
    source_decision_ids: tuple[str, ...] = ()
    heuristic: bool = True

    @field_validator(
        "domain_terms", "actors", "actions", "objects", "facets", "dependency_terms"
    )
    @classmethod
    def validate_terms(cls, values: tuple[str, ...], info: object) -> tuple[str, ...]:
        normalized = tuple(sorted(set(value.strip().lower() for value in values)))
        if any(not value for value in normalized):
            raise ValueError(f"{info.field_name} cannot contain empty terms")
        return normalized

    @field_validator("evidence_ids")
    @classmethod
    def validate_evidence_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _validated_unique_ids(values, EVIDENCE_ID_PATTERN, "evidence ID")

    @field_validator("source_request_ids")
    @classmethod
    def validate_request_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _validated_unique_ids(values, REQUEST_ID_PATTERN, "request ID")

    @field_validator("source_decision_ids")
    @classmethod
    def validate_decision_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _validated_unique_ids(values, DECISION_ID_PATTERN, "decision ID")


class DriftSeverity(str, Enum):
    NONE = "NONE"
    RELATED = "RELATED"
    EMERGING = "EMERGING"
    SUBSYSTEM = "SUBSYSTEM"


class DriftThresholds(StrictModel):
    subsystem_prior_approved_changes: Annotated[int, Field(ge=1)] = 2
    subsystem_total_increments: Annotated[int, Field(ge=2)] = 3
    subsystem_minimum_facets: Annotated[int, Field(ge=1)] = 3
    require_dependency_or_lifecycle_connection: bool = True


class DriftAssessment(StrictModel):
    severity: DriftSeverity
    cumulative_drift_detected: bool
    related_request_ids: tuple[str, ...] = ()
    related_decision_ids: tuple[str, ...] = ()
    combined_facets: tuple[str, ...] = ()
    approved_change_count: Annotated[int, Field(ge=0)]
    pattern_key: str | None = None
    rationale: NonEmptyText
    heuristic: bool = True

    @field_validator("related_request_ids")
    @classmethod
    def validate_request_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _validated_unique_ids(values, REQUEST_ID_PATTERN, "request ID")

    @field_validator("related_decision_ids")
    @classmethod
    def validate_decision_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _validated_unique_ids(values, DECISION_ID_PATTERN, "decision ID")

    @model_validator(mode="after")
    def validate_drift_boolean(self) -> DriftAssessment:
        if self.cumulative_drift_detected != (self.severity == DriftSeverity.SUBSYSTEM):
            raise ValueError("drift boolean is true only for SUBSYSTEM severity")
        return self


class AdvancedModelOutput(StrictModel):
    """Bounded model recommendation; deterministic tools own final precedence and drift."""

    request_id: str
    recommended_classification: Classification
    supporting_evidence_ids: tuple[str, ...] = ()
    conflicting_evidence_ids: tuple[str, ...] = ()
    requires_clarification: bool
    clarification_questions: tuple[NonEmptyText, ...] = ()
    dependencies: tuple[NonEmptyText, ...] = ()
    rationale: Annotated[str, Field(min_length=1, max_length=2000)]
    capability_signature: CapabilitySignature

    @field_validator("request_id")
    @classmethod
    def validate_request_id(cls, value: str) -> str:
        if not REQUEST_ID_PATTERN.fullmatch(value):
            raise ValueError(f"invalid request ID: {value!r}")
        return value

    @field_validator("supporting_evidence_ids", "conflicting_evidence_ids")
    @classmethod
    def validate_evidence_ids(
        cls, values: tuple[str, ...], info: object
    ) -> tuple[str, ...]:
        return _validated_unique_ids(values, EVIDENCE_ID_PATTERN, info.field_name)

    @model_validator(mode="after")
    def validate_clarification(self) -> AdvancedModelOutput:
        if self.requires_clarification != bool(self.clarification_questions):
            raise ValueError("clarification boolean must match presence of questions")
        return self


class AdvancedAssessment(StrictModel):
    request_id: str
    model_recommendation: Classification
    classification: Classification
    supporting_evidence_ids: tuple[str, ...] = ()
    conflicting_evidence_ids: tuple[str, ...] = ()
    requires_clarification: bool
    clarification_questions: tuple[NonEmptyText, ...] = ()
    dependencies: tuple[NonEmptyText, ...] = ()
    rationale: Annotated[str, Field(min_length=1, max_length=2000)]
    capability_signature: CapabilitySignature

    @field_validator("request_id")
    @classmethod
    def validate_request_id(cls, value: str) -> str:
        if not REQUEST_ID_PATTERN.fullmatch(value):
            raise ValueError(f"invalid request ID: {value!r}")
        return value

    @field_validator("supporting_evidence_ids", "conflicting_evidence_ids")
    @classmethod
    def validate_evidence_ids(
        cls, values: tuple[str, ...], info: object
    ) -> tuple[str, ...]:
        return _validated_unique_ids(values, EVIDENCE_ID_PATTERN, info.field_name)

    @model_validator(mode="after")
    def validate_clarification(self) -> AdvancedAssessment:
        if self.requires_clarification and not self.clarification_questions:
            raise ValueError("clarification requires at least one question")
        if not self.requires_clarification and self.clarification_questions:
            raise ValueError("questions require requires_clarification=true")
        return self


class VerificationIssue(StrictModel):
    code: NonEmptyText
    message: NonEmptyText
    evidence_ids: tuple[str, ...] = ()
    repairable: bool

    @field_validator("evidence_ids")
    @classmethod
    def validate_evidence_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _validated_unique_ids(values, EVIDENCE_ID_PATTERN, "evidence ID")


class VerificationResult(StrictModel):
    passed: bool
    issues: tuple[VerificationIssue, ...] = ()
    repair_attempted: bool = False
    repair_succeeded: bool = False

    @model_validator(mode="after")
    def validate_result(self) -> VerificationResult:
        if self.passed == bool(self.issues):
            raise ValueError("passed must be true exactly when there are no issues")
        if self.repair_succeeded and not self.repair_attempted:
            raise ValueError("successful repair requires a repair attempt")
        return self


class HumanReviewRecommendation(StrictModel):
    request_id: str
    action: HumanAction
    classification: Classification
    summary: NonEmptyText
    evidence_ids: tuple[str, ...] = ()
    clarification_questions: tuple[NonEmptyText, ...] = ()
    drift_severity: DriftSeverity

    @field_validator("request_id")
    @classmethod
    def validate_request_id(cls, value: str) -> str:
        if not REQUEST_ID_PATTERN.fullmatch(value):
            raise ValueError(f"invalid request ID: {value!r}")
        return value

    @field_validator("evidence_ids")
    @classmethod
    def validate_evidence_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _validated_unique_ids(values, EVIDENCE_ID_PATTERN, "evidence ID")


class AgentNode(str, Enum):
    LOAD_SCOPE_ANCHOR = "LOAD_SCOPE_ANCHOR"
    RETRIEVE_EVIDENCE = "RETRIEVE_EVIDENCE"
    ASSESS_SUFFICIENCY = "ASSESS_SUFFICIENCY"
    CHECK_CONTRADICTIONS = "CHECK_CONTRADICTIONS"
    CLASSIFY_REQUEST = "CLASSIFY_REQUEST"
    CALCULATE_CUMULATIVE_DRIFT = "CALCULATE_CUMULATIVE_DRIFT"
    VERIFY_ASSESSMENT = "VERIFY_ASSESSMENT"
    PREPARE_RECOMMENDATION = "PREPARE_RECOMMENDATION"
    AWAIT_HUMAN_REVIEW = "AWAIT_HUMAN_REVIEW"
    APPLY_HUMAN_DECISION = "APPLY_HUMAN_DECISION"
    BUILD_CHANGE_IMPACT_PACKAGE = "BUILD_CHANGE_IMPACT_PACKAGE"
    COMPLETE = "COMPLETE"


class AgentStatus(str, Enum):
    NEW = "NEW"
    RUNNING = "RUNNING"
    AWAITING_HUMAN_REVIEW = "AWAITING_HUMAN_REVIEW"
    REVIEWED = "REVIEWED"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


class TrajectoryEvent(StrictModel):
    sequence: Annotated[int, Field(ge=1)]
    node: AgentNode
    tool: str | None = None
    input_ids: tuple[str, ...] = ()
    input_hash: Sha256
    result_summary: NonEmptyText
    verification: str | None = None
    duration_ms: Annotated[int, Field(ge=0)]
    human_state: str | None = None
    error: str | None = None


class DraftAcceptanceCriterion(StrictModel):
    criterion_id: NonEmptyText
    text: NonEmptyText
    evidence_ids: tuple[str, ...]
    status: str = "DRAFT"

    @field_validator("evidence_ids")
    @classmethod
    def validate_evidence_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values:
            raise ValueError("draft acceptance criterion requires evidence")
        return _validated_unique_ids(values, EVIDENCE_ID_PATTERN, "evidence ID")

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        if value != "DRAFT":
            raise ValueError("acceptance criteria must remain DRAFT")
        return value


class ChangeImpactPackage(StrictModel):
    request_id: str
    review_id: str
    is_review_memo: bool
    approval_state: HumanAction
    agent_classification: Classification
    final_classification: Classification
    summary: NonEmptyText
    supporting_evidence_ids: tuple[str, ...] = ()
    conflicting_evidence_ids: tuple[str, ...] = ()
    added_requirements: tuple[NonEmptyText, ...] = ()
    changed_requirements: tuple[NonEmptyText, ...] = ()
    superseded_requirements: tuple[NonEmptyText, ...] = ()
    affected_actors: tuple[NonEmptyText, ...] = ()
    affected_components: tuple[NonEmptyText, ...] = ()
    affected_data_state: tuple[NonEmptyText, ...] = ()
    affected_integrations: tuple[NonEmptyText, ...] = ()
    dependencies: tuple[NonEmptyText, ...] = ()
    workflow_steps: tuple[NonEmptyText, ...] = ()
    open_questions: tuple[NonEmptyText, ...] = ()
    acceptance_criteria: tuple[DraftAcceptanceCriterion, ...] = ()
    drift_severity: DriftSeverity
    drift_pattern: str | None = None
    non_goals: tuple[NonEmptyText, ...] = ()
    unknowns: tuple[NonEmptyText, ...] = ()
    verification_hash: Sha256
    source_hashes: tuple[Sha256, ...]


class AdvancedRunState(StrictModel):
    run_id: NonEmptyText
    project_pack_path: NonEmptyText
    project_id: NonEmptyText
    request: IncomingRequest
    status: AgentStatus = AgentStatus.NEW
    current_node: AgentNode | None = None
    anchor_hash: Sha256 | None = None
    pause_snapshot_hash: Sha256 | None = None
    retrieval: RetrievalBundle | None = None
    sufficiency: SufficiencyAssessment | None = None
    conflicts: ConflictAssessment | None = None
    assessment: AdvancedAssessment | None = None
    drift: DriftAssessment | None = None
    verification: VerificationResult | None = None
    recommendation: HumanReviewRecommendation | None = None
    human_review: HumanReview | None = None
    ledger_update: LedgerUpdateResult | None = None
    change_package: ChangeImpactPackage | None = None
    trajectory: tuple[TrajectoryEvent, ...] = ()
    prompt_hash: Sha256 | None = None
    assembled_prompt_hash: Sha256 | None = None
    raw_response_hash: Sha256 | None = None
    token_usage: dict[str, Any] | None = None
    generation_attempts: tuple[dict[str, Any], ...] = ()


def _validated_unique_ids(
    values: tuple[str, ...], pattern: re.Pattern[str], kind: str
) -> tuple[str, ...]:
    if len(values) != len(set(values)):
        raise ValueError(f"{kind}s must be unique")
    for value in values:
        if not pattern.fullmatch(value):
            raise ValueError(f"invalid {kind}: {value!r}")
    return values


def stable_json_value(value: Any) -> Any:
    """Return a JSON-compatible value for deterministic hashing helpers."""

    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, tuple):
        return [stable_json_value(item) for item in value]
    if isinstance(value, list):
        return [stable_json_value(item) for item in value]
    if isinstance(value, dict):
        return {
            str(stable_json_value(key)): stable_json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    return value
