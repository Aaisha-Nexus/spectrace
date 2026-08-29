"""Load and validate a SpecTrace synthetic project pack."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from spectrace.models import Classification, GroundTruthRecord, IncomingRequest


EVIDENCE_ID_RE = re.compile(r"\b(?:SOW-[A-Z]{3}-\d{3}|DEC-\d{3})\b")
SOW_DEFINITION_RE = re.compile(r"^- \*\*(SOW-[A-Z]{3}-\d{3})\b", re.MULTILINE)
DECISION_HEADING_RE = re.compile(r"^## (DEC-\d{3})\b", re.MULTILINE)
DECISION_DATE_RE = re.compile(r"^- \*\*Date:\*\* (\d{4}-\d{2}-\d{2})$", re.MULTILINE)
DECISION_TRIGGER_RE = re.compile(
    r"^- \*\*Triggering request ID:\*\* (?:(CR-\d{3})|None\b)", re.MULTILINE
)

ANSWER_KEY_FIELDS = {
    "classification",
    "expected_classification",
    "valid_supporting_evidence_ids",
    "relevant_scope_ids",
    "conflicting_evidence_ids",
    "required_clarification",
    "expected_clarification_points",
    "expected_dependencies",
    "expected_reasoning_summary",
    "expected_human_action",
    "resulting_decision_id",
    "planted_conflict",
    "supersession_expected",
    "cumulative_drift_expected",
    "cumulative_pattern_id",
    "cumulative_related_request_ids",
    "cumulative_related_decision_ids",
    "unsupported_claims_forbidden",
}

DEMO_CLASSIFICATION_COUNTS = {
    Classification.IN_SCOPE: 2,
    Classification.AMBIGUOUS: 2,
    Classification.OUT_OF_SCOPE: 1,
    Classification.CONTRADICTS_APPROVED_DECISION: 2,
    Classification.POTENTIAL_SCOPE_CHANGE: 3,
}


class DatasetValidationError(ValueError):
    """Raised when a project pack violates its schema or benchmark invariants."""


@dataclass(frozen=True)
class ProjectPack:
    path: Path
    requests: list[IncomingRequest]
    ground_truth: list[GroundTruthRecord]
    evidence_ids: frozenset[str]
    available_evidence_ids_by_request: dict[str, frozenset[str]]
    decision_dates: dict[str, date]
    decision_triggers: dict[str, str | None]


def _load_json_array(path: Path) -> list[dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DatasetValidationError(f"missing required file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise DatasetValidationError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise DatasetValidationError(f"{path} must contain a JSON array of objects")
    return value


def _find_duplicates(values: list[str]) -> list[str]:
    counts = Counter(values)
    return sorted(value for value, count in counts.items() if count > 1)


def _parse_decisions(text: str) -> tuple[dict[str, date], dict[str, str | None]]:
    headings = list(DECISION_HEADING_RE.finditer(text))
    heading_ids = [heading.group(1) for heading in headings]
    duplicates = _find_duplicates(heading_ids)
    if duplicates:
        raise DatasetValidationError(f"duplicate decision IDs: {duplicates}")
    dates: dict[str, date] = {}
    triggers: dict[str, str | None] = {}
    for index, heading in enumerate(headings):
        decision_id = heading.group(1)
        section_end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        section = text[heading.end() : section_end]
        date_match = DECISION_DATE_RE.search(section)
        trigger_match = DECISION_TRIGGER_RE.search(section)
        if not date_match or not trigger_match:
            raise DatasetValidationError(
                f"{decision_id} must have an ISO date and a triggering-request entry"
            )
        dates[decision_id] = date.fromisoformat(date_match.group(1))
        triggers[decision_id] = trigger_match.group(1)
    return dates, triggers


def _validate_generic(
    requests: list[IncomingRequest],
    ground_truth: list[GroundTruthRecord],
    evidence_ids: set[str],
    available_evidence_ids_by_request: dict[str, frozenset[str]],
    decision_dates: dict[str, date],
    decision_triggers: dict[str, str | None],
) -> None:
    request_ids = [item.request_id for item in requests]
    truth_ids = [item.request_id for item in ground_truth]
    duplicate_requests = _find_duplicates(request_ids)
    duplicate_truth = _find_duplicates(truth_ids)
    if duplicate_requests:
        raise DatasetValidationError(f"duplicate request IDs: {duplicate_requests}")
    if duplicate_truth:
        raise DatasetValidationError(f"duplicate ground-truth IDs: {duplicate_truth}")
    if set(request_ids) != set(truth_ids):
        missing_truth = sorted(set(request_ids) - set(truth_ids))
        unexpected_truth = sorted(set(truth_ids) - set(request_ids))
        raise DatasetValidationError(
            "request/ground-truth ID sets differ; "
            f"missing ground truth={missing_truth}, unexpected ground truth={unexpected_truth}"
        )

    expected_orders = list(range(1, len(requests) + 1))
    actual_orders = [item.chronological_order for item in requests]
    if actual_orders != expected_orders:
        raise DatasetValidationError(
            f"requests must be stored in chronological order {expected_orders}; got {actual_orders}"
        )
    request_dates = [item.date for item in requests]
    if any(later <= earlier for earlier, later in zip(request_dates, request_dates[1:])):
        raise DatasetValidationError("request dates must be strictly increasing")
    decision_date_values = list(decision_dates.values())
    if any(later <= earlier for earlier, later in zip(decision_date_values, decision_date_values[1:])):
        raise DatasetValidationError("decision dates must be strictly increasing")

    truth_by_id = {item.request_id: item for item in ground_truth}
    requests_by_id = {item.request_id: item for item in requests}
    for request in requests:
        truth = truth_by_id[request.request_id]
        if truth.evidence_available_through != request.evidence_available_through:
            raise DatasetValidationError(
                f"{request.request_id} has mismatched evidence_available_through values"
            )
        cutoff = request.evidence_available_through
        if cutoff.startswith("CR-"):
            if cutoff not in requests_by_id:
                raise DatasetValidationError(f"{request.request_id} has unknown cutoff {cutoff}")
            if requests_by_id[cutoff].chronological_order >= request.chronological_order:
                raise DatasetValidationError(f"{request.request_id} has a non-prior request cutoff {cutoff}")
        elif cutoff not in decision_dates:
            raise DatasetValidationError(f"{request.request_id} has unknown cutoff {cutoff}")
        elif decision_dates[cutoff] > request.date:
            raise DatasetValidationError(
                f"{request.request_id} has future decision cutoff {cutoff}"
            )

        cited_ids = (
            truth.valid_supporting_evidence_ids
            + truth.relevant_scope_ids
            + truth.conflicting_evidence_ids
        )
        unknown = sorted(set(cited_ids) - evidence_ids)
        if unknown:
            raise DatasetValidationError(
                f"{request.request_id} references unknown evidence IDs: {unknown}"
            )
        unavailable_decisions = sorted(
            evidence_id
            for evidence_id in set(cited_ids)
            if evidence_id.startswith("DEC-")
            and evidence_id not in available_evidence_ids_by_request[request.request_id]
        )
        if unavailable_decisions:
            raise DatasetValidationError(
                f"{request.request_id} references future decision evidence: {unavailable_decisions}"
            )
        if truth.resulting_decision_id is not None:
            decision_id = truth.resulting_decision_id
            if decision_id not in decision_dates:
                raise DatasetValidationError(
                    f"{request.request_id} has unknown resulting decision {decision_id}"
                )
            if decision_triggers.get(decision_id) != request.request_id:
                raise DatasetValidationError(
                    f"{decision_id} does not identify {request.request_id} as its trigger"
                )
            if decision_dates[decision_id] <= request.date:
                raise DatasetValidationError(
                    f"resulting decision {decision_id} must occur after {request.request_id}"
                )

        unknown_related_requests = sorted(
            set(truth.cumulative_related_request_ids) - set(request_ids)
        )
        unknown_related_decisions = sorted(
            set(truth.cumulative_related_decision_ids) - set(decision_dates)
        )
        if unknown_related_requests or unknown_related_decisions:
            raise DatasetValidationError(
                f"{request.request_id} has unknown cumulative relationships: "
                f"requests={unknown_related_requests}, decisions={unknown_related_decisions}"
            )
        if truth.cumulative_drift_expected and request.request_id not in truth.cumulative_related_request_ids:
            raise DatasetValidationError(
                f"{request.request_id} cumulative relationships must include the current request"
            )


def _validate_demo_pack(ground_truth: list[GroundTruthRecord]) -> None:
    counts = Counter(record.expected_classification for record in ground_truth)
    if counts != Counter(DEMO_CLASSIFICATION_COUNTS):
        raise DatasetValidationError(
            f"frozen demo classification counts changed: {dict(counts)}"
        )
    by_id = {record.request_id: record for record in ground_truth}
    if by_id["CR-006"].resulting_decision_id != "DEC-005":
        raise DatasetValidationError("frozen demo requires CR-006 to result in DEC-005")
    if by_id["CR-007"].resulting_decision_id != "DEC-006":
        raise DatasetValidationError("frozen demo requires CR-007 to result in DEC-006")
    cr10 = by_id["CR-010"]
    if (
        not cr10.cumulative_drift_expected
        or cr10.cumulative_pattern_id != "CUM-FULL-SESSION-001"
        or set(cr10.cumulative_related_request_ids) != {"CR-006", "CR-007", "CR-010"}
        or set(cr10.cumulative_related_decision_ids) != {"DEC-005", "DEC-006"}
    ):
        raise DatasetValidationError("frozen demo CR-010 cumulative expectation changed")


def _build_evidence_availability(
    requests: list[IncomingRequest],
    evidence_ids: set[str],
    decision_dates: dict[str, date],
) -> dict[str, frozenset[str]]:
    """Return the SOW plus decisions available at each explicit evidence cutoff."""

    requests_by_id = {request.request_id: request for request in requests}
    sow_ids = {evidence_id for evidence_id in evidence_ids if evidence_id.startswith("SOW-")}
    availability: dict[str, frozenset[str]] = {}
    for request in requests:
        cutoff = request.evidence_available_through
        if cutoff.startswith("DEC-"):
            if cutoff not in decision_dates:
                raise DatasetValidationError(f"{request.request_id} has unknown cutoff {cutoff}")
            cutoff_date = decision_dates[cutoff]
        else:
            if cutoff not in requests_by_id:
                raise DatasetValidationError(f"{request.request_id} has unknown cutoff {cutoff}")
            cutoff_date = requests_by_id[cutoff].date
        available_decisions = {
            decision_id
            for decision_id, decision_date in decision_dates.items()
            if decision_date <= cutoff_date
        }
        availability[request.request_id] = frozenset(sow_ids | available_decisions)
    return availability


def validate_project_pack(project_pack_path: str | Path) -> ProjectPack:
    """Load a project pack and validate generic plus frozen-demo invariants."""

    path = Path(project_pack_path)
    raw_requests = _load_json_array(path / "requests.json")
    raw_ground_truth = _load_json_array(path / "ground_truth.json")

    for index, item in enumerate(raw_requests):
        leaked = sorted(set(item) & ANSWER_KEY_FIELDS)
        if leaked:
            raise DatasetValidationError(
                f"requests.json item {index} contains answer-key fields: {leaked}"
            )
    raw_request_ids = [item.get("request_id") for item in raw_requests]
    duplicate_raw_ids = _find_duplicates([value for value in raw_request_ids if isinstance(value, str)])
    if duplicate_raw_ids:
        raise DatasetValidationError(f"duplicate request IDs: {duplicate_raw_ids}")
    raw_truth_ids = [item.get("request_id") for item in raw_ground_truth]
    duplicate_raw_truth_ids = _find_duplicates(
        [value for value in raw_truth_ids if isinstance(value, str)]
    )
    if duplicate_raw_truth_ids:
        raise DatasetValidationError(
            f"duplicate ground-truth IDs: {duplicate_raw_truth_ids}"
        )

    try:
        requests = [IncomingRequest.model_validate(item) for item in raw_requests]
        ground_truth = [GroundTruthRecord.model_validate(item) for item in raw_ground_truth]
    except ValidationError as exc:
        raise DatasetValidationError(f"schema validation failed: {exc}") from exc

    try:
        sow_text = (path / "sow.md").read_text(encoding="utf-8")
        decisions_text = (path / "decisions.md").read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise DatasetValidationError(f"missing required file: {exc.filename}") from exc
    sow_definition_ids = SOW_DEFINITION_RE.findall(sow_text)
    duplicate_sow_ids = _find_duplicates(sow_definition_ids)
    if duplicate_sow_ids:
        raise DatasetValidationError(f"duplicate SOW evidence IDs: {duplicate_sow_ids}")
    decision_dates, decision_triggers = _parse_decisions(decisions_text)
    evidence_ids = set(sow_definition_ids) | set(decision_dates)

    referenced_source_ids = set(EVIDENCE_ID_RE.findall(sow_text + decisions_text))
    undeclared_source_ids = sorted(referenced_source_ids - evidence_ids)
    if undeclared_source_ids:
        raise DatasetValidationError(
            f"source documents reference undeclared evidence IDs: {undeclared_source_ids}"
        )

    available_evidence_ids_by_request = _build_evidence_availability(
        requests, evidence_ids, decision_dates
    )
    _validate_generic(
        requests,
        ground_truth,
        evidence_ids,
        available_evidence_ids_by_request,
        decision_dates,
        decision_triggers,
    )
    _validate_demo_pack(ground_truth)
    return ProjectPack(
        path=path,
        requests=requests,
        ground_truth=ground_truth,
        evidence_ids=frozenset(evidence_ids),
        available_evidence_ids_by_request=available_evidence_ids_by_request,
        decision_dates=decision_dates,
        decision_triggers=decision_triggers,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a SpecTrace project pack")
    parser.add_argument("project_pack", type=Path)
    args = parser.parse_args(argv)
    try:
        pack = validate_project_pack(args.project_pack)
    except DatasetValidationError as exc:
        print(f"Validation failed: {exc}", file=sys.stderr)
        return 1
    counts = Counter(record.expected_classification.value for record in pack.ground_truth)
    print(
        f"Validation passed: {pack.path} "
        f"({len(pack.requests)} requests, {len(pack.evidence_ids)} evidence IDs)"
    )
    print("Classification counts: " + ", ".join(f"{label.value}={counts[label.value]}" for label in Classification))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
