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

from pydantic import AliasChoices, Field, SecretStr
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
    # Accept either LANGFUSE_HOST (Langfuse SDK's canonical env var) or
    # LANGFUSE_BASE_URL (the convention some teams use; the user's
    # `.env` may use either). Both alias the same field; SDK only reads
    # `LANGFUSE_HOST`, so we re-export it via env in the observability
    # shim before constructing the client.
    langfuse_host: str = Field(
        default="https://cloud.langfuse.com",
        validation_alias=AliasChoices("LANGFUSE_HOST", "LANGFUSE_BASE_URL"),
    )

    extractor_model: str = "gpt-4o-mini-2024-07-18"
    extractor_temperature: float = 0.0
    extractor_max_output_tokens: int = 4096

    @property
    def is_langfuse_configured(self) -> bool:
        """True iff both Langfuse credentials are set.

        Code paths that emit traces should check this before calling
        into the SDK so that callers without keys (CI, local dev with
        a fresh checkout) get a no-op rather than a runtime crash."""
        return self.langfuse_public_key is not None and self.langfuse_secret_key is not None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Uses an LRU cache so reads are cheap and so test code can clear
    the cache via `get_settings.cache_clear()` after monkey-patching
    the env.
    """
    return Settings()
