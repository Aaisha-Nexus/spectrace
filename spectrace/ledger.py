"""Approval-gated SQLite memory for the deterministic SpecTrace foundation."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from spectrace.advanced_models import (
    ASSESSMENT_ID_PATTERN,
    EvidenceCategory,
    HumanAction,
    HumanReview,
    LedgerSnapshot,
    LedgerUpdateResult,
    ScopeAnchor,
    TrajectoryEvent,
)
from spectrace.models import Classification, IncomingRequest
from spectrace.scope_anchor import resolve_anchor_at_cutoff


SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    anchor_version TEXT NOT NULL,
    anchor_hash TEXT NOT NULL UNIQUE,
    source_root TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence_items (
    project_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    category TEXT NOT NULL,
    source_text TEXT NOT NULL,
    source_path TEXT NOT NULL,
    source_location TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    effective_date TEXT,
    decision_polarity TEXT,
    metadata_json TEXT NOT NULL,
    initially_approved INTEGER NOT NULL CHECK (initially_approved IN (0, 1)),
    PRIMARY KEY (project_id, evidence_id),
    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS supersession_edges (
    project_id TEXT NOT NULL,
    superseding_id TEXT NOT NULL,
    superseded_id TEXT NOT NULL,
    facet TEXT NOT NULL,
    is_partial INTEGER NOT NULL CHECK (is_partial IN (0, 1)),
    source_hash TEXT NOT NULL,
    PRIMARY KEY (project_id, superseding_id, superseded_id, facet),
    FOREIGN KEY (project_id, superseding_id)
        REFERENCES evidence_items(project_id, evidence_id),
    FOREIGN KEY (project_id, superseded_id)
        REFERENCES evidence_items(project_id, evidence_id)
);

CREATE TABLE IF NOT EXISTS requests (
    project_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    request_date TEXT NOT NULL,
    source TEXT NOT NULL,
    message TEXT NOT NULL,
    evidence_cutoff TEXT NOT NULL,
    chronological_order INTEGER NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (project_id, request_id),
    UNIQUE (project_id, chronological_order),
    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS assessments (
    project_id TEXT NOT NULL,
    assessment_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    classification TEXT,
    evidence_ids_json TEXT NOT NULL,
    assessment_json TEXT NOT NULL,
    assessment_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (project_id, assessment_id),
    FOREIGN KEY (project_id, request_id)
        REFERENCES requests(project_id, request_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS human_reviews (
    project_id TEXT NOT NULL,
    review_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    assessment_id TEXT NOT NULL,
    action TEXT NOT NULL,
    reviewer_id TEXT NOT NULL,
    reviewed_at TEXT NOT NULL,
    final_classification TEXT,
    reason TEXT,
    evidence_ids_json TEXT NOT NULL,
    decision_payload_json TEXT,
    PRIMARY KEY (project_id, review_id),
    FOREIGN KEY (project_id, request_id)
        REFERENCES requests(project_id, request_id),
    FOREIGN KEY (project_id, assessment_id)
        REFERENCES assessments(project_id, assessment_id)
);

CREATE TABLE IF NOT EXISTS ledger_entries (
    project_id TEXT NOT NULL,
    ledger_entry_id TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    review_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    effective_date TEXT NOT NULL,
    effect TEXT NOT NULL,
    decision_text TEXT NOT NULL,
    evidence_ids_json TEXT NOT NULL,
    changes_approved_scope INTEGER NOT NULL CHECK (changes_approved_scope IN (0, 1)),
    approves_requested_capability INTEGER NOT NULL CHECK (approves_requested_capability IN (0, 1)),
    entry_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (project_id, ledger_entry_id),
    UNIQUE (project_id, decision_id),
    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE CASCADE,
    FOREIGN KEY (project_id, review_id)
        REFERENCES human_reviews(project_id, review_id),
    FOREIGN KEY (project_id, request_id)
        REFERENCES requests(project_id, request_id)
);

CREATE TABLE IF NOT EXISTS trajectory_events (
    project_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    request_id TEXT,
    sequence INTEGER NOT NULL,
    node TEXT NOT NULL,
    tool TEXT,
    input_ids_json TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    result_summary TEXT NOT NULL,
    verification TEXT,
    duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
    human_state TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (project_id, event_id),
    UNIQUE (project_id, request_id, sequence),
    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE CASCADE,
    FOREIGN KEY (project_id, request_id)
        REFERENCES requests(project_id, request_id)
);
"""


class LedgerError(ValueError):
    """Raised when a ledger operation would violate human-control rules."""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()


class LedgerStore:
    """Small transactional store; callers own every consequential decision."""

    def __init__(self, database: str | Path = ":memory:") -> None:
        self.database = str(database)
        self.connection = sqlite3.connect(self.database)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        if self.database != ":memory:":
            self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.executescript(SCHEMA)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> LedgerStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def seed_anchor(
        self,
        anchor: ScopeAnchor,
        project_pack_path: str | Path,
        *,
        approved_through: str,
    ) -> None:
        """Store all source evidence and seed only temporally approved memory."""

        resolved = resolve_anchor_at_cutoff(anchor, project_pack_path, approved_through)
        available_decisions = {
            item.evidence_id
            for item in resolved
            if item.category == EvidenceCategory.DECISION
            and item.temporal_status.value != "FUTURE"
        }
        initially_approved_categories = {
            EvidenceCategory.APPROVED_SCOPE,
            EvidenceCategory.CONSTRAINT,
            EvidenceCategory.EXCLUSION,
        }
        with self.connection:
            existing = self.connection.execute(
                "SELECT anchor_hash FROM projects WHERE project_id = ?",
                (anchor.project_id,),
            ).fetchone()
            if existing and existing["anchor_hash"] != anchor.anchor_hash:
                raise LedgerError("project already exists with a different anchor hash")
            self.connection.execute(
                """INSERT OR IGNORE INTO projects
                   (project_id, anchor_version, anchor_hash, source_root, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    anchor.project_id,
                    anchor.version,
                    anchor.anchor_hash,
                    anchor.source_root,
                    _now(),
                ),
            )
            for item in anchor.items:
                metadata = {
                    "actor_terms": item.actor_terms,
                    "action_terms": item.action_terms,
                    "object_terms": item.object_terms,
                    "domain_terms": item.domain_terms,
                    "facet_terms": item.facet_terms,
                    "supersedes_ids": item.supersedes_ids,
                    "superseded_by_ids": item.superseded_by_ids,
                    "triggering_request_id": item.triggering_request_id,
                }
                self.connection.execute(
                    """INSERT OR IGNORE INTO evidence_items
                       (project_id, evidence_id, category, source_text, source_path,
                        source_location, source_hash, effective_date,
                        decision_polarity, metadata_json, initially_approved)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        anchor.project_id,
                        item.evidence_id,
                        item.category.value,
                        item.source_text,
                        item.source_path,
                        item.source_location,
                        item.source_hash,
                        item.effective_date.isoformat() if item.effective_date else None,
                        item.decision_polarity.value if item.decision_polarity else None,
                        _json(metadata),
                        int(
                            item.category in initially_approved_categories
                            or item.evidence_id in available_decisions
                        ),
                    ),
                )
            for edge in anchor.supersession_edges:
                self.connection.execute(
                    """INSERT OR IGNORE INTO supersession_edges
                       (project_id, superseding_id, superseded_id, facet, is_partial, source_hash)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        anchor.project_id,
                        edge.superseding_id,
                        edge.superseded_id,
                        edge.facet,
                        int(edge.is_partial),
                        edge.source_hash,
                    ),
                )

    def record_request(self, project_id: str, request: IncomingRequest) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT INTO requests
                   (project_id, request_id, request_date, source, message,
                    evidence_cutoff, chronological_order, recorded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    project_id,
                    request.request_id,
                    request.date.isoformat(),
                    request.source,
                    request.message,
                    request.evidence_available_through,
                    request.chronological_order,
                    _now(),
                ),
            )

    def record_assessment(
        self,
        project_id: str,
        assessment_id: str,
        request_id: str,
        *,
        classification: Classification | None,
        evidence_ids: tuple[str, ...],
        assessment: dict[str, Any],
    ) -> None:
        if not ASSESSMENT_ID_PATTERN.fullmatch(assessment_id):
            raise LedgerError(f"invalid assessment ID: {assessment_id!r}")
        self._require_known_evidence(project_id, evidence_ids)
        payload = _json(assessment)
        with self.connection:
            self.connection.execute(
                """INSERT INTO assessments
                   (project_id, assessment_id, request_id, classification,
                    evidence_ids_json, assessment_json, assessment_hash, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    project_id,
                    assessment_id,
                    request_id,
                    classification.value if classification else None,
                    _json(evidence_ids),
                    payload,
                    hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                    _now(),
                ),
            )

    def _require_known_evidence(
        self, project_id: str, evidence_ids: tuple[str, ...]
    ) -> None:
        if not evidence_ids:
            return
        placeholders = ",".join("?" for _ in evidence_ids)
        rows = self.connection.execute(
            f"SELECT evidence_id FROM evidence_items WHERE project_id = ? AND evidence_id IN ({placeholders})",
            (project_id, *evidence_ids),
        ).fetchall()
        unknown = sorted(set(evidence_ids) - {row["evidence_id"] for row in rows})
        if unknown:
            raise LedgerError(f"unknown evidence IDs: {unknown}")

    def apply_human_review(self, review: HumanReview) -> LedgerUpdateResult:
        before = self.snapshot(review.project_id)
        payload = review.decision_payload
        try:
            with self.connection:
                assessment = self.connection.execute(
                    """SELECT classification FROM assessments
                       WHERE project_id = ? AND assessment_id = ? AND request_id = ?""",
                    (review.project_id, review.assessment_id, review.request_id),
                ).fetchone()
                if assessment is None:
                    raise LedgerError("review does not match a recorded assessment")
                self._require_known_evidence(review.project_id, review.evidence_ids)
                if payload:
                    self._require_known_evidence(review.project_id, payload.evidence_ids)
                effective_classification = review.final_classification
                if effective_classification is None and assessment["classification"]:
                    effective_classification = Classification(assessment["classification"])
                if (
                    payload
                    and effective_classification in {
                        Classification.OUT_OF_SCOPE,
                        Classification.CONTRADICTS_APPROVED_DECISION,
                    }
                    and payload.approves_requested_capability
                ):
                    raise LedgerError(
                        "upholding an exclusion or contradiction cannot approve the request"
                    )
                review_json = payload.model_dump(mode="json") if payload else None
                self.connection.execute(
                    """INSERT INTO human_reviews
                       (project_id, review_id, request_id, assessment_id, action,
                        reviewer_id, reviewed_at, final_classification, reason,
                        evidence_ids_json, decision_payload_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        review.project_id,
                        review.review_id,
                        review.request_id,
                        review.assessment_id,
                        review.action.value,
                        review.reviewer_id,
                        review.reviewed_at.isoformat(),
                        review.final_classification.value if review.final_classification else None,
                        review.reason,
                        _json(review.evidence_ids),
                        _json(review_json) if review_json else None,
                    ),
                )
                ledger_entry_id = None
                if payload:
                    already_initial = self.connection.execute(
                        """SELECT 1 FROM evidence_items
                           WHERE project_id = ? AND evidence_id = ?
                             AND initially_approved = 1""",
                        (review.project_id, payload.decision_id),
                    ).fetchone()
                    if already_initial:
                        raise LedgerError(
                            f"decision {payload.decision_id} is already approved anchor evidence"
                        )
                    ledger_entry_id = f"HUMAN-{payload.decision_id}"
                    entry_value = payload.model_dump(mode="json")
                    self.connection.execute(
                        """INSERT INTO ledger_entries
                           (project_id, ledger_entry_id, decision_id, review_id,
                            request_id, effective_date, effect, decision_text,
                            evidence_ids_json, changes_approved_scope,
                            approves_requested_capability, entry_hash, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            review.project_id,
                            ledger_entry_id,
                            payload.decision_id,
                            review.review_id,
                            review.request_id,
                            payload.effective_date.isoformat(),
                            payload.effect.value,
                            payload.decision_text,
                            _json(payload.evidence_ids),
                            int(payload.changes_approved_scope),
                            int(payload.approves_requested_capability),
                            _hash(entry_value),
                            _now(),
                        ),
                    )
        except sqlite3.IntegrityError as exc:
            raise LedgerError(f"review transaction rejected: {exc}") from exc

        after = self.snapshot(review.project_id)
        return LedgerUpdateResult(
            review_id=review.review_id,
            action=review.action,
            ledger_changed=payload is not None,
            ledger_entry_id=f"HUMAN-{payload.decision_id}" if payload else None,
            before_snapshot_hash=before.snapshot_hash,
            after_snapshot_hash=after.snapshot_hash,
        )

    def approved_scope_change_records(self, project_id: str) -> tuple[dict[str, Any], ...]:
        """Return only human-approved capability additions eligible for drift."""

        rows = self.connection.execute(
            """SELECT decision_id, request_id, decision_text, evidence_ids_json,
                      effective_date, entry_hash
               FROM ledger_entries
               WHERE project_id = ? AND changes_approved_scope = 1
                 AND approves_requested_capability = 1
               ORDER BY effective_date, ledger_entry_id""",
            (project_id,),
        ).fetchall()
        return tuple(
            {
                "decision_id": row["decision_id"],
                "request_id": row["request_id"],
                "decision_text": row["decision_text"],
                "evidence_ids": tuple(json.loads(row["evidence_ids_json"])),
                "effective_date": row["effective_date"],
                "entry_hash": row["entry_hash"],
            }
            for row in rows
        )

    def record_trajectory_event(
        self,
        project_id: str,
        request_id: str,
        event: TrajectoryEvent,
    ) -> None:
        """Persist one append-only, non-reasoning execution event."""

        event_id = f"{request_id}-{event.sequence:03d}"
        with self.connection:
            self.connection.execute(
                """INSERT INTO trajectory_events
                   (project_id, event_id, request_id, sequence, node, tool,
                    input_ids_json, input_hash, result_summary, verification,
                    duration_ms, human_state, error, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    project_id,
                    event_id,
                    request_id,
                    event.sequence,
                    event.node.value,
                    event.tool,
                    _json(event.input_ids),
                    event.input_hash,
                    event.result_summary,
                    event.verification,
                    event.duration_ms,
                    event.human_state,
                    event.error,
                    _now(),
                ),
            )

    def snapshot(self, project_id: str) -> LedgerSnapshot:
        project = self.connection.execute(
            "SELECT anchor_hash FROM projects WHERE project_id = ?", (project_id,)
        ).fetchone()
        if project is None:
            raise LedgerError(f"unknown project: {project_id}")
        initial_ids = tuple(
            row["evidence_id"]
            for row in self.connection.execute(
                """SELECT evidence_id FROM evidence_items
                   WHERE project_id = ? AND initially_approved = 1
                   ORDER BY evidence_id""",
                (project_id,),
            )
        )
        decision_ids = tuple(
            row["decision_id"]
            for row in self.connection.execute(
                "SELECT decision_id FROM ledger_entries WHERE project_id = ? ORDER BY decision_id",
                (project_id,),
            )
        )
        entry_ids = tuple(
            row["ledger_entry_id"]
            for row in self.connection.execute(
                "SELECT ledger_entry_id FROM ledger_entries WHERE project_id = ? ORDER BY ledger_entry_id",
                (project_id,),
            )
        )
        request_ids = tuple(
            row["request_id"]
            for row in self.connection.execute(
                "SELECT request_id FROM requests WHERE project_id = ? ORDER BY chronological_order",
                (project_id,),
            )
        )
        assessment_ids = tuple(
            row["assessment_id"]
            for row in self.connection.execute(
                "SELECT assessment_id FROM assessments WHERE project_id = ? ORDER BY assessment_id",
                (project_id,),
            )
        )
        review_ids = tuple(
            row["review_id"]
            for row in self.connection.execute(
                "SELECT review_id FROM human_reviews WHERE project_id = ? ORDER BY review_id",
                (project_id,),
            )
        )
        approved_evidence_ids = tuple(sorted(set(initial_ids) | set(decision_ids)))
        payload = {
            "project_id": project_id,
            "anchor_hash": project["anchor_hash"],
            "approved_evidence_ids": approved_evidence_ids,
            "ledger_entry_ids": entry_ids,
            "request_ids": request_ids,
            "assessment_ids": assessment_ids,
            "review_ids": review_ids,
        }
        return LedgerSnapshot(**payload, snapshot_hash=_hash(payload))

    def table_columns(self) -> dict[str, tuple[str, ...]]:
        tables = (
            "projects", "evidence_items", "supersession_edges", "requests",
            "assessments", "human_reviews", "ledger_entries", "trajectory_events",
        )
        return {
            table: tuple(
                row["name"]
                for row in self.connection.execute(f"PRAGMA table_info({table})")
            )
            for table in tables
        }
