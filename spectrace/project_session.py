"""Project-isolated session and secret-safe failure contracts for the Streamlit UI."""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

from pydantic import Field, model_validator

from spectrace.advanced_models import AdvancedRunState, ScopeAnchor
from spectrace.config import ConfigurationError
from spectrace.llm import (
    ProviderErrorCategory,
    ProviderLLMError,
    RetryExhaustedError,
    StructuredOutputError,
    sanitize_provider_message,
)
from spectrace.models import IncomingRequest, StrictModel
from spectrace.project_documents import (
    CandidateScopeExtraction,
    CandidateWorkflow,
    DocumentExtractionError,
    ExtractedDocument,
)
from spectrace.workflow import WorkflowDraft


class ProjectSourceMode(str, Enum):
    GUIDED_DEMO = "GUIDED_DEMO"
    SYNTHETIC_EXAMPLE = "SYNTHETIC_EXAMPLE"
    UPLOADED_PROJECT = "UPLOADED_PROJECT"


class FailureCategory(str, Enum):
    DOCUMENT_READ_FAILURE = "DOCUMENT_READ_FAILURE"
    EMPTY_DOCUMENT_TEXT = "EMPTY_DOCUMENT_TEXT"
    UNSUPPORTED_OR_SCANNED_DOCUMENT = "UNSUPPORTED_OR_SCANNED_DOCUMENT"
    PROVIDER_AUTHENTICATION = "PROVIDER_AUTHENTICATION"
    PROVIDER_PERMISSION = "PROVIDER_PERMISSION"
    PROVIDER_QUOTA_OR_RATE_LIMIT = "PROVIDER_QUOTA_OR_RATE_LIMIT"
    MODEL_NOT_FOUND = "MODEL_NOT_FOUND"
    PROVIDER_INVALID_REQUEST = "PROVIDER_INVALID_REQUEST"
    STRUCTURED_OUTPUT_FAILURE = "STRUCTURED_OUTPUT_FAILURE"
    CANDIDATE_VALIDATION_FAILURE = "CANDIDATE_VALIDATION_FAILURE"
    UNKNOWN_EXTRACTION_FAILURE = "UNKNOWN_EXTRACTION_FAILURE"
    AGENT_RUN_FAILURE = "AGENT_RUN_FAILURE"


class DocumentIdentity(StrictModel):
    filename: str = Field(min_length=1, max_length=255)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class SafeDiagnostic(StrictModel):
    category: FailureCategory
    stage: str = Field(min_length=1, max_length=120)
    exception_type: str = Field(min_length=1, max_length=120)
    status_code: int | None = None
    provider_status: str | None = Field(default=None, max_length=120)
    sanitized_message: str = Field(min_length=1, max_length=500)
    retryable: bool
    timestamp: datetime
    project_session_id: str = Field(min_length=1, max_length=80)
    provider_call_occurred: bool
    attempt_number: int | None = Field(default=None, ge=1)


class ProjectSession(StrictModel):
    session_id: str = Field(min_length=1, max_length=80)
    project_name: str = Field(min_length=1, max_length=120)
    source_mode: ProjectSourceMode
    uploaded_documents: tuple[DocumentIdentity, ...] = ()
    candidate_anchor: CandidateScopeExtraction | None = None
    approved_anchor: ScopeAnchor | None = None
    candidate_workflow: CandidateWorkflow | None = None
    workflow: WorkflowDraft | None = None
    project_pack_path: str | None = None
    ledger_database_path: str
    current_request: IncomingRequest | None = None
    current_run_state: AdvancedRunState | None = None
    extraction_diagnostic: SafeDiagnostic | None = None
    run_diagnostic: SafeDiagnostic | None = None

    @model_validator(mode="after")
    def validate_project_ownership(self) -> "ProjectSession":
        if self.candidate_anchor and self.candidate_anchor.project_name != self.project_name:
            raise ValueError("candidate project name does not match project session")
        if self.current_run_state and self.approved_anchor:
            if self.current_run_state.project_id != self.approved_anchor.project_id:
                raise ValueError("run state does not belong to approved project")
        if self.session_id not in Path(self.ledger_database_path).name:
            raise ValueError("ledger database does not belong to project session")
        return self


def new_project_session(
    project_name: str,
    source_mode: ProjectSourceMode,
    storage_root: str | Path,
    *,
    session_id: str | None = None,
) -> ProjectSession:
    identifier = session_id or uuid.uuid4().hex
    database = Path(storage_root) / f"project-{identifier}.sqlite"
    return ProjectSession(
        session_id=identifier,
        project_name=project_name.strip(),
        source_mode=source_mode,
        ledger_database_path=str(database),
    )


def document_identities(documents: tuple[ExtractedDocument, ...]) -> tuple[DocumentIdentity, ...]:
    return tuple(
        DocumentIdentity(
            filename=document.filename,
            sha256=hashlib.sha256(document.text.encode("utf-8")).hexdigest(),
        )
        for document in documents
    )


_PROVIDER_CATEGORY_MAP = {
    ProviderErrorCategory.AUTHENTICATION: FailureCategory.PROVIDER_AUTHENTICATION,
    ProviderErrorCategory.PERMISSION: FailureCategory.PROVIDER_PERMISSION,
    ProviderErrorCategory.QUOTA_OR_RATE_LIMIT: FailureCategory.PROVIDER_QUOTA_OR_RATE_LIMIT,
    ProviderErrorCategory.MODEL_NOT_FOUND: FailureCategory.MODEL_NOT_FOUND,
    ProviderErrorCategory.INVALID_REQUEST_OR_SCHEMA: FailureCategory.PROVIDER_INVALID_REQUEST,
    ProviderErrorCategory.TRANSIENT_PROVIDER_ERROR: FailureCategory.UNKNOWN_EXTRACTION_FAILURE,
    ProviderErrorCategory.UNKNOWN_PROVIDER_ERROR: FailureCategory.UNKNOWN_EXTRACTION_FAILURE,
}


def safe_diagnostic(
    exc: Exception,
    *,
    stage: str,
    project_session_id: str,
    provider_call_occurred: bool,
    attempt_number: int | None = None,
    for_analysis: bool = False,
) -> SafeDiagnostic:
    category = FailureCategory.AGENT_RUN_FAILURE if for_analysis else FailureCategory.UNKNOWN_EXTRACTION_FAILURE
    status_code = None
    provider_status = None
    retryable = False
    safe_message = "The operation stopped safely before any approved project memory changed."
    provider = None
    if isinstance(exc, RetryExhaustedError) and exc.provider_errors:
        provider = exc.provider_errors[-1]
        attempt_number = provider.attempt_number or exc.attempt_count or attempt_number
    elif isinstance(exc, ProviderLLMError):
        provider = exc.diagnostic
    if provider:
        category = _PROVIDER_CATEGORY_MAP[provider.category]
        status_code = provider.status_code
        provider_status = provider.provider_status
        retryable = provider.retryable
        safe_message = provider.sanitized_provider_message
    elif isinstance(exc, StructuredOutputError):
        category = FailureCategory.STRUCTURED_OUTPUT_FAILURE
        safe_message = "The provider response did not match the required structured record."
    elif isinstance(exc, ConfigurationError):
        category = FailureCategory.PROVIDER_AUTHENTICATION
        safe_message = "Local model configuration is incomplete or unsupported."
    elif isinstance(exc, DocumentExtractionError):
        lowered = str(exc).lower()
        category = (
            FailureCategory.UNSUPPORTED_OR_SCANNED_DOCUMENT
            if "ocr" in lowered or "supported" in lowered
            else FailureCategory.EMPTY_DOCUMENT_TEXT
            if "no extractable text" in lowered or "empty" in lowered
            else FailureCategory.DOCUMENT_READ_FAILURE
        )
        safe_message = str(exc)
    else:
        name = type(exc).__name__.lower()
        if "validation" in name or "candidate" in str(exc).lower():
            category = FailureCategory.CANDIDATE_VALIDATION_FAILURE
            safe_message = "The extracted candidate did not satisfy the required project fields."
    safe_message = sanitize_provider_message(safe_message)
    safe_message = re.sub(r"https?://\S+", "[documentation link removed]", safe_message)
    return SafeDiagnostic(
        category=category,
        stage=stage,
        exception_type=type(exc).__name__,
        status_code=status_code,
        provider_status=provider_status,
        sanitized_message=safe_message[:500],
        retryable=retryable,
        timestamp=datetime.now(UTC),
        project_session_id=project_session_id,
        provider_call_occurred=provider_call_occurred,
        attempt_number=attempt_number,
    )
