from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from spectrace.advanced_models import (
    HumanAction,
    HumanDecisionPayload,
    HumanReview,
    LedgerEntryEffect,
)
from spectrace.dataset import validate_project_pack
from spectrace.ledger import LedgerError, LedgerStore
from spectrace.models import Classification
from spectrace.scope_anchor import build_scope_anchor


DEMO_PACK = Path(__file__).parents[1] / "data" / "synthetic" / "demo_project"


@pytest.fixture
def seeded_store():
    anchor = build_scope_anchor(DEMO_PACK)
    store = LedgerStore()
    store.seed_anchor(anchor, DEMO_PACK, approved_through="DEC-004")
    try:
        yield store, anchor
    finally:
        store.close()


def _record_case(store: LedgerStore, project_id: str, request_id: str = "CR-001") -> None:
    pack = validate_project_pack(DEMO_PACK)
    request = next(item for item in pack.requests if item.request_id == request_id)
    store.record_request(project_id, request)
    store.record_assessment(
        project_id,
        "ASMNT-001",
        request_id,
        classification=Classification.POTENTIAL_SCOPE_CHANGE,
        evidence_ids=("SOW-CON-002",),
        assessment={"summary": "Offline test assessment"},
    )


def _payload(
    decision_id: str = "DEC-005",
    *,
    effect: LedgerEntryEffect = LedgerEntryEffect.APPROVE_CAPABILITY,
    changes_scope: bool = True,
    approves_request: bool = True,
) -> HumanDecisionPayload:
    return HumanDecisionPayload(
        decision_id=decision_id,
        effective_date=date(2026, 5, 21),
        effect=effect,
        decision_text="Human-approved synthetic decision payload.",
        evidence_ids=("SOW-CON-002",),
        changes_approved_scope=changes_scope,
        approves_requested_capability=approves_request,
    )


def _review(
    project_id: str,
    action: HumanAction,
    *,
    review_id: str = "HR-001",
    payload: HumanDecisionPayload | None = None,
    classification: Classification | None = None,
    reason: str | None = None,
    evidence_ids: tuple[str, ...] = (),
) -> HumanReview:
    return HumanReview(
        review_id=review_id,
        project_id=project_id,
        request_id="CR-001",
        assessment_id="ASMNT-001",
        action=action,
        reviewer_id="synthetic-reviewer",
        reviewed_at=datetime(2026, 5, 21, tzinfo=UTC),
        final_classification=classification,
        reason=reason,
        evidence_ids=evidence_ids,
        decision_payload=payload,
    )


def test_schema_tables_and_foreign_keys_are_enabled(seeded_store) -> None:
    store, _ = seeded_store
    assert store.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert set(store.table_columns()) == {
        "projects", "evidence_items", "supersession_edges", "requests",
        "assessments", "human_reviews", "ledger_entries", "trajectory_events",
    }
    ledger_columns = {
        row["name"]: row
        for row in store.connection.execute("PRAGMA table_info(ledger_entries)")
    }
    assert ledger_columns["review_id"]["notnull"] == 1
    assert ledger_columns["request_id"]["notnull"] == 1
    assert store.connection.execute("SELECT COUNT(*) FROM ledger_entries").fetchone()[0] == 0


def test_raw_request_and_assessment_do_not_change_approved_memory(seeded_store) -> None:
    store, anchor = seeded_store
    before = store.snapshot(anchor.project_id)
    pack = validate_project_pack(DEMO_PACK)
    store.record_request(anchor.project_id, pack.requests[0])
    after_request = store.snapshot(anchor.project_id)
    store.record_assessment(
        anchor.project_id,
        "ASMNT-001",
        "CR-001",
        classification=Classification.IN_SCOPE,
        evidence_ids=("SOW-SCP-003",),
        assessment={"summary": "No approval implied."},
    )
    after_assessment = store.snapshot(anchor.project_id)
    assert before.approved_evidence_ids == after_request.approved_evidence_ids
    assert before.ledger_entry_ids == after_request.ledger_entry_ids
    assert before.approved_evidence_ids == after_assessment.approved_evidence_ids
    assert before.ledger_entry_ids == after_assessment.ledger_entry_ids
    assert "DEC-004" in before.approved_evidence_ids


def test_approve_with_explicit_decision_changes_memory(seeded_store) -> None:
    store, anchor = seeded_store
    _record_case(store, anchor.project_id)
    result = store.apply_human_review(
        _review(anchor.project_id, HumanAction.APPROVE, payload=_payload())
    )
    snapshot = store.snapshot(anchor.project_id)
    assert result.ledger_changed
    assert result.before_snapshot_hash != result.after_snapshot_hash
    assert "DEC-005" in snapshot.approved_evidence_ids
    assert "HUMAN-DEC-005" in snapshot.ledger_entry_ids


@pytest.mark.parametrize("action", [HumanAction.NEEDS_CLARIFICATION, HumanAction.DEFER])
def test_clarification_and_defer_never_add_approved_entries(seeded_store, action) -> None:
    store, anchor = seeded_store
    _record_case(store, anchor.project_id)
    before = store.snapshot(anchor.project_id)
    result = store.apply_human_review(_review(anchor.project_id, action))
    after = store.snapshot(anchor.project_id)
    assert not result.ledger_changed
    assert before.ledger_entry_ids == after.ledger_entry_ids
    assert before.approved_evidence_ids == after.approved_evidence_ids


def test_override_requires_classification_reason_and_evidence() -> None:
    with pytest.raises(ValidationError, match="OVERRIDE requires"):
        _review("studiolane", HumanAction.OVERRIDE)


def test_override_with_explicit_scope_decision_is_recorded(seeded_store) -> None:
    store, anchor = seeded_store
    _record_case(store, anchor.project_id)
    review = _review(
        anchor.project_id,
        HumanAction.OVERRIDE,
        payload=_payload(),
        classification=Classification.POTENTIAL_SCOPE_CHANGE,
        reason="Human reviewer changes the recommendation.",
        evidence_ids=("SOW-CON-002",),
    )
    assert store.apply_human_review(review).ledger_changed


@pytest.mark.parametrize(
    "classification",
    [Classification.OUT_OF_SCOPE, Classification.CONTRADICTS_APPROVED_DECISION],
)
def test_upholding_exclusion_or_contradiction_does_not_approve_requested_capability(
    seeded_store, classification
) -> None:
    store, anchor = seeded_store
    _record_case(store, anchor.project_id)
    payload = _payload(
        effect=LedgerEntryEffect.UPHOLD_EXISTING_SCOPE,
        changes_scope=False,
        approves_request=False,
    )
    review = _review(
        anchor.project_id,
        HumanAction.APPROVE,
        payload=payload,
        classification=classification,
    )
    store.apply_human_review(review)
    row = store.connection.execute(
        "SELECT approves_requested_capability FROM ledger_entries WHERE ledger_entry_id = 'HUMAN-DEC-005'"
    ).fetchone()
    assert row[0] == 0


def test_invalid_review_rolls_back_review_and_entry(seeded_store) -> None:
    store, anchor = seeded_store
    _record_case(store, anchor.project_id)
    duplicate = _payload(decision_id="DEC-001")
    with pytest.raises(LedgerError, match="already approved anchor evidence"):
        store.apply_human_review(
            _review(anchor.project_id, HumanAction.APPROVE, payload=duplicate)
        )
    assert store.connection.execute(
        "SELECT COUNT(*) FROM human_reviews WHERE review_id = 'HR-001'"
    ).fetchone()[0] == 0


def test_snapshot_is_immutable_and_stable(seeded_store) -> None:
    store, anchor = seeded_store
    first = store.snapshot(anchor.project_id)
    second = store.snapshot(anchor.project_id)
    assert first == second
    with pytest.raises(ValidationError):
        first.project_id = "changed"


def test_file_backed_ledger_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite3"
    anchor = build_scope_anchor(DEMO_PACK)
    with LedgerStore(path) as store:
        store.seed_anchor(anchor, DEMO_PACK, approved_through="DEC-004")
        expected = store.snapshot(anchor.project_id)
        mode = store.connection.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
    with LedgerStore(path) as reopened:
        assert reopened.snapshot(anchor.project_id) == expected


def test_production_tables_have_no_ground_truth_fields(seeded_store) -> None:
    store, _ = seeded_store
    columns = {column.lower() for values in store.table_columns().values() for column in values}
    forbidden_fragments = ("ground_truth", "expected_classification", "expected_reasoning")
    assert not any(fragment in column for column in columns for fragment in forbidden_fragments)
