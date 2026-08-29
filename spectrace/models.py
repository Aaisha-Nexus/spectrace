"""Strict data contracts shared by dataset validation and deterministic scoring."""

from __future__ import annotations

import re
from datetime import date
from enum import Enum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


REQUEST_ID_PATTERN = re.compile(r"^CR-\d{3}$")
DECISION_ID_PATTERN = re.compile(r"^DEC-\d{3}$")
EVIDENCE_ID_PATTERN = re.compile(r"^(?:SOW-[A-Z]{3}-\d{3}|DEC-\d{3})$")
CUMULATIVE_PATTERN_ID_PATTERN = re.compile(r"^CUM-[A-Z0-9]+(?:-[A-Z0-9]+)*-\d{3}$")
ISO_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

NonEmptyText = Annotated[str, Field(min_length=1)]


class Classification(str, Enum):
    """Frozen SpecTrace classification taxonomy."""

    IN_SCOPE = "IN_SCOPE"
    AMBIGUOUS = "AMBIGUOUS"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    CONTRADICTS_APPROVED_DECISION = "CONTRADICTS_APPROVED_DECISION"
    POTENTIAL_SCOPE_CHANGE = "POTENTIAL_SCOPE_CHANGE"


class StrictModel(BaseModel):
    """Base contract that rejects fields not declared by the schema."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


def _validate_id(value: str, pattern: re.Pattern[str], kind: str) -> str:
    if not pattern.fullmatch(value):
        raise ValueError(f"invalid {kind}: {value!r}")
    return value


def _unique(values: list[str], field_name: str) -> list[str]:
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must not contain duplicates")
    return values


class IncomingRequest(StrictModel):
    request_id: str
    date: date
    source: NonEmptyText
    message: NonEmptyText
    evidence_available_through: str
    chronological_order: Annotated[int, Field(ge=1)]

    @field_validator("request_id")
    @classmethod
    def validate_request_id(cls, value: str) -> str:
        return _validate_id(value, REQUEST_ID_PATTERN, "request ID")

    @field_validator("date", mode="before")
    @classmethod
    def validate_iso_date(cls, value: object) -> object:
        if isinstance(value, str) and not ISO_DATE_PATTERN.fullmatch(value):
            raise ValueError("date must use ISO YYYY-MM-DD format")
        return value

    @field_validator("evidence_available_through")
    @classmethod
    def validate_cutoff_id(cls, value: str) -> str:
        if not (REQUEST_ID_PATTERN.fullmatch(value) or DECISION_ID_PATTERN.fullmatch(value)):
            raise ValueError(f"invalid evidence cutoff ID: {value!r}")
        return value


class GroundTruthRecord(StrictModel):
    request_id: str
    expected_classification: Classification
    evidence_available_through: str
    valid_supporting_evidence_ids: list[str]
    relevant_scope_ids: list[str]
    conflicting_evidence_ids: list[str]
    required_clarification: bool
    expected_clarification_points: list[NonEmptyText]
    expected_dependencies: list[NonEmptyText]
    expected_reasoning_summary: NonEmptyText
    expected_human_action: NonEmptyText
    resulting_decision_id: str | None
    planted_conflict: bool
    supersession_expected: bool
    cumulative_drift_expected: bool
    cumulative_pattern_id: str | None
    cumulative_related_request_ids: list[str]
    cumulative_related_decision_ids: list[str]
    unsupported_claims_forbidden: list[NonEmptyText]

    @field_validator("request_id")
    @classmethod
    def validate_request_id(cls, value: str) -> str:
        return _validate_id(value, REQUEST_ID_PATTERN, "request ID")

    @field_validator("evidence_available_through")
    @classmethod
    def validate_cutoff_id(cls, value: str) -> str:
        if not (REQUEST_ID_PATTERN.fullmatch(value) or DECISION_ID_PATTERN.fullmatch(value)):
            raise ValueError(f"invalid evidence cutoff ID: {value!r}")
        return value

    @field_validator(
        "valid_supporting_evidence_ids", "relevant_scope_ids", "conflicting_evidence_ids"
    )
    @classmethod
    def validate_evidence_ids(cls, values: list[str], info: object) -> list[str]:
        for value in values:
            _validate_id(value, EVIDENCE_ID_PATTERN, "evidence ID")
        return _unique(values, info.field_name)

    @field_validator("resulting_decision_id")
    @classmethod
    def validate_resulting_decision_id(cls, value: str | None) -> str | None:
        if value is not None:
            _validate_id(value, DECISION_ID_PATTERN, "decision ID")
        return value

    @field_validator("cumulative_pattern_id")
    @classmethod
    def validate_pattern_id(cls, value: str | None) -> str | None:
        if value is not None:
            _validate_id(value, CUMULATIVE_PATTERN_ID_PATTERN, "cumulative pattern ID")
        return value

    @field_validator("cumulative_related_request_ids")
    @classmethod
    def validate_related_requests(cls, values: list[str]) -> list[str]:
        for value in values:
            _validate_id(value, REQUEST_ID_PATTERN, "request ID")
        return _unique(values, "cumulative_related_request_ids")

    @field_validator("cumulative_related_decision_ids")
    @classmethod
    def validate_related_decisions(cls, values: list[str]) -> list[str]:
        for value in values:
            _validate_id(value, DECISION_ID_PATTERN, "decision ID")
        return _unique(values, "cumulative_related_decision_ids")

    @model_validator(mode="after")
    def validate_cumulative_consistency(self) -> GroundTruthRecord:
        fields_present = bool(
            self.cumulative_pattern_id
            or self.cumulative_related_request_ids
            or self.cumulative_related_decision_ids
        )
        if self.cumulative_drift_expected and not (
            self.cumulative_pattern_id and self.cumulative_related_request_ids
        ):
            raise ValueError(
                "expected cumulative drift requires a pattern ID and related request IDs"
            )
        if not self.cumulative_drift_expected and fields_present:
            raise ValueError("non-cumulative records must leave cumulative fields empty")
        return self


class ModelPrediction(StrictModel):
    request_id: str
    classification: Classification
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    conflicting_evidence_ids: list[str] = Field(default_factory=list)
    requires_clarification: bool
    clarification_questions: list[NonEmptyText] = Field(default_factory=list)
    dependencies: list[NonEmptyText] = Field(default_factory=list)
    reasoning_summary: Annotated[str, Field(min_length=1, max_length=2000)]
    cumulative_drift_detected: bool
    cumulative_related_request_ids: list[str] = Field(default_factory=list)
    cumulative_related_decision_ids: list[str] = Field(default_factory=list)

    @field_validator("request_id")
    @classmethod
    def validate_request_id(cls, value: str) -> str:
        return _validate_id(value, REQUEST_ID_PATTERN, "request ID")

    @field_validator("supporting_evidence_ids", "conflicting_evidence_ids")
    @classmethod
    def validate_evidence_ids(cls, values: list[str], info: object) -> list[str]:
        for value in values:
            _validate_id(value, EVIDENCE_ID_PATTERN, "evidence ID")
        return _unique(values, info.field_name)

    @field_validator("cumulative_related_request_ids")
    @classmethod
    def validate_related_requests(cls, values: list[str]) -> list[str]:
        for value in values:
            _validate_id(value, REQUEST_ID_PATTERN, "request ID")
        return _unique(values, "cumulative_related_request_ids")

    @field_validator("cumulative_related_decision_ids")
    @classmethod
    def validate_related_decisions(cls, values: list[str]) -> list[str]:
        for value in values:
            _validate_id(value, DECISION_ID_PATTERN, "decision ID")
        return _unique(values, "cumulative_related_decision_ids")

    @model_validator(mode="after")
    def validate_clarification_consistency(self) -> ModelPrediction:
        if not self.requires_clarification and self.clarification_questions:
            raise ValueError(
                "clarification_questions must be empty when clarification is not required"
            )
        return self


class PerCaseResult(StrictModel):
    request_id: str
    classification_correct: bool
    citation_references_valid: bool
    invalid_citation_ids: list[str]
    nonexistent_citation_ids: list[str]
    unavailable_citation_ids: list[str]
    expected_evidence_hit: bool
    clarification_decision_correct: bool
    contradiction_detected: bool
    cumulative_drift_correct: bool
    cumulative_related_request_ids_correct: bool | None
    cumulative_related_decision_ids_correct: bool | None
    strict_pass: bool
    failure_reasons: list[str]


class LabelMetrics(StrictModel):
    precision: float
    recall: float
    f1: float
    support: int


class AggregateEvaluationResult(StrictModel):
    total_cases: int
    passed_cases: int
    evidence_grounded_scope_accuracy: float
    classification_accuracy: float
    macro_precision: float
    macro_recall: float
    macro_f1: float
    per_label: dict[Classification, LabelMetrics]
    citation_reference_validity_rate: float
    expected_evidence_hit_rate: float
    clarification_decision_accuracy: float
    clarification_precision: float
    clarification_recall: float
    contradiction_detection_recall: float
    cumulative_drift_detection_accuracy: float
    cumulative_drift_detection_rate: float
    cumulative_related_request_ids_accuracy: float
    cumulative_related_decision_ids_accuracy: float
    cases: list[PerCaseResult]
