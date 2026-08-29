"""Secret-safe, provider-aware configuration for model-backed commands."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values


class ConfigurationError(ValueError):
    """Raised when required model configuration is missing or unsupported."""


@dataclass(frozen=True)
class LLMSettings:
    provider: str
    model: str
    api_key: str
    base_url: str | None = None


def load_llm_settings(
    *,
    environ: Mapping[str, str] | None = None,
    dotenv_path: str | Path | None = ".env",
) -> LLMSettings:
    """Load settings without ever including a secret value in an error.

    Passing ``environ`` makes loading fully isolated and disables .env access,
    which keeps tests independent of a developer's local configuration.
    """

    if environ is None:
        values: dict[str, str | None] = {}
        if dotenv_path is not None:
            values.update(dotenv_values(dotenv_path))
        values.update(os.environ)
    else:
        values = dict(environ)

    provider = (values.get("LLM_PROVIDER") or "").strip().lower()
    model = (values.get("LLM_MODEL") or "").strip()
    api_key = (values.get("LLM_API_KEY") or "").strip()
    base_url = (values.get("LLM_BASE_URL") or "").strip() or None

    missing = [
        name
        for name, value in (
            ("LLM_PROVIDER", provider),
            ("LLM_MODEL", model),
            ("LLM_API_KEY", api_key),
        )
        if not value
    ]
    if missing:
        raise ConfigurationError(
            "missing required configuration: " + ", ".join(missing)
        )
    if provider != "google":
        raise ConfigurationError(
            "unsupported LLM_PROVIDER; this milestone requires 'google'"
        )
    return LLMSettings(
        provider=provider,
        model=model,
        api_key=api_key,
        base_url=base_url,
    )


def safe_settings_summary(settings: LLMSettings) -> dict[str, str | bool]:
    """Return display-safe configuration metadata with no credential value."""

    return {
        "provider": settings.provider,
        "model": settings.model,
        "api_key_configured": bool(settings.api_key),
        "base_url_configured": settings.base_url is not None,
    }
