"""Join + rollup node: sort indexed verdicts, summarize, decide eligibility.

Reuses the existing `_summarize` and `_rollup` helpers from
`clinical_demo.scoring.score_pair` so the imperative and graph-based
score paths land on identical aggregation logic. The only graph-only
work this node does is restoring the verdict order — parallel
fan-in via the `operator.add` reducer means the verdicts arrive in
arbitrary completion order, and we want a deterministic ordering for
eval / replay / human review.

The node returns a `dict` with all the keys the graph's exit
function reads to assemble the public `ScorePairResult`. We don't
construct `ScorePairResult` here because the graph's exit function
also needs `patient_id`, `nct_id`, and `as_of` — pulling that into
this node would couple it to fields it doesn't otherwise care about.
"""

from __future__ import annotations

from typing import Any

from ...matcher import MatchVerdict
from ...scoring.score_pair import _rollup, _summarize
from ..state import ScoringState


def rollup_node(state: ScoringState) -> dict[str, Any]:
    """Sort indexed verdicts, run the rollup + summary."""
    indexed = state.get("indexed_verdicts", [])
    # Sort by criterion_index to restore extraction order. Ties
    # shouldn't occur (one branch per index by construction); if they
    # do, latest-arrival wins via the stable sort, which is fine.
    sorted_pairs = sorted(indexed, key=lambda pair: pair[0])
    verdicts: list[MatchVerdict] = [v for _, v in sorted_pairs]

    summary = _summarize(verdicts)
    eligibility = _rollup(verdicts)

    return {
        # Carry the cleaned verdicts list back onto the channel so the
        # public entry point can read it. We use a different key
        # (`final_verdicts`) than the reducer slot
        # (`indexed_verdicts`) to make it obvious which is which to
        # downstream readers and to avoid feeding back into the
        # reducer.
        "final_verdicts": verdicts,
        "summary": summary,
        "eligibility": eligibility,
    }
