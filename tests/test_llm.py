from __future__ import annotations

from dataclasses import dataclass
import json

import pytest
from google.genai import errors, types
from pydantic import ValidationError

from spectrace.config import LLMSettings
from spectrace.llm import (
    GoogleGenAIClient,
    ProviderErrorCategory,
    ProviderLLMError,
    TransientLLMError,
    gemini_model_prediction_schema,
    sanitize_gemini_json_schema,
)
from spectrace.models import Classification, ModelPrediction


FAKE_KEY = "unit-test-secret-value"


@dataclass
class FakeModels:
    error: Exception
    calls: int = 0
    last_kwargs: dict[str, object] | None = None

    def generate_content(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
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


def _prediction_payload() -> dict[str, object]:
    return {
        "request_id": "CR-001",
        "classification": "IN_SCOPE",
        "supporting_evidence_ids": ["SOW-SCP-003"],
        "conflicting_evidence_ids": [],
        "requires_clarification": False,
        "clarification_questions": [],
        "dependencies": ["Existing listing data"],
        "reasoning_summary": "The requested listing content is approved.",
        "cumulative_drift_detected": False,
        "cumulative_related_request_ids": [],
        "cumulative_related_decision_ids": [],
    }


def _schema_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {
            nested_key
            for nested_value in value.values()
            for nested_key in _schema_keys(nested_value)
        }
    if isinstance(value, list):
        return {
            nested_key
            for nested_value in value
            for nested_key in _schema_keys(nested_value)
        }
    return set()


def test_outbound_schema_removes_incompatible_metadata_recursively() -> None:
    original = {
        "type": "object",
        "title": "Outer",
        "additionalProperties": False,
        "additional_properties": False,
        "default": {},
        "properties": {
            "nested": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "value": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 20,
                        "examples": ["example"],
                    }
                },
                "required": ["value"],
            }
        },
        "required": ["nested"],
    }

    outbound = sanitize_gemini_json_schema(original)
    keys = _schema_keys(outbound)
    assert "additionalProperties" not in keys
    assert "additional_properties" not in keys
    assert "default" not in keys
    assert "examples" not in keys
    assert "minLength" not in keys
    assert "maxLength" not in keys
    assert outbound["properties"]["nested"]["properties"]["value"] == {
        "type": "string"
    }
    assert outbound["required"] == ["nested"]
    assert outbound["properties"]["nested"]["required"] == ["value"]


def test_outbound_prediction_schema_preserves_fields_required_list_and_taxonomy() -> None:
    outbound = gemini_model_prediction_schema()
    local = ModelPrediction.model_json_schema()
    keys = _schema_keys(outbound)
    assert "additionalProperties" not in keys
    assert "additional_properties" not in keys
    assert set(outbound["properties"]) == set(ModelPrediction.model_fields)
    assert outbound["required"] == local["required"]
    assert outbound["$defs"]["Classification"]["enum"] == [
        classification.value for classification in Classification
    ]


def test_extra_returned_field_is_still_rejected_by_strict_local_model() -> None:
    payload = _prediction_payload()
    payload["unexpected_provider_field"] = "must fail locally"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ModelPrediction.model_validate_json(json.dumps(payload))


def test_valid_returned_json_still_parses_with_strict_local_model() -> None:
    prediction = ModelPrediction.model_validate_json(json.dumps(_prediction_payload()))
    assert prediction.request_id == "CR-001"
    assert prediction.classification == Classification.IN_SCOPE


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
    assert models.last_kwargs is not None
    config = models.last_kwargs["config"]
    assert isinstance(config, types.GenerateContentConfig)
    assert config.response_schema is None
    assert config.response_json_schema == gemini_model_prediction_schema()
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
