"""Finalize node: pass-through that marks the end of the critic loop.

The rollup node already produced `final_verdicts`, `summary`, and
`eligibility`. Finalize exists for two reasons that are NOT cosmetic:

1. **Trace boundary.** It gives the critic loop a clear "the loop
   has ended" span in Langfuse. Without it, the trace would show
   `rollup` firing N times with no signal that the last one is the
   "real" one.

2. **Human checkpoint seam.** The graph compiles with
   `interrupt_before=["finalize"]` when `human_checkpoint=True`.
   Pausing immediately before finalize gives a human reviewer the
   full, post-revision rollup to inspect; resuming proceeds to END
   with no further work. Pausing before *rollup* would force the
   reviewer to re-run aggregation; pausing after *rollup* but
   before END would have to inject a synthetic node anyway.

The node itself does no work. It returns an empty update and the
graph proceeds to END. Keeping it visible as a separate node makes
the dashboard story honest: "we paused for human review here."
"""

from __future__ import annotations

from typing import Any

from ...observability import traced
from ..state import ScoringState


def finalize_node(state: ScoringState) -> dict[str, Any]:
    """Pass-through; marks the end of the scoring loop in the trace."""
    iterations = state.get("critic_iterations", 0)
    revisions = state.get("critic_revisions", []) or []
    with traced(
        "finalize",
        as_type="span",
        input={
            "critic_iterations": iterations,
            "revisions_total": len(revisions),
        },
        metadata={
            "critic_iterations": str(iterations),
            "revisions_total": str(len(revisions)),
            "verdict_changed_total": str(sum(1 for r in revisions if r.verdict_changed)),
        },
    ):
        pass
    return {}
