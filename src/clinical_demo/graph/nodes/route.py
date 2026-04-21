"""Routing for the scoring graph.

Two distinct routing functions, played at two distinct seams:

1. `fan_out_criteria` — a conditional edge from `extract` that
   returns either a list of `Send` objects (one per criterion) or
   the rollup node name when there's nothing to fan out. This is
   the LangGraph idiom for dynamic per-item parallelism.

2. `route_by_kind` — a conditional edge function (criterion → node
   name) that picks deterministic vs. LLM matcher for one criterion.
   Pulled out as its own module-level function so we can unit-test
   the routing decision in isolation, and so the v0.2 fall-back
   ("if deterministic returns indeterminate(unmapped_concept), try
   LLM") plugs in here without touching the graph wiring.

Routing rule v0
---------------
  - `kind == "free_text"`            → llm_match
  - everything else                  → deterministic_match

This is deliberately conservative: the deterministic matcher is
fast, free, and exhaustively tested; we only call the LLM when the
deterministic matcher *cannot* decide by construction. Phase 2.2
will add the dynamic fallback.
"""

from __future__ import annotations

from typing import Literal

from langgraph.types import Send

from ...extractor.schema import ExtractedCriterion
from ..state import ScoringState

MatchNodeName = Literal["deterministic_match", "llm_match"]

# Module-level node-name constants. Typed as the Literal alias (not
# bare `str`) so `route_by_kind` returning one of them satisfies the
# Literal return signature without a cast.
DETERMINISTIC_NODE: MatchNodeName = "deterministic_match"
LLM_NODE: MatchNodeName = "llm_match"
ROLLUP_NODE: Literal["rollup"] = "rollup"


def route_by_kind(criterion: ExtractedCriterion) -> MatchNodeName:
    """Pick which matcher should handle this criterion.

    v0: only `free_text` goes to the LLM matcher; every other kind
    has a typed payload the deterministic matcher can decide on
    structurally. The LLM is reserved for the literal text that the
    extractor couldn't structure."""
    if criterion.kind == "free_text":
        return LLM_NODE
    return DETERMINISTIC_NODE


def fan_out_criteria(state: ScoringState) -> list[Send] | str:
    """Conditional edge: emit one `Send` per criterion, or route
    directly to rollup if there are no criteria to score.

    Each `Send` carries the per-criterion payload to the matcher
    node selected by `route_by_kind`. We pre-attach the
    criterion_index so the rollup can restore extraction order
    after parallel fan-in.

    Returning the rollup node name (not an empty list) for the
    zero-criteria case is important: LangGraph treats an empty
    `Send` list as "no edges fired", which would leave the graph
    stuck after `extract`. The string form routes control directly
    to rollup, which then produces a (correct, empty) result.
    """
    extraction = state.get("extraction")
    if extraction is None or not extraction.extracted.criteria:
        return ROLLUP_NODE

    criteria = extraction.extracted.criteria
    sends: list[Send] = []
    for index, criterion in enumerate(criteria):
        node = route_by_kind(criterion)
        sends.append(
            Send(
                node,
                {
                    # Per-branch slice. We pass the whole patient/trial
                    # so the matcher can build evidence; the profile is
                    # already on state but Send carries an *isolated*
                    # state dict to the destination node, so we must
                    # forward it explicitly here.
                    "patient": state["patient"],
                    "trial": state["trial"],
                    "as_of": state["as_of"],
                    "profile": state["profile"],
                    "extraction": extraction,
                    "_criterion": criterion,
                    "_criterion_index": index,
                },
            )
        )
    return sends
