"""Evidence-gated workflow contracts and deterministic draft generation.

The workflow layer is intentionally provider-neutral.  It converts an explicit
approved workflow section and human-approved ledger additions into a draft, then
verifies every consequential node before any exporter may consume it.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from datetime import date
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field, field_validator, model_validator

from spectrace.advanced_models import (
    DecisionPolarity,
    EvidenceItem,
    EvidenceCategory,
    LedgerSnapshot,
    ScopeAnchor,
    TemporalStatus,
)
from spectrace.ledger import LedgerStore
from spectrace.models import EVIDENCE_ID_PATTERN, StrictModel
from spectrace.retrieval import retrieval_tokens
from spectrace.scope_anchor import resolve_anchor_at_cutoff


WORKFLOW_ID_PATTERN = re.compile(r"^WF-[A-Z0-9][A-Z0-9_-]*$")
ACTOR_ID_PATTERN = re.compile(r"^ACTOR-[A-Z0-9][A-Z0-9_-]*$")
NODE_ID_PATTERN = re.compile(r"^NODE-[A-Z0-9][A-Z0-9_-]*$")
EDGE_ID_PATTERN = re.compile(r"^EDGE-[A-Z0-9][A-Z0-9_-]*$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class WorkflowNodeType(str, Enum):
    START = "START"
    ACTION = "ACTION"
    DECISION = "DECISION"
    SYSTEM = "SYSTEM"
    ERROR = "ERROR"
    CLARIFICATION = "CLARIFICATION"
    END = "END"


class WorkflowChangeType(str, Enum):
    UNCHANGED = "UNCHANGED"
    ADDED = "ADDED"
    MODIFIED = "MODIFIED"
    REMOVED = "REMOVED"


class WorkflowActor(StrictModel):
    actor_id: str
    label: str = Field(min_length=1, max_length=80)

    @field_validator("actor_id")
    @classmethod
    def validate_actor_id(cls, value: str) -> str:
        if not ACTOR_ID_PATTERN.fullmatch(value):
            raise ValueError(f"invalid workflow actor ID: {value!r}")
        return value


class WorkflowNode(StrictModel):
    node_id: str
    label: str = Field(min_length=1, max_length=180)
    actor_id: str
    node_type: WorkflowNodeType
    supporting_evidence_ids: tuple[str, ...] = ()
    change_type: WorkflowChangeType = WorkflowChangeType.UNCHANGED
    requires_clarification: bool = False
    uncertainty: str | None = Field(default=None, max_length=500)

    @field_validator("node_id")
    @classmethod
    def validate_node_id(cls, value: str) -> str:
        if not NODE_ID_PATTERN.fullmatch(value):
            raise ValueError(f"invalid workflow node ID: {value!r}")
        return value

    @field_validator("actor_id")
    @classmethod
    def validate_actor_id(cls, value: str) -> str:
        if not ACTOR_ID_PATTERN.fullmatch(value):
            raise ValueError(f"invalid workflow actor ID: {value!r}")
        return value

    @field_validator("supporting_evidence_ids")
    @classmethod
    def validate_evidence_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("supporting evidence IDs must be unique")
        for value in values:
            if not EVIDENCE_ID_PATTERN.fullmatch(value):
                raise ValueError(f"invalid evidence ID: {value!r}")
        return values

    @model_validator(mode="after")
    def validate_uncertainty(self) -> "WorkflowNode":
        if self.node_type == WorkflowNodeType.CLARIFICATION:
            if not self.requires_clarification or not self.uncertainty:
                raise ValueError("CLARIFICATION nodes require explicit uncertainty")
        elif self.requires_clarification:
            raise ValueError("uncertain behavior must use a CLARIFICATION node")
        if self.node_type in {WorkflowNodeType.START, WorkflowNodeType.END}:
            if self.supporting_evidence_ids:
                raise ValueError("START and END nodes do not carry evidence")
        elif not self.supporting_evidence_ids:
            raise ValueError("every consequential workflow node requires evidence")
        return self


class WorkflowEdge(StrictModel):
    edge_id: str
    source_id: str
    target_id: str
    condition: str | None = Field(default=None, max_length=160)
    evidence_ids: tuple[str, ...] = ()

    @field_validator("edge_id")
    @classmethod
    def validate_edge_id(cls, value: str) -> str:
        if not EDGE_ID_PATTERN.fullmatch(value):
            raise ValueError(f"invalid workflow edge ID: {value!r}")
        return value

    @field_validator("source_id", "target_id")
    @classmethod
    def validate_node_reference(cls, value: str) -> str:
        if not NODE_ID_PATTERN.fullmatch(value):
            raise ValueError(f"invalid workflow node reference: {value!r}")
        return value

    @field_validator("evidence_ids")
    @classmethod
    def validate_evidence_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("edge evidence IDs must be unique")
        for value in values:
            if not EVIDENCE_ID_PATTERN.fullmatch(value):
                raise ValueError(f"invalid evidence ID: {value!r}")
        return values


class WorkflowDraft(StrictModel):
    workflow_id: str
    project_id: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=160)
    anchor_hash: str = Field(pattern=SHA256_PATTERN.pattern)
    ledger_snapshot_hash: str = Field(pattern=SHA256_PATTERN.pattern)
    evidence_cutoff: str
    actors: tuple[WorkflowActor, ...]
    nodes: tuple[WorkflowNode, ...]
    edges: tuple[WorkflowEdge, ...]
    draft_hash: str = Field(pattern=SHA256_PATTERN.pattern)

    @field_validator("workflow_id")
    @classmethod
    def validate_workflow_id(cls, value: str) -> str:
        if not WORKFLOW_ID_PATTERN.fullmatch(value):
            raise ValueError(f"invalid workflow ID: {value!r}")
        return value

    @model_validator(mode="after")
    def validate_structure(self) -> "WorkflowDraft":
        actor_ids = [actor.actor_id for actor in self.actors]
        node_ids = [node.node_id for node in self.nodes]
        edge_ids = [edge.edge_id for edge in self.edges]
        if len(actor_ids) != len(set(actor_ids)):
            raise ValueError("workflow actor IDs must be unique")
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("workflow node IDs must be unique")
        if len(edge_ids) != len(set(edge_ids)):
            raise ValueError("workflow edge IDs must be unique")
        known_actors, known_nodes = set(actor_ids), set(node_ids)
        if any(node.actor_id not in known_actors for node in self.nodes):
            raise ValueError("workflow node references an unknown actor")
        if any(
            edge.source_id not in known_nodes or edge.target_id not in known_nodes
            for edge in self.edges
        ):
            raise ValueError("workflow edge references an unknown node")
        if sum(node.node_type == WorkflowNodeType.START for node in self.nodes) != 1:
            raise ValueError("workflow requires exactly one START node")
        if sum(node.node_type == WorkflowNodeType.END for node in self.nodes) != 1:
            raise ValueError("workflow requires exactly one END node")
        expected = workflow_draft_hash(self, include_hash=False)
        if self.draft_hash != expected:
            raise ValueError("workflow draft hash does not match its contents")
        return self


class WorkflowVerificationIssue(StrictModel):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    node_id: str | None = None
    evidence_ids: tuple[str, ...] = ()


class WorkflowVerificationResult(StrictModel):
    passed: bool
    draft_hash: str = Field(pattern=SHA256_PATTERN.pattern)
    verified_node_ids: tuple[str, ...] = ()
    approved_evidence_ids: tuple[str, ...] = ()
    issues: tuple[WorkflowVerificationIssue, ...] = ()

    @model_validator(mode="after")
    def validate_pass_state(self) -> "WorkflowVerificationResult":
        if self.passed == bool(self.issues):
            raise ValueError("passed must be true exactly when issues are empty")
        return self


class StructuredDecision(StrictModel):
    effective_date: date
    text: str = Field(min_length=1, max_length=1000)
    approves_requested_capability: bool = True


class StructuredWorkflowStep(StrictModel):
    """One explicitly documented, human-approved business workflow step."""

    actor: str = Field(min_length=1, max_length=120)
    action: str = Field(min_length=1, max_length=1000)
    branch: str | None = Field(default=None, max_length=500)

    @field_validator("actor", "action")
    @classmethod
    def validate_workflow_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("workflow step needs both an actor and an action")
        return cleaned


class StructuredProjectInput(StrictModel):
    project_name: str = Field(min_length=1, max_length=120)
    approved_requirements: tuple[str, ...] = Field(min_length=1)
    constraints: tuple[str, ...] = ()
    exclusions: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    unresolved_questions: tuple[str, ...] = ()
    decisions: tuple[StructuredDecision, ...] = ()
    workflow_steps: tuple[StructuredWorkflowStep, ...] = ()

    @field_validator(
        "approved_requirements",
        "constraints",
        "exclusions",
        "assumptions",
        "unresolved_questions",
    )
    @classmethod
    def validate_lines(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(value.strip() for value in values)
        if any(not value for value in cleaned):
            raise ValueError("structured project lines cannot be empty")
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("structured project lines must be unique")
        return cleaned


def _canonical(value: object) -> str:
    def normalize(item: object) -> object:
        if isinstance(item, BaseModel):
            return item.model_dump(mode="json")
        if isinstance(item, Enum):
            return item.value
        if isinstance(item, tuple):
            return [normalize(child) for child in item]
        if isinstance(item, list):
            return [normalize(child) for child in item]
        if isinstance(item, dict):
            return {str(key): normalize(child) for key, child in item.items()}
        return item

    return json.dumps(normalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def workflow_draft_hash(draft: WorkflowDraft | dict[str, object], *, include_hash: bool = True) -> str:
    payload = draft.model_dump(mode="json") if isinstance(draft, WorkflowDraft) else dict(draft)
    if not include_hash:
        payload.pop("draft_hash", None)
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _slug(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Z0-9]+", "-", value.upper()).strip("-")
    return cleaned[:48] or fallback


def stable_local_id(prefix: str, value: str, *, length: int = 10) -> str:
    """Return a reproducible local ID without exposing the original text."""

    digest = hashlib.sha256(value.strip().encode("utf-8")).hexdigest()[:length].upper()
    return f"{prefix}-{digest}"


def structured_project_hash(project: StructuredProjectInput) -> str:
    return hashlib.sha256(
        _canonical(project.model_dump(mode="json")).encode("utf-8")
    ).hexdigest()


def build_structured_scope_anchor(
    project: StructuredProjectInput,
    *,
    human_approved: bool,
) -> ScopeAnchor:
    """Build a local anchor only after the reviewer explicitly approves the form."""

    if not human_approved:
        raise ValueError("structured scope anchor requires explicit human approval")
    project_hash = structured_project_hash(project)
    project_id = f"local-{_slug(project.project_name, 'PROJECT').lower()}-{project_hash[:8]}"
    items: list[EvidenceItem] = []
    groups = (
        ("SCP", EvidenceCategory.APPROVED_SCOPE, project.approved_requirements),
        ("CON", EvidenceCategory.CONSTRAINT, project.constraints),
        ("EXC", EvidenceCategory.EXCLUSION, project.exclusions),
        ("ASM", EvidenceCategory.ASSUMPTION, project.assumptions),
        ("QUE", EvidenceCategory.UNRESOLVED_QUESTION, project.unresolved_questions),
    )
    for code, category, values in groups:
        for index, text in enumerate(values, start=1):
            evidence_id = f"SOW-{code}-{index:03d}"
            items.append(
                EvidenceItem(
                    evidence_id=evidence_id,
                    category=category,
                    source_text=text,
                    source_path="structured-form",
                    source_location=f"{category.value}:{index}",
                    source_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                )
            )
    for index, step in enumerate(project.workflow_steps, start=1):
        text = f"{step.actor}: {step.action}"
        if step.branch:
            text += f" ({step.branch})"
        items.append(
            EvidenceItem(
                evidence_id=f"SOW-WFL-{index:03d}",
                category=EvidenceCategory.APPROVED_SCOPE,
                source_text=text,
                source_path="structured-form",
                source_location=f"APPROVED_WORKFLOW:{index}",
                source_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                actor_terms=(step.actor.lower(),),
            )
        )
    for index, decision in enumerate(project.decisions, start=1):
        text = decision.text
        items.append(
            EvidenceItem(
                evidence_id=f"DEC-{index:03d}",
                category=EvidenceCategory.DECISION,
                source_text=text,
                source_path="structured-form",
                source_location=f"DECISION:{index}",
                source_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                effective_date=decision.effective_date,
                decision_polarity=(
                    DecisionPolarity.APPROVES
                    if decision.approves_requested_capability
                    else DecisionPolarity.REJECTS
                ),
            )
        )
    anchor_payload = {
        "project_id": project_id,
        "version": "structured-v1",
        "source_root": "local-structured-project",
        "items": tuple(items),
        "supersession_edges": (),
    }
    anchor_hash = hashlib.sha256(_canonical(anchor_payload).encode("utf-8")).hexdigest()
    return ScopeAnchor(**anchor_payload, anchor_hash=anchor_hash)


def _workflow_lines(project_pack_path: str | Path) -> tuple[str, ...]:
    text = (Path(project_pack_path) / "sow.md").read_text(encoding="utf-8")
    match = re.search(
        r"^## Original approved workflow\s*$([\s\S]*?)(?=^## |\Z)",
        text,
        re.MULTILINE,
    )
    if not match:
        return ()
    return tuple(
        " ".join(item.split())
        for item in re.findall(r"^\d+\.\s+([^\n]*(?:\n {2,}[^\n]*)*)", match.group(1), re.MULTILINE)
        if item.strip()
    )


def _best_evidence(step: str, approved_items: Iterable[object]) -> object | None:
    step_terms = set(retrieval_tokens(step))
    ranked: list[tuple[int, str, object]] = []
    for item in approved_items:
        overlap = len(step_terms & set(retrieval_tokens(item.source_text)))
        if overlap:
            ranked.append((overlap, item.evidence_id, item))
    return max(ranked, key=lambda row: (row[0], row[1]))[2] if ranked else None


def _actor_for_step(step: str, actors: tuple[WorkflowActor, ...]) -> str:
    normalized = step.lower()
    matches = [actor for actor in actors if actor.label.lower() in normalized]
    if matches:
        return sorted(matches, key=lambda actor: (-len(actor.label), actor.actor_id))[0].actor_id
    return "ACTOR-SYSTEM"


def _node_type(step: str, evidence_text: str) -> tuple[WorkflowNodeType, str | None]:
    text = f"{step} {evidence_text}".lower()
    if "unresolved" in text or "later defined" in text:
        return WorkflowNodeType.CLARIFICATION, "Approved behavior depends on an unresolved rule."
    if any(term in text for term in ("invalid", "rejected", "decline", "failure", "error")):
        return WorkflowNodeType.ERROR, None
    if any(term in text for term in ("when ", " if ", "checks ", "validat")):
        return WorkflowNodeType.DECISION, None
    if "system" in step.lower():
        return WorkflowNodeType.SYSTEM, None
    return WorkflowNodeType.ACTION, None


def generate_workflow_draft(
    anchor: ScopeAnchor,
    project_pack_path: str | Path,
    ledger: LedgerStore,
    *,
    evidence_cutoff: str,
    title: str | None = None,
) -> WorkflowDraft:
    """Build a deterministic draft from explicit approved workflow prose and ledger state."""

    snapshot = ledger.snapshot(anchor.project_id)
    resolved = resolve_anchor_at_cutoff(anchor, project_pack_path, evidence_cutoff)
    approved_ids = set(snapshot.approved_evidence_ids)
    approved_items = tuple(
        item
        for item in resolved
        if item.evidence_id in approved_ids
        and item.category in {EvidenceCategory.APPROVED_SCOPE, EvidenceCategory.DECISION}
        and item.temporal_status == TemporalStatus.CURRENT
    )
    labels = sorted(
        {actor for item in approved_items for actor in item.actor_terms} | {"system"}
    )
    actors = tuple(
        WorkflowActor(actor_id=f"ACTOR-{_slug(label, 'SYSTEM')}", label=label.title())
        for label in labels
    )
    nodes: list[WorkflowNode] = [
        WorkflowNode(
            node_id="NODE-START",
            label="Start",
            actor_id="ACTOR-SYSTEM",
            node_type=WorkflowNodeType.START,
        )
    ]
    for index, step in enumerate(_workflow_lines(project_pack_path), start=1):
        evidence = _best_evidence(step, approved_items)
        if evidence is None:
            continue
        node_type, uncertainty = _node_type(step, evidence.source_text)
        nodes.append(
            WorkflowNode(
                node_id=f"NODE-BASE-{index:03d}",
                label=step.rstrip("."),
                actor_id=_actor_for_step(step, actors),
                node_type=node_type,
                supporting_evidence_ids=(evidence.evidence_id,),
                requires_clarification=node_type == WorkflowNodeType.CLARIFICATION,
                uncertainty=uncertainty,
            )
        )
    for index, record in enumerate(ledger.approved_scope_change_records(anchor.project_id), start=1):
        # The human-authored decision is the authority for the added path.  Its
        # cited source evidence remains available in ledger/package traceability,
        # but may be a boundary or partially superseded item and must not be
        # misrepresented as independently approving the new behavior.
        evidence_ids = (record["decision_id"],)
        nodes.append(
            WorkflowNode(
                node_id=f"NODE-CHANGE-{index:03d}",
                label=record["decision_text"],
                actor_id=_actor_for_step(record["decision_text"], actors),
                node_type=WorkflowNodeType.ACTION,
                supporting_evidence_ids=evidence_ids,
                change_type=WorkflowChangeType.ADDED,
            )
        )
    nodes.append(
        WorkflowNode(
            node_id="NODE-END",
            label="End",
            actor_id="ACTOR-SYSTEM",
            node_type=WorkflowNodeType.END,
        )
    )
    edges = tuple(
        WorkflowEdge(
            edge_id=f"EDGE-{index:03d}",
            source_id=source.node_id,
            target_id=target.node_id,
            evidence_ids=target.supporting_evidence_ids,
            condition=("Clarification resolved" if source.node_type == WorkflowNodeType.CLARIFICATION else None),
        )
        for index, (source, target) in enumerate(zip(nodes, nodes[1:]), start=1)
    )
    payload: dict[str, object] = {
        "workflow_id": f"WF-{_slug(anchor.project_id, 'PROJECT')}",
        "project_id": anchor.project_id,
        "title": title or f"{anchor.project_id.title()} approved workflow",
        "anchor_hash": anchor.anchor_hash,
        "ledger_snapshot_hash": snapshot.snapshot_hash,
        "evidence_cutoff": evidence_cutoff,
        "actors": actors,
        "nodes": tuple(nodes),
        "edges": edges,
    }
    payload["draft_hash"] = workflow_draft_hash(payload, include_hash=False)
    return WorkflowDraft.model_validate(payload)


def verify_workflow_draft(
    draft: WorkflowDraft,
    anchor: ScopeAnchor,
    project_pack_path: str | Path,
    snapshot: LedgerSnapshot,
) -> WorkflowVerificationResult:
    """Fail closed when a draft uses unapproved, inactive, or non-authoritative evidence."""

    issues: list[WorkflowVerificationIssue] = []
    if draft.anchor_hash != anchor.anchor_hash:
        issues.append(WorkflowVerificationIssue(code="ANCHOR_HASH_MISMATCH", message="Draft was built from another scope anchor."))
    if draft.ledger_snapshot_hash != snapshot.snapshot_hash:
        issues.append(WorkflowVerificationIssue(code="LEDGER_HASH_MISMATCH", message="Approved ledger changed after the draft was built."))
    resolved = {item.evidence_id: item for item in resolve_anchor_at_cutoff(anchor, project_pack_path, draft.evidence_cutoff)}
    approved = set(snapshot.approved_evidence_ids)
    ledger_decisions = approved - set(resolved)
    verified: list[str] = []
    for node in draft.nodes:
        if node.node_type in {WorkflowNodeType.START, WorkflowNodeType.END}:
            continue
        node_ok = True
        for evidence_id in node.supporting_evidence_ids:
            item = resolved.get(evidence_id)
            if evidence_id not in approved:
                code, message = "UNAPPROVED_EVIDENCE", "Workflow evidence is not human-approved."
            elif item is None and evidence_id not in ledger_decisions:
                code, message = "UNKNOWN_EVIDENCE", "Workflow evidence does not exist."
            elif item is not None and item.temporal_status == TemporalStatus.FUTURE:
                code, message = "FUTURE_EVIDENCE", "Future evidence cannot support a workflow."
            elif item is not None and item.temporal_status in {TemporalStatus.SUPERSEDED, TemporalStatus.PARTIALLY_SUPERSEDED}:
                code, message = "SUPERSEDED_EVIDENCE", "Superseded evidence cannot support a workflow node without facet proof."
            elif item is not None and item.category in {EvidenceCategory.ASSUMPTION, EvidenceCategory.UNRESOLVED_QUESTION}:
                code, message = "NONAUTHORITATIVE_EVIDENCE", "Assumptions and unresolved questions cannot approve workflow behavior."
            else:
                continue
            node_ok = False
            issues.append(WorkflowVerificationIssue(code=code, message=message, node_id=node.node_id, evidence_ids=(evidence_id,)))
        if node_ok:
            verified.append(node.node_id)
    return WorkflowVerificationResult(
        passed=not issues,
        draft_hash=draft.draft_hash,
        verified_node_ids=tuple(verified),
        approved_evidence_ids=tuple(sorted(approved)),
        issues=tuple(issues),
    )


def require_verified_workflow(
    draft: WorkflowDraft, verification: WorkflowVerificationResult
) -> None:
    if verification.draft_hash != draft.draft_hash or not verification.passed:
        raise ValueError("workflow export requires a matching passed verification result")
