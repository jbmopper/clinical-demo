"""Deterministic match node: thin LangGraph wrapper over `match_criterion`.

The node intentionally does not duplicate matcher logic. It reads
`(_criterion, _criterion_index, profile, trial)` off the per-branch
state slice that `fan_out_criteria` constructs, calls into the
existing `clinical_demo.matcher.match_criterion`, and emits a
`(index, verdict)` tuple on the `indexed_verdicts` reducer slot.

Wrapping rather than re-implementing keeps the matcher's 79 unit
tests authoritative — the graph adds *orchestration*, not new
correctness surface.
"""

from __future__ import annotations

from typing import Any

from ...matcher import match_criterion
from ..state import ScoringState


def deterministic_match_node(state: ScoringState) -> dict[str, Any]:
    """Run one criterion through the deterministic matcher."""
    criterion = state["_criterion"]
    index = state["_criterion_index"]
    profile = state["profile"]
    trial = state["trial"]

    verdict = match_criterion(criterion, profile, trial)
    return {"indexed_verdicts": [(index, verdict)]}
