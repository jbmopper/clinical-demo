"""Tests for the ScoringState reducers."""

from __future__ import annotations

import operator
from typing import Annotated, get_type_hints

from clinical_demo.graph.state import ScoringState


def test_indexed_verdicts_uses_add_reducer() -> None:
    """The reducer is the sole concurrency contract between the
    matcher branches; pin its identity so a refactor that swaps
    `operator.add` for `+` (or whatever) trips a test."""
    hints = get_type_hints(ScoringState, include_extras=True)
    annotation = hints["indexed_verdicts"]
    # `Annotated[list[tuple[int, MatchVerdict]], operator.add]`
    metadata = getattr(annotation, "__metadata__", ())
    assert metadata == (operator.add,)


def test_indexed_verdicts_concatenation_semantics() -> None:
    """Sanity: applying the reducer to two list slices yields the
    concatenation, in arrival order. This is the contract every
    matcher branch relies on."""
    a: list[tuple[int, str]] = [(0, "v0"), (2, "v2")]
    b: list[tuple[int, str]] = [(1, "v1")]
    merged = operator.add(a, b)
    assert merged == [(0, "v0"), (2, "v2"), (1, "v1")]


def test_state_is_typeddict_total_false() -> None:
    """`total=False` is essential: per-branch state slices only carry
    the keys that branch needs. Pin it so a refactor doesn't
    accidentally make every key required."""
    assert ScoringState.__total__ is False


_ = Annotated  # kept imported for the type hint above
