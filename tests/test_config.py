from __future__ import annotations

import pytest

from spectrace.config import (
    ConfigurationError,
    load_llm_settings,
    safe_settings_summary,
)


FAKE_ENV = {
    "LLM_PROVIDER": "google",
    "LLM_MODEL": "fake-gemini-model",
    "LLM_API_KEY": "unit-test-secret-value",
    "LLM_BASE_URL": "https://example.invalid",
}


def test_configuration_loads_from_explicit_fake_environment() -> None:
    settings = load_llm_settings(environ=FAKE_ENV)
    assert settings.provider == "google"
    assert settings.model == "fake-gemini-model"
    assert settings.api_key == "unit-test-secret-value"
    assert settings.base_url == "https://example.invalid"


def test_safe_summary_never_contains_api_key() -> None:
    settings = load_llm_settings(environ=FAKE_ENV)
    rendered = repr(safe_settings_summary(settings))
    assert "unit-test-secret-value" not in rendered
    assert safe_settings_summary(settings)["api_key_configured"] is True


@pytest.mark.parametrize("missing_name", ["LLM_PROVIDER", "LLM_MODEL", "LLM_API_KEY"])
def test_missing_configuration_fails_without_leaking_secret(missing_name: str) -> None:
    fake = dict(FAKE_ENV)
    fake.pop(missing_name)
    with pytest.raises(ConfigurationError) as captured:
        load_llm_settings(environ=fake)
    message = str(captured.value)
    assert missing_name in message
    assert "unit-test-secret-value" not in message


def test_non_google_provider_fails_without_leaking_secret() -> None:
    fake = dict(FAKE_ENV, LLM_PROVIDER="another-provider")
    with pytest.raises(ConfigurationError) as captured:
        load_llm_settings(environ=fake)
    assert "requires 'google'" in str(captured.value)
    assert "unit-test-secret-value" not in str(captured.value)
