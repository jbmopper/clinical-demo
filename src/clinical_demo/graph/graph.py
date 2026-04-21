"""Build and compile the scoring graph.

Graph shape
-----------

      START
        │
        ▼
   ┌─────────┐
   │ extract │   ← LLM call (or replay from cache)
   └────┬────┘
        │
        │   conditional edges via fan_out_criteria
        │   (one Send per criterion)
        │
        ├─────────────────────┬───────────────────┐
        ▼                     ▼                   …
 ┌──────────────────┐  ┌────────────┐
 │ deterministic_   │  │ llm_match  │   ← parallel
 │     match        │  └─────┬──────┘
 └────────┬─────────┘        │
          │                  │
          └────────┬─────────┘
                   ▼
               ┌────────┐
               │ rollup │   ← join: sort indexed verdicts, summarize
               └───┬────┘
                   ▼
                  END

Why this shape
--------------
- `extract` is its own node (not folded into the start) so it can be
  short-circuited by a pre-supplied `extraction` and so the LLM call
  has a clean Langfuse span boundary nested under the parent.
- Fan-out is dynamic (per-criterion, count varies trial-to-trial) so
  it has to be a `Send`-returning conditional edge — LangGraph's
  static `add_edge` can't model this.
- Routing happens *inside* `fan_out_criteria` rather than as a
  second hop (extract → router → matchers). One hop keeps the
  trace tree shallow and avoids a bookkeeping node that does
  nothing visible.
- The two match nodes both edge to `rollup`. LangGraph's reducer
  semantics auto-wait for all parallel branches before invoking
  `rollup`.
- `rollup` is its own node, not a post-graph step, so the join /
  summary land in the trace tree at the right place and so a
  Phase-2.2 critic loop can branch off `rollup` (re-run a subset
  of criteria with a different matcher and re-aggregate) without
  changing the public entry function.

Why the closure-passed client
-----------------------------
LangGraph nodes are plain callables registered by name. To inject a
stub OpenAI client (for tests) and avoid coupling the node modules
to a global, we wrap the underlying node functions in closures
inside `build_graph`. The graph is a *factory output*, not a
module-level constant — callers re-build with a different client
to stub.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from ..settings import Settings
from .nodes.deterministic import deterministic_match_node
from .nodes.extract import extract_node
from .nodes.llm_match import _ClientLike as _LLMMatcherClient
from .nodes.llm_match import llm_match_node
from .nodes.rollup import rollup_node
from .nodes.route import (
    DETERMINISTIC_NODE,
    LLM_NODE,
    fan_out_criteria,
)
from .state import ScoringState

EXTRACT_NODE = "extract"
ROLLUP_NODE = "rollup"


def build_graph(
    *,
    extractor_client: Any | None = None,
    llm_matcher_client: _LLMMatcherClient | None = None,
    settings: Settings | None = None,
) -> Any:
    """Compose and compile the scoring graph.

    Parameters
    ----------
    extractor_client, llm_matcher_client : optional
        Stub clients for tests. In production, leave both as None
        and the nodes construct their own OpenAI clients from
        `settings.openai_api_key`.
    settings : optional
        Threaded through to both LLM-calling nodes. Mostly used by
        tests to override the model snapshot.

    Returns
    -------
    A compiled LangGraph runnable. Invoke via
    `graph.invoke(initial_state)` or `graph.stream(...)`.
    """

    def _extract(state: ScoringState) -> dict[str, Any]:
        return extract_node(state, client=extractor_client, settings=settings)

    def _llm_match(state: ScoringState) -> dict[str, Any]:
        return llm_match_node(state, client=llm_matcher_client, settings=settings)

    builder: StateGraph = StateGraph(ScoringState)
    builder.add_node(EXTRACT_NODE, _extract)
    builder.add_node(DETERMINISTIC_NODE, deterministic_match_node)
    builder.add_node(LLM_NODE, _llm_match)
    builder.add_node(ROLLUP_NODE, rollup_node)

    builder.add_edge(START, EXTRACT_NODE)
    builder.add_conditional_edges(
        EXTRACT_NODE,
        fan_out_criteria,
        # Telling LangGraph the destination set so it can infer
        # the join behaviour and validate the wiring at compile
        # time. Without this list, LangGraph still works but
        # complains about untyped destinations.
        [DETERMINISTIC_NODE, LLM_NODE, ROLLUP_NODE],
    )
    builder.add_edge(DETERMINISTIC_NODE, ROLLUP_NODE)
    builder.add_edge(LLM_NODE, ROLLUP_NODE)
    builder.add_edge(ROLLUP_NODE, END)

    return builder.compile()


__all__ = ["build_graph"]
