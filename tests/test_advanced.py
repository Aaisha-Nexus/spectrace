from __future__ import annotations

import json
import hashlib
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from spectrace.advanced import DEFAULT_ADVANCED_PROMPT, AdvancedAgentError, advanced_prompt_hash, new_run_state, resume_after_human_review, run_until_human_review
from spectrace.advanced_models import (
    AgentNode,
    AgentStatus,
    AdvancedModelOutput,
    AdvancedRunState,
    DriftSeverity,
    HumanAction,
    HumanDecisionPayload,
    HumanReview,
    LedgerEntryEffect,
)
from spectrace.dataset import validate_project_pack
from spectrace.ledger import LedgerStore
from spectrace.llm import RawGeneration, gemini_schema_for_model
from spectrace.models import Classification
from spectrace.baseline import prompt_hash
from spectrace.scope_anchor import build_scope_anchor


PACK = Path(__file__).parents[1] / "data" / "synthetic" / "demo_project"
FROZEN_BASELINE_PROMPT_HASH = "369b16540e18ac3592867bdcde4a9d37e156ef8ee726371e1782380edb48a687"


class FakeClient:
    def __init__(self, request_id: str, recommendation: Classification) -> None:
        self.request_id = request_id
        self.recommendation = recommendation
        self.calls = 0

    def generate(self, prompt: str) -> RawGeneration:
        self.calls += 1
        assert "ground_truth" not in prompt
        return RawGeneration(text=json.dumps({
            "request_id": self.request_id,
            "recommended_classification": self.recommendation.value,
            "supporting_evidence_ids": [],
            "conflicting_evidence_ids": [],
            "requires_clarification": False,
            "clarification_questions": [],
            "dependencies": [],
            "rationale": "Offline fake recommendation for human review.",
            "capability_signature": {"heuristic": True},
        }), usage={"fake": True})


def test_prompt_hashes_cover_exact_bytes_and_baseline_remains_frozen() -> None:
    assert prompt_hash() == FROZEN_BASELINE_PROMPT_HASH
    assert advanced_prompt_hash() == hashlib.sha256(DEFAULT_ADVANCED_PROMPT.read_bytes()).hexdigest()


def test_generic_provider_schema_preserves_local_advanced_contract() -> None:
    schema = gemini_schema_for_model(AdvancedModelOutput)
    assert "recommended_classification" in schema["properties"]
    assert "additionalProperties" not in json.dumps(schema)


def test_production_advanced_modules_have_no_ground_truth_dependency() -> None:
    root = Path(__file__).parents[1]
    for relative in (
        "spectrace/advanced.py",
        "spectrace/analysis_tools.py",
        "spectrace/change_package.py",
        "spectrace/verification.py",
    ):
        assert "ground_truth" not in (root / relative).read_text(encoding="utf-8")


def test_state_machine_pauses_before_memory_mutation_and_resumes_with_review() -> None:
    request = validate_project_pack(PACK).requests[4]
    state = new_run_state(PACK, "studiolane", request)
    with LedgerStore() as ledger:
        paused = run_until_human_review(state, ledger, FakeClient(request.request_id, Classification.IN_SCOPE))
        assert paused.status == AgentStatus.AWAITING_HUMAN_REVIEW
        assert not ledger.snapshot(paused.project_id).review_ids
        assert ledger.approved_scope_change_records(paused.project_id) == ()
        assert [event.node for event in paused.trajectory][-1] == AgentNode.AWAIT_HUMAN_REVIEW

        paused = AdvancedRunState.model_validate_json(paused.model_dump_json())
        review = HumanReview(review_id="HR-OFFLINE", project_id=paused.project_id, request_id=request.request_id, assessment_id=f"ASMNT-{request.request_id}", action=HumanAction.DEFER, reviewer_id="offline-human", reviewed_at=datetime.now(UTC))
        completed = resume_after_human_review(paused, ledger, review)
        assert completed.status == AgentStatus.COMPLETE
        assert completed.change_package and completed.change_package.is_review_memo
        assert completed.trajectory[-1].node == AgentNode.COMPLETE


def test_resume_rejects_tampered_snapshot() -> None:
    request = validate_project_pack(PACK).requests[0]
    state = new_run_state(PACK, "studiolane", request)
    with LedgerStore() as ledger:
        paused = run_until_human_review(state, ledger, FakeClient(request.request_id, Classification.IN_SCOPE))
        paused.pause_snapshot_hash = "0" * 64
        review = HumanReview(review_id="HR-TAMPER", project_id=paused.project_id, request_id=request.request_id, assessment_id=f"ASMNT-{request.request_id}", action=HumanAction.DEFER, reviewer_id="human", reviewed_at=datetime.now(UTC))
        with pytest.raises(AdvancedAgentError, match="snapshot changed"):
            resume_after_human_review(paused, ledger, review)


@pytest.mark.parametrize(
    ("index", "recommendation", "expected_classification", "expected_action", "expected_evidence"),
    [
        (2, Classification.IN_SCOPE, Classification.AMBIGUOUS, HumanAction.NEEDS_CLARIFICATION, ()),
        (7, Classification.IN_SCOPE, Classification.CONTRADICTS_APPROVED_DECISION, HumanAction.DEFER, ("DEC-003",)),
    ],
)
def test_fake_demonstrations_pause_for_ambiguity_and_contradiction(
    index, recommendation, expected_classification, expected_action, expected_evidence
) -> None:
    request = validate_project_pack(PACK).requests[index]
    with LedgerStore() as ledger:
        state = run_until_human_review(new_run_state(PACK, "studiolane", request), ledger, FakeClient(request.request_id, recommendation))
        assert state.status == AgentStatus.AWAITING_HUMAN_REVIEW
        assert state.assessment.classification == expected_classification
        assert state.recommendation.action == expected_action
        assert state.assessment.conflicting_evidence_ids == expected_evidence


@pytest.mark.parametrize("action", list(HumanAction))
def test_all_human_actions_resume_through_the_transaction_boundary(action: HumanAction) -> None:
    request = validate_project_pack(PACK).requests[0]
    with LedgerStore() as ledger:
        state = run_until_human_review(new_run_state(PACK, "studiolane", request), ledger, FakeClient(request.request_id, Classification.IN_SCOPE))
        kwargs = {}
        if action == HumanAction.OVERRIDE:
            kwargs = {"final_classification": Classification.IN_SCOPE, "reason": "Human correction.", "evidence_ids": ("SOW-SCP-003",)}
        review = HumanReview(review_id=f"HR-{action.value}", project_id=state.project_id, request_id=request.request_id, assessment_id=f"ASMNT-{request.request_id}", action=action, reviewer_id="human", reviewed_at=datetime.now(UTC), **kwargs)
        completed = resume_after_human_review(state, ledger, review)
        assert completed.status == AgentStatus.COMPLETE
        assert completed.ledger_update.action == action


def test_trajectory_uses_the_declared_node_order() -> None:
    request = validate_project_pack(PACK).requests[4]
    with LedgerStore() as ledger:
        state = run_until_human_review(new_run_state(PACK, "studiolane", request), ledger, FakeClient(request.request_id, Classification.OUT_OF_SCOPE))
        assert [event.node for event in state.trajectory] == [
            AgentNode.LOAD_SCOPE_ANCHOR,
            AgentNode.RETRIEVE_EVIDENCE,
            AgentNode.ASSESS_SUFFICIENCY,
            AgentNode.CHECK_CONTRADICTIONS,
            AgentNode.CLASSIFY_REQUEST,
            AgentNode.CALCULATE_CUMULATIVE_DRIFT,
            AgentNode.VERIFY_ASSESSMENT,
            AgentNode.PREPARE_RECOMMENDATION,
            AgentNode.AWAIT_HUMAN_REVIEW,
        ]


def test_failed_verification_still_fails_closed_at_human_review() -> None:
    request = validate_project_pack(PACK).requests[5]

    class InvalidClaimClient:
        def generate(self, prompt: str) -> RawGeneration:
            return RawGeneration(text=json.dumps({
                "request_id": request.request_id,
                "recommended_classification": Classification.POTENTIAL_SCOPE_CHANGE.value,
                "supporting_evidence_ids": ["DEC-999"],
                "conflicting_evidence_ids": [],
                "requires_clarification": False,
                "clarification_questions": [],
                "dependencies": [],
                "rationale": "The request is approved.",
                "capability_signature": {"heuristic": True},
            }))

    with LedgerStore() as ledger:
        state = run_until_human_review(new_run_state(PACK, "studiolane", request), ledger, InvalidClaimClient())
        assert state.status == AgentStatus.AWAITING_HUMAN_REVIEW
        assert not state.verification.passed
        assert state.recommendation.action == HumanAction.DEFER


def _approve_change(ledger: LedgerStore, request, decision_id: str, text: str) -> None:
    ledger.record_request("studiolane", request)
    assessment_id = f"ASMNT-{request.request_id}"
    ledger.record_assessment("studiolane", assessment_id, request.request_id, classification=Classification.POTENTIAL_SCOPE_CHANGE, evidence_ids=("SOW-CON-002",), assessment={"summary": "offline fake"})
    before = ledger.snapshot("studiolane")
    review = HumanReview(
        review_id=f"HR-{decision_id}",
        project_id="studiolane",
        request_id=request.request_id,
        assessment_id=assessment_id,
        action=HumanAction.APPROVE,
        reviewer_id="offline-human",
        reviewed_at=datetime.now(UTC),
        final_classification=Classification.POTENTIAL_SCOPE_CHANGE,
        decision_payload=HumanDecisionPayload(
            decision_id=decision_id,
            effective_date=date(2026, 5, 23),
            effect=LedgerEntryEffect.APPROVE_CAPABILITY,
            decision_text=text,
            evidence_ids=("SOW-CON-002",),
            changes_approved_scope=True,
            approves_requested_capability=True,
        ),
    )
    if decision_id == "DEC-005":
        assert before.ledger_entry_ids == ()
    else:
        assert len(before.ledger_entry_ids) == 1
    ledger.apply_human_review(review)


def test_fake_approved_history_drives_subsystem_only_after_human_transactions() -> None:
    pack = validate_project_pack(PACK)
    with LedgerStore() as ledger:
        ledger.seed_anchor(build_scope_anchor(PACK), PACK, approved_through="DEC-004")
        assert ledger.approved_scope_change_records("studiolane") == ()
        _approve_change(ledger, pack.requests[5], "DEC-005", "Approve persistent queue join ordering and email notification for a full session.")
        assert len(ledger.approved_scope_change_records("studiolane")) == 1
        _approve_change(ledger, pack.requests[6], "DEC-006", "Approve stored queue priority workflow and ordering status.")
        current = pack.requests[9]
        state = run_until_human_review(new_run_state(PACK, "studiolane", current), ledger, FakeClient(current.request_id, Classification.POTENTIAL_SCOPE_CHANGE))
        assert state.drift.severity == DriftSeverity.SUBSYSTEM
        assert state.drift.cumulative_drift_detected
