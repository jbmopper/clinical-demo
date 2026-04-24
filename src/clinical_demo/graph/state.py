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


def merge_indexed_verdicts(
    left: list[tuple[int, MatchVerdict]],
    right: list[tuple[int, MatchVerdict]],
) -> list[tuple[int, MatchVerdict]]:
    """Reducer: merge `(index, verdict)` tuples with replace-by-index.

    Why not just `operator.add`?
    --------------------------
    The first matcher fan-out emits one tuple per criterion; concat
    is the right semantics there (no index appears twice). But the
    critic loop's revise node re-runs the matcher for *one*
    criterion to fix a finding, which means we want the new tuple
    for that index to *supersede* the old one, not sit beside it.
    Plain concat would leave both, and the rollup's `sorted()` call
    would arbitrarily pick whichever stable-sort happened to land
    last — fine on a deterministic sort within one process, but
    fragile (any change to the rollup's sort key would silently
    flip which verdict wins).

    Replace-by-index is also what an auditor would expect: the most
    recent verdict for a given criterion is the verdict.

    On a fresh run with no revisions, this collapses to the same
    behaviour as concat (each index appears once on the right
    side). The cost is O(n_left + n_right * n_left) for the
    set-membership check, which is negligible at our criterion
    counts (typically < 50 per trial).
    """
    if not right:
        return left
    if not left:
        return list(right)

    right_indices = {idx for idx, _ in right}
    merged: list[tuple[int, MatchVerdict]] = [pair for pair in left if pair[0] not in right_indices]
    merged.extend(right)
    return merged


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
    # `merge_indexed_verdicts` collapses concurrent updates with
    # replace-by-index semantics (last write per index wins). The
    # rollup node sorts on criterion_index to restore extraction
    # order, then strips the indices when constructing the final
    # verdict list. We carry the index explicitly because (a)
    # `ExtractedCriterion` has no stable id, and (b) parallel
    # execution doesn't preserve arrival order — we want a
    # deterministic verdict ordering for eval / replay.
    indexed_verdicts: Annotated[list[tuple[int, MatchVerdict]], merge_indexed_verdicts]

    # Per-branch payload carried on the `Send` from `fan_out_criteria`
    # to a matcher node. These keys are only ever populated on the
    # isolated state dict the destination match node receives; they
    # are not present on the parent channel between extract and the
    # rollup. Underscore prefix marks them as internal-to-the-graph
    # plumbing, distinct from the durable channel keys above.
    _criterion: ExtractedCriterion
    _criterion_index: int

    # Optional per-branch reviewer context for critic-triggered LLM
    # re-runs. Normal matcher fan-out leaves this absent; revise
    # supplies it so the follow-up prompt differs from the original
    # free-text match.
    _reviewer_note: str

    # Final outputs written by the rollup node and read by the
    # public entry function (`score_pair_graph`). `final_verdicts`
    # is the order-restored, index-stripped sibling of the
    # `indexed_verdicts` reducer slot.
    final_verdicts: list[MatchVerdict]
    summary: ScoringSummary
    eligibility: EligibilityRollup

    # ---- critic loop (Phase 2.2) ----
    #
    # Iteration counter, monotonically increasing. Initialized to 0
    # by `score_pair_graph()`; the critic node bumps it before
    # emitting findings. Used by the budget check in
    # `route_after_critic` and surfaced in trace metadata so the
    # dashboard can pivot on "average critic iterations per pair."
    critic_iterations: int

    # Findings emitted by the most recent critic pass. Replaced
    # wholesale each iteration (not appended) — the critic operates
    # on the current rollup, not the cumulative history. The
    # historical record lives in trace spans, not in state.
    #
    # `None` means the critic has not run yet (distinct from `[]`
    # which means "ran, found nothing, terminate"). The router
    # checks for this distinction.
    critic_findings: list[CriticFinding] | None

    # Audit trail of revise actions taken across all iterations.
    # Append-only (each iteration's revisions land here in addition
    # to the new verdicts in `indexed_verdicts`), so an auditor can
    # replay "what did the critic actually change."
    critic_revisions: Annotated[list[CriticRevision], operator.add]

    # Snapshot of the *previous* iteration's finding fingerprints,
    # written by the critic node at the start of each pass. The
    # post-critic router consults this to detect "no progress"
    # (current fingerprints == previous fingerprints → terminate).
    # Underscore prefix marks it as graph-internal plumbing. Kept
    # off the public envelope; tests can poke at it directly when
    # exercising the no-progress path.
    _critic_prev_fingerprints: set[tuple[int, str]]


# Forward-declared types (real definitions in nodes/critic.py to
# keep state.py free of LLM-prompt churn). Imported lazily to break
# the circular dep — these names are only ever read by type
# checkers and as `list` element types at runtime, never instantiated
# from this module.
from .critic_types import CriticFinding, CriticRevision  # noqa: E402

__all__ = [
    "CriticFinding",
    "CriticRevision",
    "ScoringState",
    "ScoringStateInput",
    "merge_indexed_verdicts",
]
