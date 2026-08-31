"""Human-reviewed StudioLane workflow asset outside the frozen benchmark pack."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from spectrace.ledger import LedgerStore
from spectrace.scope_anchor import build_scope_anchor
from spectrace.workflow import (
    WorkflowDraft,
    WorkflowEdge,
    WorkflowChangeType,
    WorkflowNode,
    WorkflowVerificationResult,
    verify_workflow_draft,
    workflow_draft_hash,
)


DEFAULT_ASSET = Path(__file__).resolve().parents[1] / "assets" / "studiolane_workflows.json"
DEFAULT_PACK = Path(__file__).resolve().parents[1] / "data" / "synthetic" / "demo_project"


@dataclass(frozen=True)
class CuratedWorkflowPair:
    original: WorkflowDraft
    updated: WorkflowDraft
    original_verification: WorkflowVerificationResult
    updated_verification: WorkflowVerificationResult


def _edge_payloads(
    rows: list[list[str | None]], prefix: str, nodes: tuple[WorkflowNode, ...]
) -> tuple[WorkflowEdge, ...]:
    evidence_by_node = {node.node_id: node.supporting_evidence_ids for node in nodes}
    return tuple(
        WorkflowEdge(
            edge_id=f"EDGE-{prefix}-{index:03d}",
            source_id=str(source),
            target_id=str(target),
            condition=str(condition) if condition else None,
            evidence_ids=evidence_by_node.get(str(target), ()),
        )
        for index, (source, target, condition) in enumerate(rows, start=1)
    )


def _draft(
    *, asset: dict[str, object], nodes: tuple[WorkflowNode, ...], edges: tuple[WorkflowEdge, ...],
    cutoff: str, snapshot: object, anchor_hash: str, title: str, workflow_id: str,
) -> WorkflowDraft:
    payload: dict[str, object] = {
        "workflow_id": workflow_id,
        "project_id": snapshot.project_id,
        "title": title,
        "anchor_hash": anchor_hash,
        "ledger_snapshot_hash": snapshot.snapshot_hash,
        "evidence_cutoff": cutoff,
        "actors": tuple(asset["actors"]),
        "nodes": nodes,
        "edges": edges,
    }
    payload["draft_hash"] = workflow_draft_hash(payload, include_hash=False)
    return WorkflowDraft.model_validate(payload)


def load_studiolane_workflows(
    asset_path: str | Path = DEFAULT_ASSET,
    project_pack_path: str | Path = DEFAULT_PACK,
) -> CuratedWorkflowPair:
    """Load and verify the original and DEC-006 queue-aware workflows."""

    asset = json.loads(Path(asset_path).read_text(encoding="utf-8"))
    pack = Path(project_pack_path)
    anchor = build_scope_anchor(pack)
    original_nodes = tuple(WorkflowNode.model_validate(item) for item in asset["original_nodes"])
    added_nodes = tuple(WorkflowNode.model_validate(item) for item in asset["updated_nodes"])
    remove = {(source, target) for source, target in asset["updated_remove_edges"]}
    kept_rows = [row for row in asset["original_edges"] if (row[0], row[1]) not in remove]
    updated_nodes = tuple(
        node.model_copy(update={"change_type": WorkflowChangeType.MODIFIED})
        if node.node_id in {"NODE-FULL", "NODE-RESTORE"}
        else node
        for node in original_nodes
    ) + added_nodes
    original_edges = _edge_payloads(asset["original_edges"], "ORIGINAL", original_nodes)
    updated_edges = _edge_payloads([*kept_rows, *asset["updated_edges"]], "UPDATED", updated_nodes)

    with LedgerStore() as original_store:
        original_store.seed_anchor(anchor, pack, approved_through="DEC-004")
        original_snapshot = original_store.snapshot(anchor.project_id)
        original = _draft(
            asset=asset, nodes=original_nodes, edges=original_edges, cutoff="DEC-004",
            snapshot=original_snapshot, anchor_hash=anchor.anchor_hash,
            title="StudioLane original approved workflow", workflow_id="WF-STUDIOLANE-ORIGINAL",
        )
        original_check = verify_workflow_draft(original, anchor, pack, original_snapshot)
    with LedgerStore() as updated_store:
        updated_store.seed_anchor(anchor, pack, approved_through="DEC-006")
        updated_snapshot = updated_store.snapshot(anchor.project_id)
        updated = _draft(
            asset=asset, nodes=updated_nodes, edges=updated_edges, cutoff="DEC-006",
            snapshot=updated_snapshot, anchor_hash=anchor.anchor_hash,
            title="StudioLane updated approved workflow", workflow_id="WF-STUDIOLANE-UPDATED",
        )
        updated_check = verify_workflow_draft(updated, anchor, pack, updated_snapshot)
    if not original_check.passed or not updated_check.passed:
        raise ValueError("curated StudioLane workflow failed evidence verification")
    validate_workflow_quality(original, allow_admin_inputs=True)
    validate_workflow_quality(updated, allow_admin_inputs=True)
    return CuratedWorkflowPair(
        original=original,
        updated=updated,
        original_verification=original_check,
        updated_verification=updated_check,
    )


def validate_workflow_quality(draft: WorkflowDraft, *, allow_admin_inputs: bool = False) -> None:
    """Check connectivity, branching, evidence and documented admin inputs."""

    outgoing: dict[str, set[str]] = {node.node_id: set() for node in draft.nodes}
    incoming: dict[str, set[str]] = {node.node_id: set() for node in draft.nodes}
    for edge in draft.edges:
        outgoing[edge.source_id].add(edge.target_id)
        incoming[edge.target_id].add(edge.source_id)
    start = next(node.node_id for node in draft.nodes if node.node_type.value == "START")
    end = next(node.node_id for node in draft.nodes if node.node_type.value == "END")
    visited, frontier = set(), [start]
    while frontier:
        current = frontier.pop()
        if current in visited:
            continue
        visited.add(current)
        frontier.extend(outgoing[current] - visited)
    if end not in visited:
        raise ValueError("workflow has no connected start-to-end path")
    allowed_orphans = {
        node.node_id for node in draft.nodes
        if allow_admin_inputs and node.actor_id == "ACTOR-ADMINISTRATOR"
    }
    orphans = {
        node.node_id for node in draft.nodes
        if node.node_id != start and not incoming[node.node_id]
    } - allowed_orphans
    if orphans:
        raise ValueError(f"workflow contains orphan nodes: {sorted(orphans)}")
    decisions = [node for node in draft.nodes if node.node_type.value == "DECISION"]
    if not decisions or any(len(outgoing[node.node_id]) < 2 for node in decisions):
        raise ValueError("every workflow decision requires at least two branches")
    if any(
        not node.supporting_evidence_ids
        for node in draft.nodes
        if node.node_type.value not in {"START", "END"}
    ):
        raise ValueError("workflow contains unsupported consequential nodes")
