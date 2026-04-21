"""LangGraph state schema for the scoring graph.

Why TypedDict, not Pydantic
---------------------------
LangGraph uses the state schema two ways:
  (1) as the contract between nodes, and
  (2) as the type the *reducers* dispatch on (the `Annotated[T, fn]`
      pattern).

Reducers compose updates from concurrent branches into a single
state. They don't get along with Pydantic's validation: every reducer
call would re-validate the model, which is both slow and incorrect
(intermediate states violate invariants by design — verdicts
accumulate one criterion at a time, so the "all criteria scored"
invariant can't hold mid-fan-in). `TypedDict + Annotated[list,
operator.add]` is what the LangGraph docs themselves recommend, and
what every example in the wild uses.

Domain models that *are* Pydantic (Patient, Trial, MatchVerdict,
ExtractionResult, …) are stored *inside* the dict by reference —
Pydantic's invariants apply to them individually; the dict is just
the carrier.

Two-channel design
------------------
We split the state into two TypedDicts:

  - `ScoringStateInput` — what `score_pair_graph` receives from the
    caller. Required keys only.
  - `ScoringState` — the full working state the graph operates on.
    Optional keys carry intermediate results (extraction, verdicts)
    and the final summary/rollup. Reducers handle the verdict list.

The split keeps the public entry point's typing crisp without
forcing callers to construct the full intermediate state.
"""

from __future__ import annotations

import operator
from datetime import date
from typing import Annotated, TypedDict

from ..domain.patient import Patient
from ..domain.trial import Trial
from ..extractor.extractor import ExtractionResult
from ..extractor.schema import ExtractedCriterion
from ..matcher import MatchVerdict
from ..profile import PatientProfile
from ..scoring.score_pair import EligibilityRollup, ScoringSummary


class ScoringStateInput(TypedDict):
    """The minimum a caller must put on the channel to start the graph."""

    patient: Patient
    trial: Trial
    as_of: date
    # Optional pre-computed extraction; if absent, the extract node
    # calls the LLM. Using a sentinel (`None`) instead of leaving the
    # key off, because TypedDict's optional-keys story is brittle and
    # downstream nodes do `state.get("extraction")` either way.
    extraction: ExtractionResult | None


class ScoringState(TypedDict, total=False):
    """Full working state. `total=False` so individual nodes only
    have to write the slice they care about (LangGraph merges
    partials into the channel)."""

    # Inputs (carried through every node so they're always available)
    patient: Patient
    trial: Trial
    as_of: date
    extraction: ExtractionResult | None

    # Computed once after the extract node completes; cached in
    # state so the matcher nodes don't each re-build it.
    profile: PatientProfile

    # The fan-in slot. Each match branch (deterministic or LLM)
    # emits `{"indexed_verdicts": [(criterion_index, verdict)]}` and
    # `operator.add` concatenates across branches. The rollup node
    # sorts on criterion_index to restore extraction order, then
    # strips the indices when constructing the final verdict list.
    # We carry the index explicitly because (a) `ExtractedCriterion`
    # has no stable id, and (b) parallel execution doesn't preserve
    # arrival order — we want a deterministic verdict ordering for
    # eval / replay.
    indexed_verdicts: Annotated[list[tuple[int, MatchVerdict]], operator.add]

    # Per-branch payload carried on the `Send` from `fan_out_criteria`
    # to a matcher node. These keys are only ever populated on the
    # isolated state dict the destination match node receives; they
    # are not present on the parent channel between extract and the
    # rollup. Underscore prefix marks them as internal-to-the-graph
    # plumbing, distinct from the durable channel keys above.
    _criterion: ExtractedCriterion
    _criterion_index: int

    # Final outputs written by the rollup node and read by the
    # public entry function (`score_pair_graph`). `final_verdicts`
    # is the order-restored, index-stripped sibling of the
    # `indexed_verdicts` reducer slot.
    final_verdicts: list[MatchVerdict]
    summary: ScoringSummary
    eligibility: EligibilityRollup


__all__ = [
    "ScoringState",
    "ScoringStateInput",
]
