"""Provider-aware structured generation with a Google GenAI implementation."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Protocol

from pydantic import ValidationError

from spectrace.config import LLMSettings
from spectrace.models import ModelPrediction


_GEMINI_JSON_SCHEMA_KEYWORDS = frozenset(
    {
        "$id",
        "$defs",
        "$ref",
        "$anchor",
        "type",
        "format",
        "title",
        "description",
        "enum",
        "items",
        "prefixItems",
        "minItems",
        "maxItems",
        "minimum",
        "maximum",
        "anyOf",
        "oneOf",
        "properties",
        "required",
        "propertyOrdering",
    }
)


def sanitize_gemini_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return the subset accepted by Gemini's explicit JSON-schema field.

    Property and definition names are data, not schema keywords, so they are
    retained while their nested schemas are sanitized recursively. In
    particular, ``additionalProperties`` is deliberately omitted because the
    Gemini endpoint rejected its SDK-converted ``additional_properties`` form.
    Strict extra-field rejection remains a local ModelPrediction concern.
    """

    sanitized: dict[str, Any] = {}
    for key, value in schema.items():
        if key not in _GEMINI_JSON_SCHEMA_KEYWORDS:
            continue
        if key in {"properties", "$defs"} and isinstance(value, dict):
            sanitized[key] = {
                name: sanitize_gemini_json_schema(nested)
                for name, nested in value.items()
                if isinstance(nested, dict)
            }
        elif isinstance(value, dict):
            sanitized[key] = sanitize_gemini_json_schema(value)
        elif isinstance(value, list):
            sanitized[key] = [
                sanitize_gemini_json_schema(item)
                if isinstance(item, dict)
                else item
                for item in value
            ]
        else:
            sanitized[key] = value
    return sanitized


def gemini_model_prediction_schema() -> dict[str, Any]:
    """Build the explicit provider schema from the strict local model."""

    return sanitize_gemini_json_schema(ModelPrediction.model_json_schema())


@dataclass(frozen=True)
class RawGeneration:
    text: str
    usage: dict[str, Any] | None = None


class StructuredGenerationClient(Protocol):
    """Small provider-neutral boundary used by the baseline runner."""

    def generate(self, prompt: str) -> RawGeneration:
        """Make one independent structured-generation request."""


class LLMError(RuntimeError):
    """Base error for a failed model call."""


class ProviderErrorCategory(str, Enum):
    """Stable provider-error categories safe to persist in run artifacts."""

    AUTHENTICATION = "AUTHENTICATION"
    PERMISSION = "PERMISSION"
    QUOTA_OR_RATE_LIMIT = "QUOTA_OR_RATE_LIMIT"
    MODEL_NOT_FOUND = "MODEL_NOT_FOUND"
    INVALID_REQUEST_OR_SCHEMA = "INVALID_REQUEST_OR_SCHEMA"
    TRANSIENT_PROVIDER_ERROR = "TRANSIENT_PROVIDER_ERROR"
    UNKNOWN_PROVIDER_ERROR = "UNKNOWN_PROVIDER_ERROR"


@dataclass(frozen=True)
class ProviderErrorDiagnostic:
    provider: str
    exception_type: str
    status_code: int | None
    provider_status: str | None
    category: ProviderErrorCategory
    sanitized_provider_message: str
    retryable: bool
    request_id: str | None = None
    attempt_number: int | None = None

    def with_attempt(
        self, *, request_id: str | None, attempt_number: int
    ) -> ProviderErrorDiagnostic:
        return replace(
            self,
            request_id=request_id,
            attempt_number=attempt_number,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "exception_type": self.exception_type,
            "status_code": self.status_code,
            "provider_status": self.provider_status,
            "category": self.category.value,
            "sanitized_provider_message": self.sanitized_provider_message,
            "request_id": self.request_id,
            "attempt_number": self.attempt_number,
            "retryable": self.retryable,
        }


class ProviderLLMError(LLMError):
    """A provider failure carrying secret-safe structured diagnostics."""

    def __init__(self, diagnostic: ProviderErrorDiagnostic) -> None:
        self.diagnostic = diagnostic
        super().__init__(
            f"{diagnostic.category.value}: {diagnostic.sanitized_provider_message}"
        )


class TransientLLMError(ProviderLLMError):
    """A provider failure that may succeed on a bounded retry."""


class StructuredOutputError(LLMError):
    """A response that cannot be validated as ModelPrediction."""

    def __init__(self, message: str, *, raw_response: str) -> None:
        super().__init__(message)
        self.raw_response = raw_response


@dataclass(frozen=True)
class PredictionAttempt:
    prediction: ModelPrediction
    raw_response: str
    raw_responses: tuple[str, ...]
    attempt_errors: tuple[str, ...]
    provider_errors: tuple[ProviderErrorDiagnostic, ...]
    usage: dict[str, Any] | None
    attempt_count: int


@dataclass
class RetryExhaustedError(LLMError):
    """All permitted attempts failed, retaining exact non-secret diagnostics."""

    errors: list[str] = field(default_factory=list)
    raw_responses: list[str] = field(default_factory=list)
    provider_errors: list[ProviderErrorDiagnostic] = field(default_factory=list)
    attempt_count: int = 0
    retry_exhausted: bool = True

    def __str__(self) -> str:
        disposition = "exhausted retries" if self.retry_exhausted else "stopped"
        return (
            f"structured generation {disposition} after {self.attempt_count} "
            f"attempts: {self.errors[-1]}"
        )


_QUERY_CREDENTIAL_RE = re.compile(
    r"(?i)([?&](?:key|api[_-]?key|access[_-]?token|token)=)[^&#\s]+"
)
_HEADER_CREDENTIAL_RE = re.compile(
    r"(?i)((?:authorization|x-goog-api-key)\s*:\s*)(?:bearer\s+)?[^\s,;]+"
)
_ASSIGNED_CREDENTIAL_RE = re.compile(
    r"(?i)([\"']?(?:api[_-]?key|access[_-]?token|token)[\"']?\s*[:=]\s*[\"']?)"
    r"[^\"',}\s;&]+"
)
_GOOGLE_KEY_RE = re.compile(r"AIza[0-9A-Za-z_-]{20,}")
_BEARER_RE = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/-]+")


def sanitize_provider_message(message: object, *, api_key: str | None = None) -> str:
    """Redact known secrets and credential-like parameters from provider text."""

    text = str(message) if message is not None else "No provider message supplied"
    if api_key:
        text = text.replace(api_key, "[REDACTED]")
    text = _QUERY_CREDENTIAL_RE.sub(r"\1[REDACTED]", text)
    text = _HEADER_CREDENTIAL_RE.sub(r"\1[REDACTED]", text)
    text = _ASSIGNED_CREDENTIAL_RE.sub(r"\1[REDACTED]", text)
    text = _GOOGLE_KEY_RE.sub("[REDACTED]", text)
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    return text[:2000]


def _status_code(exc: Exception) -> int | None:
    for attribute in ("code", "status_code"):
        value = getattr(exc, attribute, None)
        if isinstance(value, int):
            return value
    return None


def _provider_status(exc: Exception) -> str | None:
    value = getattr(exc, "status", None)
    return str(value) if value else None


def _provider_category(
    status_code: int | None,
    provider_status: str | None,
    provider_message: object,
) -> ProviderErrorCategory:
    normalized_status = (provider_status or "").upper()
    normalized_message = str(provider_message or "").lower()
    invalid_credential_message = any(
        marker in normalized_message
        for marker in (
            "api key not valid",
            "invalid api key",
            "authentication credential",
        )
    )
    if (
        status_code == 401
        or normalized_status == "UNAUTHENTICATED"
        or invalid_credential_message
    ):
        return ProviderErrorCategory.AUTHENTICATION
    if status_code == 429 or normalized_status == "RESOURCE_EXHAUSTED":
        return ProviderErrorCategory.QUOTA_OR_RATE_LIMIT
    if status_code == 403 or normalized_status == "PERMISSION_DENIED":
        return ProviderErrorCategory.PERMISSION
    if status_code == 404 or normalized_status == "NOT_FOUND":
        return ProviderErrorCategory.MODEL_NOT_FOUND
    if status_code == 400 or normalized_status == "INVALID_ARGUMENT":
        return ProviderErrorCategory.INVALID_REQUEST_OR_SCHEMA
    if status_code in {408, 500, 502, 503, 504}:
        return ProviderErrorCategory.TRANSIENT_PROVIDER_ERROR
    return ProviderErrorCategory.UNKNOWN_PROVIDER_ERROR


def provider_error_diagnostic(
    exc: Exception,
    *,
    provider: str,
    api_key: str | None = None,
) -> ProviderErrorDiagnostic:
    """Extract only diagnostic fields that are safe and useful to persist."""

    status_code = _status_code(exc)
    provider_status = _provider_status(exc)
    provider_message = getattr(exc, "message", None) or str(exc)
    category = _provider_category(status_code, provider_status, provider_message)
    return ProviderErrorDiagnostic(
        provider=provider,
        exception_type=type(exc).__name__,
        status_code=status_code,
        provider_status=provider_status,
        category=category,
        sanitized_provider_message=sanitize_provider_message(
            provider_message, api_key=api_key
        ),
        retryable=category == ProviderErrorCategory.TRANSIENT_PROVIDER_ERROR,
    )


def _usage_dict(response: Any) -> dict[str, Any] | None:
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return None
    if hasattr(usage, "model_dump"):
        return usage.model_dump(mode="json", exclude_none=True)
    return {"reported": str(usage)}


class GoogleGenAIClient:
    """Official google-genai adapter; authentication remains SDK-managed."""

    def __init__(
        self,
        settings: LLMSettings,
        *,
        temperature: float = 0.0,
        seed: int | None = None,
        max_output_tokens: int = 2048,
    ) -> None:
        from google import genai
        from google.genai import types

        self._settings = settings
        self._types = types
        client_options: dict[str, Any] = {"api_key": settings.api_key}
        if settings.base_url:
            client_options["http_options"] = types.HttpOptions(
                base_url=settings.base_url
            )
        self._client = genai.Client(**client_options)
        self._temperature = temperature
        self._seed = seed
        self._max_output_tokens = max_output_tokens

    def generate(self, prompt: str) -> RawGeneration:
        config = self._types.GenerateContentConfig(
            temperature=self._temperature,
            seed=self._seed,
            max_output_tokens=self._max_output_tokens,
            response_mime_type="application/json",
            response_json_schema=gemini_model_prediction_schema(),
        )
        try:
            response = self._client.models.generate_content(
                model=self._settings.model,
                contents=prompt,
                config=config,
            )
        except Exception as exc:
            diagnostic = provider_error_diagnostic(
                exc,
                provider=self._settings.provider,
                api_key=self._settings.api_key,
            )
            error_type = TransientLLMError if diagnostic.retryable else ProviderLLMError
            raise error_type(diagnostic) from exc
        try:
            text = response.text
        except Exception as exc:
            diagnostic = provider_error_diagnostic(
                exc,
                provider=self._settings.provider,
                api_key=self._settings.api_key,
            )
            raise ProviderLLMError(diagnostic) from exc
        if not isinstance(text, str) or not text.strip():
            raise StructuredOutputError(
                "provider returned no structured response text", raw_response=""
            )
        return RawGeneration(text=text, usage=_usage_dict(response))


def generate_prediction_with_retry(
    client: StructuredGenerationClient,
    prompt: str,
    *,
    max_attempts: int = 3,
    expected_request_id: str | None = None,
) -> PredictionAttempt:
    """Retry only transient and structured-output failures, never indefinitely."""

    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    errors: list[str] = []
    raw_responses: list[str] = []
    provider_errors: list[ProviderErrorDiagnostic] = []
    for attempt in range(1, max_attempts + 1):
        try:
            raw = client.generate(prompt)
            raw_responses.append(raw.text)
            try:
                prediction = ModelPrediction.model_validate_json(raw.text)
            except ValidationError as exc:
                raise StructuredOutputError(
                    f"ModelPrediction validation failed: {exc}",
                    raw_response=raw.text,
                ) from exc
            if (
                expected_request_id is not None
                and prediction.request_id != expected_request_id
            ):
                raise StructuredOutputError(
                    "ModelPrediction request_id does not match the current request",
                    raw_response=raw.text,
                )
            return PredictionAttempt(
                prediction=prediction,
                raw_response=raw.text,
                raw_responses=tuple(raw_responses),
                attempt_errors=tuple(errors),
                provider_errors=tuple(provider_errors),
                usage=raw.usage,
                attempt_count=attempt,
            )
        except ProviderLLMError as exc:
            diagnostic = exc.diagnostic.with_attempt(
                request_id=expected_request_id,
                attempt_number=attempt,
            )
            provider_errors.append(diagnostic)
            errors.append(str(exc))
            if not diagnostic.retryable:
                raise RetryExhaustedError(
                    errors=errors,
                    raw_responses=raw_responses,
                    provider_errors=provider_errors,
                    attempt_count=attempt,
                    retry_exhausted=False,
                ) from exc
        except StructuredOutputError as exc:
            errors.append(str(exc))
            if isinstance(exc, StructuredOutputError) and (
                not raw_responses or raw_responses[-1] != exc.raw_response
            ):
                raw_responses.append(exc.raw_response)
    raise RetryExhaustedError(
        errors=errors,
        raw_responses=raw_responses,
        provider_errors=provider_errors,
        attempt_count=max_attempts,
        retry_exhausted=True,
    )
