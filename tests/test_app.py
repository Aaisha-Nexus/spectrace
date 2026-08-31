from __future__ import annotations

import json
import hashlib
import shutil
from pathlib import Path

import pytest

from app import (
    _agent_canvas_html,
    _animate_recorded_trajectory,
    _friendly_event_summary,
    _initial_node_states,
    load_recorded_advanced_v1,
    load_curated_comparison,
    load_guided_demo_data,
)
from spectrace.advanced_models import AdvancedRunState, AgentNode, AgentStatus
from spectrace.llm import ProviderErrorCategory, ProviderErrorDiagnostic, ProviderLLMError
from spectrace.project_session import safe_diagnostic


ROOT = Path(__file__).resolve().parents[1]


def _open_guided(app):
    next(button for button in app.button if button.label == "Open Guided Demo").click().run()
    return app


def test_guided_demo_loads_all_synthetic_requests_and_real_trajectories() -> None:
    data = load_guided_demo_data(ROOT)
    assert [item["request_id"] for item in data["requests"]] == [f"CR-{index:03d}" for index in range(1, 11)]
    assert set(data["assessments"]) == {f"CR-{index:03d}" for index in range(1, 11)}
    assert all(trajectory for trajectory in data["trajectories"].values())
    assert data["trajectories"]["CR-001"][-1]["node"] == AgentNode.COMPLETE.value


def test_comparison_values_are_loaded_from_committed_artifact(monkeypatch: pytest.MonkeyPatch) -> None:
    source = json.loads((ROOT / "results/comparison_v1/comparison.json").read_text(encoding="utf-8"))
    source["aggregate_metrics"]["classification_accuracy"]["advanced"] = 0.73
    monkeypatch.setattr(Path, "read_text", lambda self, encoding=None: json.dumps(source))
    assert load_curated_comparison("comparison.json")["aggregate_metrics"]["classification_accuracy"]["advanced"] == 0.73


def test_non_curated_comparison_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "read_text", lambda self, encoding=None: '{"status":"EXPLORATORY"}')
    with pytest.raises(ValueError, match="not curated"):
        load_curated_comparison("comparison.json")


def test_guided_load_does_not_use_network(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network forbidden")))
    data = load_guided_demo_data(ROOT)
    assert data["comparison"]["aggregate_metrics"]["evidence_grounded_scope_accuracy"] == {
        "baseline": 0.8,
        "advanced": 1.0,
        "difference": 0.2,
    }


def test_dark_agent_ui_navigation_and_safe_initial_state() -> None:
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
    assert not app.exception
    assert [button.label for button in app.button] == ["Open Guided Demo", "Create a Project"]
    assert not app.segmented_control
    _open_guided(app)
    assert [control.label for control in app.segmented_control] == ["Navigation"]
    assert app.segmented_control[0].options == [
        "Home",
        "Analyse Request",
        "Scope Ledger",
        "Business Workflow",
        "Results",
    ]
    assert "Try Guided Demo" in [button.label for button in app.button]
    assert "Create Project" in [button.label for button in app.button]
    assert "analysis_state" not in app.session_state.filtered_state
    next(button for button in app.button if button.label == "Try Guided Demo").click().run()
    assert app.segmented_control[0].value == "Analyse Request"
    analysis_mode = next(control for control in app.radio if control.label == "Analysis mode")
    assert analysis_mode.value == "Replay Verified Advanced V1"
    assert "▶ Replay Verified Advanced V1" in [button.label for button in app.button]
    analysis_mode.set_value("Run Live Analysis").run()
    assert "▶ Run SpecTrace Agent" in [button.label for button in app.button]
    request_picker = next(
        control for control in app.selectbox if control.label == "Synthetic request"
    )
    assert all(option.startswith("Case ") for option in request_picker.options)
    assert all("CR-" not in option for option in request_picker.options)


def test_guided_demo_reset_clears_stale_session_state() -> None:
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
    _open_guided(app)
    database = ROOT / ".spectrace_ui" / "reset-test.sqlite"
    database.parent.mkdir(exist_ok=True)
    database.write_text("synthetic test state", encoding="utf-8")
    app.session_state["analysis_state"] = "stale synthetic state"
    app.session_state["analysis_database"] = str(database)
    reset = next(button for button in app.button if button.label == "Reset Guided Demo")
    reset.click().run()
    assert "analysis_state" not in app.session_state.filtered_state
    assert app.session_state["welcome_entered"] is True
    assert app.segmented_control[0].value == "Home"
    assert not database.exists()


def test_welcome_entry_state_survives_rerun_and_back_clears_stale_project_state() -> None:
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
    _open_guided(app)
    app.run()
    assert "Open Guided Demo" not in [button.label for button in app.button]
    app.session_state["analysis_state"] = "stale"
    next(button for button in app.button if button.label == "← Back to welcome").click().run()
    assert [button.label for button in app.button] == ["Open Guided Demo", "Create a Project"]
    assert "analysis_state" not in app.session_state.filtered_state
    assert "project_session" not in app.session_state.filtered_state


def test_guided_reset_preserves_new_project_state_when_modes_are_switched() -> None:
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
    next(button for button in app.button if button.label == "Create a Project").click().run()
    next(button for button in app.button if button.label == "Load Synthetic Example").click().run()
    candidate = app.session_state["beta_candidate"]
    app.radio[0].set_value("Guided Demo").run()
    next(button for button in app.button if button.label == "Reset Guided Demo").click().run()
    assert app.session_state["beta_candidate"] == candidate
    app.radio[0].set_value("New Project — Beta").run()
    assert app.session_state["beta_candidate"].project_name == "CampusFlow"


def test_every_guided_demo_page_opens_without_rendering_exceptions() -> None:
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
    _open_guided(app)
    for page in ("Analyse Request", "Scope Ledger", "Business Workflow", "Results"):
        app.segmented_control[0].set_value(page).run()
        assert not app.exception, page


def test_new_project_synthetic_example_enters_shared_workspace() -> None:
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
    next(button for button in app.button if button.label == "Create a Project").click().run()
    assert not app.exception
    assert "Load Synthetic Example" in [button.label for button in app.button]
    next(button for button in app.button if button.label == "Load Synthetic Example").click().run()
    assert not app.exception
    assert next(item for item in app.text_input if item.label == "Project name").value == "CampusFlow"
    assert app.session_state["project_session"].project_name == "CampusFlow"
    approval = next(
        checkbox for checkbox in app.checkbox
        if checkbox.label == "I reviewed this candidate and approve it as project scope memory"
    )
    approval.set_value(True).run()
    next(button for button in app.button if button.label == "Approve Scope Anchor").click().run()
    assert not app.exception
    assert app.segmented_control[0].options == [
        "Home", "Analyse Request", "Scope Ledger", "Business Workflow", "Results"
    ]
    assert "Analyse First Request" in [button.label for button in app.button]
    beta_root = Path(app.session_state["beta_pack"]).parent.resolve()
    database = Path(app.session_state["analysis_database"])
    assert beta_root.parent == (ROOT / ".spectrace_ui").resolve()
    shutil.rmtree(beta_root)
    database.unlink(missing_ok=True)


def test_synthetic_example_atomically_replaces_unsaved_uploaded_name_and_reset_clears_it() -> None:
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
    next(button for button in app.button if button.label == "Create a Project").click().run()
    name = next(item for item in app.text_input if item.label == "Project name")
    name.set_value("Harbor Basket").run()
    next(button for button in app.button if button.label == "Load Synthetic Example").click().run()
    assert next(item for item in app.text_input if item.label == "Project name").value == "CampusFlow"
    assert app.session_state["project_session"].source_mode.value == "SYNTHETIC_EXAMPLE"
    assert app.session_state["beta_candidate"].project_name == "CampusFlow"
    assert any("Unsaved Harbor Basket input was cleared" in item.value for item in app.success)
    next(button for button in app.button if button.label == "Reset Project").click().run()
    for key in ("project_session", "beta_candidate", "beta_workflow_candidate", "analysis_state", "analysis_database"):
        assert key not in app.session_state.filtered_state


def test_grouped_agent_canvas_uses_real_states_without_fake_animation() -> None:
    states = _initial_node_states()
    states[AgentNode.CLASSIFY_REQUEST.value]["status"] = "RUNNING"
    rendered = _agent_canvas_html(states)
    assert rendered.count('class="agent-node ') == 6
    assert "Client Request Trigger" in rendered
    assert "Scope Intelligence" in rendered
    assert "Evidence Verification" in rendered
    assert "Gemini LLM" in rendered
    assert "Running" in rendered
    assert "node-port input-port" in rendered and "node-port output-port" in rendered
    assert "setTimeout" not in rendered


def test_failed_node_blocks_every_downstream_canvas_group() -> None:
    from app import _block_downstream_nodes

    states = _initial_node_states()
    states[AgentNode.CLASSIFY_REQUEST.value]["status"] = "FAILED"
    _block_downstream_nodes(states, AgentNode.CLASSIFY_REQUEST)
    rendered = _agent_canvas_html(states)
    assert "! Failed" in rendered
    assert rendered.count("Blocked") >= 3


def test_failed_scope_intelligence_does_not_reuse_successful_conflict_copy() -> None:
    states = _initial_node_states()
    states[AgentNode.CHECK_CONTRADICTIONS.value] = {
        "status": "COMPLETED",
        "event": {"result_summary": "Conflicts were successfully checked.", "duration_ms": 1},
    }
    states[AgentNode.CLASSIFY_REQUEST.value] = {"status": "FAILED", "event": None}
    from app import _block_downstream_nodes

    _block_downstream_nodes(states, AgentNode.CLASSIFY_REQUEST)
    rendered = _agent_canvas_html(states)
    assert "Conflicts were successfully checked" not in rendered
    assert "Execution stopped safely at this node" in rendered


def test_primary_ui_contains_no_technical_record_or_hash_disclosures() -> None:
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    forbidden_user_copy = (
        '"Technical trace and reproducibility"',
        '"View technical record"',
        "Pydantic",
        "DeltaGenerator",
    )
    assert all(value not in source for value in forbidden_user_copy)
    assert source.count('"Evidence & Decision Trail"') >= 2
    assert 'st.expander("Audit & export")' in source


def test_workflow_change_controls_explain_every_disabled_state() -> None:
    from app import workflow_change_controls

    assert workflow_change_controls("Updated Approved Workflow", True) == (True, None)
    assert workflow_change_controls("Original Approved Workflow", True) == (
        False, "Select Updated workflow to highlight changes."
    )
    assert workflow_change_controls("Original Approved Workflow", False) == (
        False, "No approved workflow change exists yet."
    )


def test_ui_source_has_no_raw_expression_or_framework_object_leakage() -> None:
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    forbidden = (
        "st.success(package.summary) if",
        "DeltaGenerator",
        "Agent Run",
    )
    assert all(value not in source for value in forbidden)
    assert "st.html(" in source


def test_visible_pages_never_render_none_as_standalone_content() -> None:
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()

    def assert_clean(rendered) -> None:
        for collection in (
            rendered.markdown, rendered.caption, rendered.info, rendered.warning,
            rendered.error, rendered.success,
        ):
            for element in collection:
                value = str(element.value).strip()
                assert value != "None"
                assert "DeltaGenerator(" not in value

    assert_clean(app)
    _open_guided(app)
    for page in ("Home", "Analyse Request", "Scope Ledger", "Business Workflow", "Results"):
        app.segmented_control[0].set_value(page).run()
        assert_clean(app)


def test_quota_failure_has_safe_outcomes_and_three_recovery_actions() -> None:
    from streamlit.testing.v1 import AppTest

    provider = ProviderErrorDiagnostic(
        provider="google",
        exception_type="ClientError",
        status_code=429,
        provider_status="RESOURCE_EXHAUSTED",
        category=ProviderErrorCategory.QUOTA_OR_RATE_LIMIT,
        sanitized_provider_message=(
            "Retry after 30 seconds. https://example.invalid/docs?key=fake-secret"
        ),
        retryable=False,
        attempt_number=1,
    )
    diagnostic = safe_diagnostic(
        ProviderLLMError(provider),
        stage="candidate scope extraction",
        project_session_id="quota-test-session",
        provider_call_occurred=True,
    )
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
    next(button for button in app.button if button.label == "Create a Project").click().run()
    app.session_state["extraction_error"] = diagnostic
    app.run()
    assert any(
        "Gemini’s free-tier request limit was reached" in item.value for item in app.error
    )
    labels = [button.label for button in app.button]
    assert all(
        label in labels
        for label in ("Retry extraction", "Load fictional example", "Return to project setup")
    )
    visible_items = [
        str(item.value)
        for collection in (app.markdown, app.caption, app.error)
        for item in collection
    ]
    visible_items.extend(str(item.proto) for item in app.get("html"))
    page_text = "\n".join(visible_items)
    assert "Document reading" in page_text and "Successful" in page_text
    assert "Candidate extraction" in page_text and "Not completed" in page_text
    assert "Project approval / saving" in page_text and "Not performed" in page_text
    assert "example.invalid" not in page_text
    assert "fake-secret" not in page_text


class _RecordedCanvas:
    def __init__(self) -> None:
        self.frames: list[str] = []

    def markdown(self, value: str, **_kwargs) -> None:
        self.frames.append(value)


def test_recorded_replay_is_provider_free_and_stops_at_human_pause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app as app_module

    monkeypatch.setattr(
        app_module,
        "GoogleGenAIClient",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("provider called")),
    )
    bundle = load_recorded_advanced_v1("CR-004")
    canvas = _RecordedCanvas()
    states = _animate_recorded_trajectory(canvas, bundle["trajectory"], delay_seconds=0)
    assert states[AgentNode.AWAIT_HUMAN_REVIEW.value]["status"] == "PAUSED"
    assert states[AgentNode.APPLY_HUMAN_DECISION.value]["status"] == "WAITING"
    assert bundle["review"]["review"]["action"] == "NEEDS_CLARIFICATION"
    assert all("NO LIVE PROVIDER CALL" in frame for frame in canvas.frames)


def test_recorded_replay_reads_only_hash_verified_advanced_v1_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    advanced_root = (ROOT / "results" / "advanced_v1").resolve()
    original_text = Path.read_text
    original_bytes = Path.read_bytes
    accessed: list[Path] = []

    def guarded_text(path: Path, *args, **kwargs):
        resolved = path.resolve()
        assert resolved == advanced_root or advanced_root in resolved.parents
        accessed.append(resolved)
        return original_text(path, *args, **kwargs)

    def guarded_bytes(path: Path, *args, **kwargs):
        resolved = path.resolve()
        assert advanced_root in resolved.parents
        accessed.append(resolved)
        return original_bytes(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_text)
    monkeypatch.setattr(Path, "read_bytes", guarded_bytes)
    bundle = load_recorded_advanced_v1("CR-008", advanced_root)
    assert bundle["assessment"]["assessment"]["conflicting_evidence_ids"] == ["DEC-003"]
    assert accessed and all(advanced_root in path.parents for path in accessed)


def test_required_recorded_cases_match_preserved_outcomes() -> None:
    clarification = load_recorded_advanced_v1("CR-004")
    contradiction = load_recorded_advanced_v1("CR-008")
    drift = load_recorded_advanced_v1("CR-010")
    assert clarification["assessment"]["recommendation"]["action"] == "NEEDS_CLARIFICATION"
    assert contradiction["assessment"]["assessment"]["classification"] == "CONTRADICTS_APPROVED_DECISION"
    assert contradiction["assessment"]["assessment"]["conflicting_evidence_ids"] == ["DEC-003"]
    assert drift["assessment"]["drift"]["cumulative_drift_detected"] is True
    assert drift["assessment"]["drift"]["related_request_ids"] == ["CR-006", "CR-007", "CR-010"]
    assert drift["assessment"]["drift"]["related_decision_ids"] == ["DEC-005", "DEC-006"]
    assert drift["review"]["review"]["action"] == "DEFER"
    assert drift["review"]["ledger_update"]["ledger_changed"] is False
    assert drift["assessment"]["change_package"]["workflow_steps"] == []


def test_live_and_recorded_modes_are_visibly_distinct_and_replay_preserves_failure() -> None:
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
    _open_guided(app)
    app.segmented_control[0].set_value("Analyse Request").run()
    assert any("Recorded Verified Run · Advanced V1" in item.value for item in app.success)
    assert "▶ Run SpecTrace Agent" not in [button.label for button in app.button]
    failed_state = AdvancedRunState.model_validate(
        load_recorded_advanced_v1("CR-004")["paused"]
    ).model_copy(update={"status": AgentStatus.FAILED})
    failed_diagnostic = safe_diagnostic(
        ValueError("offline failed live test"),
        stage="model generation",
        project_session_id="failed-live-session",
        provider_call_occurred=False,
        for_analysis=True,
    )
    app.session_state["run_error"] = failed_diagnostic
    app.session_state["analysis_state"] = failed_state
    next(button for button in app.button if button.label == "▶ Replay Verified Advanced V1").click().run()
    assert app.session_state["run_error"] == failed_diagnostic
    assert app.session_state["analysis_state"] == failed_state
    visible = "\n".join(str(item.value) for item in (*app.markdown, *app.caption, *app.info, *app.success))
    assert "None" not in {line.strip() for line in visible.splitlines()}
    next(control for control in app.radio if control.label == "Analysis mode").set_value("Run Live Analysis").run()
    assert any("Requires configured Gemini access" in item.value for item in app.warning)
    assert "▶ Run SpecTrace Agent" in [button.label for button in app.button]


def test_replay_does_not_change_curated_advanced_v1_hashes() -> None:
    root = ROOT / "results" / "advanced_v1"
    manifest = json.loads((root / "curation_manifest.json").read_text(encoding="utf-8"))
    expected = manifest["source_copy_validation"]["artifact_sha256"]
    before = {
        relative: hashlib.sha256((root / relative).read_bytes()).hexdigest()
        for relative in expected
    }
    load_recorded_advanced_v1("CR-004")
    load_recorded_advanced_v1("CR-008")
    load_recorded_advanced_v1("CR-010")
    after = {
        relative: hashlib.sha256((root / relative).read_bytes()).hexdigest()
        for relative in expected
    }
    assert before == expected == after


def test_missing_provider_diagnostic_copy_is_explicit_and_never_none() -> None:
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    message = "Detailed provider diagnostics were unavailable for this attempt."
    assert message in source
    assert source.count("_render_missing_provider_diagnostic()") >= 3


def test_canvas_summaries_never_expose_internal_enum_names() -> None:
    summary = _friendly_event_summary(
        "Recommended POTENTIAL_SCOPE_CHANGE; reconciled CONTRADICTS_APPROVED_DECISION. "
        "OUT_OF_SCOPE was considered. Drift severity: NONE."
    )
    assert summary == (
        "Recommended Potential Scope Change; reconciled Conflicts with Approved "
        "Decision. Out of Scope was considered. Cumulative scope growth: No pattern detected."
    )
    assert not any(internal in summary for internal in CLASSIFICATION_NAMES)


CLASSIFICATION_NAMES = (
    "IN_SCOPE",
    "AMBIGUOUS",
    "OUT_OF_SCOPE",
    "CONTRADICTS_APPROVED_DECISION",
    "POTENTIAL_SCOPE_CHANGE",
)


def test_dark_canvas_styles_are_responsive_and_status_specific() -> None:
    css = (ROOT / "assets/styles.css").read_text(encoding="utf-8")
    assert "#12141A" in css
    assert "grid-template-columns:repeat(3" in css
    assert ".agent-node.running" in css
    assert ".agent-node.completed" in css
    assert ".agent-node.paused" in css
    assert ".agent-node.failed" in css
    assert ".agent-node.blocked" in css
    assert "@media (max-width: 1000px)" in css
    assert "prefers-reduced-motion" in css
    assert ".node-port" in css
