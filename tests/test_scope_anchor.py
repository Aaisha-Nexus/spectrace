from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from spectrace.advanced_models import EvidenceCategory, TemporalStatus
from spectrace.scope_anchor import (
    ScopeAnchorError,
    build_scope_anchor,
    resolve_anchor_at_cutoff,
)


DEMO_PACK = Path(__file__).parents[1] / "data" / "synthetic" / "demo_project"


def _by_id(items):
    return {item.evidence_id: item for item in items}


def test_anchor_parsing_and_hash_are_stable_across_path_forms() -> None:
    first = build_scope_anchor(DEMO_PACK)
    second = build_scope_anchor(DEMO_PACK.as_posix())
    assert first == second
    assert first.anchor_hash == second.anchor_hash
    assert len(first.anchor_hash) == 64


def test_all_expected_source_evidence_ids_are_found() -> None:
    anchor = build_scope_anchor(DEMO_PACK)
    expected = {
        *(f"SOW-SCP-{number:03d}" for number in range(1, 13)),
        *(f"SOW-CON-{number:03d}" for number in range(1, 5)),
        *(f"SOW-EXC-{number:03d}" for number in range(1, 8)),
        *(f"SOW-ASM-{number:03d}" for number in range(1, 6)),
        *(f"SOW-QUE-{number:03d}" for number in range(1, 6)),
        *(f"DEC-{number:03d}" for number in range(1, 7)),
    }
    assert {item.evidence_id for item in anchor.items} == expected


def test_duplicate_sow_evidence_id_fails(tmp_path: Path) -> None:
    copied = tmp_path / "pack"
    shutil.copytree(DEMO_PACK, copied)
    sow = (copied / "sow.md").read_text(encoding="utf-8")
    duplicate = next(
        item.source_text
        for item in build_scope_anchor(copied).items
        if item.evidence_id == "SOW-SCP-001"
    )
    (copied / "sow.md").write_text(sow + "\n\n" + duplicate + "\n", encoding="utf-8")
    with pytest.raises(ScopeAnchorError, match="duplicate SOW evidence IDs"):
        build_scope_anchor(copied)


def test_assumptions_and_questions_remain_distinct_from_approved_scope() -> None:
    anchor = build_scope_anchor(DEMO_PACK)
    categories = _by_id(anchor.items)
    assert categories["SOW-ASM-001"].category == EvidenceCategory.ASSUMPTION
    assert categories["SOW-QUE-001"].category == EvidenceCategory.UNRESOLVED_QUESTION
    assert categories["SOW-SCP-001"].category == EvidenceCategory.APPROVED_SCOPE


def test_dec003_partial_supersession_preserves_ordinary_confirmation() -> None:
    anchor = build_scope_anchor(DEMO_PACK)
    edge = next(
        edge
        for edge in anchor.supersession_edges
        if edge.superseding_id == "DEC-003" and edge.superseded_id == "SOW-SCP-006"
    )
    resolved = _by_id(resolve_anchor_at_cutoff(anchor, DEMO_PACK, "DEC-003"))
    assert edge.is_partial
    assert "ceramic-kiln" in edge.facet
    assert resolved["SOW-SCP-006"].temporal_status == TemporalStatus.PARTIALLY_SUPERSEDED
    assert "ordinary studio" in resolved["SOW-SCP-006"].source_text


def test_dec005_and_dec006_become_effective_only_at_correct_cutoffs() -> None:
    anchor = build_scope_anchor(DEMO_PACK)
    before = _by_id(resolve_anchor_at_cutoff(anchor, DEMO_PACK, "CR-005"))
    at_five = _by_id(resolve_anchor_at_cutoff(anchor, DEMO_PACK, "DEC-005"))
    at_six = _by_id(resolve_anchor_at_cutoff(anchor, DEMO_PACK, "DEC-006"))
    assert before["DEC-005"].temporal_status == TemporalStatus.FUTURE
    assert before["DEC-006"].temporal_status == TemporalStatus.FUTURE
    assert at_five["DEC-005"].temporal_status == TemporalStatus.CURRENT
    assert at_five["DEC-006"].temporal_status == TemporalStatus.FUTURE
    assert at_six["DEC-005"].temporal_status == TemporalStatus.PARTIALLY_SUPERSEDED
    assert at_six["DEC-006"].temporal_status == TemporalStatus.CURRENT


def test_future_decisions_remain_explicitly_marked() -> None:
    anchor = build_scope_anchor(DEMO_PACK)
    resolved = _by_id(resolve_anchor_at_cutoff(anchor, DEMO_PACK, "DEC-004"))
    assert resolved["DEC-005"].temporal_status == TemporalStatus.FUTURE
    assert resolved["DEC-006"].temporal_status == TemporalStatus.FUTURE


def test_source_text_and_hash_are_preserved_exactly() -> None:
    anchor = build_scope_anchor(DEMO_PACK)
    item = _by_id(anchor.items)["SOW-SCP-003"]
    assert item.source_text.startswith("- **SOW-SCP-003")
    assert item.source_hash == hashlib.sha256(item.source_text.encode("utf-8")).hexdigest()
    assert item.source_path == "sow.md"
    assert item.source_location.startswith("sow.md:line-")
