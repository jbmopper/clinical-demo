"""Thin wrapper around the Langfuse v4 SDK.

Two design properties drive everything in this module:

1. **No-op when unconfigured.** A fresh checkout, a CI runner, or a
   local dev session without Langfuse keys must run end-to-end
   without crashing. The shim returns a sentinel client that
   accepts the same observation-context API but discards everything.
   No code path that emits a trace should have to know whether
   tracing is on.

2. **Defensive on every call.** Observability is *never* allowed to
   break the application path it instruments. Every Langfuse call
   the shim makes is wrapped in try/except; on failure we log a
   warning and continue. A lost trace is acceptable; a lost
   eligibility verdict because the analytics provider is down is
   not.

The actual Langfuse SDK class is imported lazily inside helpers so
that test code that monkey-patches `langfuse.Langfuse` works, and
so that import-time of the application doesn't pay the SDK setup
cost (which spins up an OpenTelemetry exporter thread) when tracing
is off.

Usage
-----
    from clinical_demo.observability import traced

    with traced("extract_criteria", as_type="generation",
                model="gpt-4o-mini", input=eligibility_text) as span:
        result = client.chat.completions.parse(...)
        span.update(output=result, usage_details={
            "input": result.usage.prompt_tokens,
            "output": result.usage.completion_tokens,
        })

If Langfuse keys are absent, `traced(...)` returns a no-op span whose
`.update()` / `.set_status()` / etc. methods exist but do nothing.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from typing import Any, Literal

from ..settings import Settings, get_settings

logger = logging.getLogger(__name__)

ObservationType = Literal["span", "generation", "event"]


class _NoopSpan:
    """Sentinel observation returned when tracing is disabled.

    Implements the subset of the Langfuse observation API the
    application uses. Methods accept arbitrary kwargs and silently
    discard them so production callers don't need an
    `if span is not None` everywhere."""

    def update(self, **_kwargs: Any) -> None: ...

    def update_trace(self, **_kwargs: Any) -> None: ...

    def end(self, **_kwargs: Any) -> None: ...

    def set_status(self, _status: str, **_kwargs: Any) -> None: ...

    def __enter__(self) -> _NoopSpan:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


@lru_cache(maxsize=1)
def get_client() -> Any | None:
    """Return the singleton Langfuse client, or None if unconfigured.

    Cached so that repeated callers share one client (and one
    OpenTelemetry exporter thread). Test code can clear the cache
    via `get_client.cache_clear()` after monkey-patching the env or
    settings. We don't construct the SDK class until both keys are
    present so that fresh checkouts pay zero startup cost.
    """
    settings = get_settings()
    if not settings.is_langfuse_configured:
        return None
    try:
        # Re-export both env-var aliases so the SDK reads them no
        # matter which spelling the user's .env used. The SDK
        # constructor *also* takes them explicitly below; the env
        # export is belt-and-suspenders for any sub-process that
        # the SDK spawns (otel exporter thread, etc.).
        if settings.langfuse_public_key is not None:
            os.environ["LANGFUSE_PUBLIC_KEY"] = settings.langfuse_public_key.get_secret_value()
        if settings.langfuse_secret_key is not None:
            os.environ["LANGFUSE_SECRET_KEY"] = settings.langfuse_secret_key.get_secret_value()
        os.environ["LANGFUSE_HOST"] = settings.langfuse_host

        from langfuse import Langfuse  # lazy: avoid OTel import at app start

        return Langfuse(
            public_key=settings.langfuse_public_key.get_secret_value()
            if settings.langfuse_public_key
            else None,
            secret_key=settings.langfuse_secret_key.get_secret_value()
            if settings.langfuse_secret_key
            else None,
            host=settings.langfuse_host,
        )
    except Exception:
        logger.warning("failed to initialise Langfuse; tracing disabled", exc_info=True)
        return None


def is_enabled() -> bool:
    """True iff a real Langfuse client is available.

    Cheap and side-effect free; useful in callers that want to
    short-circuit expensive serialisation work when tracing is off."""
    return get_client() is not None


@contextmanager
def traced(
    name: str,
    *,
    as_type: ObservationType = "span",
    settings: Settings | None = None,
    **observation_kwargs: Any,
) -> Iterator[Any]:
    """Start a Langfuse observation and yield the span object.

    Parameters
    ----------
    name : str
        Observation name; shows up as the row label in the Langfuse UI.
    as_type : "span" | "generation" | "event", default "span"
        `"generation"` is the right choice for individual LLM calls
        (prompts/completions/usage shown specially in the UI);
        `"span"` is the default for application-level work units;
        `"event"` is a zero-duration marker.
    **observation_kwargs : Any
        Forwarded to `client.start_as_current_observation`. Common
        keys: `input`, `output`, `metadata`, `model`,
        `model_parameters`, `usage_details`, `cost_details`,
        `version`.

    Yields
    ------
    The Langfuse observation object on the happy path; a `_NoopSpan`
    sentinel when tracing is unconfigured or fails to initialize.
    Either way, code inside the `with` block runs unchanged.
    """
    settings = settings or get_settings()
    client = get_client()
    if client is None:
        yield _NoopSpan()
        return

    cm = None
    try:
        cm = client.start_as_current_observation(
            name=name,
            as_type=as_type,
            **observation_kwargs,
        )
    except Exception:
        logger.warning(
            "failed to start Langfuse observation %r; running without trace",
            name,
            exc_info=True,
        )
        yield _NoopSpan()
        return

    try:
        with cm as span:
            yield span
    except Exception:
        # Re-raise: observability must not swallow application errors.
        raise


def flush() -> None:
    """Block until all pending observations have been sent.

    Call once at process exit (or at the end of a CLI invocation) so
    short-lived runs don't drop traces. No-op if tracing is off."""
    client = get_client()
    if client is None:
        return
    try:
        client.flush()
    except Exception:
        logger.warning("Langfuse flush failed", exc_info=True)


__all__ = [
    "ObservationType",
    "flush",
    "get_client",
    "is_enabled",
    "traced",
]
