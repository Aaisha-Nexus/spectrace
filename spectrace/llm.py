"""Provider-aware structured generation with a Google GenAI implementation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import ValidationError

from spectrace.config import LLMSettings
from spectrace.models import ModelPrediction


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


class TransientLLMError(LLMError):
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
    usage: dict[str, Any] | None
    attempt_count: int


@dataclass
class RetryExhaustedError(LLMError):
    """All permitted attempts failed, retaining exact non-secret diagnostics."""

    errors: list[str] = field(default_factory=list)
    raw_responses: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return f"structured generation failed after {len(self.errors)} attempts: {self.errors[-1]}"


def _status_code(exc: Exception) -> int | None:
    for attribute in ("code", "status_code"):
        value = getattr(exc, attribute, None)
        if isinstance(value, int):
            return value
    return None


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
            response_schema=ModelPrediction,
        )
        try:
            response = self._client.models.generate_content(
                model=self._settings.model,
                contents=prompt,
                config=config,
            )
        except Exception as exc:
            if _status_code(exc) in {408, 429, 500, 502, 503, 504}:
                raise TransientLLMError(
                    f"transient provider error ({_status_code(exc)})"
                ) from exc
            raise LLMError(f"provider request failed: {type(exc).__name__}") from exc
        try:
            text = response.text
        except Exception as exc:
            raise LLMError(
                f"provider response text unavailable: {type(exc).__name__}"
            ) from exc
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
                usage=raw.usage,
                attempt_count=attempt,
            )
        except (TransientLLMError, StructuredOutputError) as exc:
            errors.append(str(exc))
            if isinstance(exc, StructuredOutputError) and (
                not raw_responses or raw_responses[-1] != exc.raw_response
            ):
                raw_responses.append(exc.raw_response)
    raise RetryExhaustedError(errors=errors, raw_responses=raw_responses)
