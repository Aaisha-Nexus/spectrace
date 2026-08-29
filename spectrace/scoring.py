"""Deterministic, API-free scoring against frozen SpecTrace ground truth."""

from __future__ import annotations

from collections.abc import Iterable

from spectrace.models import (
    AggregateEvaluationResult,
    Classification,
    GroundTruthRecord,
    LabelMetrics,
    ModelPrediction,
    PerCaseResult,
)


class ScoringError(ValueError):
    """Raised when predictions cannot be paired unambiguously with ground truth."""


def _safe_divide(numerator: int, denominator: int, *, empty: float = 0.0) -> float:
    return numerator / denominator if denominator else empty


def _exact_id_set(actual: list[str], expected: list[str]) -> bool:
    return set(actual) == set(expected)


def _index_unique(items: Iterable[object], kind: str) -> dict[str, object]:
    indexed: dict[str, object] = {}
    duplicates: set[str] = set()
    for item in items:
        request_id = getattr(item, "request_id")
        if request_id in indexed:
            duplicates.add(request_id)
        indexed[request_id] = item
    if duplicates:
        raise ScoringError(f"duplicate {kind} IDs: {sorted(duplicates)}")
    return indexed


def score_predictions(
    predictions: list[ModelPrediction],
    ground_truth: list[GroundTruthRecord],
    evidence_ids: set[str] | frozenset[str],
    available_evidence_ids_by_request: dict[str, frozenset[str]],
) -> AggregateEvaluationResult:
    """Compare one prediction per request with ground truth, independent of order.

    Evidence-Grounded Scope Accuracy is the fraction of strict passing cases. A
    case passes only when its classification and clarification decision are
    correct, at least one expected citation is placed in the classification-
    appropriate evidence field when expected, every cited ID exists and was
    available at the request cutoff, and cumulative drift detection exactly
    matches ground truth for every request. Contradiction cases use expected
    conflicting evidence; all other classifications use expected supporting
    evidence.
    """

    prediction_by_id = _index_unique(predictions, "prediction")
    truth_by_id = _index_unique(ground_truth, "ground-truth")
    prediction_ids = set(prediction_by_id)
    truth_ids = set(truth_by_id)
    if prediction_ids != truth_ids:
        missing = sorted(truth_ids - prediction_ids)
        unexpected = sorted(prediction_ids - truth_ids)
        raise ScoringError(
            f"prediction ID set differs from ground truth; missing={missing}, unexpected={unexpected}"
        )
    missing_availability = sorted(truth_ids - set(available_evidence_ids_by_request))
    if missing_availability:
        raise ScoringError(
            f"missing evidence-availability entries for: {missing_availability}"
        )

    cases: list[PerCaseResult] = []
    valid_citation_count = 0
    total_citation_count = 0
    appropriate_evidence_hit_count = 0
    appropriate_evidence_expected_count = 0
    clarification_tp = 0
    clarification_fp = 0
    clarification_fn = 0
    contradiction_tp = 0
    contradiction_expected = 0
    cumulative_tp = 0
    cumulative_expected = 0
    related_request_correct = 0
    related_decision_correct = 0

    for request_id in sorted(truth_by_id):
        truth_object = truth_by_id[request_id]
        truth = truth_object
        prediction = prediction_by_id[request_id]
        assert isinstance(truth, GroundTruthRecord)
        assert isinstance(prediction, ModelPrediction)

        classification_correct = prediction.classification == truth.expected_classification
        cited_ids = prediction.supporting_evidence_ids + prediction.conflicting_evidence_ids
        available_evidence_ids = available_evidence_ids_by_request[request_id]
        nonexistent_ids = sorted(set(cited_ids) - set(evidence_ids))
        unavailable_ids = sorted(
            (set(cited_ids) & set(evidence_ids)) - set(available_evidence_ids)
        )
        invalid_ids = sorted(set(nonexistent_ids) | set(unavailable_ids))
        citation_references_valid = not invalid_ids
        valid_citation_count += sum(citation in available_evidence_ids for citation in cited_ids)
        total_citation_count += len(cited_ids)

        expected_contradiction = (
            truth.expected_classification
            == Classification.CONTRADICTS_APPROVED_DECISION
        )
        if expected_contradiction:
            expected_evidence_ids = truth.conflicting_evidence_ids
            predicted_evidence_ids = prediction.conflicting_evidence_ids
            evidence_field = "conflicting_evidence_ids"
        else:
            expected_evidence_ids = truth.valid_supporting_evidence_ids
            predicted_evidence_ids = prediction.supporting_evidence_ids
            evidence_field = "supporting_evidence_ids"

        evidence_expected = bool(expected_evidence_ids)
        classification_appropriate_evidence_hit = (
            bool(set(predicted_evidence_ids) & set(expected_evidence_ids))
            if evidence_expected
            else True
        )
        if evidence_expected:
            appropriate_evidence_expected_count += 1
            appropriate_evidence_hit_count += classification_appropriate_evidence_hit

        clarification_correct = (
            prediction.requires_clarification == truth.required_clarification
        )
        clarification_tp += prediction.requires_clarification and truth.required_clarification
        clarification_fp += prediction.requires_clarification and not truth.required_clarification
        clarification_fn += not prediction.requires_clarification and truth.required_clarification

        detected_contradiction = (
            prediction.classification == Classification.CONTRADICTS_APPROVED_DECISION
        )
        contradiction_expected += expected_contradiction
        contradiction_tp += expected_contradiction and detected_contradiction

        cumulative_correct = (
            prediction.cumulative_drift_detected == truth.cumulative_drift_expected
        )
        cumulative_expected += truth.cumulative_drift_expected
        cumulative_tp += truth.cumulative_drift_expected and prediction.cumulative_drift_detected
        related_requests_correct: bool | None = None
        related_decisions_correct: bool | None = None
        if truth.cumulative_drift_expected:
            related_requests_correct = _exact_id_set(
                prediction.cumulative_related_request_ids,
                truth.cumulative_related_request_ids,
            )
            related_decisions_correct = _exact_id_set(
                prediction.cumulative_related_decision_ids,
                truth.cumulative_related_decision_ids,
            )
            related_request_correct += related_requests_correct
            related_decision_correct += related_decisions_correct

        failure_reasons: list[str] = []
        if not classification_correct:
            failure_reasons.append(
                f"classification {prediction.classification.value} != "
                f"{truth.expected_classification.value}"
            )
        if not clarification_correct:
            failure_reasons.append(
                f"requires_clarification {prediction.requires_clarification} != "
                f"{truth.required_clarification}"
            )
        if not classification_appropriate_evidence_hit:
            failure_reasons.append(
                f"no expected evidence ID was cited in {evidence_field}"
            )
        if invalid_ids:
            if nonexistent_ids:
                failure_reasons.append(f"nonexistent cited evidence IDs: {nonexistent_ids}")
            if unavailable_ids:
                failure_reasons.append(
                    f"cited evidence IDs unavailable at request cutoff: {unavailable_ids}"
                )
        if not cumulative_correct:
            failure_reasons.append(
                "cumulative drift detection does not match the expected value"
            )
        if related_requests_correct is False:
            failure_reasons.append("cumulative related request IDs do not exactly match")
        if related_decisions_correct is False:
            failure_reasons.append("cumulative related decision IDs do not exactly match")

        strict_pass = (
            classification_correct
            and clarification_correct
            and classification_appropriate_evidence_hit
            and citation_references_valid
            and cumulative_correct
        )
        cases.append(
            PerCaseResult(
                request_id=request_id,
                classification_correct=classification_correct,
                citation_references_valid=citation_references_valid,
                invalid_citation_ids=invalid_ids,
                nonexistent_citation_ids=nonexistent_ids,
                unavailable_citation_ids=unavailable_ids,
                classification_appropriate_evidence_hit=(
                    classification_appropriate_evidence_hit
                ),
                clarification_decision_correct=clarification_correct,
                contradiction_detected=detected_contradiction,
                cumulative_drift_correct=cumulative_correct,
                cumulative_related_request_ids_correct=related_requests_correct,
                cumulative_related_decision_ids_correct=related_decisions_correct,
                strict_pass=strict_pass,
                failure_reasons=failure_reasons,
            )
        )

    total = len(cases)
    per_label: dict[Classification, LabelMetrics] = {}
    for label in Classification:
        tp = sum(
            prediction_by_id[request_id].classification == label
            and truth_by_id[request_id].expected_classification == label
            for request_id in truth_ids
        )
        fp = sum(
            prediction_by_id[request_id].classification == label
            and truth_by_id[request_id].expected_classification != label
            for request_id in truth_ids
        )
        fn = sum(
            prediction_by_id[request_id].classification != label
            and truth_by_id[request_id].expected_classification == label
            for request_id in truth_ids
        )
        precision = _safe_divide(tp, tp + fp)
        recall = _safe_divide(tp, tp + fn)
        f1 = _safe_divide(2 * precision * recall, precision + recall)
        per_label[label] = LabelMetrics(
            precision=precision,
            recall=recall,
            f1=f1,
            support=tp + fn,
        )

    passed = sum(case.strict_pass for case in cases)
    correct_classifications = sum(case.classification_correct for case in cases)
    correct_clarifications = sum(case.clarification_decision_correct for case in cases)
    correct_cumulative = sum(case.cumulative_drift_correct for case in cases)
    return AggregateEvaluationResult(
        total_cases=total,
        passed_cases=passed,
        evidence_grounded_scope_accuracy=_safe_divide(passed, total, empty=1.0),
        classification_accuracy=_safe_divide(correct_classifications, total, empty=1.0),
        macro_precision=sum(item.precision for item in per_label.values()) / len(Classification),
        macro_recall=sum(item.recall for item in per_label.values()) / len(Classification),
        macro_f1=sum(item.f1 for item in per_label.values()) / len(Classification),
        per_label=per_label,
        citation_reference_validity_rate=_safe_divide(
            valid_citation_count, total_citation_count, empty=1.0
        ),
        classification_appropriate_evidence_hit_rate=_safe_divide(
            appropriate_evidence_hit_count,
            appropriate_evidence_expected_count,
            empty=1.0,
        ),
        clarification_decision_accuracy=_safe_divide(
            correct_clarifications, total, empty=1.0
        ),
        clarification_precision=_safe_divide(
            clarification_tp, clarification_tp + clarification_fp
        ),
        clarification_recall=_safe_divide(
            clarification_tp, clarification_tp + clarification_fn
        ),
        contradiction_detection_recall=_safe_divide(
            contradiction_tp, contradiction_expected
        ),
        cumulative_drift_detection_accuracy=_safe_divide(
            correct_cumulative, total, empty=1.0
        ),
        cumulative_drift_detection_rate=_safe_divide(cumulative_tp, cumulative_expected),
        cumulative_related_request_ids_accuracy=_safe_divide(
            related_request_correct, cumulative_expected, empty=1.0
        ),
        cumulative_related_decision_ids_accuracy=_safe_divide(
            related_decision_correct, cumulative_expected, empty=1.0
        ),
        cases=cases,
    )
