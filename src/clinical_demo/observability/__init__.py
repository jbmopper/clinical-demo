"""Observability shim for the clinical-demo project.

Wraps Langfuse v4 in a tiny adapter that:
  - is a no-op when Langfuse keys are not configured (so CI, local
    dev, and unit tests work without the SDK), and
  - is defensive on every call (a failing tracer never breaks the
    application path it instruments).

Single import surface — application code uses `traced(...)` and
`flush()` and never touches the SDK directly. See
`langfuse_client` for the full design rationale."""

from .langfuse_client import (
    ObservationType,
    flush,
    get_client,
    is_enabled,
    traced,
)

__all__ = [
    "ObservationType",
    "flush",
    "get_client",
    "is_enabled",
    "traced",
]
