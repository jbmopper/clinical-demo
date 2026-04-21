"""Centralized settings: API keys, default models, observability config.

Read from environment (with .env loaded as a fallback). Keys are
**never** persisted to disk by this module — `.env` is in `.gitignore`,
and pydantic-settings holds them in memory only.

Construct via the singleton accessor `get_settings()` so that
imports don't pay the env-parse cost more than once and so test code
can swap the cached instance via `set_settings_for_test`.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process-wide configuration.

    All credentials are `SecretStr` so they don't leak into logs or
    error messages by accident; call `.get_secret_value()` only at
    the call-site that actually needs the raw string.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    openai_api_key: SecretStr | None = Field(default=None)
    anthropic_api_key: SecretStr | None = Field(default=None)
    google_api_key: SecretStr | None = Field(default=None)

    langfuse_public_key: SecretStr | None = Field(default=None)
    langfuse_secret_key: SecretStr | None = Field(default=None)
    langfuse_host: str = "https://cloud.langfuse.com"

    extractor_model: str = "gpt-4o-mini-2024-07-18"
    extractor_temperature: float = 0.0
    extractor_max_output_tokens: int = 4096


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Uses an LRU cache so reads are cheap and so test code can clear
    the cache via `get_settings.cache_clear()` after monkey-patching
    the env.
    """
    return Settings()
