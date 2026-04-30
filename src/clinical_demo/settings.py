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
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

BindingStrategy = Literal["alias"]


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

    # NLM UTS API key, used as the password against
    # https://uts.nlm.nih.gov for VSAC, RxNorm, and UMLS REST calls.
    # See PLAN.md §12 D-69 for the terminology-API comparison this
    # unblocks. Optional: the current hand-curated alias path does
    # not need it, so a fresh checkout still runs without an NLM
    # account.
    umls_api_key: SecretStr | None = Field(default=None)

    # Reserved for future terminology-binding experiments. Only
    # `alias` is accepted today because the matcher still uses the
    # hand-curated lookup path; accepting inactive strategy names
    # would make eval runs look terminology-backed when they are not.
    binding_strategy: BindingStrategy = "alias"

    # Where the terminology cache (D-69 follow-on slice 2) writes
    # resolved bindings. Lives under `data/cache/` which is already
    # gitignored, so cached VSAC/RxNorm/UMLS results don't pollute
    # commits but do persist across local runs and shells. Override
    # via `TERMINOLOGY_CACHE_DIR` for tests or for sharing a cache
    # between checkouts.
    terminology_cache_dir: Path = Path("data/cache/terminology")

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
    # gpt-4o-mini supports 16384 output tokens. The extractor returns a
    # structured array of criteria that scales with eligibility-text
    # length; trials in the curated set hit ~6.3k input tokens and can
    # exceed the old 4096 output ceiling on the largest protocols
    # (NCT05268237 was the first observed case). Headroom is free
    # below the actual response size — provider only bills for tokens
    # emitted — so we set the cap at the model's hard ceiling and rely
    # on the graceful-truncation path in the extractor for anything
    # larger.
    extractor_max_output_tokens: int = 16384

    # Per-criterion structured verdict from the LLM matcher node. A
    # verdict is a small object (verdict + reason + 1-2 sentence
    # rationale + a list of evidence ids), so 1024 tokens is roughly
    # 4-8x what we'd ever expect to see, but cheap insurance against
    # length truncation on a free-text criterion that prompts a long
    # rationale. Was 512.
    llm_matcher_max_output_tokens: int = 1024

    # Critic emits a list of structured findings across all criteria
    # in the rollup, so its output scales with criterion count, not
    # with any single criterion. 2048 lets the critic flag warnings
    # on a ~30-criterion trial without overflow. Was 1024.
    critic_max_output_tokens: int = 2048

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
