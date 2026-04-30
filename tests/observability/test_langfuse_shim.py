"""Behavioural tests for the Langfuse observability shim.

Two regimes:
  - **unconfigured** (default in CI / fresh checkout): the shim must
    return a no-op span and never instantiate the SDK.
  - **configured** (explicit fake injected): the shim must forward
    `traced(...)` calls into the SDK's
    `start_as_current_observation` and pass through `update(...)`
    on the yielded span.

We exercise the shim directly here; the extractor- and scoring-level
tracing assertions live in `tests/observability/test_traced_paths.py`.
"""

from __future__ import annotations

from typing import Any

import pytest

from clinical_demo import observability as obs
from clinical_demo.observability import langfuse_client
from clinical_demo.settings import Settings, get_settings


@pytest.fixture(autouse=True)
def _reset_caches() -> Any:
    """Settings + langfuse client are LRU-cached across the process;
    every test in this file mutates the env, so reset before/after."""
    get_settings.cache_clear()
    langfuse_client.get_client.cache_clear()
    yield
    get_settings.cache_clear()
    langfuse_client.get_client.cache_clear()


# ---------- unconfigured regime ----------


def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip both real env vars and the on-disk `.env` so the test
    sees a truly unconfigured Settings, regardless of the dev
    environment Langfuse keys."""
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)
    monkeypatch.delenv("LANGFUSE_BASE_URL", raising=False)
    monkeypatch.setitem(Settings.model_config, "env_file", None)


def test_get_client_returns_none_when_keys_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_env(monkeypatch)
    assert langfuse_client.get_client() is None
    assert obs.is_enabled() is False


def test_traced_is_a_noop_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_env(monkeypatch)
    ran = []
    with obs.traced("any-name", as_type="generation", model="m") as span:
        ran.append("inside")
        span.update(output={"x": 1}, usage_details={"input": 1})
        span.set_status("ERROR")
        span.end()
    assert ran == ["inside"]


def test_flush_is_safe_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_env(monkeypatch)
    obs.flush()  # must not raise


# ---------- configured regime (with a recording fake) ----------


class _RecordingSpan:
    """Minimal stand-in for a Langfuse observation."""

    def __init__(self, name: str, kwargs: dict[str, Any]) -> None:
        self.name = name
        self.start_kwargs = kwargs
        self.updates: list[dict[str, Any]] = []
        self.entered = False
        self.exited = False

    def update(self, **kwargs: Any) -> None:
        self.updates.append(kwargs)

    def set_status(self, status: str, **_kwargs: Any) -> None:
        self.updates.append({"status": status})

    def end(self, **_kwargs: Any) -> None:
        return None

    def __enter__(self) -> _RecordingSpan:
        self.entered = True
        return self

    def __exit__(self, *_args: object) -> None:
        self.exited = True


class _RecordingClient:
    """Captures every `start_as_current_observation` call."""

    def __init__(self) -> None:
        self.spans: list[_RecordingSpan] = []
        self.flushes = 0

    def start_as_current_observation(self, **kwargs: Any) -> _RecordingSpan:
        span = _RecordingSpan(kwargs.get("name", "<unnamed>"), kwargs)
        self.spans.append(span)
        return span

    def flush(self) -> None:
        self.flushes += 1


def _install_fake_client(monkeypatch: pytest.MonkeyPatch) -> _RecordingClient:
    """Helper: replace the cached client with a recording fake."""
    fake = _RecordingClient()
    langfuse_client.get_client.cache_clear()
    monkeypatch.setattr(langfuse_client, "get_client", lambda: fake)
    return fake


def test_traced_forwards_to_client_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install_fake_client(monkeypatch)

    with obs.traced(
        "extract_criteria",
        as_type="generation",
        model="gpt-4o-mini-2024-07-18",
        input="hello",
        metadata={"prompt_version": "extractor-v0.1"},
    ) as span:
        span.update(output={"ok": True}, usage_details={"input": 10, "output": 20})

    assert len(fake.spans) == 1
    rec = fake.spans[0]
    assert rec.name == "extract_criteria"
    assert rec.start_kwargs["as_type"] == "generation"
    assert rec.start_kwargs["model"] == "gpt-4o-mini-2024-07-18"
    assert rec.start_kwargs["input"] == "hello"
    assert rec.start_kwargs["metadata"] == {"prompt_version": "extractor-v0.1"}
    assert rec.entered and rec.exited
    assert rec.updates == [{"output": {"ok": True}, "usage_details": {"input": 10, "output": 20}}]


def test_flush_forwards_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install_fake_client(monkeypatch)
    obs.flush()
    obs.flush()
    assert fake.flushes == 2


def test_traced_swallows_client_exceptions_and_yields_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Observability must never break the application path. If the
    client errors during span construction, the application code
    inside the `with` must still run."""

    class _BrokenClient:
        def start_as_current_observation(self, **_kwargs: Any) -> Any:
            raise RuntimeError("langfuse exporter exploded")

        def flush(self) -> None:  # pragma: no cover - flush not exercised here
            pass

    langfuse_client.get_client.cache_clear()
    monkeypatch.setattr(langfuse_client, "get_client", lambda: _BrokenClient())

    ran = False
    with obs.traced("anything") as span:
        ran = True
        span.update(output={"still_ok": True})
    assert ran is True


def test_application_exceptions_propagate_through_traced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shim must not swallow errors raised inside the `with` block —
    those are application errors and the caller decides how to handle
    them."""
    _install_fake_client(monkeypatch)
    with pytest.raises(ValueError, match="boom"), obs.traced("anything"):
        raise ValueError("boom")


# ---------- settings helper ----------


def test_settings_helper_detects_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Otherwise `delenv` below does not clear configuration: pydantic
    # still reads LANGFUSE_* from `.env` on disk (dev machines / CI with secrets file).
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-x")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-x")
    get_settings.cache_clear()
    assert get_settings().is_langfuse_configured is True

    monkeypatch.delenv("LANGFUSE_SECRET_KEY")
    get_settings.cache_clear()
    assert get_settings().is_langfuse_configured is False


def test_settings_accepts_langfuse_base_url_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://us.cloud.langfuse.com")
    get_settings.cache_clear()
    assert get_settings().langfuse_host == "https://us.cloud.langfuse.com"
