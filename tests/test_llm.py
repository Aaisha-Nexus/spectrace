from __future__ import annotations

from dataclasses import dataclass

import pytest
from google.genai import errors, types

from spectrace.config import LLMSettings
from spectrace.llm import (
    GoogleGenAIClient,
    ProviderErrorCategory,
    ProviderLLMError,
    TransientLLMError,
)


FAKE_KEY = "unit-test-secret-value"


@dataclass
class FakeModels:
    error: Exception
    calls: int = 0

    def generate_content(self, **kwargs):
        self.calls += 1
        raise self.error


@dataclass
class FakeSDKClient:
    models: FakeModels


def _adapter(error: Exception) -> tuple[GoogleGenAIClient, FakeModels]:
    models = FakeModels(error)
    adapter = object.__new__(GoogleGenAIClient)
    adapter._settings = LLMSettings("google", "fake-model", FAKE_KEY)
    adapter._types = types
    adapter._client = FakeSDKClient(models)
    adapter._temperature = 0.0
    adapter._seed = None
    adapter._max_output_tokens = 2048
    return adapter, models


@pytest.mark.parametrize(
    ("status_code", "provider_status", "category", "retryable"),
    [
        (400, "INVALID_ARGUMENT", ProviderErrorCategory.INVALID_REQUEST_OR_SCHEMA, False),
        (401, "UNAUTHENTICATED", ProviderErrorCategory.AUTHENTICATION, False),
        (403, "PERMISSION_DENIED", ProviderErrorCategory.PERMISSION, False),
        (404, "NOT_FOUND", ProviderErrorCategory.MODEL_NOT_FOUND, False),
        (429, "RESOURCE_EXHAUSTED", ProviderErrorCategory.QUOTA_OR_RATE_LIMIT, False),
        (500, "INTERNAL", ProviderErrorCategory.TRANSIENT_PROVIDER_ERROR, True),
        (502, "BAD_GATEWAY", ProviderErrorCategory.TRANSIENT_PROVIDER_ERROR, True),
        (503, "UNAVAILABLE", ProviderErrorCategory.TRANSIENT_PROVIDER_ERROR, True),
        (504, "DEADLINE_EXCEEDED", ProviderErrorCategory.TRANSIENT_PROVIDER_ERROR, True),
    ],
)
def test_google_adapter_maps_and_sanitizes_provider_errors_offline(
    status_code: int,
    provider_status: str,
    category: ProviderErrorCategory,
    retryable: bool,
) -> None:
    error_type = errors.ServerError if status_code >= 500 else errors.ClientError
    error = error_type(
        status_code,
        {
            "error": {
                "status": provider_status,
                "message": (
                    "Provider diagnostic "
                    "https://example.invalid/path?"
                    + "key="
                    + FAKE_KEY
                    + "&safe=yes "
                    + "Authoriza"
                    + "tion: "
                    + "Bearer "
                    + "fake-bearer-token"
                ),
            }
        },
    )
    adapter, models = _adapter(error)
    expected_exception = TransientLLMError if retryable else ProviderLLMError

    with pytest.raises(expected_exception) as captured:
        adapter.generate("offline fake prompt")

    diagnostic = captured.value.diagnostic
    assert models.calls == 1
    assert diagnostic.provider == "google"
    assert diagnostic.exception_type == error_type.__name__
    assert diagnostic.status_code == status_code
    assert diagnostic.provider_status == provider_status
    assert diagnostic.category == category
    assert diagnostic.retryable is retryable
    assert FAKE_KEY not in diagnostic.sanitized_provider_message
    assert "fake-bearer-token" not in diagnostic.sanitized_provider_message
    assert "[REDACTED]" in diagnostic.sanitized_provider_message


def test_invalid_api_key_message_is_authentication_even_when_http_status_is_400() -> None:
    error = errors.ClientError(
        400,
        {
            "error": {
                "status": "INVALID_ARGUMENT",
                "message": f"API key not valid: api_key={FAKE_KEY}",
            }
        },
    )
    adapter, models = _adapter(error)

    with pytest.raises(ProviderLLMError) as captured:
        adapter.generate("offline fake prompt")

    diagnostic = captured.value.diagnostic
    assert models.calls == 1
    assert diagnostic.category == ProviderErrorCategory.AUTHENTICATION
    assert diagnostic.retryable is False
    assert FAKE_KEY not in diagnostic.sanitized_provider_message
