"""Ignored local project-pack materialization for the upload-first Beta."""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from spectrace.workflow import StructuredProjectInput


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:48] or "project"


def materialize_beta_project(
    project: StructuredProjectInput, storage_root: Path, session_id: str
) -> Path:
    """Write only an ignored local source pack after explicit human approval."""

    root = storage_root / f"beta-{session_id}" / _slug(project.project_name)
    root.mkdir(parents=True, exist_ok=True)
    workflow_requirements = tuple(
        f"{step.actor}: {step.action}" + (f" ({step.branch})" if step.branch else "")
        for step in project.workflow_steps
    )
    groups = (
        ("Approved scope", "SCP", (*project.approved_requirements, *workflow_requirements)),
        ("Constraints", "CON", project.constraints),
        ("Explicit exclusions", "EXC", project.exclusions),
        ("Assumptions", "ASM", project.assumptions),
        ("Unresolved questions", "QUE", project.unresolved_questions),
    )
    role_names = tuple(dict.fromkeys(step.actor for step in project.workflow_steps))
    sow = [f"# {project.project_name} Statement of Work", "", "## User roles", ""]
    if role_names:
        sow.extend(f"- **{role}:** Performs the documented approved workflow actions." for role in role_names)
    else:
        sow.append("- **User:** Uses the approved project capabilities.")
    if not any(role.lower() == "system" for role in role_names):
        sow.append("- **System:** Enforces the approved project rules.")
    sow.append("")
    for heading, code, values in groups:
        sow.extend((f"## {heading}", ""))
        sow.extend(
            f"- **SOW-{code}-{index:03d} — Approved human-reviewed item:** {value}"
            for index, value in enumerate(values, start=1)
        )
        sow.append("")
    if project.workflow_steps:
        sow.extend(("## Original approved workflow", ""))
        for index, step in enumerate(project.workflow_steps, start=1):
            branch = f" [{step.branch}]" if step.branch else ""
            sow.append(f"{index}. {step.actor}: {step.action}{branch}")
        sow.append("")
    (root / "sow.md").write_text("\n".join(sow), encoding="utf-8")

    decisions = list(project.decisions)
    if not decisions:
        from spectrace.workflow import StructuredDecision
        decisions = [
            StructuredDecision(
                effective_date=date.today(),
                text="Approve the human-reviewed project boundary recorded in the local scope anchor.",
            )
        ]
    history = [f"# {project.project_name} Approved Decision History", ""]
    for index, decision in enumerate(decisions, start=1):
        status = "APPROVED" if decision.approves_requested_capability else "APPROVED_REJECTION"
        history.extend(
            (
                f"## DEC-{index:03d} — Human-reviewed project decision",
                "",
                f"- **Date:** {decision.effective_date.isoformat()}",
                f"- **Status:** {status}",
                "- **Triggering request ID:** None; initial project review",
                f"- **Approves:** {decision.text}" if decision.approves_requested_capability else f"- **Rejects:** {decision.text}",
                "- **Supersession:** None.",
                "- **Remaining unresolved:** Refer to the approved unresolved-question list.",
                "",
            )
        )
    (root / "decisions.md").write_text("\n".join(history), encoding="utf-8")
    (root / "requests.json").write_text(json.dumps([], indent=2), encoding="utf-8")
    return root
