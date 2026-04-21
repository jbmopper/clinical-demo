"""Routing for the scoring graph.

Routing functions, played at distinct seams:

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

3. `route_after_critic` — Phase 2.2 conditional edge from the
   critic node, deciding revise / loop / finalize based on the
   findings list, the iteration budget, and a no-progress check.

Routing rule v0
---------------
  - `kind == "free_text"`            → llm_match
  - everything else                  → deterministic_match

This is deliberately conservative: the deterministic matcher is
fast, free, and exhaustively tested; we only call the LLM when the
deterministic matcher *cannot* decide by construction.
"""

from __future__ import annotations

from typing import Literal

from langgraph.types import Send

from ...extractor.schema import ExtractedCriterion
from ..critic_types import CriticFinding
from ..state import ScoringState

MatchNodeName = Literal["deterministic_match", "llm_match"]

# Module-level node-name constants. Typed as the Literal alias (not
# bare `str`) so `route_by_kind` returning one of them satisfies the
# Literal return signature without a cast.
DETERMINISTIC_NODE: MatchNodeName = "deterministic_match"
LLM_NODE: MatchNodeName = "llm_match"
ROLLUP_NODE: Literal["rollup"] = "rollup"
CRITIC_NODE: Literal["critic"] = "critic"
REVISE_NODE: Literal["revise"] = "revise"
FINALIZE_NODE: Literal["finalize"] = "finalize"
HUMAN_REVIEW_NODE: Literal["human_review"] = "human_review"


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


PostCriticTarget = Literal["revise", "rollup", "finalize"]


def route_after_critic(
    state: ScoringState,
    *,
    max_iterations: int,
) -> PostCriticTarget:
    """Decide what to do after the critic has emitted findings.

    Three layered termination conditions, checked in cost order:

      1. No actionable findings (severity != 'warning')
         → `finalize` (we're done; the critic is happy or only
         emitted info-level notes).

      2. Iteration budget exhausted (`critic_iterations >=
         max_iterations`)
         → `finalize` (we already used our re-run budget; one
         more critic pass would be a waste of an LLM call).

      3. No-progress check: the current findings' fingerprint set
         equals `_critic_prev_fingerprints` (which the critic node
         snapshotted at entry). The critic is repeating itself;
         the revise step isn't moving the needle.
         → `finalize`.

      4. Otherwise → `revise` (which then routes back to `rollup`
         to re-aggregate, then back to the critic for another pass).

    `recursion_limit` from the LangGraph config is the hard
    backstop above all of this; the explicit budget is the
    soft, observable one.
    """
    findings = state.get("critic_findings") or []
    iteration = state.get("critic_iterations", 0)

    actionable = [f for f in findings if f.severity == "warning"]
    if not actionable:
        return FINALIZE_NODE

    if iteration >= max_iterations:
        return FINALIZE_NODE

    previous = state.get("_critic_prev_fingerprints")
    if previous:
        current = {(f.criterion_index, f.kind) for f in findings}
        if current == previous:
            return FINALIZE_NODE

    return REVISE_NODE


def fingerprint_findings(findings: list[CriticFinding]) -> set[tuple[int, str]]:
    """Stable identity for the no-progress detector.

    Pulled out so the graph builder and the tests share one
    definition. Severity is intentionally excluded from the
    fingerprint — promoting `info` to `warning` shouldn't reset
    the no-progress counter."""
    return {f.fingerprint for f in findings}
