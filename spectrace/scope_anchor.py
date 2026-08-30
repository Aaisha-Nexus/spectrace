"""Deterministically parse the documented SpecTrace benchmark Markdown.

This parser intentionally targets the benchmark's documented Markdown
convention: evidence is expressed as ``- **SOW-...`` bullets and decisions as
``## DEC-...`` sections with labeled metadata. It is not a universal document
parser and consumes only source evidence and request chronology.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

from spectrace.advanced_models import (
    DecisionPolarity,
    EvidenceCategory,
    EvidenceItem,
    ScopeAnchor,
    SupersessionEdge,
    TemporalStatus,
)


ANCHOR_VERSION = "benchmark-markdown-v1"
SOW_ITEM_RE = re.compile(
    r"^- \*\*(SOW-(SCP|CON|EXC|ASM|QUE)-\d{3})\b[\s\S]*?"
    r"(?=^- \*\*SOW-(?:SCP|CON|EXC|ASM|QUE)-\d{3}\b|^## |\Z)",
    re.MULTILINE,
)
DECISION_SECTION_RE = re.compile(
    r"^## (DEC-\d{3})\b[\s\S]*?(?=^## DEC-\d{3}\b|\Z)", re.MULTILINE
)
DECISION_DATE_RE = re.compile(r"^- \*\*Date:\*\* (\d{4}-\d{2}-\d{2})$", re.MULTILINE)
DECISION_STATUS_RE = re.compile(r"^- \*\*Status:\*\* ([A-Z_]+)$", re.MULTILINE)
DECISION_TRIGGER_RE = re.compile(
    r"^- \*\*Triggering request ID:\*\* (?:(CR-\d{3})|None\b)", re.MULTILINE
)
ROLE_RE = re.compile(r"^- \*\*([^*]+):\*\*", re.MULTILINE)
WORD_RE = re.compile(r"[a-z0-9]+")

CATEGORY_BY_CODE = {
    "SCP": EvidenceCategory.APPROVED_SCOPE,
    "CON": EvidenceCategory.CONSTRAINT,
    "EXC": EvidenceCategory.EXCLUSION,
    "ASM": EvidenceCategory.ASSUMPTION,
    "QUE": EvidenceCategory.UNRESOLVED_QUESTION,
}

METADATA_STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "can", "does",
        "for", "from", "has", "in", "is", "it", "may", "of", "on", "or",
        "that", "the", "their", "this", "to", "when", "with",
    }
)
ACTION_WORDS = frozenset(
    {
        "add", "allocate", "approve", "block", "book", "cancel", "change",
        "confirm", "create", "decline", "display", "email", "join", "leave",
        "manage", "notify", "prevent", "provision", "record", "register",
        "reject", "reserve", "restore", "review", "send", "sign", "store",
        "submit", "synchronize", "update", "view",
    }
)
FACET_KEYWORDS = {
    "authorization": {"account", "permission", "role", "signin", "user"},
    "notification": {"alert", "email", "notification", "notify"},
    "persistence": {"history", "persistent", "retention", "stored"},
    "ordering": {"first", "order", "ordered", "priority", "queue"},
    "automation": {"allocate", "allocation", "automatic", "automatically", "promotion"},
    "integration": {"calendar", "external", "integration", "synchronization"},
    "performance": {"availability", "instant", "latency", "refresh", "response"},
    "mobile": {"android", "ios", "mobile", "native"},
    "capacity": {"availability", "capacity", "full", "place"},
    "workflow": {"approval", "confirm", "decline", "pending", "review", "status"},
}


class ScopeAnchorError(ValueError):
    """Raised when source Markdown cannot produce an unambiguous anchor."""


def normalize_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).lower().replace("-", " ")
    return " ".join(WORD_RE.findall(normalized))


def tokenize(text: str) -> tuple[str, ...]:
    return tuple(
        token for token in normalize_text(text).split() if token not in METADATA_STOPWORDS
    )


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _line_location(path: str, full_text: str, offset: int) -> str:
    return f"{path}:line-{full_text.count(chr(10), 0, offset) + 1}"


def _sorted_terms(values: set[str]) -> tuple[str, ...]:
    return tuple(sorted(value for value in values if value))


def _metadata(text: str, roles: tuple[str, ...]) -> dict[str, tuple[str, ...]]:
    normalized = normalize_text(text)
    tokens = set(tokenize(text))
    actors = {role for role in roles if normalize_text(role) in normalized}
    actor_words = {word for actor in actors for word in actor.split()}
    actions = tokens & ACTION_WORDS
    facets = {
        facet
        for facet, keywords in FACET_KEYWORDS.items()
        if tokens & keywords
    }
    objects = tokens - actions - actor_words
    return {
        "actor_terms": _sorted_terms(actors),
        "action_terms": _sorted_terms(actions),
        "object_terms": _sorted_terms(objects),
        "domain_terms": _sorted_terms(tokens),
        "facet_terms": _sorted_terms(facets),
    }


def _unique_matches(matches: list[re.Match[str]], kind: str) -> None:
    ids = [match.group(1) for match in matches]
    duplicates = sorted(item for item, count in Counter(ids).items() if count > 1)
    if duplicates:
        raise ScopeAnchorError(f"duplicate {kind} IDs: {duplicates}")


def _decision_polarity(status: str) -> DecisionPolarity:
    mapping = {
        "APPROVED": DecisionPolarity.APPROVES,
        "APPROVED_REJECTION": DecisionPolarity.REJECTS,
        "APPROVED_WITH_OPEN_DETAILS": DecisionPolarity.APPROVES_WITH_OPEN_DETAILS,
    }
    try:
        return mapping[status]
    except KeyError as exc:
        raise ScopeAnchorError(f"unsupported decision status: {status}") from exc


def _supersession_edges(
    decision_id: str, section: str, section_hash: str
) -> list[SupersessionEdge]:
    block_match = re.search(
        r"^- \*\*Supersession:\*\* ([\s\S]*?)(?=^- \*\*[A-Za-z ]+:\*\*|\Z)",
        section,
        re.MULTILINE,
    )
    supersession_line = (
        " ".join(block_match.group(1).split()) if block_match else ""
    )
    if not supersession_line or supersession_line == "None.":
        return []

    candidates: list[tuple[str, str]] = []
    patterns = (
        re.compile(
            r"(?:Partially supersedes|Supersedes) (SOW-[A-Z]{3}-\d{3}|DEC-\d{3})"
            r"(?:'s)?\s*(.*?)(?=\.\s+[A-Z]|\.$|$)"
        ),
        re.compile(
            r"(SOW-[A-Z]{3}-\d{3}|DEC-\d{3}) is (?:further )?superseded\s*"
            r"(.*?)(?=\.\s+[A-Z]|\.$|$)"
        ),
    )
    for pattern in patterns:
        candidates.extend((match.group(1), match.group(2).strip()) for match in pattern.finditer(supersession_line))

    edges: list[SupersessionEdge] = []
    seen: set[tuple[str, str]] = set()
    for target, detail in candidates:
        key = (decision_id, target)
        if key in seen:
            continue
        seen.add(key)
        facet = detail.strip(" ,") or "entire evidence item"
        is_partial = any(
            marker in supersession_line.lower() or marker in facet.lower()
            for marker in ("partial", " only", "only ", "further", "'s ")
        )
        edges.append(
            SupersessionEdge(
                superseding_id=decision_id,
                superseded_id=target,
                facet=facet,
                is_partial=is_partial,
                source_hash=section_hash,
            )
        )
    return edges


def _anchor_hash(
    project_id: str,
    version: str,
    items: tuple[EvidenceItem, ...],
    edges: tuple[SupersessionEdge, ...],
) -> str:
    payload: dict[str, Any] = {
        "project_id": project_id,
        "version": version,
        "items": [item.model_dump(mode="json") for item in items],
        "supersession_edges": [edge.model_dump(mode="json") for edge in edges],
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_scope_anchor(project_pack_path: str | Path) -> ScopeAnchor:
    """Build an atomic anchor without consulting benchmark ground truth."""

    root = Path(project_pack_path)
    try:
        sow_text = (root / "sow.md").read_text(encoding="utf-8")
        decisions_text = (root / "decisions.md").read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ScopeAnchorError(f"missing source file: {exc.filename}") from exc

    sow_matches = list(SOW_ITEM_RE.finditer(sow_text))
    decision_matches = list(DECISION_SECTION_RE.finditer(decisions_text))
    _unique_matches(sow_matches, "SOW evidence")
    _unique_matches(decision_matches, "decision")
    if not sow_matches or not decision_matches:
        raise ScopeAnchorError("source pack must contain SOW evidence and decisions")

    roles_section_match = re.search(
        r"^## User roles\s*$([\s\S]*?)(?=^## )", sow_text, re.MULTILINE
    )
    roles_section = roles_section_match.group(1) if roles_section_match else ""
    roles = tuple(
        sorted(normalize_text(match.group(1)) for match in ROLE_RE.finditer(roles_section))
    )
    items: list[EvidenceItem] = []
    for match in sow_matches:
        evidence_id, code = match.group(1), match.group(2)
        source_text = match.group(0).rstrip()
        items.append(
            EvidenceItem(
                evidence_id=evidence_id,
                category=CATEGORY_BY_CODE[code],
                source_text=source_text,
                source_path="sow.md",
                source_location=_line_location("sow.md", sow_text, match.start()),
                source_hash=_sha256_text(source_text),
                **_metadata(source_text, roles),
            )
        )

    edges: list[SupersessionEdge] = []
    for match in decision_matches:
        decision_id = match.group(1)
        source_text = match.group(0).rstrip()
        date_match = DECISION_DATE_RE.search(source_text)
        status_match = DECISION_STATUS_RE.search(source_text)
        trigger_match = DECISION_TRIGGER_RE.search(source_text)
        if not date_match or not status_match or not trigger_match:
            raise ScopeAnchorError(
                f"{decision_id} requires date, status, and triggering-request metadata"
            )
        source_hash = _sha256_text(source_text)
        decision_edges = _supersession_edges(decision_id, source_text, source_hash)
        edges.extend(decision_edges)
        items.append(
            EvidenceItem(
                evidence_id=decision_id,
                category=EvidenceCategory.DECISION,
                source_text=source_text,
                source_path="decisions.md",
                source_location=_line_location(
                    "decisions.md", decisions_text, match.start()
                ),
                source_hash=source_hash,
                effective_date=date.fromisoformat(date_match.group(1)),
                decision_polarity=_decision_polarity(status_match.group(1)),
                supersedes_ids=tuple(sorted(edge.superseded_id for edge in decision_edges)),
                triggering_request_id=trigger_match.group(1),
                **_metadata(source_text, roles),
            )
        )

    superseded_by: dict[str, set[str]] = {}
    for edge in edges:
        superseded_by.setdefault(edge.superseded_id, set()).add(edge.superseding_id)
    items = [
        item.model_copy(
            update={"superseded_by_ids": tuple(sorted(superseded_by.get(item.evidence_id, set())))}
        )
        for item in items
    ]
    ordered_items = tuple(sorted(items, key=lambda item: item.evidence_id))
    ordered_edges = tuple(
        sorted(edges, key=lambda edge: (edge.superseding_id, edge.superseded_id, edge.facet))
    )
    project_match = re.search(r"^#\s+(.+?)\s+Statement of Work\s*$", sow_text, re.MULTILINE)
    project_id = normalize_text(project_match.group(1)).replace(" ", "-") if project_match else root.name
    return ScopeAnchor(
        project_id=project_id,
        version=ANCHOR_VERSION,
        source_root=root.name,
        items=ordered_items,
        supersession_edges=ordered_edges,
        anchor_hash=_anchor_hash(project_id, ANCHOR_VERSION, ordered_items, ordered_edges),
    )


def _cutoff_date(project_pack_path: str | Path, anchor: ScopeAnchor, cutoff: str) -> date:
    decisions = {
        item.evidence_id: item.effective_date
        for item in anchor.items
        if item.category == EvidenceCategory.DECISION
    }
    if cutoff.startswith("DEC-"):
        value = decisions.get(cutoff)
        if value is None:
            raise ScopeAnchorError(f"unknown decision cutoff: {cutoff}")
        return value
    try:
        requests = json.loads(
            (Path(project_pack_path) / "requests.json").read_text(encoding="utf-8")
        )
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise ScopeAnchorError("cannot resolve request evidence cutoff") from exc
    by_id = {item.get("request_id"): item for item in requests if isinstance(item, dict)}
    if cutoff not in by_id:
        raise ScopeAnchorError(f"unknown request cutoff: {cutoff}")
    return date.fromisoformat(by_id[cutoff]["date"])


def resolve_anchor_at_cutoff(
    anchor: ScopeAnchor,
    project_pack_path: str | Path,
    evidence_cutoff: str,
) -> tuple[EvidenceItem, ...]:
    """Return source items with temporal status resolved at an explicit cutoff."""

    cutoff_date = _cutoff_date(project_pack_path, anchor, evidence_cutoff)
    statuses: dict[str, TemporalStatus] = {}
    for item in anchor.items:
        statuses[item.evidence_id] = (
            TemporalStatus.FUTURE
            if item.effective_date is not None and item.effective_date > cutoff_date
            else TemporalStatus.CURRENT
        )
    item_by_id = {item.evidence_id: item for item in anchor.items}
    for edge in anchor.supersession_edges:
        superseding = item_by_id[edge.superseding_id]
        if superseding.effective_date is None or superseding.effective_date > cutoff_date:
            continue
        if statuses[edge.superseded_id] == TemporalStatus.FUTURE:
            continue
        statuses[edge.superseded_id] = (
            TemporalStatus.PARTIALLY_SUPERSEDED
            if edge.is_partial
            else TemporalStatus.SUPERSEDED
        )
    return tuple(
        item.model_copy(update={"temporal_status": statuses[item.evidence_id]})
        for item in anchor.items
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a deterministic SpecTrace scope anchor")
    parser.add_argument("project_pack", type=Path)
    parser.add_argument("--cutoff", help="optionally report temporal state at an evidence cutoff")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        anchor = build_scope_anchor(args.project_pack)
        items = (
            resolve_anchor_at_cutoff(anchor, args.project_pack, args.cutoff)
            if args.cutoff
            else anchor.items
        )
    except (ScopeAnchorError, ValueError) as exc:
        print(f"Scope-anchor error: {exc}", file=sys.stderr)
        return 1
    counts = Counter(item.category.value for item in items)
    temporal = Counter(item.temporal_status.value for item in items)
    print(
        json.dumps(
            {
                "project_id": anchor.project_id,
                "version": anchor.version,
                "anchor_hash": anchor.anchor_hash,
                "item_count": len(anchor.items),
                "category_counts": dict(sorted(counts.items())),
                "supersession_edge_count": len(anchor.supersession_edges),
                "temporal_counts": dict(sorted(temporal.items())),
                "cutoff": args.cutoff,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
