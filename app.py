"""SpecTrace Streamlit product experience. Run with ``streamlit run app.py``."""

from __future__ import annotations

import json
import html
import hashlib
import re
import time
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import streamlit as st

from spectrace.advanced import new_run_state, resume_after_human_review, run_until_human_review
from spectrace.advanced_models import (
    AdvancedModelOutput,
    AdvancedRunState,
    AgentNode,
    HumanAction,
    HumanDecisionPayload,
    HumanReview,
    LedgerEntryEffect,
)
from spectrace.beta_project import materialize_beta_project
from spectrace.config import ConfigurationError, load_llm_settings, safe_settings_summary
from spectrace.curated_workflow import load_studiolane_workflows
from spectrace.ledger import LedgerStore
from spectrace.llm import GoogleGenAIClient, generate_structured_with_retry
from spectrace.models import Classification, IncomingRequest
from spectrace.presentation import semantic_impacts
from spectrace.project_documents import (
    CandidateScopeExtraction,
    candidate_from_structured_markdown,
    extract_explicit_approved_workflow,
    extract_document,
    friendly_validation_messages,
    render_candidate_prompt,
)
from spectrace.project_session import (
    FailureCategory,
    ProjectSession,
    ProjectSourceMode,
    document_identities,
    new_project_session,
    safe_diagnostic,
)
from spectrace.scope_anchor import build_scope_anchor
from spectrace.workflow import (
    StructuredDecision,
    StructuredProjectInput,
    build_structured_scope_anchor,
    generate_workflow_draft,
    stable_local_id,
    verify_workflow_draft,
)
from spectrace.workflow_export import (
    DRAWIO_MIME_TYPE,
    export_drawio_bundle,
    export_mermaid,
    mermaid_preview_html,
    workflow_overview_html,
)


ROOT = Path(__file__).resolve().parent
DEMO_PACK = ROOT / "data" / "synthetic" / "demo_project"
COMPARISON_PATH = ROOT / "results" / "comparison_v1" / "comparison.json"
UI_ROOT = ROOT / ".spectrace_ui"
ADVANCED_V1_ROOT = ROOT / "results" / "advanced_v1"
RECORDED_REPLAY_CASES = ("CR-004", "CR-008", "CR-010")

CLASSIFICATION_LABELS = {
    "IN_SCOPE": "In Scope",
    "AMBIGUOUS": "Needs Clarification",
    "OUT_OF_SCOPE": "Out of Scope",
    "CONTRADICTS_APPROVED_DECISION": "Conflicts with Approved Decision",
    "POTENTIAL_SCOPE_CHANGE": "Potential Scope Change",
}
DRIFT_LABELS = {
    "NONE": "No pattern detected",
    "RELATED": "Related requests detected",
    "EMERGING": "Emerging scope pattern",
    "SUBSYSTEM": "New subsystem pattern",
}
REVIEW_LABELS = {
    "APPROVE": "Approve Recommendation",
    "OVERRIDE": "Change Decision",
    "NEEDS_CLARIFICATION": "Ask Client for Clarification",
    "DEFER": "Defer Request",
}
STATUS_LABELS = {
    "AWAITING_HUMAN_REVIEW": "Waiting for your decision",
    "COMPLETE": "Decision recorded",
    "FAILED": "Analysis stopped safely",
}

PROJECT_SESSION_KEYS = (
    "beta_candidate", "beta_documents", "beta_workflow_candidate",
    "beta_workflow_approved", "beta_project", "beta_pack", "beta_anchor",
    "beta_navigation", "beta_pending_project_name", "analysis_state",
    "analysis_database", "agent_node_states", "review_message", "review_trail",
    "extraction_error", "run_error", "beta_requirements", "beta_constraints",
    "beta_exclusions", "beta_assumptions", "beta_questions", "beta_decisions",
    "synthetic_load_message",
)


def _clear_project_state(*, include_name: bool = False) -> None:
    for key in PROJECT_SESSION_KEYS:
        st.session_state.pop(key, None)
    if include_name:
        st.session_state.pop("beta_project_name", None)
    st.session_state.pop("project_session", None)


def _replace_project_session(session: ProjectSession) -> None:
    _clear_project_state(include_name=True)
    st.session_state.project_session = session
    st.session_state.beta_project_name = session.project_name


def _load_synthetic_example_state() -> None:
    previous_name = st.session_state.get("beta_project_name", "").strip()
    sample_path = ROOT / "assets" / "CampusFlow_Synthetic_Project.md"
    document = extract_document(sample_path.name, sample_path.read_bytes())
    candidate = candidate_from_structured_markdown(document, "CampusFlow")
    workflow = extract_explicit_approved_workflow((document,))
    session = new_project_session(
        "CampusFlow", ProjectSourceMode.SYNTHETIC_EXAMPLE, UI_ROOT
    ).model_copy(
        update={
            "uploaded_documents": document_identities((document,)),
            "candidate_anchor": candidate,
            "candidate_workflow": workflow,
        }
    )
    _replace_project_session(session)
    st.session_state.beta_candidate = candidate
    st.session_state.beta_documents = (document,)
    st.session_state.beta_workflow_candidate = workflow
    _set_candidate_fields(candidate)
    st.session_state.synthetic_load_message = (
        "Loaded fictional CampusFlow example. Unsaved Harbor Basket input was cleared."
        if previous_name and previous_name != "CampusFlow"
        else "Loaded fictional CampusFlow example."
    )


def _analysis_failure_stage(state: AdvancedRunState, exc: Exception) -> str:
    from spectrace.ledger import LedgerError
    from spectrace.llm import StructuredOutputError

    if isinstance(exc, LedgerError):
        return "ledger access"
    if isinstance(exc, StructuredOutputError):
        return "structured parsing"
    return {
        AgentNode.LOAD_SCOPE_ANCHOR: "evidence retrieval",
        AgentNode.RETRIEVE_EVIDENCE: "evidence retrieval",
        AgentNode.ASSESS_SUFFICIENCY: "ambiguity assessment",
        AgentNode.CHECK_CONTRADICTIONS: "contradiction assessment",
        AgentNode.CLASSIFY_REQUEST: "model generation",
        AgentNode.VERIFY_ASSESSMENT: "verification",
    }.get(state.current_node, "agent orchestration")


def _render_safe_diagnostic(diagnostic: Any, *, extraction: bool) -> None:
    if extraction:
        if diagnostic.category == FailureCategory.PROVIDER_QUOTA_OR_RATE_LIMIT:
            st.error(
                "Gemini’s free-tier request limit was reached. Your document was read "
                "successfully, but model extraction could not run. Wait before retrying "
                "or use the fictional example."
            )
        else:
            st.error(
                "SpecTrace could read the document but could not create a valid structured scope."
                if diagnostic.provider_call_occurred
                else "SpecTrace could not prepare the uploaded document for extraction."
            )
        document_outcome = "Successful" if diagnostic.provider_call_occurred else "Not completed"
        st.html(
            '<div class="extraction-outcomes" aria-label="Extraction outcome">'
            f'<div><span>Document reading</span><strong>{document_outcome}</strong></div>'
            '<div><span>Candidate extraction</span><strong>Not completed</strong></div>'
            '<div><span>Project approval / saving</span><strong>Not performed</strong></div>'
            '</div>'
        )
    else:
        st.error(
            f"Analysis stopped during {diagnostic.stage}. Approved scope and decision memory were not changed."
        )
        st.caption("Review the safe diagnostic, correct the cause, and retry the request.")
    with st.expander("Developer details"):
        details = {
            "Category": diagnostic.category.value,
            "Stage": diagnostic.stage,
            "Exception type": diagnostic.exception_type,
            "Status": diagnostic.status_code or diagnostic.provider_status or "Not available",
            "Safe message": diagnostic.sanitized_message,
            "Retryable": "Yes" if diagnostic.retryable else "No",
            "Provider call occurred": "Yes" if diagnostic.provider_call_occurred else "No",
            "Attempt": diagnostic.attempt_number or "Not available",
            "Project session": diagnostic.project_session_id,
            "Timestamp": diagnostic.timestamp.isoformat(),
        }
        for label, value in details.items():
            if value is not None:
                st.markdown(f"**{label}:** {html.escape(str(value))}")


def _render_missing_provider_diagnostic() -> None:
    with st.expander("Developer details"):
        st.caption("Detailed provider diagnostics were unavailable for this attempt.")


def _friendly_classification(value: str | Classification) -> str:
    raw = value.value if isinstance(value, Classification) else value
    return CLASSIFICATION_LABELS.get(raw, raw.replace("_", " ").title())


def _friendly_date(value: str | date) -> str:
    parsed = date.fromisoformat(value[:10]) if isinstance(value, str) else value
    return parsed.strftime("%d %B %Y").lstrip("0")


def _friendly_clause(value: str) -> str:
    value = value.strip()
    if value.startswith("## DEC-"):
        match = re.search(
            r"^- \*\*(?:Approves|Rejects):\*\*\s+([\s\S]*?)(?=^- \*\*|\Z)",
            value,
            re.MULTILINE,
        )
        value = " ".join(match.group(1).split()) if match else "Approved project decision."
    value = re.sub(r"^- \*\*(?:SOW-[A-Z]+-\d+)[^*]*:\*\*\s*", "", value)
    value = re.sub(r"\b(?:SOW-[A-Z]+-\d+|DEC-\d+|CR-\d+)\b", "approved evidence", value)
    return value.strip()


def _friendly_event_summary(value: str) -> str:
    """Translate terse backend trajectory summaries for the BA-facing canvas."""

    friendly = value
    for internal, display in CLASSIFICATION_LABELS.items():
        friendly = re.sub(rf"\b{re.escape(internal)}\b", display, friendly)
    for internal, display in DRIFT_LABELS.items():
        friendly = re.sub(rf"\b{re.escape(internal)}\b", display, friendly)
    for internal, display in REVIEW_LABELS.items():
        friendly = re.sub(rf"\b{re.escape(internal)}\b", display, friendly)
    return (
        friendly.replace("Drift severity", "Cumulative scope growth")
        .replace("Assessment verification", "Evidence check")
        .replace("Classification sufficiency", "Request clarity")
        .replace("cutoff-safe", "currently approved")
        .replace("Advanced run", "SpecTrace analysis")
    )


def load_curated_comparison(path: str | Path = COMPARISON_PATH) -> dict[str, Any]:
    """Load only the committed curated comparison; UI metrics are never literals."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("status") != "CURATED_COMPARISON":
        raise ValueError("comparison artifact is not curated")
    return payload


def load_guided_demo_data(root: str | Path = ROOT) -> dict[str, Any]:
    """Load synthetic inputs and recorded Advanced V1 displays without a network call."""

    base = Path(root)
    requests = json.loads((base / "data/synthetic/demo_project/requests.json").read_text(encoding="utf-8"))
    assessments = json.loads((base / "results/advanced_v1/assessments.json").read_text(encoding="utf-8"))
    trajectories = {
        request["request_id"]: json.loads(
            (base / f"results/advanced_v1/trajectories/{request['request_id']}.json").read_text(encoding="utf-8")
        )
        for request in requests
    }
    return {
        "requests": requests,
        "assessments": {item["request_id"]: item for item in assessments},
        "trajectories": trajectories,
        "comparison": load_curated_comparison(base / "results/comparison_v1/comparison.json"),
    }


def load_recorded_advanced_v1(
    request_id: str, root: str | Path = ADVANCED_V1_ROOT
) -> dict[str, Any]:
    """Load one hash-verified replay exclusively from curated Advanced V1 files."""

    if request_id not in RECORDED_REPLAY_CASES:
        raise ValueError("Recorded replay is available only for the verified demo cases.")
    replay_root = Path(root).resolve()

    def read_json(relative: str) -> Any:
        path = (replay_root / relative).resolve()
        if path.parent != replay_root and replay_root not in path.parents:
            raise ValueError("Recorded replay path escaped Advanced V1.")
        return json.loads(path.read_text(encoding="utf-8"))

    curation = read_json("curation_manifest.json")
    if curation.get("result_id") != "advanced_v1" or curation.get("status") != "CURATED_ADVANCED":
        raise ValueError("Advanced V1 is not marked as curated.")
    expected_hashes = curation["source_copy_validation"]["artifact_sha256"]

    def verified_json(relative: str) -> Any:
        path = (replay_root / relative).resolve()
        expected = expected_hashes.get(relative)
        if not expected or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f"Recorded Advanced V1 artifact failed verification: {relative}")
        return read_json(relative)

    paused = verified_json(f"paused_states/{request_id}.json")
    trajectory = verified_json(f"trajectories/{request_id}.json")
    retrieval = verified_json(f"retrieved_evidence/{request_id}.json")
    assessments = verified_json("assessments.json")
    predictions = verified_json("predictions.json")
    reviews = verified_json("human_reviews.json")
    snapshots = verified_json("ledger_snapshots.json")
    assessment = next(item for item in assessments if item["request_id"] == request_id)
    prediction = next(item for item in predictions if item["request_id"] == request_id)
    review = next(item for item in reviews if item["review"]["request_id"] == request_id)
    case_snapshots = tuple(item for item in snapshots if item["request_id"] == request_id)
    if not (
        paused["request"]["request_id"]
        == assessment["request_id"]
        == prediction["request_id"]
        == review["review"]["request_id"]
    ):
        raise ValueError("Recorded Advanced V1 request identities do not match.")
    if paused["assessment"]["classification"] != prediction["classification"]:
        raise ValueError("Recorded Advanced V1 classifications do not match.")
    return {
        "request_id": request_id,
        "paused": paused,
        "trajectory": trajectory,
        "retrieval": retrieval,
        "assessment": assessment,
        "prediction": prediction,
        "review": review,
        "ledger_snapshots": case_snapshots,
        "curation": curation,
    }


def _lines(value: str) -> tuple[str, ...]:
    return tuple(line.strip() for line in value.splitlines() if line.strip())


def _structured_decisions(value: str) -> tuple[StructuredDecision, ...]:
    decisions = []
    for line in _lines(value):
        if "|" not in line:
            raise ValueError("decision date format")
        date_text, decision_text = (part.strip() for part in line.split("|", 1))
        if not decision_text:
            raise ValueError("decision text is empty")
        try:
            effective_date = date.fromisoformat(date_text)
        except ValueError as exc:
            raise ValueError("decision date format") from exc
        decisions.append(StructuredDecision(effective_date=effective_date, text=decision_text))
    return tuple(decisions)


def extract_candidate_scope(
    documents: tuple[Any, ...], project_name: str, client: Any
) -> CandidateScopeExtraction:
    """Injectable provider boundary used by the UI and offline integration tests."""

    return generate_structured_with_retry(
        client,
        render_candidate_prompt(documents, project_name),
        CandidateScopeExtraction,
        max_attempts=2,
    ).output


def _session_database() -> Path:
    UI_ROOT.mkdir(exist_ok=True)
    project_session = st.session_state.get("project_session")
    if project_session and project_session.source_mode != ProjectSourceMode.GUIDED_DEMO:
        return Path(project_session.ledger_database_path)
    session_id = st.session_state.setdefault("ui_session_id", uuid.uuid4().hex)
    prefix = "beta" if st.session_state.get("experience_mode") == "New Project — Beta" else "demo"
    return UI_ROOT / f"{prefix}-{session_id}.sqlite"


def _safe_error(exc: Exception) -> str:
    text = str(exc).lower()
    if "date" in text:
        return "Decision date must use YYYY-MM-DD."
    if "project" in text and ("short" in text or "at least" in text or "empty" in text):
        return "Enter a project name."
    if "requirement" in text and ("short" in text or "at least" in text or "empty" in text):
        return "Add at least one approved requirement."
    return "SpecTrace could not complete this step. Review the information and try again; no project decision was recorded."


def workflow_change_controls(view: str, has_approved_update: bool) -> tuple[bool, str | None]:
    """Return highlight availability and its business-facing disabled reason."""

    if not has_approved_update:
        return False, "No approved workflow change exists yet."
    if view != "Updated Approved Workflow":
        return False, "Select Updated workflow to highlight changes."
    return True, None


def _json_download(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)


def _markdown_package(state: AdvancedRunState) -> str:
    package = state.change_package
    if package is None:
        return ""
    lines = [f"# SpecTrace {'review memo' if package.is_review_memo else 'change package'}", "", package.summary, ""]
    for title, values in (
        ("Supporting evidence", package.supporting_evidence_ids),
        ("Conflicting evidence", package.conflicting_evidence_ids),
        ("Added requirements", package.added_requirements),
        ("Dependencies", package.dependencies),
        ("Open questions", package.open_questions),
        ("Non-goals", package.non_goals),
    ):
        if values:
            lines.extend([f"## {title}", "", *(f"- {value}" for value in values), ""])
    return "\n".join(lines)


def _render_header() -> None:
    styles = (ROOT / "assets" / "styles.css").read_text(encoding="utf-8")
    logo = (ROOT / "assets" / "spectrace-monogram.svg").read_text(encoding="utf-8")
    try:
        load_llm_settings()
        model_pill = '<span class="pill blue">Live Analysis Ready</span>'
    except ConfigurationError:
        model_pill = '<span class="pill">Offline Demo</span>'
    st.html(
        f"""<style>{styles}</style>
        <div class="spec-hero">
          <div class="spec-brand"><div class="spec-logo" aria-label="SpecTrace">{logo}</div><div><h1>SpecTrace</h1>
          <p>Requirements and Scope Intelligence</p></div></div>
          <div class="pill-row"><span class="pill blue">Synthetic Guided Demo</span>
          <span class="pill">39 Evidence Items</span><span class="pill green">Anchor Approved</span>
          {model_pill}</div>
        </div>
        <div class="privacy-note">Synthetic project only · Human approval is mandatory · No automatic client communication, estimate, or legal conclusion.</div>
        """
    )


def _audit_details(anchor: Any, state: AdvancedRunState | None = None) -> None:
    """Keep reproducibility evidence available without leading the business flow."""

    anchor_hash = anchor.anchor_hash
    ledger_hash = state.pause_snapshot_hash if state else None
    short_anchor = f"{anchor_hash[:8]}…{anchor_hash[-6:]}"
    short_ledger = f"{ledger_hash[:8]}…{ledger_hash[-6:]}" if ledger_hash else "Not created"
    st.html(
        '<div class="fingerprints" title="This fingerprint proves which exact scope and decision history produced the recommendation.">'
        f'<span>Scope version <code>{html.escape(short_anchor)}</code></span>'
        f'<span>Decision-ledger version <code>{html.escape(short_ledger)}</code></span></div>'
    )
    with st.expander("Audit & export"):
        st.markdown("**Audit details**")
        st.caption("Full fingerprints and run identifiers for reproducibility review.")
        st.markdown("Scope anchor SHA-256")
        st.code(anchor_hash, language=None)
        if ledger_hash:
            st.markdown("Decision ledger SHA-256")
            st.code(ledger_hash, language=None)
        if state:
            st.markdown(f"Run identifier: `{state.run_id}`")
            if state.prompt_hash:
                st.markdown("Prompt SHA-256")
                st.code(state.prompt_hash, language=None)
            if state.assembled_prompt_hash:
                st.markdown("Assembled prompt SHA-256")
                st.code(state.assembled_prompt_hash, language=None)


def _scope_summary(anchor: Any) -> None:
    counts: dict[str, int] = {}
    for item in anchor.items:
        counts[item.category.value] = counts.get(item.category.value, 0) + 1
    values = (
        ("Approved Scope", counts.get("APPROVED_SCOPE", 0)),
        ("Constraints", counts.get("CONSTRAINT", 0)),
        ("Exclusions", counts.get("EXCLUSION", 0)),
        ("Decisions", counts.get("DECISION", 0)),
        ("Open Questions", counts.get("UNRESOLVED_QUESTION", 0)),
    )
    st.markdown(
        '<div class="project-strip">' + "".join(
            f'<div class="project-metric"><span>{label}</span><strong>{value}</strong></div>'
            for label, value in values
        ) + "</div>",
        unsafe_allow_html=True,
    )
    st.caption("Approved project evidence is ready for scope analysis.")


def _current_anchor() -> Any:
    if st.session_state.get("experience_mode") == "New Project — Beta":
        return st.session_state.get("beta_anchor")
    return build_scope_anchor(DEMO_PACK)


def _evidence_label(evidence_id: str) -> str:
    anchor = _current_anchor()
    if not anchor:
        return "Approved evidence"
    item = next((value for value in anchor.items if value.evidence_id == evidence_id), None)
    if not item:
        return "Approved evidence"
    document = "Decision history" if item.source_path == "decisions.md" else "Project scope"
    location = item.source_location.split(":")[-1].replace("-", " ")
    return f"{document} · {location}"


def _evidence_trail(
    evidence_ids: tuple[str, ...] | list[str],
    *,
    recommendation: str | None = None,
    human_decision: str | None = None,
    reviewer_note: str | None = None,
    decision_date: str | date | None = None,
) -> None:
    anchor = _current_anchor()
    evidence = {item.evidence_id: item for item in anchor.items}
    with st.expander("Evidence & Decision Trail"):
        if decision_date:
            st.caption(f"Decision date · {_friendly_date(decision_date)}")
        for evidence_id in evidence_ids:
            item = evidence.get(evidence_id)
            if item:
                st.markdown(
                    f"**{Path(item.source_path).stem.replace('_', ' ').title()} · "
                    f"{item.source_location.split(':')[-1].replace('-', ' ')}**"
                )
                st.markdown(f"> {_friendly_clause(item.source_text)}")
        if recommendation:
            st.markdown(f"**Agent recommendation**  \n{recommendation}")
        if human_decision:
            st.markdown(f"**Human decision**  \n{human_decision}")
        if reviewer_note:
            st.markdown(f"**Reviewer note**  \n{reviewer_note}")


_CANVAS_GROUPS = (
    (
        "Client Request Trigger", "Receives the selected request and approved project context.", "◇",
        (AgentNode.LOAD_SCOPE_ANCHOR,), ("Scope Anchor",),
    ),
    (
        "Scope Intelligence", "Retrieves evidence, checks clarity and classifies the request.", "◎",
        (
            AgentNode.RETRIEVE_EVIDENCE, AgentNode.ASSESS_SUFFICIENCY,
            AgentNode.CHECK_CONTRADICTIONS, AgentNode.CLASSIFY_REQUEST,
        ),
        ("Evidence Retriever", "Ambiguity Check", "Contradiction Check", "Gemini LLM"),
    ),
    (
        "Decision & Drift Analysis", "Compares prior decisions and cumulative scope growth.", "⌁",
        (AgentNode.CALCULATE_CUMULATIVE_DRIFT, AgentNode.PREPARE_RECOMMENDATION),
        ("Approved Decision Memory", "Cumulative Drift Tool", "SQLite Ledger"),
    ),
    (
        "Evidence Verification", "Checks that every consequential claim is supported and current.", "✓",
        (AgentNode.VERIFY_ASSESSMENT,), ("Citation Validator", "Temporal Evidence Check"),
    ),
    (
        "Human Review", "Pauses before any approved project memory can change.", "Ⅱ",
        (AgentNode.AWAIT_HUMAN_REVIEW, AgentNode.APPLY_HUMAN_DECISION), ("Human Decision", "SQLite Ledger"),
    ),
    (
        "Workflow Update", "Builds the approved change package and workflow outputs.", "↗",
        (AgentNode.BUILD_CHANGE_IMPACT_PACKAGE, AgentNode.COMPLETE),
        ("Change Package", "Workflow Generator", "Mermaid / Draw.io Export"),
    ),
)


def _initial_node_states() -> dict[str, dict[str, Any]]:
    return {node.value: {"status": "WAITING", "event": None} for node in AgentNode}


def _block_downstream_nodes(states: dict[str, dict[str, Any]], failed_node: AgentNode) -> None:
    seen_failure = False
    for node in AgentNode:
        if node == failed_node:
            seen_failure = True
            continue
        if seen_failure and states[node.value].get("status") == "WAITING":
            states[node.value] = {"status": "BLOCKED", "event": None}


def _agent_canvas_html(
    states: dict[str, dict[str, Any]], *, recorded: bool = False
) -> str:
    cards = []
    for index, (title, purpose, icon, nodes, tools) in enumerate(_CANVAS_GROUPS, start=1):
        records = [states.get(node.value, {"status": "WAITING", "event": None}) for node in nodes]
        statuses = [record.get("status", "WAITING") for record in records]
        if "FAILED" in statuses:
            status = "FAILED"
        elif "RUNNING" in statuses:
            status = "RUNNING"
        elif title == "Human Review" and statuses[-1] == "COMPLETED":
            status = "COMPLETED"
        elif "PAUSED" in statuses:
            status = "PAUSED"
        elif all(value == "COMPLETED" for value in statuses):
            status = "COMPLETED"
        elif all(value in {"BLOCKED", "COMPLETED"} for value in statuses) and "BLOCKED" in statuses:
            status = "BLOCKED"
        else:
            status = "WAITING"
        display_status = {
            "WAITING": "Waiting",
            "RUNNING": "Running",
            "COMPLETED": "Completed",
            "PAUSED": "Your decision",
            "FAILED": "! Failed",
            "BLOCKED": "Blocked",
        }.get(status, status.title())
        event = (
            None
            if status == "FAILED"
            else next(
                (record.get("event") for record in reversed(records) if record.get("event")),
                None,
            )
        )
        css = status.lower()
        if event:
            summary = html.escape(
                _friendly_event_summary(event.get("result_summary", "Completed"))
            )
            detail = f"{event.get('duration_ms', 0)} ms"
            if event.get("verification"):
                evidence_check = (
                    "Evidence checked"
                    if event["verification"] == "PASS"
                    else "Evidence needs review"
                )
                detail += f" · {evidence_check}"
            summary = f"{summary}<br><span style='color:#64748b'>{detail}</span>"
        elif status == "RUNNING":
            summary = "Executing this real state-machine node…"
        elif status == "PAUSED":
            summary = "Waiting for an explicit human decision."
        elif status == "FAILED":
            summary = "Execution stopped safely at this node."
        elif status == "BLOCKED":
            summary = "Not run because an earlier step failed."
        else:
            summary = purpose
        badges = "".join(f'<span class="tool-badge">{html.escape(tool)}</span>' for tool in tools)
        cards.append(
            f'<div class="agent-node {css}"><span class="node-port input-port"></span>'
            f'<span class="node-port output-port"></span><div class="node-top">'
            f'<span class="node-icon">{icon}</span><span class="node-state">{html.escape(display_status)}</span></div>'
            f'<h4>{html.escape(title)}</h4><p>{summary}</p><div class="tool-stack">{badges}</div></div>'
        )
    canvas_label = (
        "RECORDED ADVANCED V1 TRAJECTORY · NO LIVE PROVIDER CALL"
        if recorded
        else "ORCHESTRATED SPECTRACE AGENT · LIVE WORKFLOW"
    )
    return (f'<div class="agent-canvas"><div class="canvas-label">{canvas_label}</div>'
            '<div class="canvas-grid">' + "".join(cards) + "</div></div>")


def _states_from_events(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    states = _initial_node_states()
    for event in events:
        status = "PAUSED" if event["node"] == AgentNode.AWAIT_HUMAN_REVIEW.value else "COMPLETED"
        states[event["node"]] = {"status": status, "event": event}
    return states


def _render_assessment(data: dict[str, Any], request_message: str | None = None) -> None:
    supporting_ids = tuple(data.get("supporting_evidence_ids") or ())
    conflicting_ids = tuple(data.get("conflicting_evidence_ids") or ())
    questions = tuple(data.get("clarification_questions") or ())
    impacts = semantic_impacts(data)
    evidence_badges = "".join(
        f'<span class="trace-badge">{html.escape(_evidence_label(value))}</span>'
        for value in (*supporting_ids, *conflicting_ids)
    ) or '<span class="muted-copy">No evidence citation was required.</span>'
    affected_items = "".join(
        f"<div class='impact-group'><b>{html.escape(heading)}</b><ul>" +
        "".join(f"<li>{html.escape(item)}</li>" for item in values) + "</ul></div>"
        for heading, values in impacts.items()
    ) or "<p>Impact requires human review.</p>"
    question_items = "".join(f"<li>{html.escape(value)}</li>" for value in questions) or "<li>No blocking questions.</li>"
    classification = _friendly_classification(data.get("classification", ""))
    conflict_comparison = ""
    if data.get("classification") == "CONTRADICTS_APPROVED_DECISION" and request_message:
        anchor = _current_anchor()
        by_id = {item.evidence_id: item for item in anchor.items} if anchor else {}
        conflict_text = " ".join(
            _friendly_clause(by_id[value].source_text)
            for value in conflicting_ids if value in by_id
        ) or "The cited approved decision conflicts with this request."
        conflict_comparison = (
            '<div class="conflict-comparison"><div><span>New request</span>'
            f'<p>{html.escape(request_message)}</p></div><div><span>Conflicting approved evidence</span>'
            f'<p>{html.escape(conflict_text)}</p></div></div>'
        )
    st.markdown(
        f"""<div class="section-card"><span class="eyebrow">Step 3 · Decision</span>
        <div class="decision-heading">{html.escape(classification)}</div>
        <p class="decision-summary">{html.escape(data.get('rationale', 'SpecTrace prepared an evidence-grounded recommendation.'))}</p>{conflict_comparison}
        <div class="decision-grid">
          <div class="decision-section"><h4>Why SpecTrace reached this result</h4><p>{html.escape(data.get('rationale', 'Evidence and approved decisions were checked.'))}</p></div>
          <div class="decision-section"><h4>Evidence used</h4><div>{evidence_badges}</div></div>
          <div class="decision-section"><h4>What could change?</h4>{affected_items}</div>
          <div class="decision-section"><h4>Questions or uncertainties</h4><ul>{question_items}</ul></div>
        </div>
        <div class="human-gate-copy">SpecTrace has prepared a recommendation. A human decision is required before project scope changes.</div>
        </div>""",
        unsafe_allow_html=True,
    )


def _project_exists(ledger: LedgerStore, project_id: str) -> bool:
    return ledger.connection.execute("SELECT 1 FROM projects WHERE project_id = ?", (project_id,)).fetchone() is not None


def _run_analysis(
    request: IncomingRequest,
    canvas: Any,
    *,
    project_pack: Path = DEMO_PACK,
    project_id: str | None = None,
    client: Any | None = None,
) -> None:
    anchor = build_scope_anchor(project_pack)
    ledger = LedgerStore(_session_database())
    state = new_run_state(project_pack, project_id or anchor.project_id, request, run_id=f"ui-{uuid.uuid4().hex}")
    states = _initial_node_states()
    st.session_state.agent_node_states = states
    canvas.markdown(_agent_canvas_html(states), unsafe_allow_html=True)

    def update_canvas(node: AgentNode, status: str, event: Any) -> None:
        states[node.value] = {
            "status": status,
            "event": event.model_dump(mode="json") if event else None,
        }
        if status == "FAILED":
            _block_downstream_nodes(states, node)
        st.session_state.agent_node_states = states
        canvas.markdown(_agent_canvas_html(states), unsafe_allow_html=True)

    try:
        provider_call_occurred = client is not None
        if client is None:
            settings = load_llm_settings()
            client = GoogleGenAIClient(settings, temperature=0.0, output_model=AdvancedModelOutput)
            provider_call_occurred = True
        state = run_until_human_review(
            state, ledger, client, max_attempts=2, event_callback=update_canvas
        )
        st.session_state.analysis_state = state
        st.session_state.analysis_database = str(_session_database())
        project_session = st.session_state.get("project_session")
        if project_session:
            st.session_state.project_session = project_session.model_copy(
                update={"current_request": request, "current_run_state": state, "run_diagnostic": None}
            )
    except Exception as exc:
        state.status = state.status.__class__.FAILED
        st.session_state.analysis_state = state
        st.session_state.analysis_database = str(_session_database())
        project_session = st.session_state.get("project_session")
        session_id = project_session.session_id if project_session else st.session_state.get("ui_session_id", "guided-demo")
        diagnostic = safe_diagnostic(
            exc,
            stage=_analysis_failure_stage(state, exc),
            project_session_id=session_id,
            provider_call_occurred=(
                provider_call_occurred
                and state.current_node not in {
                    AgentNode.LOAD_SCOPE_ANCHOR,
                    AgentNode.RETRIEVE_EVIDENCE,
                    AgentNode.ASSESS_SUFFICIENCY,
                    AgentNode.CHECK_CONTRADICTIONS,
                }
            ),
            attempt_number=getattr(exc, "attempt_count", None),
            for_analysis=True,
        )
        st.session_state.run_error = diagnostic
        if project_session:
            st.session_state.project_session = project_session.model_copy(
                update={"current_request": request, "current_run_state": state, "run_diagnostic": diagnostic}
            )
        raise
    finally:
        ledger.close()


def _review_panel(
    state: AdvancedRunState,
    canvas: Any,
    *,
    project_pack: Path = DEMO_PACK,
) -> None:
    st.subheader("Human decision")
    st.caption("Nothing changes approved memory until this explicit review transaction succeeds.")
    database = Path(st.session_state.analysis_database)
    with LedgerStore(database) as ledger:
        current = ledger.snapshot(state.project_id)
    display_to_action = {label: value for value, label in REVIEW_LABELS.items()}
    recommended = REVIEW_LABELS[
        state.recommendation.action.value if state.recommendation else HumanAction.DEFER.value
    ]
    display_action = st.pills(
        "Human decision",
        list(display_to_action),
        default=recommended,
        help="Approve keeps the recommendation; Change Decision records a supported override; Clarification requests more information; Defer records no scope change.",
    )
    action = display_to_action[display_action]
    final_classification = None
    reason = None
    evidence_ids = tuple(sorted(set(state.assessment.supporting_evidence_ids) | set(state.assessment.conflicting_evidence_ids))) if state.assessment else ()
    changes_scope = False
    confirmed = True
    if action == HumanAction.OVERRIDE.value:
        label_to_classification = {
            _friendly_classification(item): item for item in Classification
        }
        final_label = st.selectbox("Final decision", list(label_to_classification))
        final_classification = label_to_classification[final_label]
        reason = st.text_area("Override reason (required)")
        anchor = build_scope_anchor(project_pack)
        evidence_text = {item.evidence_id: item.source_text for item in anchor.items}
        evidence_ids = tuple(
            st.multiselect(
                "Override evidence (required)",
                evidence_ids,
                default=list(evidence_ids),
                format_func=lambda value: (
                    f"Approved evidence — {evidence_text.get(value, 'Supporting record')[:88]}"
                ),
            )
        )
    if action in {HumanAction.APPROVE.value, HumanAction.OVERRIDE.value}:
        changes_scope = st.checkbox("This decision approves a new scope-changing capability")
        if changes_scope:
            confirmed = st.checkbox("I confirm this will update approved decision memory")
    if st.button("Apply human decision", type="primary", disabled=changes_scope and not confirmed):
        payload = None
        if changes_scope:
            if not evidence_ids:
                st.error("A scope-changing decision requires approved supporting evidence.")
                return
            payload = HumanDecisionPayload(
                decision_id=f"DEC-{900 + len(current.ledger_entry_ids):03d}",
                effective_date=date.today(),
                effect=LedgerEntryEffect.APPROVE_CAPABILITY,
                decision_text=state.request.message,
                evidence_ids=evidence_ids,
                changes_approved_scope=True,
                approves_requested_capability=True,
            )
        try:
            review = HumanReview(
                review_id=f"HR-UI-{uuid.uuid4().hex[:10].upper()}", project_id=state.project_id,
                request_id=state.request.request_id, assessment_id=f"ASMNT-{state.request.request_id}",
                action=HumanAction(action), reviewer_id="local-human-reviewer", reviewed_at=datetime.now(UTC),
                final_classification=final_classification, reason=reason, evidence_ids=evidence_ids, decision_payload=payload,
            )
            with LedgerStore(database) as ledger:
                states = st.session_state.get("agent_node_states", _initial_node_states())

                def update_canvas(node: AgentNode, status: str, event: Any) -> None:
                    states[node.value] = {
                        "status": status,
                        "event": event.model_dump(mode="json") if event else None,
                    }
                    st.session_state.agent_node_states = states
                    canvas.markdown(_agent_canvas_html(states), unsafe_allow_html=True)

                state = resume_after_human_review(
                    state, ledger, review, event_callback=update_canvas
                )
            st.session_state.analysis_state = state
            st.session_state.review_message = "Approved project memory updated successfully."
            st.session_state.review_trail = {
                "decision": REVIEW_LABELS[review.action.value],
                "note": review.reason,
                "date": review.reviewed_at.date().isoformat(),
                "evidence_ids": evidence_ids,
            }
            st.toast("Human decision recorded", icon="✅")
            st.rerun()
        except Exception as exc:
            st.error(_safe_error(exc))


def _ledger_view() -> None:
    st.header("Scope Ledger")
    st.caption("Review human decisions, approved scope changes and requests that still need follow-up.")
    database = st.session_state.get("analysis_database")
    if database and Path(database).exists():
        with LedgerStore(database) as ledger:
            records = [
                dict(row)
                for row in ledger.connection.execute(
                    """SELECT r.request_id, r.request_date, r.message, r.source,
                              a.assessment_id, a.classification, a.evidence_ids_json,
                              h.review_id, h.action, h.reviewed_at, h.reason,
                              le.decision_id, le.decision_text,
                              le.changes_approved_scope, le.approves_requested_capability
                       FROM requests r
                       LEFT JOIN assessments a
                         ON a.project_id = r.project_id AND a.request_id = r.request_id
                       LEFT JOIN human_reviews h
                         ON h.project_id = r.project_id AND h.request_id = r.request_id
                       LEFT JOIN ledger_entries le
                         ON le.project_id = r.project_id AND le.request_id = r.request_id
                       ORDER BY r.chronological_order"""
                )
            ]
        summary = {
            "Requests reviewed": len(records),
            "Approved scope changes": sum(bool(record.get("changes_approved_scope")) for record in records),
            "Clarifications requested": sum(record.get("action") == "NEEDS_CLARIFICATION" for record in records),
            "Deferred requests": sum(record.get("action") == "DEFER" for record in records),
            "Active scope-growth patterns": sum(bool(record.get("changes_approved_scope")) for record in records),
        }
        st.markdown(
            '<div class="project-strip">' + "".join(
                f'<div class="project-metric"><span>{html.escape(label)}</span><strong>{value}</strong></div>'
                for label, value in summary.items()
            ) + "</div>",
            unsafe_allow_html=True,
        )
        view = st.pills("Ledger view", ("Decision Timeline", "Approved Scope Changes"), default="Decision Timeline")
        filter_columns = st.columns(4)
        selected_status = filter_columns[0].selectbox("Status", ("All statuses", "Decision recorded", "Waiting for your decision"))
        selected_filter = filter_columns[1].selectbox("Classification", ("All classifications", *CLASSIFICATION_LABELS.values()))
        selected_date = filter_columns[2].selectbox("Date", ("All dates", *sorted({_friendly_date(record["request_date"]) for record in records}, reverse=True)))
        selected_area = filter_columns[3].selectbox("Affected area", ("All areas", "Reservations", "Availability", "Roles", "Integrations", "Other"))

        def area_for(message: str) -> str:
            lowered = message.lower()
            if any(term in lowered for term in ("reservation", "booking", "cancel")):
                return "Reservations"
            if any(term in lowered for term in ("capacity", "available", "queue", "full")):
                return "Availability"
            if any(term in lowered for term in ("role", "helper", "coordinator", "admin")):
                return "Roles"
            if any(term in lowered for term in ("calendar", "email", "sms", "integration")):
                return "Integrations"
            return "Other"

        visible = [
            record for record in records
            if (view != "Approved Scope Changes" or record.get("changes_approved_scope"))
            and (selected_filter == "All classifications" or _friendly_classification(record.get("classification") or "") == selected_filter)
            and (selected_status == "All statuses" or ("Decision recorded" if record.get("action") else "Waiting for your decision") == selected_status)
            and (selected_date == "All dates" or _friendly_date(record["request_date"]) == selected_date)
            and (selected_area == "All areas" or area_for(record["message"]) == selected_area)
        ]
        if not visible:
            st.info("No decisions match the selected filters.")
        for record in visible:
            classification = _friendly_classification(record.get("classification") or "PENDING")
            action = REVIEW_LABELS.get(record.get("action"), "Waiting for your decision")
            scope_effect = (
                f"Scope updated: {record['decision_text']}"
                if record.get("changes_approved_scope")
                else "Approved project scope has not changed."
            )
            status = "Decision recorded" if record.get("action") else "Waiting for your decision"
            evidence = json.loads(record.get("evidence_ids_json") or "[]")
            st.markdown(
                f"""<div class="timeline-card"><div class="timeline-date">{_friendly_date(record['request_date'])}</div>
                <div class="timeline-message">“{html.escape(record['message'])}”</div>
                <div class="timeline-meta"><span class="classification-badge">{html.escape(classification)}</span>
                <span>{html.escape(action)}</span><span>{html.escape(status)}</span><span>{len(evidence)} evidence sources</span></div>
                <div class="timeline-effect">{html.escape(scope_effect)}</div></div>""",
                unsafe_allow_html=True,
            )
            _evidence_trail(
                evidence,
                human_decision=action if record.get("action") else None,
                reviewer_note=record.get("reason"),
                decision_date=record.get("reviewed_at") or record["request_date"],
            )
    else:
        st.markdown(
            """<div class="empty-state"><strong>No decisions have been recorded in this project yet.</strong>
            <span>Analyse a request to begin the decision timeline.</span></div>""",
            unsafe_allow_html=True,
        )


def _change_package_view(state: AdvancedRunState | None) -> None:
    st.subheader("Change Package")
    st.caption("A change package summarizes what an approved request affects so the BA can review it with the project team.")
    if not state or not state.change_package:
        st.info("Complete the human review to create a change package or review memo.")
        return
    package = state.change_package
    if package.is_review_memo:
        st.info(package.summary)
    else:
        st.success(package.summary)
    impacts = semantic_impacts(
        state.assessment.model_dump(mode="json"), package.model_dump(mode="json")
    )
    sections = (
        ("Scope items added or changed", (*package.added_requirements, *package.changed_requirements)),
        *((heading, values) for heading, values in impacts.items()),
        ("Affected process and workflow steps", package.workflow_steps),
        ("Dependencies", package.dependencies),
        ("Open questions", package.open_questions),
        ("Draft acceptance criteria", tuple(item.text for item in package.acceptance_criteria)),
        ("Explicit non-goals", package.non_goals),
    )
    for title, values in sections:
        if values:
            st.markdown(f"**{title}**")
            st.markdown("\n".join(f"- {value}" for value in values))
    _evidence_trail(
        (*package.supporting_evidence_ids, *package.conflicting_evidence_ids),
        recommendation=_friendly_classification(package.agent_classification.value),
        human_decision=REVIEW_LABELS[package.approval_state.value],
    )
    left, right = st.columns(2)
    left.download_button("Download Change Package (.md)", _markdown_package(state), file_name="spectrace-change-package.md", mime="text/markdown", width="stretch")
    right.download_button("Download Structured Record (.json)", _json_download(package), file_name="spectrace-structured-record.json", mime="application/json", width="stretch")


def _workflow_view(state: AdvancedRunState | None = None) -> None:
    st.header("Approved Business Workflow")
    st.caption("See how human-approved scope decisions change the member, coordinator, administrator and system process.")
    st.info("Agent Analysis Flow shows how SpecTrace reasons. Business Workflow shows how the approved product or process works.")
    pair = load_studiolane_workflows()
    # The Guided Demo ships with a committed, human-reviewed original/updated pair.
    # A fresh session should not hide that approved demonstration artifact.
    has_update = True
    view = st.pills(
        "Workflow view",
        ["Original Approved Workflow", "Updated Approved Workflow"],
        default="Updated Approved Workflow",
    )
    showing_original = view == "Original Approved Workflow"
    highlight_enabled, disabled_reason = workflow_change_controls(view, has_update)
    highlight = st.toggle("Highlight Changes", value=highlight_enabled, disabled=not highlight_enabled)
    if disabled_reason:
        st.caption(disabled_reason)
    presentation = st.pills("Presentation level", ("Overview", "Full detail"), default="Overview")
    draft = pair.original if showing_original else pair.updated
    verification = pair.original_verification if showing_original else pair.updated_verification
    mermaid = export_mermaid(
        draft, verification, direction="LR", swimlanes=True,
        highlight_changes=bool(highlight and not showing_original),
        reference_draft=pair.original if not showing_original else None,
    )
    display_mermaid = mermaid.content
    st.caption(
        f"{'Original approved path' if showing_original else 'Updated queue-aware path'} · "
        f"{len(draft.nodes)} steps · {len(draft.edges)} connectors"
    )
    if presentation == "Overview":
        st.iframe(workflow_overview_html(draft, verification), height=390)
    else:
        st.caption("Drag or scroll horizontally to inspect the complete process.")
        st.iframe(mermaid_preview_html(display_mermaid), height=720)

    added = [node.label for node in draft.nodes if node.change_type.value == "ADDED"]
    modified = [node.label for node in draft.nodes if node.change_type.value == "MODIFIED"]
    clarifications = [node.label for node in draft.nodes if node.requires_clarification]
    summary_columns = st.columns(3)
    summary_columns[0].markdown("**Added steps**")
    summary_columns[0].markdown("\n".join(f"- {value}" for value in added) if added else "No added steps")
    summary_columns[1].markdown("**Modified decisions**")
    summary_columns[1].markdown("\n".join(f"- {value}" for value in modified) if modified else "No modified decisions")
    summary_columns[2].markdown("**Open clarification nodes**")
    summary_columns[2].markdown("\n".join(f"- {value}" for value in clarifications) if clarifications else "No open clarification nodes")
    with st.expander("Evidence & Decision Trail"):
        anchor = build_scope_anchor(DEMO_PACK)
        by_id = {item.evidence_id: item for item in anchor.items}
        for node in draft.nodes:
            clauses = [by_id[value] for value in node.supporting_evidence_ids if value in by_id]
            if clauses:
                st.markdown(f"**{node.label}**")
                for clause in clauses:
                    st.caption(f"{Path(clause.source_path).stem.title()} · {clause.source_location.split(':')[-1].replace('-', ' ')}")
                    st.markdown(f"> {_friendly_clause(clause.source_text)}")
    bundle = export_drawio_bundle(
        pair.original, pair.original_verification, pair.updated, pair.updated_verification
    )
    st.markdown("**Editable workflow exports**")
    download_columns = st.columns(3)
    download_columns[0].download_button(
        "Download editable Draw.io", bundle.content,
        file_name="StudioLane_Approved_Workflows.drawio", mime=DRAWIO_MIME_TYPE,
        type="primary", width="stretch",
    )
    download_columns[1].download_button(
        "Download Draw.io XML", bundle.content,
        file_name="StudioLane_Approved_Workflows.xml", mime="application/xml", width="stretch",
    )
    download_columns[2].download_button(
        "Download Mermaid source", display_mermaid,
        file_name="StudioLane_Approved_Workflow.mmd", mime="text/plain", width="stretch",
    )
    st.download_button(
        "Download workflow summary", _json_download(draft),
        file_name="StudioLane_Approved_Workflow.json", mime="application/json",
    )
    st.markdown("**Import into Lucidchart**")
    st.info("In Lucidchart, use File → Import Diagram → Draw.io. Do not drag the file onto the canvas, because Lucid may treat it as an attachment.")
    st.caption("Manual import only. SpecTrace does not open Lucidchart automatically or claim a direct API integration.")


def _comparison_view(comparison: dict[str, Any]) -> None:
    st.header("Committed Evaluation")
    st.caption("Baseline V1 versus Advanced V1 on the frozen ten-request StudioLane benchmark.")
    metrics = comparison["aggregate_metrics"]
    tradeoff = comparison["runtime_and_tokens"]
    cards = []
    for label, key in (
        ("Evidence-grounded scope accuracy", "evidence_grounded_scope_accuracy"),
        ("Classification accuracy", "classification_accuracy"),
        ("Clarification recall", "clarification_recall"),
        ("Cumulative-scope accuracy", "cumulative_drift_detection_accuracy"),
    ):
        value = metrics[key]
        cards.append(f'<div class="result-card"><span>{label}</span><strong>{value["baseline"]:.2f} → {value["advanced"]:.2f}</strong></div>')
    st.markdown('<div class="result-grid">' + "".join(cards) + "</div>", unsafe_allow_html=True)
    cards.extend((
        f'<div class="result-card"><span>Runtime</span><strong>{tradeoff["baseline"]["runtime_seconds"]:.2f}s → {tradeoff["advanced"]["runtime_seconds"]:.2f}s</strong></div>',
        f'<div class="result-card"><span>Tokens</span><strong>{tradeoff["baseline"]["total_tokens"]:,} → {tradeoff["advanced"]["total_tokens"]:,}</strong></div>',
    ))
    st.markdown('<div class="result-grid">' + "".join(cards[-2:]) + "</div>", unsafe_allow_html=True)
    st.info("Advanced orchestration improved reliability on this ten-case synthetic benchmark while increasing runtime and token usage.")
    st.dataframe(
        [
            {
                "Case": index,
                "Expected": _friendly_classification(item["expected"]),
                "Baseline": _friendly_classification(item["baseline"]),
                "Advanced": _friendly_classification(item["advanced"]),
                "Outcome": re.sub(r"CR-\d{3}", f"Case {index}", item["change"]),
            }
            for index, item in enumerate(comparison["per_request"], start=1)
        ],
        width="stretch",
        hide_index=True,
    )
    st.warning("Limitations: one synthetic project, ten cases and one run per system. There is no statistical or generalization claim. The improvement belongs to the combined pipeline—not evidence retrieval alone.")


def _home_view() -> None:
    state = st.session_state.get("analysis_state")
    database = st.session_state.get("analysis_database")
    records: list[dict[str, Any]] = []
    if database and Path(database).exists():
        with LedgerStore(database) as ledger:
            records = [
                dict(row) for row in ledger.connection.execute(
                    """SELECT r.request_id, r.message, a.classification, h.action,
                              le.changes_approved_scope
                       FROM requests r
                       LEFT JOIN assessments a ON a.project_id=r.project_id AND a.request_id=r.request_id
                       LEFT JOIN human_reviews h ON h.project_id=r.project_id AND h.request_id=r.request_id
                       LEFT JOIN ledger_entries le ON le.project_id=r.project_id AND le.request_id=r.request_id
                       ORDER BY r.chronological_order DESC LIMIT 4"""
                )
            ]
    st.markdown(
        """<div class="welcome"><h2>Project overview</h2>
        <p>SpecTrace turns changing requests into evidence-backed recommendations, then pauses for a human decision before approved project memory changes.</p></div>
        <div class="journey">
          <div class="journey-step"><b>1</b>Review evidence</div><div class="journey-step"><b>2</b>Analyse request</div>
          <div class="journey-step"><b>3</b>Inspect citations</div><div class="journey-step"><b>4</b>Make decision</div>
          <div class="journey-step"><b>5</b>Export workflow</div>
        </div>""",
        unsafe_allow_html=True,
    )
    metrics = (
        ("Requests analysed", len(records)),
        ("Reviews awaiting action", int(bool(state and state.status.value == "AWAITING_HUMAN_REVIEW"))),
        ("Approved changes", sum(bool(item.get("changes_approved_scope")) for item in records)),
        ("Open contradictions / clarifications", sum(item.get("classification") in {"AMBIGUOUS", "CONTRADICTS_APPROVED_DECISION"} and not item.get("action") for item in records)),
    )
    st.markdown(
        '<div class="project-strip operational">' + "".join(
            f'<div class="project-metric"><span>{label}</span><strong>{value}</strong></div>'
            for label, value in metrics
        ) + "</div>", unsafe_allow_html=True,
    )
    if state and state.status.value == "AWAITING_HUMAN_REVIEW":
        st.warning(f"Human attention required: review the recommendation for {state.request.request_id}.")
    if records:
        st.markdown("**Recent requests**")
        for record in records:
            st.markdown(f"- {html.escape(record['message'])} — {_friendly_classification(record.get('classification') or 'PENDING')}")
    demo_column, project_column = st.columns(2)
    demo_column.button(
        "Try Guided Demo",
        type="primary",
        width="stretch",
        on_click=lambda: st.session_state.update(navigation="Analyse Request"),
    )
    project_column.button(
        "Create Project",
        width="stretch",
        on_click=lambda: st.session_state.update(
            experience_mode="New Project — Beta"
        ),
    )
    with st.expander("What SpecTrace does"):
        st.write("It compares requests with approved scope and decision history, surfaces ambiguity, contradictions and cumulative drift, and preserves an auditable human decision trail.")
    st.caption("The guided experience uses wholly synthetic project data. Do not enter private client or internship information.")


def _animate_recorded_trajectory(
    canvas: Any, trajectory: list[dict[str, Any]], *, delay_seconds: float = 0.06
) -> dict[str, dict[str, Any]]:
    states = _initial_node_states()
    canvas.markdown(_agent_canvas_html(states, recorded=True), unsafe_allow_html=True)
    for event in trajectory:
        node = AgentNode(event["node"])
        states[node.value] = {"status": "RUNNING", "event": None}
        canvas.markdown(_agent_canvas_html(states, recorded=True), unsafe_allow_html=True)
        if delay_seconds:
            time.sleep(delay_seconds)
        final_status = "PAUSED" if node == AgentNode.AWAIT_HUMAN_REVIEW else "COMPLETED"
        states[node.value] = {"status": final_status, "event": event}
        canvas.markdown(_agent_canvas_html(states, recorded=True), unsafe_allow_html=True)
        if node == AgentNode.AWAIT_HUMAN_REVIEW:
            break
    return states


def _render_recorded_replay(bundle: dict[str, Any]) -> None:
    record = bundle["assessment"]
    assessment = record["assessment"]
    request = bundle["paused"]["request"]
    review = bundle["review"]
    drift = record["drift"]
    verification = record["verification"]
    st.markdown(
        f"**Recorded request:** {html.escape(request['message'])}"
    )
    st.caption(
        f"Preserved classification · `{html.escape(assessment['classification'])}`"
    )
    _render_assessment(assessment, request["message"])
    verification_copy = (
        "Preserved citation verification passed."
        if verification["passed"]
        else "The preserved run recorded a verification issue."
    )
    st.info(verification_copy)

    cited_ids = set(assessment.get("supporting_evidence_ids") or ())
    cited_ids.update(assessment.get("conflicting_evidence_ids") or ())
    cited_ids.update(drift.get("related_decision_ids") or ())
    evidence = [
        item["evidence"] for item in bundle["retrieval"]["items"]
        if item["evidence"]["evidence_id"] in cited_ids
    ]
    with st.expander("Preserved retrieved evidence", expanded=True):
        for item in evidence:
            st.markdown(
                f"**{html.escape(item['evidence_id'])} · "
                f"{html.escape(item['source_location'])}**"
            )
            st.markdown(f"> {_friendly_clause(item['source_text'])}")

    if drift["cumulative_drift_detected"]:
        st.markdown("**Preserved cumulative-drift result**")
        st.markdown(
            f"{html.escape(DRIFT_LABELS[drift['severity']])} · Related requests: "
            f"{', '.join(drift['related_request_ids'])} · Related decisions: "
            f"{', '.join(drift['related_decision_ids'])}"
        )

    action = review["review"]["action"]
    ledger_update = review["ledger_update"]
    st.markdown(
        '<div class="recorded-review"><span>Preserved human review</span>'
        f'<strong>{html.escape(action)}</strong>'
        f'<p>{html.escape(review["review"]["reason"])}</p></div>',
        unsafe_allow_html=True,
    )
    st.info(
        "Ledger result · "
        + (
            f"Approved memory changed in preserved entry {ledger_update['ledger_entry_id']}."
            if ledger_update["ledger_changed"]
            else "Approved memory remained unchanged."
        )
    )
    workflow_steps = record["change_package"].get("workflow_steps") or ()
    if workflow_steps:
        st.markdown("**Preserved workflow output**")
        st.markdown("\n".join(f"- {step}" for step in workflow_steps))
    else:
        st.info("No workflow update was preserved for this request.")


def _recorded_analyse_view() -> None:
    st.success("Recorded Verified Run · Advanced V1 · No live provider call")
    st.caption(
        "Replays committed benchmark evidence and events. This is not a fresh execution "
        "and cannot change approved memory."
    )
    labels = {
        "CR-004": "CR-004 · Clarification · NEEDS_CLARIFICATION",
        "CR-008": "CR-008 · Contradiction · DEC-003",
        "CR-010": "CR-010 · Cumulative drift · DEC-005 / DEC-006",
    }
    request_id = st.selectbox(
        "Recorded verified case",
        RECORDED_REPLAY_CASES,
        format_func=lambda value: labels[value],
        key="recorded_replay_case",
    )
    st.markdown(
        """<div class="section-card"><span class="eyebrow">Recorded orchestration</span>
        <div class="section-title">Reveal the preserved Advanced V1 trajectory</div>
        <div class="section-copy">Every node, citation and outcome below comes from committed artifacts.</div></div>""",
        unsafe_allow_html=True,
    )
    replay_requested = st.button(
        "▶ Replay Verified Advanced V1", type="primary", width="stretch"
    )
    canvas = st.empty()
    previous = st.session_state.get("recorded_replay_result")
    if previous and previous.get("request_id") == request_id:
        canvas.markdown(
            _agent_canvas_html(
                _states_from_events(
                    [
                        event for event in previous["trajectory"]
                        if event["sequence"]
                        <= next(
                            item["sequence"] for item in previous["trajectory"]
                            if item["node"] == AgentNode.AWAIT_HUMAN_REVIEW.value
                        )
                    ]
                ),
                recorded=True,
            ),
            unsafe_allow_html=True,
        )
    else:
        canvas.markdown(
            _agent_canvas_html(_initial_node_states(), recorded=True),
            unsafe_allow_html=True,
        )
    if replay_requested:
        bundle = load_recorded_advanced_v1(request_id)
        st.session_state.recorded_replay_result = bundle
        _animate_recorded_trajectory(canvas, bundle["trajectory"])
        previous = bundle
    if previous and previous.get("request_id") == request_id:
        _render_recorded_replay(previous)


def _analyse_view(demo: dict[str, Any]) -> None:
    st.header("Analyse Request")
    st.caption("Replay the verified Advanced V1 benchmark or explicitly choose a live Gemini analysis.")
    mode = st.radio(
        "Analysis mode",
        ("Replay Verified Advanced V1", "Run Live Analysis"),
        horizontal=True,
        key="guided_analysis_mode",
    )
    if mode == "Replay Verified Advanced V1":
        _recorded_analyse_view()
        return
    st.warning("Live analysis · Requires configured Gemini access and available quota")
    _live_analyse_view(demo)


def _live_analyse_view(demo: dict[str, Any]) -> None:
    st.caption("Submit one fictional client request and watch SpecTrace compare it with approved evidence before pausing for your decision.")
    st.markdown(
        """<div class="section-card"><span class="eyebrow">Step 1 · Incoming request</span>
        <div class="section-title">Choose a benchmark request or write a fictional one</div>
        <div class="section-copy">Analysis never changes approved scope. The agent always pauses for your decision.</div></div>""",
        unsafe_allow_html=True,
    )
    demo_labels = {
        "CR-004": "Needs clarification",
        "CR-008": "Contradiction",
        "CR-009": "Contradiction",
        "CR-010": "Cumulative drift",
    }
    request_options = {
        f"Case {index} · {demo_labels.get(item['request_id'], 'Scope analysis')} · {item['message']}": item
        for index, item in enumerate(demo["requests"], start=1)
    }
    selected = request_options[st.selectbox("Synthetic request", list(request_options), index=6)]
    use_custom = st.toggle("Write a new fictional request")
    message = st.text_area("Request text", value="" if use_custom else selected["message"], disabled=not use_custom, placeholder="Describe a fictional scope request…")
    source_column, date_column = st.columns(2)
    source = source_column.text_input("Source", value="Fictional request" if use_custom else "Synthetic client message")
    request_date = date_column.date_input("Date", value=date.today() if use_custom else date.fromisoformat(selected["date"]))
    st.markdown(
        """<div class="section-card"><span class="eyebrow">Step 2 · Agent analysis flow</span>
        <div class="section-title">Real SpecTrace execution canvas</div>
        <div class="section-copy">The canvas shows how SpecTrace reasons and uses tools. The separate Business Workflow shows the approved product process.</div></div>""",
        unsafe_allow_html=True,
    )
    run_requested = st.button("▶ Run SpecTrace Agent", type="primary", width="stretch")
    canvas = st.empty()
    canvas.markdown(_agent_canvas_html(st.session_state.get("agent_node_states", _initial_node_states())), unsafe_allow_html=True)
    if run_requested:
        try:
            request = IncomingRequest(
                request_id=(f"CR-{900 + int(stable_local_id('ID', message)[-2:], 16) % 100:03d}" if use_custom else selected["request_id"]),
                date=request_date, source=source, message=message,
                evidence_available_through="DEC-006" if use_custom else selected["evidence_available_through"],
                chronological_order=11 if use_custom else selected["chronological_order"],
            )
            with st.status("SpecTrace is analysing approved evidence…", expanded=True) as status:
                st.write("Checking scope, prior decisions and evidence support.")
                _run_analysis(request, canvas)
                status.update(label="Recommendation ready for human review", state="complete", expanded=False)
            st.success("Execution paused safely at human review.")
        except ConfigurationError:
            st.error("Live analysis is not configured locally. No API call was made; switch to Replay Verified Advanced V1.")
            diagnostic = st.session_state.get("run_error")
            if diagnostic:
                _render_safe_diagnostic(diagnostic, extraction=False)
            else:
                _render_missing_provider_diagnostic()
        except Exception as exc:
            diagnostic = st.session_state.get("run_error")
            if diagnostic:
                _render_safe_diagnostic(diagnostic, extraction=False)
            else:
                st.error(_safe_error(exc))
                _render_missing_provider_diagnostic()
    state = st.session_state.get("analysis_state")
    if state and state.assessment:
        _render_assessment(state.assessment.model_dump(mode="json"), state.request.message)
        summary_parts = ["Evidence verified" if state.verification.passed else "Evidence check needs review"]
        if not state.assessment.conflicting_evidence_ids:
            summary_parts.append("No contradiction")
        if state.drift.severity.value == "NONE":
            summary_parts.append("No cumulative pattern")
        elif state.drift.severity.value != "RELATED":
            summary_parts.append(DRIFT_LABELS[state.drift.severity.value])
        st.info(" • ".join(summary_parts))
        if st.session_state.get("review_message"):
            st.success(st.session_state.review_message)
            trail = st.session_state.get("review_trail", {})
            _evidence_trail(
                trail.get("evidence_ids", ()), human_decision=trail.get("decision"),
                reviewer_note=trail.get("note"), decision_date=trail.get("date"),
            )
        if state.status.value == "AWAITING_HUMAN_REVIEW":
            _review_panel(state, canvas)
        _change_package_view(state)
    elif not state:
        st.markdown("<div class='empty-state'><strong>No request has been analysed in this session.</strong><span>The canvas is waiting. Run the agent when you are ready.</span></div>", unsafe_allow_html=True)
    elif st.session_state.get("run_error") and not run_requested:
        _render_safe_diagnostic(st.session_state.run_error, extraction=False)


def _guided_demo() -> None:
    demo, anchor = load_guided_demo_data(), build_scope_anchor(DEMO_PACK)
    if "navigation" not in st.session_state:
        st.session_state.navigation = "Home"
    navigation = st.segmented_control(
        "Navigation",
        ("Home", "Analyse Request", "Scope Ledger", "Business Workflow", "Results"),
        key="navigation",
        label_visibility="collapsed",
    )
    if navigation == "Home":
        _home_view()
        return
    st.markdown("### StudioLane <span style='color:#64748b;font-size:.8rem'>/ approved synthetic project</span>", unsafe_allow_html=True)
    _scope_summary(anchor)
    _audit_details(anchor, st.session_state.get("analysis_state"))
    if navigation == "Analyse Request":
        _analyse_view(demo)
    elif navigation == "Scope Ledger":
        _ledger_view()
    elif navigation == "Business Workflow":
        _workflow_view(st.session_state.get("analysis_state"))
    else:
        _comparison_view(demo["comparison"])


def _set_candidate_fields(candidate: CandidateScopeExtraction) -> None:
    by_category: dict[str, list[str]] = {}
    for item in candidate.items:
        by_category.setdefault(item.category, []).append(item.text)
    st.session_state.beta_pending_project_name = candidate.project_name
    for key, category in (
        ("beta_requirements", "APPROVED_REQUIREMENT"), ("beta_constraints", "CONSTRAINT"),
        ("beta_exclusions", "EXCLUSION"), ("beta_assumptions", "ASSUMPTION"),
        ("beta_questions", "UNRESOLVED_QUESTION"), ("beta_decisions", "DECISION"),
    ):
        st.session_state[key] = "\n".join(by_category.get(category, ()))


def _beta_setup() -> None:
    st.header("New Project — Beta")
    st.caption("Start with project documents, review the extracted scope, then approve it before analysis begins.")
    st.info("New Project mode is structurally and human validated. It has not been benchmark-evaluated.")
    st.markdown("### 1 · Upload project documents")
    st.caption("Use fictional, text-based SOW, proposal, requirements, discovery or decision documents. Scanned-image OCR is not supported.")
    pending_name = st.session_state.pop("beta_pending_project_name", None)
    if pending_name:
        st.session_state.beta_project_name = pending_name
    project_name = st.text_input("Project name", key="beta_project_name", placeholder="Fictional project name")
    uploaded = st.file_uploader(
        "Upload project documents", type=("pdf", "docx", "md", "txt"),
        accept_multiple_files=True,
        help="Up to 8 MB per file. Files stay in memory until you approve a local project.",
    )
    st.caption("Document text is sent to the configured model only after you click Extract Candidate Scope. Uploaded content is never written to Git paths.")
    extraction_error = st.session_state.get("extraction_error")
    extract_requested = False
    if extraction_error:
        _render_safe_diagnostic(extraction_error, extraction=True)
        retry_column, example_column, return_column = st.columns(3)
        extract_requested = retry_column.button(
            "Retry extraction", type="primary", width="stretch"
        )
        example_column.button(
            "Load fictional example", width="stretch", on_click=_load_synthetic_example_state
        )
        return_column.button(
            "Return to project setup", width="stretch",
            on_click=lambda: _clear_project_state(include_name=False),
        )
        st.caption("Quota failures are never retried automatically.")
    else:
        load_column, extract_column = st.columns(2)
        load_column.button(
            "Load Synthetic Example", width="stretch", on_click=_load_synthetic_example_state
        )
        extract_requested = extract_column.button(
            "Extract Candidate Scope", type="primary", width="stretch",
        )
    if st.session_state.get("synthetic_load_message"):
        st.success(st.session_state.pop("synthetic_load_message"))
    if extract_requested:
        if not project_name.strip():
            st.error("Enter a project name.")
        elif not uploaded:
            st.error("Upload at least one supported document or use the synthetic example.")
        else:
            session = new_project_session(
                project_name.strip(), ProjectSourceMode.UPLOADED_PROJECT, UI_ROOT
            )
            for key in PROJECT_SESSION_KEYS:
                st.session_state.pop(key, None)
            st.session_state.project_session = session
            provider_call_occurred = False
            try:
                documents = tuple(extract_document(item.name, item.getvalue()) for item in uploaded)
                workflow = extract_explicit_approved_workflow(documents)
                session = session.model_copy(
                    update={
                        "uploaded_documents": document_identities(documents),
                        "candidate_workflow": workflow,
                    }
                )
                st.session_state.project_session = session
                settings = load_llm_settings()
                client = GoogleGenAIClient(settings, temperature=0.0, output_model=CandidateScopeExtraction)
                with st.status("Extracting a candidate scope…", expanded=True) as status:
                    provider_call_occurred = True
                    output = extract_candidate_scope(documents, project_name.strip(), client)
                    if output.project_name != session.project_name:
                        raise ValueError("candidate project name does not match project session")
                    status.update(label="Candidate ready for human review", state="complete", expanded=False)
                st.session_state.beta_candidate = output
                st.session_state.beta_documents = documents
                st.session_state.beta_workflow_candidate = workflow
                st.session_state.project_session = session.model_copy(
                    update={"candidate_anchor": output, "extraction_diagnostic": None}
                )
                _set_candidate_fields(output)
                st.rerun()
            except Exception as exc:
                diagnostic = safe_diagnostic(
                    exc,
                    stage="candidate scope extraction",
                    project_session_id=session.session_id,
                    provider_call_occurred=provider_call_occurred,
                    attempt_number=getattr(exc, "attempt_count", None),
                )
                st.session_state.extraction_error = diagnostic
                st.session_state.project_session = session.model_copy(
                    update={"candidate_anchor": None, "extraction_diagnostic": diagnostic}
                )

    candidate = st.session_state.get("beta_candidate")
    if not candidate:
        st.markdown("<div class='empty-state'><strong>No candidate scope yet.</strong><span>Upload supported documents or load the fictional CampusFlow example.</span></div>", unsafe_allow_html=True)
        return
    st.success("Candidate project record — human review required.")
    category_counts: dict[str, int] = {}
    for item in candidate.items:
        category_counts[item.category] = category_counts.get(item.category, 0) + 1
    st.markdown(
        '<div class="upload-summary">' + "".join(
            f'<div class="project-metric"><span>{label}</span><strong>{category_counts.get(category, 0)}</strong></div>'
            for label, category in (
                ("Requirements found", "APPROVED_REQUIREMENT"), ("Constraints found", "CONSTRAINT"),
                ("Exclusions found", "EXCLUSION"), ("Decisions found", "DECISION"),
                ("Open questions found", "UNRESOLVED_QUESTION"),
            )
        ) + (
            f'<div class="project-metric"><span>Workflow steps found</span><strong>{len(st.session_state.beta_workflow_candidate.steps) if st.session_state.get("beta_workflow_candidate") else 0}</strong></div>'
            f'<div class="project-metric"><span>Items needing human review</span><strong>{len(candidate.items) + (len(st.session_state.beta_workflow_candidate.steps) if st.session_state.get("beta_workflow_candidate") else 0)}</strong></div>'
        ) + "</div>", unsafe_allow_html=True,
    )
    workflow_candidate = st.session_state.get("beta_workflow_candidate")
    if workflow_candidate:
        st.markdown("### Candidate approved workflow")
        st.caption(
            f"Deterministically parsed from {workflow_candidate.source_filename} · "
            f"{workflow_candidate.source_location}. Review every actor and action before approval."
        )
        st.dataframe(
            [
                {"Step": index, "Actor": step.actor, "Action": step.action, "Exception branch": step.branch or "—"}
                for index, step in enumerate(workflow_candidate.steps, start=1)
            ],
            hide_index=True, width="stretch",
        )
        if workflow_candidate.exception_branches:
            with st.expander("Explicit exception branches"):
                st.markdown("\n".join(f"- {branch}" for branch in workflow_candidate.exception_branches))
        workflow_approved = st.checkbox(
            "I reviewed and approve this documented workflow", key="beta_workflow_approved"
        )
    else:
        workflow_approved = False
        st.info("No explicit approved workflow was found. Add or import an approved process before workflow comparison.")
    with st.expander("Evidence & Decision Trail"):
        for item in candidate.items:
            st.markdown(f"**{item.source_filename} · {item.source_location}**")
            st.markdown(f"> {item.supporting_quote}")
            if item.uncertainty:
                st.caption(f"Needs review: {item.uncertainty}")
    with st.expander("Edit extracted scope manually", expanded=True):
        st.text_area("Approved requirements — one per line", key="beta_requirements")
        left, right = st.columns(2)
        left.text_area("Constraints — one per line", key="beta_constraints")
        left.text_area("Exclusions — one per line", key="beta_exclusions")
        right.text_area("Assumptions — one per line", key="beta_assumptions")
        right.text_area("Unresolved questions — one per line", key="beta_questions")
        st.text_area("Dated decisions — YYYY-MM-DD | decision", key="beta_decisions")
    approved = st.checkbox("I reviewed this candidate and approve it as project scope memory")
    approval_blocked = not approved or bool(workflow_candidate and not workflow_approved)
    if approval_blocked:
        st.caption(
            "Review and approve the candidate scope"
            + (" and documented workflow" if workflow_candidate else "")
            + " to enable project approval."
        )
    if st.button("Approve Scope Anchor", type="primary", disabled=approval_blocked, width="stretch"):
        try:
            session = st.session_state.get("project_session")
            if not session or session.candidate_anchor != candidate:
                raise ValueError("candidate project session mismatch")
            if session.project_name != candidate.project_name or st.session_state.beta_project_name != candidate.project_name:
                raise ValueError("candidate project name mismatch")
            decisions = _structured_decisions(st.session_state.beta_decisions)
            project = StructuredProjectInput(
                project_name=session.project_name,
                approved_requirements=_lines(st.session_state.beta_requirements),
                constraints=_lines(st.session_state.beta_constraints),
                exclusions=_lines(st.session_state.beta_exclusions),
                assumptions=_lines(st.session_state.beta_assumptions),
                unresolved_questions=_lines(st.session_state.beta_questions),
                decisions=decisions,
                workflow_steps=workflow_candidate.steps if workflow_candidate and workflow_approved else (),
            )
            build_structured_scope_anchor(project, human_approved=True)
            pack = materialize_beta_project(project, UI_ROOT, session.session_id)
            anchor = build_scope_anchor(pack)
            for key in ("analysis_state", "analysis_database", "agent_node_states", "review_message"):
                st.session_state.pop(key, None)
            st.session_state.beta_project = project
            st.session_state.beta_pack = str(pack)
            st.session_state.beta_anchor = anchor
            decision_ids = sorted(item.evidence_id for item in anchor.items if item.evidence_id.startswith("DEC-"))
            with LedgerStore(session.ledger_database_path) as ledger:
                ledger.seed_anchor(anchor, pack, approved_through=decision_ids[-1])
                approved_workflow = None
                if project.workflow_steps:
                    approved_workflow = generate_workflow_draft(
                        anchor, pack, ledger, evidence_cutoff=decision_ids[-1]
                    )
                    verification = verify_workflow_draft(
                        approved_workflow, anchor, pack, ledger.snapshot(anchor.project_id)
                    )
                    if not verification.passed:
                        raise ValueError("approved workflow verification failed")
            st.session_state.analysis_database = session.ledger_database_path
            st.session_state.project_session = session.model_copy(
                update={
                    "approved_anchor": anchor,
                    "project_pack_path": str(pack),
                    "candidate_workflow": workflow_candidate,
                    "workflow": approved_workflow,
                }
            )
            st.session_state.beta_navigation = "Home"
            st.toast("Project scope approved", icon="✅")
            st.rerun()
        except Exception as exc:
            for message in friendly_validation_messages(exc):
                st.error(message)


def _beta_analyse_view(anchor: Any, pack: Path) -> None:
    st.header("Analyse Request")
    st.caption("Analyse a fictional request against your human-approved Beta project scope.")
    message = st.text_area("Client request", placeholder="Describe a fictional change request…")
    source_column, date_column = st.columns(2)
    source = source_column.text_input("Source", value="Fictional request")
    request_date = date_column.date_input("Date", value=date.today())
    st.info("SpecTrace will analyse this request, but project scope will not change until you make a decision.")
    run_requested = st.button("▶ Run SpecTrace Agent", type="primary", width="stretch")
    canvas = st.empty()
    canvas.markdown(_agent_canvas_html(st.session_state.get("agent_node_states", _initial_node_states())), unsafe_allow_html=True)
    if run_requested:
        if not message.strip():
            st.error("Enter a fictional client request.")
        else:
            try:
                decision_ids = sorted(item.evidence_id for item in anchor.items if item.evidence_id.startswith("DEC-"))
                request = IncomingRequest(
                    request_id="CR-900", date=request_date, source=source or "Fictional request",
                    message=message.strip(), evidence_available_through=decision_ids[-1], chronological_order=1,
                )
                with st.status("SpecTrace is analysing approved evidence…", expanded=True) as status:
                    _run_analysis(request, canvas, project_pack=pack, project_id=anchor.project_id)
                    status.update(label="Recommendation ready for human review", state="complete", expanded=False)
            except Exception as exc:
                diagnostic = st.session_state.get("run_error")
                if diagnostic:
                    _render_safe_diagnostic(diagnostic, extraction=False)
                else:
                    st.error(_safe_error(exc))
                    _render_missing_provider_diagnostic()
    state = st.session_state.get("analysis_state")
    if state and state.assessment:
        _render_assessment(state.assessment.model_dump(mode="json"), state.request.message)
        if state.status.value == "AWAITING_HUMAN_REVIEW":
            _review_panel(state, canvas, project_pack=pack)
        _change_package_view(state)
    elif not state:
        st.markdown("<div class='empty-state'><strong>No request has been analysed in this project yet.</strong><span>The agent canvas is waiting for a fictional request.</span></div>", unsafe_allow_html=True)
    elif st.session_state.get("run_error") and not run_requested:
        _render_safe_diagnostic(st.session_state.run_error, extraction=False)


def _beta_workflow_view(anchor: Any, pack: Path) -> None:
    st.header("Business Workflow")
    st.caption("Only explicitly documented and human-approved process steps appear here.")
    project = st.session_state.beta_project
    if not project.workflow_steps:
        st.info("No explicit approved workflow was found. Add or import an approved process before workflow comparison.")
        return
    decision_ids = sorted(item.evidence_id for item in anchor.items if item.evidence_id.startswith("DEC-"))
    cutoff = decision_ids[-1]
    with LedgerStore() as original_ledger:
        original_ledger.seed_anchor(anchor, pack, approved_through=cutoff)
        original = generate_workflow_draft(anchor, pack, original_ledger, evidence_cutoff=cutoff)
        original_verification = verify_workflow_draft(
            original, anchor, pack, original_ledger.snapshot(anchor.project_id)
        )
    current, current_verification = original, original_verification
    database = st.session_state.get("analysis_database")
    if database and Path(database).exists():
        with LedgerStore(database) as ledger:
            current = generate_workflow_draft(anchor, pack, ledger, evidence_cutoff=cutoff)
            current_verification = verify_workflow_draft(
                current, anchor, pack, ledger.snapshot(anchor.project_id)
            )
    has_update = current.draft_hash != original.draft_hash
    options = ["Original Approved Workflow"] + (["Updated Approved Workflow"] if has_update else [])
    view = st.pills("Workflow view", options, default=options[-1])
    highlight_enabled, reason = workflow_change_controls(view, has_update)
    highlight = st.toggle("Highlight Changes", value=highlight_enabled, disabled=not highlight_enabled)
    if reason:
        st.caption(reason)
    level = st.pills("Presentation level", ("Overview", "Full detail"), default="Overview")
    showing_updated = view == "Updated Approved Workflow"
    draft = current if showing_updated else original
    verification = current_verification if showing_updated else original_verification
    source = export_mermaid(
        draft, verification, direction="LR", swimlanes=True,
        highlight_changes=bool(showing_updated and highlight),
        reference_draft=original if showing_updated else None,
    ).content
    if level == "Overview":
        st.iframe(workflow_overview_html(draft, verification), height=390)
    else:
        st.caption("Drag or scroll horizontally to inspect the complete process.")
        st.iframe(mermaid_preview_html(source), height=720)
    if has_update:
        drawio = export_drawio_bundle(
            original, original_verification, current, current_verification
        )
    else:
        from spectrace.workflow_export import export_drawio
        drawio = export_drawio(original, original_verification)
    left, middle, right = st.columns(3)
    left.download_button(
        "Download editable Draw.io", drawio.content,
        file_name=f"{project.project_name}_Approved_Workflow.drawio",
        mime=DRAWIO_MIME_TYPE, type="primary", width="stretch",
    )
    middle.download_button(
        "Download Draw.io XML", drawio.content,
        file_name=f"{project.project_name}_Approved_Workflow.xml",
        mime="application/xml", width="stretch",
    )
    right.download_button(
        "Download Mermaid source", source,
        file_name=f"{project.project_name}_Approved_Workflow.mmd",
        mime="text/plain", width="stretch",
    )
    st.info("In Lucidchart, use File → Import Diagram → Draw.io. Do not drag the file onto the canvas, because Lucid may treat it as an attachment.")


def _beta_workspace() -> None:
    anchor = st.session_state.beta_anchor
    pack = Path(st.session_state.beta_pack)
    if "beta_navigation" not in st.session_state:
        st.session_state.beta_navigation = "Home"
    navigation = st.segmented_control(
        "Navigation", ("Home", "Analyse Request", "Scope Ledger", "Business Workflow", "Results"),
        key="beta_navigation", label_visibility="collapsed",
    )
    st.markdown(f"### {html.escape(st.session_state.beta_project.project_name)} <span style='color:#34d399;font-size:.9rem'>/ Anchor Approved · Beta</span>", unsafe_allow_html=True)
    _scope_summary(anchor)
    _audit_details(anchor, st.session_state.get("analysis_state"))
    if navigation == "Home":
        st.markdown("<div class='section-card'><span class='eyebrow'>Project ready</span><div class='section-title'>Your approved scope workspace is active</div><div class='section-copy'>Analyse a fictional request, record the human decision and review the resulting scope ledger.</div></div>", unsafe_allow_html=True)
        st.button("Analyse First Request", type="primary", on_click=lambda: st.session_state.update(beta_navigation="Analyse Request"))
    elif navigation == "Analyse Request":
        _beta_analyse_view(anchor, pack)
    elif navigation == "Scope Ledger":
        _ledger_view()
    elif navigation == "Business Workflow":
        _beta_workflow_view(anchor, pack)
    else:
        st.info("These measured results belong to the evaluated StudioLane Guided Demo, not this Beta project.")
        _comparison_view(load_curated_comparison())


def _beta_project() -> None:
    if st.session_state.get("beta_anchor"):
        _beta_workspace()
    else:
        _beta_setup()


def _remove_local_database(database: str | Path | None) -> None:
    if not database:
        return
    path = Path(database).resolve()
    if path.parent != UI_ROOT.resolve():
        return
    for candidate in (path, Path(str(path) + "-shm"), Path(str(path) + "-wal")):
        if candidate.exists() and candidate.parent == UI_ROOT.resolve():
            candidate.unlink()


def _reset_beta_project() -> None:
    session = st.session_state.get("project_session")
    if session:
        _remove_local_database(session.ledger_database_path)
    _clear_project_state(include_name=True)


def _return_to_welcome() -> None:
    session = st.session_state.get("project_session")
    if session:
        _remove_local_database(session.ledger_database_path)
    _remove_local_database(st.session_state.get("analysis_database"))
    st.session_state.clear()


def _reset_guided_demo() -> None:
    database = st.session_state.get("analysis_database")
    if not database and st.session_state.get("ui_session_id"):
        database = UI_ROOT / f"demo-{st.session_state.ui_session_id}.sqlite"
    _remove_local_database(database)
    for key in (
        "analysis_state", "analysis_database", "agent_node_states", "review_message",
        "review_trail", "run_error", "ui_session_id",
    ):
        st.session_state.pop(key, None)
    st.session_state.navigation = "Home"
    session = st.session_state.get("project_session")
    if session and session.source_mode == ProjectSourceMode.GUIDED_DEMO:
        anchor = build_scope_anchor(DEMO_PACK)
        st.session_state.project_session = new_project_session(
            "StudioLane", ProjectSourceMode.GUIDED_DEMO, UI_ROOT
        ).model_copy(update={"approved_anchor": anchor})


def _handle_mode_change() -> None:
    for key in ("analysis_state", "analysis_database", "agent_node_states", "review_message", "review_trail", "run_error"):
        st.session_state.pop(key, None)
    if st.session_state.get("experience_mode") == "New Project — Beta":
        session = st.session_state.get("project_session")
        if session:
            if session.current_run_state:
                st.session_state.analysis_state = session.current_run_state
            st.session_state.analysis_database = session.ledger_database_path


def _enter_guided_demo() -> None:
    _clear_project_state(include_name=True)
    anchor = build_scope_anchor(DEMO_PACK)
    session = new_project_session(
        "StudioLane", ProjectSourceMode.GUIDED_DEMO, UI_ROOT
    ).model_copy(update={"approved_anchor": anchor})
    st.session_state.project_session = session
    st.session_state.experience_mode = "Guided Demo"
    st.session_state.navigation = "Home"
    st.session_state.welcome_entered = True


def _enter_new_project() -> None:
    _clear_project_state(include_name=True)
    st.session_state.experience_mode = "New Project — Beta"
    st.session_state.welcome_entered = True


def _welcome_screen() -> None:
    styles = (ROOT / "assets" / "styles.css").read_text(encoding="utf-8")
    logo = (ROOT / "assets" / "spectrace-monogram.svg").read_text(encoding="utf-8")
    st.html(
        f'<style>{styles}</style><div class="welcome-brand welcome-brand-standalone">'
        f'<span class="welcome-logo">{logo}</span><span class="welcome-wordmark">SpecTrace'
        '<small>Evidence → decision</small></span></div>'
    )
    hero = st.container(key="welcome_hero")
    left, right = hero.columns((0.94, 1.06), gap="large", vertical_alignment="center")
    left.html(
        '<section class="welcome-copy"><p class="welcome-kicker">Evidence and decision workspace</p>'
        '<h1>Turn changing requests into <span>evidence-backed scope decisions.</span></h1>'
        '<p class="welcome-lead">Trace each request to approved scope, preserve the human decision boundary, and export an editable business workflow.</p></section>'
    )
    left.button("Open Guided Demo", type="primary", width="stretch", on_click=_enter_guided_demo)
    left.button("Create a Project", width="stretch", on_click=_enter_new_project)
    right.html(
        '<section class="casefile-art" role="img" aria-label="Document evidence trail leading through a decision checkpoint to an approved workflow">'
        '<div class="sow-sheet"><small>APPROVED SOW</small><b>Scope baseline</b><i></i><i></i><i></i><code>SOW-SCP-004</code></div>'
        '<svg viewBox="0 0 560 270" aria-hidden="true"><path d="M95 155 C180 155 178 68 275 68 S370 188 462 188" fill="none" stroke="#C98A3E" stroke-width="4" stroke-dasharray="8 7"/><circle cx="275" cy="68" r="14" fill="#12141A" stroke="#C98A3E" stroke-width="4"/><path d="M268 68l5 5 10-12" fill="none" stroke="#5C8768" stroke-width="4"/><rect x="410" y="145" width="110" height="38" rx="6" fill="#243129" stroke="#5C8768"/><rect x="410" y="199" width="110" height="38" rx="6" fill="#1B1E27" stroke="#7A7E8C"/></svg>'
        '<div class="evidence-slip"><small>EVIDENCE CITATION</small><b>Approved delivery boundary</b><code>DEC-003 · verified</code></div>'
        '<div class="decision-stamp">HUMAN<br>APPROVED</div>'
        '<div class="workflow-continuation"><span>Approved</span><b>Workflow update</b><i>→</i><span>Editable</span></div></section>'
    )
    st.html('<div class="credibility"><span>Human approval required</span><span>Evidence citations</span><span>Approved decision memory</span><span>Editable workflow output</span></div>')
    st.markdown("### Real execution, shown honestly")
    st.caption("Run a request to see real workflow events move from evidence retrieval to a mandatory human-review pause. This welcome screen stays still because no agent is running yet.")


def main() -> None:
    st.set_page_config(
        page_title="SpecTrace Agent",
        page_icon=str(ROOT / "assets" / "spectrace-monogram.svg"),
        layout="wide",
        initial_sidebar_state="expanded",
    )
    if not st.session_state.get("welcome_entered"):
        _welcome_screen()
        return
    _render_header()
    with st.sidebar:
        st.markdown("### Workspace")
        mode = st.radio(
            "Mode",
            ("Guided Demo", "New Project — Beta"),
            key="experience_mode",
            label_visibility="collapsed",
            on_change=_handle_mode_change,
        )
        st.divider()
        st.caption("CURRENT PROJECT")
        st.markdown("**StudioLane**" if mode == "Guided Demo" else f"**{getattr(st.session_state.get('beta_project'), 'project_name', 'New project')}**")
        try:
            safe_settings_summary(load_llm_settings())
            st.success("Live model connected")
        except ConfigurationError:
            st.info("Offline demo ready · live model not connected")
        st.divider()
        if st.button("← Back to welcome", width="stretch", on_click=_return_to_welcome):
            st.rerun()
        if mode == "Guided Demo" and st.button(
            "Reset Guided Demo", width="stretch", on_click=_reset_guided_demo
        ):
            st.rerun()
        if mode == "New Project — Beta" and st.button(
            "Reset Project", width="stretch", on_click=_reset_beta_project
        ):
            st.rerun()
        st.caption("Synthetic data only. No automatic approval or external communication.")
    _guided_demo() if mode == "Guided Demo" else _beta_project()


if __name__ == "__main__":
    main()
