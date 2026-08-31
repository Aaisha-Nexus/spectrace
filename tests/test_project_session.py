from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import extract_candidate_scope
from spectrace.llm import (
    ProviderErrorCategory,
    ProviderErrorDiagnostic,
    ProviderLLMError,
    RawGeneration,
    RetryExhaustedError,
)
from spectrace.project_documents import extract_document
from spectrace.project_session import (
    FailureCategory,
    ProjectSourceMode,
    document_identities,
    new_project_session,
    safe_diagnostic,
    ProjectSession,
)


class FakeStructuredClient:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.calls = 0

    def generate(self, _prompt: str) -> RawGeneration:
        self.calls += 1
        return RawGeneration(json.dumps(self.payload))


class FakeQuotaClient:
    def __init__(self) -> None:
        self.calls = 0

    def generate(self, _prompt: str) -> RawGeneration:
        self.calls += 1
        raise ProviderLLMError(
            ProviderErrorDiagnostic(
                provider="google",
                exception_type="ClientError",
                status_code=429,
                provider_status="RESOURCE_EXHAUSTED",
                category=ProviderErrorCategory.QUOTA_OR_RATE_LIMIT,
                sanitized_provider_message="Free-tier request limit reached; retry after 30 seconds.",
                retryable=False,
            )
        )


def _candidate_payload(name: str) -> dict[str, object]:
    return {
        "project_name": name,
        "items": [
            {
                "text": "A member can request a pickup.",
                "source_filename": "HarborBasket.txt",
                "source_location": "lines 1–2",
                "supporting_quote": "A member can request a pickup.",
                "category": "APPROVED_REQUIREMENT",
                "confidence": 1.0,
                "uncertainty": None,
            }
        ],
    }


def test_uploaded_extraction_succeeds_with_injected_fake_client() -> None:
    document = extract_document("HarborBasket.txt", b"A member can request a pickup.")
    client = FakeStructuredClient(_candidate_payload("Harbor Basket"))
    candidate = extract_candidate_scope((document,), "Harbor Basket", client)
    assert client.calls == 1
    assert candidate.project_name == "Harbor Basket"


def test_quota_failure_is_not_automatically_retried() -> None:
    document = extract_document("HarborBasket.txt", b"A member can request a pickup.")
    client = FakeQuotaClient()
    with pytest.raises(RetryExhaustedError):
        extract_candidate_scope((document,), "Harbor Basket", client)
    assert client.calls == 1


def test_project_sessions_have_isolated_candidates_documents_and_databases(tmp_path: Path) -> None:
    first = new_project_session(
        "CampusFlow", ProjectSourceMode.SYNTHETIC_EXAMPLE, tmp_path, session_id="campus-session"
    )
    second = new_project_session(
        "Harbor Basket", ProjectSourceMode.UPLOADED_PROJECT, tmp_path, session_id="harbor-session"
    )
    document = extract_document("HarborBasket.txt", b"Fictional approved scope")
    second = second.model_copy(update={"uploaded_documents": document_identities((document,))})
    assert first.session_id != second.session_id
    assert first.ledger_database_path != second.ledger_database_path
    assert not first.uploaded_documents and second.uploaded_documents
    assert "campus-session" in first.ledger_database_path
    assert "harbor-session" in second.ledger_database_path


def test_candidate_name_mismatch_is_rejected_by_project_session(tmp_path: Path) -> None:
    client = FakeStructuredClient(_candidate_payload("CampusFlow"))
    document = extract_document("scope.txt", b"Fictional scope")
    candidate = extract_candidate_scope((document,), "CampusFlow", client)
    session = new_project_session(
        "Harbor Basket", ProjectSourceMode.UPLOADED_PROJECT, tmp_path, session_id="mismatch"
    )
    with pytest.raises(ValueError, match="candidate project name"):
        ProjectSession.model_validate(
            {**session.model_dump(mode="python"), "candidate_anchor": candidate}
        )


def test_provider_diagnostic_is_categorized_and_sanitized() -> None:
    provider = ProviderErrorDiagnostic(
        provider="google",
        exception_type="ClientError",
        status_code=404,
        provider_status="NOT_FOUND",
        category=ProviderErrorCategory.MODEL_NOT_FOUND,
        sanitized_provider_message="model missing; key=AIzaABCDEFGHIJKLMNOPQRSTUVWX",
        retryable=False,
        attempt_number=1,
    )
    diagnostic = safe_diagnostic(
        ProviderLLMError(provider),
        stage="candidate scope extraction",
        project_session_id="project-session",
        provider_call_occurred=True,
    )
    assert diagnostic.category == FailureCategory.MODEL_NOT_FOUND
    assert diagnostic.status_code == 404
    assert "AIza" not in diagnostic.sanitized_message
    assert diagnostic.provider_call_occurred


def test_failed_extraction_session_has_no_approvable_candidate(tmp_path: Path) -> None:
    session = new_project_session(
        "Harbor Basket", ProjectSourceMode.UPLOADED_PROJECT, tmp_path, session_id="failed"
    )
    diagnostic = safe_diagnostic(
        ValueError("candidate validation failed https://errors.pydantic.dev/secret"),
        stage="candidate scope extraction",
        project_session_id=session.session_id,
        provider_call_occurred=True,
    )
    failed = session.model_copy(update={"candidate_anchor": None, "extraction_diagnostic": diagnostic})
    assert failed.candidate_anchor is None
    assert "http" not in failed.extraction_diagnostic.sanitized_message
    assert failed.extraction_diagnostic.category == FailureCategory.CANDIDATE_VALIDATION_FAILURE
