from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from spectrace.dataset import validate_project_pack
from spectrace.models import Classification, GroundTruthRecord, ModelPrediction
from spectrace.scoring import ScoringError, score_predictions


DEMO_PACK = Path(__file__).parents[1] / "data" / "synthetic" / "demo_project"


@pytest.fixture
def benchmark() -> tuple[
    list[GroundTruthRecord], frozenset[str], dict[str, frozenset[str]]
]:
    pack = validate_project_pack(DEMO_PACK)
    return (
        pack.ground_truth,
        pack.evidence_ids,
        pack.available_evidence_ids_by_request,
    )


def _perfect_predictions(truth: list[GroundTruthRecord]) -> list[ModelPrediction]:
    return [
        ModelPrediction(
            request_id=record.request_id,
            classification=record.expected_classification,
            supporting_evidence_ids=[record.valid_supporting_evidence_ids[0]],
            conflicting_evidence_ids=record.conflicting_evidence_ids,
            requires_clarification=record.required_clarification,
            clarification_questions=(
                [record.expected_clarification_points[0]]
                if record.required_clarification
                else []
            ),
            dependencies=record.expected_dependencies,
            reasoning_summary=record.expected_reasoning_summary,
            cumulative_drift_detected=record.cumulative_drift_expected,
            cumulative_related_request_ids=record.cumulative_related_request_ids,
            cumulative_related_decision_ids=record.cumulative_related_decision_ids,
        )
        for record in truth
    ]


def _score(
    predictions: list[ModelPrediction],
    benchmark: tuple[
        list[GroundTruthRecord], frozenset[str], dict[str, frozenset[str]]
    ],
):
    truth, evidence_ids, availability = benchmark
    return score_predictions(predictions, truth, evidence_ids, availability)


def test_perfect_predictions_have_perfect_deterministic_scores(benchmark) -> None:
    predictions = _perfect_predictions(benchmark[0])
    result = _score(predictions, benchmark)
    assert result.passed_cases == 10
    assert result.evidence_grounded_scope_accuracy == 1.0
    assert result.classification_accuracy == 1.0
    assert result.macro_precision == 1.0
    assert result.macro_recall == 1.0
    assert result.macro_f1 == 1.0
    assert result.citation_reference_validity_rate == 1.0
    assert result.expected_evidence_hit_rate == 1.0
    assert result.clarification_decision_accuracy == 1.0
    assert result.clarification_precision == 1.0
    assert result.clarification_recall == 1.0
    assert result.contradiction_detection_recall == 1.0
    assert result.cumulative_drift_detection_accuracy == 1.0
    assert result.cumulative_drift_detection_rate == 1.0


def test_wrong_classification_reduces_accuracy_and_macro_metrics(benchmark) -> None:
    predictions = _perfect_predictions(benchmark[0])
    predictions[0].classification = Classification.OUT_OF_SCOPE
    result = _score(predictions, benchmark)
    assert result.classification_accuracy == 0.9
    assert result.macro_precision < 1.0
    assert result.macro_recall < 1.0
    assert result.macro_f1 < 1.0


def test_invalid_citation_fails_strict_case(benchmark) -> None:
    predictions = _perfect_predictions(benchmark[0])
    predictions[0].supporting_evidence_ids.append("SOW-SCP-999")
    result = _score(predictions, benchmark)
    case = result.cases[0]
    assert not case.citation_references_valid
    assert case.nonexistent_citation_ids == ["SOW-SCP-999"]
    assert case.unavailable_citation_ids == []
    assert not case.strict_pass
    assert result.citation_reference_validity_rate < 1.0


@pytest.mark.parametrize(
    "citation_field", ["supporting_evidence_ids", "conflicting_evidence_ids"]
)
def test_existing_future_decision_is_invalid_for_earlier_request(
    benchmark, citation_field: str
) -> None:
    predictions = _perfect_predictions(benchmark[0])
    getattr(predictions[0], citation_field).append("DEC-005")
    result = _score(predictions, benchmark)
    case = result.cases[0]
    assert not case.citation_references_valid
    assert case.nonexistent_citation_ids == []
    assert case.unavailable_citation_ids == ["DEC-005"]
    assert not case.strict_pass


def test_decision_is_valid_after_it_becomes_available(benchmark) -> None:
    predictions = _perfect_predictions(benchmark[0])
    cr007 = predictions[6]
    cr007.supporting_evidence_ids = ["DEC-005"]
    result = _score(predictions, benchmark)
    case = result.cases[6]
    assert case.citation_references_valid
    assert case.unavailable_citation_ids == []
    assert case.strict_pass


def test_missing_expected_citation_fails_strict_case(benchmark) -> None:
    predictions = _perfect_predictions(benchmark[0])
    predictions[0].supporting_evidence_ids = []
    result = _score(predictions, benchmark)
    case = result.cases[0]
    assert not case.expected_evidence_hit
    assert not case.strict_pass
    assert result.expected_evidence_hit_rate == 0.9


def test_incorrect_clarification_behavior_is_detected(benchmark) -> None:
    predictions = _perfect_predictions(benchmark[0])
    predictions[2].clarification_questions = []
    predictions[2].requires_clarification = False
    result = _score(predictions, benchmark)
    assert not result.cases[2].clarification_decision_correct
    assert result.clarification_decision_accuracy == 0.9
    assert result.clarification_recall == 0.5


def test_missed_contradiction_affects_recall(benchmark) -> None:
    predictions = _perfect_predictions(benchmark[0])
    predictions[7].classification = Classification.OUT_OF_SCOPE
    result = _score(predictions, benchmark)
    assert result.contradiction_detection_recall == 0.5


def test_missed_cr010_drift_affects_cumulative_metrics(benchmark) -> None:
    predictions = _perfect_predictions(benchmark[0])
    predictions[9].cumulative_drift_detected = False
    predictions[9].cumulative_related_request_ids = []
    predictions[9].cumulative_related_decision_ids = []
    result = _score(predictions, benchmark)
    assert result.cumulative_drift_detection_accuracy == 0.9
    assert result.cumulative_drift_detection_rate == 0.0
    assert not result.cases[9].strict_pass


def test_false_positive_drift_reduces_accuracy_and_strict_score(benchmark) -> None:
    predictions = _perfect_predictions(benchmark[0])
    predictions[0].cumulative_drift_detected = True
    result = _score(predictions, benchmark)
    assert result.cumulative_drift_detection_accuracy == 0.9
    assert result.evidence_grounded_scope_accuracy == 0.9
    assert not result.cases[0].cumulative_drift_correct
    assert not result.cases[0].strict_pass


@pytest.mark.parametrize(
    ("field", "value", "result_field"),
    [
        (
            "cumulative_related_request_ids",
            ["CR-006", "CR-010"],
            "cumulative_related_request_ids_correct",
        ),
        (
            "cumulative_related_decision_ids",
            ["DEC-005", "DEC-006", "DEC-004"],
            "cumulative_related_decision_ids_correct",
        ),
    ],
)
def test_extra_or_missing_cumulative_ids_are_detected(
    benchmark, field: str, value: list[str], result_field: str
) -> None:
    predictions = _perfect_predictions(benchmark[0])
    setattr(predictions[9], field, value)
    result = _score(predictions, benchmark)
    assert getattr(result.cases[9], result_field) is False
    assert any("do not exactly match" in reason for reason in result.cases[9].failure_reasons)


def test_prediction_order_does_not_affect_scoring(benchmark) -> None:
    predictions = list(reversed(_perfect_predictions(benchmark[0])))
    result = _score(predictions, benchmark)
    assert result.evidence_grounded_scope_accuracy == 1.0
    assert [case.request_id for case in result.cases] == [
        record.request_id for record in benchmark[0]
    ]


def test_output_order_is_deterministic_when_ground_truth_is_reversed(benchmark) -> None:
    predictions = _perfect_predictions(benchmark[0])
    truth = list(reversed(benchmark[0]))
    result = score_predictions(predictions, truth, benchmark[1], benchmark[2])
    assert [case.request_id for case in result.cases] == [
        f"CR-{number:03d}" for number in range(1, 11)
    ]


def test_macro_metrics_include_omitted_prediction_label(benchmark) -> None:
    predictions = _perfect_predictions(benchmark[0])
    for prediction in predictions:
        if prediction.classification == Classification.IN_SCOPE:
            prediction.classification = Classification.OUT_OF_SCOPE
    result = _score(predictions, benchmark)
    assert set(result.per_label) == set(Classification)
    omitted = result.per_label[Classification.IN_SCOPE]
    assert omitted.precision == 0.0
    assert omitted.recall == 0.0
    assert omitted.f1 == 0.0


def test_zero_denominators_return_zero_for_precision_recall_and_f1(benchmark) -> None:
    predictions = _perfect_predictions(benchmark[0])
    for prediction in predictions:
        if prediction.classification == Classification.IN_SCOPE:
            prediction.classification = Classification.OUT_OF_SCOPE
    result = _score(predictions, benchmark)
    metrics = result.per_label[Classification.IN_SCOPE]
    assert metrics.precision == metrics.recall == metrics.f1 == 0.0


def test_missing_prediction_id_fails_clearly(benchmark) -> None:
    predictions = _perfect_predictions(benchmark[0])[:-1]
    with pytest.raises(ScoringError, match=r"missing=\['CR-010'\]"):
        _score(predictions, benchmark)


def test_duplicate_prediction_id_fails_clearly(benchmark) -> None:
    predictions = _perfect_predictions(benchmark[0])
    predictions.append(deepcopy(predictions[0]))
    with pytest.raises(ScoringError, match="duplicate prediction IDs"):
        _score(predictions, benchmark)


def test_unknown_prediction_id_fails_clearly(benchmark) -> None:
    predictions = _perfect_predictions(benchmark[0])
    predictions[-1].request_id = "CR-999"
    with pytest.raises(
        ScoringError,
        match=r"missing=\['CR-010'\], unexpected=\['CR-999'\]",
    ):
        _score(predictions, benchmark)
