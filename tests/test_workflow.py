from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from spectrace.advanced_models import EvidenceCategory, TemporalStatus
from spectrace.ledger import LedgerStore
from spectrace.scope_anchor import build_scope_anchor
from spectrace.workflow import (
    StructuredProjectInput,
    WorkflowActor,
    WorkflowChangeType,
    WorkflowDraft,
    WorkflowEdge,
    WorkflowNode,
    WorkflowNodeType,
    build_structured_scope_anchor,
    generate_workflow_draft,
    stable_local_id,
    structured_project_hash,
    verify_workflow_draft,
    workflow_draft_hash,
)


PACK = Path("data/synthetic/demo_project")


def _context(cutoff: str = "DEC-006"):
    anchor = build_scope_anchor(PACK)
    ledger = LedgerStore()
    ledger.seed_anchor(anchor, PACK, approved_through=cutoff)
    return anchor, ledger


def _replace_node_evidence(draft: WorkflowDraft, evidence_id: str) -> WorkflowDraft:
    nodes = list(draft.nodes)
    index = next(i for i, node in enumerate(nodes) if node.node_type not in {WorkflowNodeType.START, WorkflowNodeType.END})
    nodes[index] = nodes[index].model_copy(update={"supporting_evidence_ids": (evidence_id,)})
    payload = draft.model_dump(mode="python")
    payload["nodes"] = tuple(nodes)
    payload["draft_hash"] = workflow_draft_hash(payload, include_hash=False)
    return WorkflowDraft.model_validate(payload)


def test_generated_workflow_uses_only_approved_current_evidence() -> None:
    anchor, ledger = _context()
    draft = generate_workflow_draft(anchor, PACK, ledger, evidence_cutoff="DEC-006")
    verification = verify_workflow_draft(draft, anchor, PACK, ledger.snapshot(anchor.project_id))
    assert verification.passed
    assert any(node.node_type == WorkflowNodeType.CLARIFICATION for node in draft.nodes)
    assert all(
        node.supporting_evidence_ids
        for node in draft.nodes
        if node.node_type not in {WorkflowNodeType.START, WorkflowNodeType.END}
    )
    ledger.close()


@pytest.mark.parametrize("evidence_id,expected_code", [
    ("SOW-ASM-001", "UNAPPROVED_EVIDENCE"),
    ("SOW-QUE-001", "UNAPPROVED_EVIDENCE"),
    ("DEC-999", "UNAPPROVED_EVIDENCE"),
])
def test_assumptions_questions_and_nonexistent_evidence_are_rejected(
    evidence_id: str, expected_code: str
) -> None:
    anchor, ledger = _context()
    draft = generate_workflow_draft(anchor, PACK, ledger, evidence_cutoff="DEC-006")
    changed = _replace_node_evidence(draft, evidence_id)
    result = verify_workflow_draft(changed, anchor, PACK, ledger.snapshot(anchor.project_id))
    assert not result.passed
    assert expected_code in {issue.code for issue in result.issues}
    ledger.close()


def test_superseded_evidence_is_rejected() -> None:
    anchor, ledger = _context()
    resolved = {item.evidence_id: item for item in __import__("spectrace.scope_anchor", fromlist=["resolve_anchor_at_cutoff"]).resolve_anchor_at_cutoff(anchor, PACK, "DEC-006")}
    assert resolved["SOW-SCP-006"].temporal_status == TemporalStatus.PARTIALLY_SUPERSEDED
    draft = generate_workflow_draft(anchor, PACK, ledger, evidence_cutoff="DEC-006")
    result = verify_workflow_draft(
        _replace_node_evidence(draft, "SOW-SCP-006"),
        anchor,
        PACK,
        ledger.snapshot(anchor.project_id),
    )
    assert {issue.code for issue in result.issues} == {"SUPERSEDED_EVIDENCE"}
    ledger.close()


def test_clarification_node_requires_explicit_status_and_approved_evidence() -> None:
    with pytest.raises(ValueError, match="explicit uncertainty"):
        WorkflowNode(
            node_id="NODE-CLARIFY",
            label="Clarify behavior",
            actor_id="ACTOR-SYSTEM",
            node_type=WorkflowNodeType.CLARIFICATION,
            supporting_evidence_ids=("SOW-SCP-008",),
        )


def test_workflow_contract_rejects_disconnected_references() -> None:
    actor = WorkflowActor(actor_id="ACTOR-SYSTEM", label="System")
    nodes = (
        WorkflowNode(node_id="NODE-START", label="Start", actor_id=actor.actor_id, node_type=WorkflowNodeType.START),
        WorkflowNode(node_id="NODE-END", label="End", actor_id=actor.actor_id, node_type=WorkflowNodeType.END),
    )
    payload = {
        "workflow_id": "WF-TEST",
        "project_id": "test",
        "title": "Test",
        "anchor_hash": "0" * 64,
        "ledger_snapshot_hash": "1" * 64,
        "evidence_cutoff": "DEC-001",
        "actors": (actor,),
        "nodes": nodes,
        "edges": (WorkflowEdge(edge_id="EDGE-001", source_id="NODE-START", target_id="NODE-MISSING"),),
    }
    payload["draft_hash"] = workflow_draft_hash(payload, include_hash=False)
    with pytest.raises(ValueError, match="unknown node"):
        WorkflowDraft.model_validate(payload)


def test_structured_project_ids_and_anchor_are_stable_and_human_gated() -> None:
    project = StructuredProjectInput(
        project_name="Fictional local demo",
        approved_requirements=("A reviewer can inspect one record.",),
        constraints=("One local timezone.",),
        assumptions=("A local account exists.",),
        unresolved_questions=("How long is history retained?",),
    )
    assert structured_project_hash(project) == structured_project_hash(project)
    assert stable_local_id("CR", "same text") == stable_local_id("CR", "same text")
    with pytest.raises(ValueError, match="explicit human approval"):
        build_structured_scope_anchor(project, human_approved=False)
    first = build_structured_scope_anchor(project, human_approved=True)
    second = build_structured_scope_anchor(project, human_approved=True)
    assert first == second
    categories = {item.category for item in first.items}
    assert {EvidenceCategory.APPROVED_SCOPE, EvidenceCategory.ASSUMPTION, EvidenceCategory.UNRESOLVED_QUESTION} <= categories
