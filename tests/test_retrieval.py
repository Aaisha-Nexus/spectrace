from __future__ import annotations

import json
from pathlib import Path

import pytest

from spectrace.advanced_models import EvidenceCategory, RetrievalLimits
from spectrace.dataset import validate_project_pack
from spectrace.models import Classification
from spectrace.retrieval import retrieve_evidence
from spectrace.scope_anchor import build_scope_anchor


DEMO_PACK = Path(__file__).parents[1] / "data" / "synthetic" / "demo_project"


def evaluate_retrieval(
    project_pack_path: str | Path,
    *,
    limits: RetrievalLimits | None = None,
) -> dict[str, object]:
    """Score retrieval only inside the offline test/evaluation boundary."""

    limits = limits or RetrievalLimits()
    pack = validate_project_pack(project_pack_path)
    anchor = build_scope_anchor(project_pack_path)
    truth_by_id = {record.request_id: record for record in pack.ground_truth}
    relevant_hits = relevant_total = 0
    conflict_hits = conflict_total = 0
    question_hits = question_total = 0
    appropriate_case_hits = 0
    category_fraction_total = 0.0
    temporal_leakage = 0
    expanded_cases = 0
    cases: list[dict[str, object]] = []

    for request in pack.requests:
        bundle = retrieve_evidence(
            anchor,
            project_pack_path,
            request.message,
            request.evidence_available_through,
            limits,
        )
        expanded_cases += bundle.expanded
        truth = truth_by_id[request.request_id]
        retrieved_ids = {item.evidence.evidence_id for item in bundle.items}
        relevant = set(truth.relevant_scope_ids)
        relevant_hits += len(relevant & retrieved_ids)
        relevant_total += len(relevant)
        conflict_decisions = {
            item for item in truth.conflicting_evidence_ids if item.startswith("DEC-")
        }
        conflict_hits += len(conflict_decisions & retrieved_ids)
        conflict_total += len(conflict_decisions)
        questions = {
            item for item in truth.relevant_scope_ids if item.startswith("SOW-QUE-")
        }
        question_hits += len(questions & retrieved_ids)
        question_total += len(questions)
        appropriate = (
            set(truth.conflicting_evidence_ids)
            if truth.expected_classification
            == Classification.CONTRADICTS_APPROVED_DECISION
            else set(truth.valid_supporting_evidence_ids)
        )
        appropriate_hit = bool(appropriate & retrieved_ids)
        appropriate_case_hits += appropriate_hit
        category_fraction_total += len(bundle.category_coverage) / len(EvidenceCategory)
        available_ids = pack.available_evidence_ids_by_request[request.request_id]
        leaked = sorted(
            item
            for item in retrieved_ids
            if item.startswith("DEC-") and item not in available_ids
        )
        temporal_leakage += len(leaked)
        cases.append(
            {
                "request_id": request.request_id,
                "retrieved_count": len(bundle.items),
                "expanded": bundle.expanded,
                "relevant_hits": len(relevant & retrieved_ids),
                "relevant_total": len(relevant),
                "classification_appropriate_hit": appropriate_hit,
                "temporal_leakage_ids": leaked,
            }
        )

    case_count = len(pack.requests)
    return {
        "dataset": Path(project_pack_path).as_posix(),
        "anchor_hash": anchor.anchor_hash,
        "k": limits.expanded_max_total if expanded_cases else limits.max_total,
        "initial_k": limits.max_total,
        "expanded_case_count": expanded_cases,
        "case_count": case_count,
        "recall_at_k": relevant_hits / relevant_total if relevant_total else 1.0,
        "contradiction_decision_recall": (
            conflict_hits / conflict_total if conflict_total else 1.0
        ),
        "unresolved_question_recall": (
            question_hits / question_total if question_total else 1.0
        ),
        "classification_appropriate_evidence_recall": (
            appropriate_case_hits / case_count if case_count else 1.0
        ),
        "mean_category_coverage": (
            category_fraction_total / case_count if case_count else 1.0
        ),
        "temporal_leakage_count": temporal_leakage,
        "cases": cases,
        "limitation": (
            "Single-project synthetic benchmark metrics do not establish "
            "general-domain retrieval performance."
        ),
    }


@pytest.fixture(scope="module")
def context():
    pack = validate_project_pack(DEMO_PACK)
    return build_scope_anchor(DEMO_PACK), {request.request_id: request for request in pack.requests}


def _ids(bundle):
    return [item.evidence.evidence_id for item in bundle.items]


def _retrieve(context, request_id: str):
    anchor, requests = context
    request = requests[request_id]
    return retrieve_evidence(
        anchor,
        DEMO_PACK,
        request.message,
        request.evidence_available_through,
    )


def test_retrieval_order_and_scores_are_deterministic(context) -> None:
    first = _retrieve(context, "CR-008")
    second = _retrieve(context, "CR-008")
    assert first == second
    assert [item.rank for item in first.items] == list(range(1, len(first.items) + 1))
    assert all(item.score_components for item in first.items)


def test_future_decisions_never_leak(context) -> None:
    bundle = _retrieve(context, "CR-001")
    assert "DEC-005" not in _ids(bundle)
    assert "DEC-006" not in _ids(bundle)


def test_category_balancing_preserves_broad_coverage(context) -> None:
    bundle = _retrieve(context, "CR-004")
    assert len(bundle.category_coverage) >= 4
    assert EvidenceCategory.UNRESOLVED_QUESTION in bundle.category_coverage


def test_weak_category_coverage_triggers_one_deterministic_expansion(context) -> None:
    anchor, _ = context
    bundle = retrieve_evidence(
        anchor,
        DEMO_PACK,
        "quuxxyzz",
        "DEC-004",
        RetrievalLimits(minimum_category_coverage=4),
    )
    assert bundle.expanded
    assert len(bundle.category_coverage) == 4
    assert 4 <= len(bundle.items) <= 18


@pytest.mark.parametrize(
    ("request_id", "expected_id"),
    [
        ("CR-003", "SOW-QUE-003"),
        ("CR-004", "SOW-QUE-002"),
        ("CR-005", "SOW-EXC-001"),
        ("CR-008", "DEC-003"),
        ("CR-009", "DEC-002"),
    ],
)
def test_known_benchmark_evidence_is_retrieved(
    context, request_id: str, expected_id: str
) -> None:
    assert expected_id in _ids(_retrieve(context, request_id))


def test_runtime_retrieval_never_reads_ground_truth(
    context, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = Path.read_text

    def guarded(self: Path, *args, **kwargs):
        if self.name == "ground_truth.json":
            raise AssertionError("runtime retrieval attempted to read ground truth")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded)
    _retrieve(context, "CR-004")


def test_offline_evaluation_reports_required_metrics_and_no_leakage() -> None:
    summary = evaluate_retrieval(DEMO_PACK)
    assert summary["temporal_leakage_count"] == 0
    assert summary["classification_appropriate_evidence_recall"] == 1.0
    assert summary["contradiction_decision_recall"] == 1.0
    assert 0 <= summary["unresolved_question_recall"] <= 1
    assert 0 <= summary["recall_at_k"] <= 1
    assert "general-domain" in summary["limitation"]


if __name__ == "__main__":
    print(json.dumps(evaluate_retrieval(DEMO_PACK), indent=2, sort_keys=True))
