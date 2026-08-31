from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import UTC, date, datetime
from pathlib import Path

from spectrace.advanced_models import (
    HumanAction,
    HumanDecisionPayload,
    HumanReview,
    LedgerEntryEffect,
)
from spectrace.dataset import validate_project_pack
from spectrace.ledger import LedgerStore
from spectrace.models import Classification
from spectrace.scope_anchor import build_scope_anchor
from spectrace.workflow import (
    WorkflowChangeType,
    WorkflowDraft,
    generate_workflow_draft,
    verify_workflow_draft,
    workflow_draft_hash,
)
from spectrace.workflow_export import (
    DRAWIO_MIME_TYPE,
    export_drawio,
    export_mermaid,
    mermaid_preview_html,
    validate_drawio_xml,
    validate_mermaid_source,
)


PACK = Path("data/synthetic/demo_project")


def _verified():
    anchor = build_scope_anchor(PACK)
    ledger = LedgerStore()
    ledger.seed_anchor(anchor, PACK, approved_through="DEC-006")
    draft = generate_workflow_draft(anchor, PACK, ledger, evidence_cutoff="DEC-006")
    verification = verify_workflow_draft(draft, anchor, PACK, ledger.snapshot(anchor.project_id))
    return anchor, ledger, draft, verification


def _highlighted(draft: WorkflowDraft) -> WorkflowDraft:
    nodes = list(draft.nodes)
    nodes[1] = nodes[1].model_copy(
        update={"label": 'Review "quoted": [A & B | C]\nlabel', "change_type": WorkflowChangeType.ADDED}
    )
    nodes[2] = nodes[2].model_copy(update={"change_type": WorkflowChangeType.MODIFIED})
    payload = draft.model_dump(mode="python")
    payload["nodes"] = tuple(nodes)
    payload["draft_hash"] = workflow_draft_hash(payload, include_hash=False)
    return WorkflowDraft.model_validate(payload)


def test_mermaid_is_escaped_deterministic_and_highlights_changes() -> None:
    anchor, ledger, draft, _ = _verified()
    draft = _highlighted(draft)
    verification = verify_workflow_draft(draft, anchor, PACK, ledger.snapshot(anchor.project_id))
    first = export_mermaid(draft, verification)
    second = export_mermaid(draft, verification)
    assert first == second
    assert "flowchart TD" in first.content
    assert "&quot;" in first.content
    assert "&#58;" in first.content
    assert "&#91;" in first.content and "&#93;" in first.content
    assert "&amp;" in first.content
    assert "&#124;" in first.content
    assert "classDef added" in first.content
    assert "classDef modified" in first.content
    assert "classDef clarification" in first.content
    assert "class n_NODE_BASE_001 added" in first.content
    assert "class n_NODE_BASE_002 modified" in first.content
    assert "NODE-BASE-001" not in first.content
    assert draft.nodes[1].supporting_evidence_ids[0] not in first.content
    assert draft.draft_hash in first.content
    validate_mermaid_source(first.content)
    ledger.close()


def test_actual_guided_workflow_uses_only_mermaid_safe_ids_and_labels() -> None:
    anchor, ledger, draft, verification = _verified()
    exported = export_mermaid(draft, verification)
    validate_mermaid_source(exported.content)
    assert all(
        character not in exported.content.splitlines()[index]
        for index in range(1, len(draft.nodes) + 1)
        for character in ("NODE-", "SOW-", "DEC-")
    )
    ledger.close()


def test_actual_updated_guided_workflow_passes_source_validation() -> None:
    anchor, ledger, _, _ = _verified()
    request = validate_project_pack(PACK).requests[5]
    ledger.record_request(anchor.project_id, request)
    ledger.record_assessment(
        anchor.project_id,
        "ASMNT-UPDATED-WORKFLOW",
        request.request_id,
        classification=Classification.POTENTIAL_SCOPE_CHANGE,
        evidence_ids=("SOW-CON-002",),
        assessment={"summary": "Synthetic workflow export test"},
    )
    payload = HumanDecisionPayload(
        decision_id="DEC-900",
        effective_date=date(2026, 5, 27),
        effect=LedgerEntryEffect.APPROVE_CAPABILITY,
        decision_text='Add ordered queue: [full sessions] & "priority" | review.',
        evidence_ids=("SOW-CON-002",),
        changes_approved_scope=True,
        approves_requested_capability=True,
    )
    ledger.apply_human_review(
        HumanReview(
            review_id="HR-UPDATED-WORKFLOW",
            project_id=anchor.project_id,
            request_id=request.request_id,
            assessment_id="ASMNT-UPDATED-WORKFLOW",
            action=HumanAction.APPROVE,
            reviewer_id="synthetic-reviewer",
            reviewed_at=datetime(2026, 5, 27, tzinfo=UTC),
            decision_payload=payload,
        )
    )
    updated = generate_workflow_draft(
        anchor, PACK, ledger, evidence_cutoff="DEC-006"
    )
    verification = verify_workflow_draft(
        updated, anchor, PACK, ledger.snapshot(anchor.project_id)
    )
    exported = export_mermaid(updated, verification)
    validate_mermaid_source(exported.content)
    assert "n_NODE_CHANGE_001" in exported.content
    assert "&#58;" in exported.content
    assert "&#91;" in exported.content
    assert "&#124;" in exported.content
    ledger.close()


def test_drawio_round_trip_contains_all_native_nodes_and_edges() -> None:
    anchor, ledger, draft, _ = _verified()
    draft = _highlighted(draft)
    verification = verify_workflow_draft(draft, anchor, PACK, ledger.snapshot(anchor.project_id))
    exported = export_drawio(draft, verification)
    assert exported == export_drawio(draft, verification)
    root = ET.fromstring(exported.content)
    cells = {cell.attrib["id"]: cell for cell in root.findall(".//mxCell")}
    for node in draft.nodes:
        assert cells[node.node_id].attrib["vertex"] == "1"
        assert cells[node.node_id].attrib["nodeType"] == node.node_type.value
        assert cells[node.node_id].attrib["evidenceIds"] == ",".join(node.supporting_evidence_ids)
    for edge in draft.edges:
        assert cells[edge.edge_id].attrib["edge"] == "1"
        assert cells[edge.edge_id].attrib["source"] == edge.source_id
        assert cells[edge.edge_id].attrib["target"] == edge.target_id
    assert len([cell for cell in cells.values() if cell.attrib.get("vertex") == "1"]) == len(draft.nodes)
    assert len([cell for cell in cells.values() if cell.attrib.get("edge") == "1"]) == len(draft.edges)
    assert "strokeColor=#198754" in cells[draft.nodes[1].node_id].attrib["style"]
    assert "strokeColor=#C47F00" in cells[draft.nodes[2].node_id].attrib["style"]
    assert root.tag == "mxfile" and root.attrib["host"] == "app.diagrams.net"
    assert DRAWIO_MIME_TYPE == "application/vnd.jgraph.mxfile"
    assert not exported.content.startswith("%PDF")
    validate_drawio_xml(exported.content, expected_pages=1)
    ledger.close()


def test_full_detail_viewer_never_shrinks_to_thumbnail_scale() -> None:
    html = mermaid_preview_html("flowchart LR\n    n_A([\"Start\"])\n")
    assert "Fit readable area" in html
    assert "Actual size" in html
    assert "Fullscreen" in html
    assert "Math.max(.72" in html
    assert "scrollLeft=0" in html
    assert "Fit to width" not in html
