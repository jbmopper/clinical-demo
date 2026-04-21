"""Build and compile the scoring graph.

Graph shape (Phase 2.2)
-----------------------

      START
        │
        ▼
   ┌─────────┐
   │ extract │   ← LLM call (or replay from cache)
   └────┬────┘
        │   conditional edges via fan_out_criteria
        ├─────────────────────┬───────────────────┐
        ▼                     ▼                   …
 ┌──────────────────┐  ┌────────────┐
 │ deterministic_   │  │ llm_match  │   ← parallel
 │     match        │  └─────┬──────┘
 └────────┬─────────┘        │
          └────────┬─────────┘
                   ▼
               ┌────────┐
               │ rollup │   ← join: sort indexed verdicts, summarize
               └───┬────┘
                   │  (critic loop, opt-in)
                   ▼
              ┌────────┐
              │ critic │   ← LLM critique → CriticFinding[]
              └───┬────┘
                  │ route_after_critic
                  ├──────────► finalize ─► END
                  │
                  ▼ (revisable findings + budget)
              ┌────────┐
              │ revise │   ← one targeted matcher re-run
              └───┬────┘
                  │
                  ▼ (back to rollup, then critic again)
                ROLLUP

When the critic loop is disabled (`critic_enabled=False`),
`rollup` edges directly to `finalize`, the critic / revise nodes
are still registered (LangGraph requires all nodes named in the
state schema to exist if they're referenced by edges) but
unreachable. The compiled graph is thus a strict superset of the
2.1 shape, and the no-critic path produces byte-identical results.

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
  summary land in the trace tree at the right place and so the
  critic loop can re-enter it after a revise without changing the
  public entry function.
- `finalize` is a pass-through node whose only purpose is to be
  the `interrupt_before` target for the human checkpoint. See
  `nodes/finalize.py` for the full justification.

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

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph

from ..settings import Settings
from .nodes.critic import _ClientLike as _CriticClient
from .nodes.critic import critic_node
from .nodes.deterministic import deterministic_match_node
from .nodes.extract import extract_node
from .nodes.finalize import finalize_node
from .nodes.llm_match import _ClientLike as _LLMClient
from .nodes.llm_match import llm_match_node
from .nodes.revise import revise_node
from .nodes.rollup import rollup_node
from .nodes.route import (
    CRITIC_NODE,
    DETERMINISTIC_NODE,
    FINALIZE_NODE,
    LLM_NODE,
    REVISE_NODE,
    ROLLUP_NODE,
    fan_out_criteria,
    route_after_critic,
)
from .state import ScoringState

EXTRACT_NODE = "extract"

# Default critic budget. Two iterations is one critique + one
# revision + one re-critique that confirms convergence; in
# practice 95% of runs terminate at iteration 1.
DEFAULT_MAX_CRITIC_ITERATIONS = 2


def build_graph(
    *,
    extractor_client: Any | None = None,
    llm_matcher_client: _LLMClient | None = None,
    critic_client: _CriticClient | None = None,
    settings: Settings | None = None,
    critic_enabled: bool = False,
    max_critic_iterations: int = DEFAULT_MAX_CRITIC_ITERATIONS,
    human_checkpoint: bool = False,
) -> Any:
    """Compose and compile the scoring graph.

    Parameters
    ----------
    extractor_client, llm_matcher_client, critic_client : optional
        Stub clients for tests. In production, leave all None and
        the nodes construct their own OpenAI clients from
        `settings.openai_api_key`. The critic and matcher are
        separate kwargs because we may eventually point them at
        different model snapshots.
    settings : optional
        Threaded through to all LLM-calling nodes.
    critic_enabled : bool
        When False (default), `rollup` → `finalize` and the critic
        loop is unreachable. The 2.1 path is preserved bit-for-bit
        in this mode (modulo the `finalize` span, which is a
        pass-through). When True, `rollup` → `critic` → router.
    max_critic_iterations : int
        Soft budget. The router terminates the loop when the
        critic has run this many times, even if it would still
        emit findings. The hard backstop is the LangGraph
        `recursion_limit` runtime config.
    human_checkpoint : bool
        When True, compile with an `InMemorySaver` checkpointer
        and `interrupt_before=["finalize"]`. The graph pauses
        immediately before END, returning an `__interrupt__` event;
        callers must invoke with a `thread_id` config and resume
        with `Command(resume=...)`. v0 doesn't ship a UI; the seam
        is the deliverable. Phase 2.8 will consume it.

    Returns
    -------
    A compiled LangGraph runnable. Invoke via
    `graph.invoke(initial_state)` or `graph.stream(...)`.
    """

    def _extract(state: ScoringState) -> dict[str, Any]:
        return extract_node(state, client=extractor_client, settings=settings)

    def _llm_match(state: ScoringState) -> dict[str, Any]:
        return llm_match_node(state, client=llm_matcher_client, settings=settings)

    def _critic(state: ScoringState) -> dict[str, Any]:
        return critic_node(state, client=critic_client, settings=settings)

    def _revise(state: ScoringState) -> dict[str, Any]:
        # Revise re-matches via the LLM matcher; share its client.
        return revise_node(state, client=llm_matcher_client, settings=settings)

    def _route_after_critic(state: ScoringState) -> str:
        return route_after_critic(state, max_iterations=max_critic_iterations)

    builder: StateGraph = StateGraph(ScoringState)
    builder.add_node(EXTRACT_NODE, _extract)
    builder.add_node(DETERMINISTIC_NODE, deterministic_match_node)
    builder.add_node(LLM_NODE, _llm_match)
    builder.add_node(ROLLUP_NODE, rollup_node)
    builder.add_node(CRITIC_NODE, _critic)
    builder.add_node(REVISE_NODE, _revise)
    builder.add_node(FINALIZE_NODE, finalize_node)

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

    if critic_enabled:
        builder.add_edge(ROLLUP_NODE, CRITIC_NODE)
        builder.add_conditional_edges(
            CRITIC_NODE,
            _route_after_critic,
            [REVISE_NODE, FINALIZE_NODE],
        )
        builder.add_edge(REVISE_NODE, ROLLUP_NODE)
    else:
        builder.add_edge(ROLLUP_NODE, FINALIZE_NODE)

    builder.add_edge(FINALIZE_NODE, END)

    compile_kwargs: dict[str, Any] = {}
    if human_checkpoint:
        # `pickle_fallback=True` so the checkpointer can serialize
        # `PatientProfile` (a plain Python class, not a Pydantic
        # model). The checkpoint stays in-process (`InMemorySaver`),
        # so the deserialization-of-untrusted-data risk that prompted
        # `langgraph-checkpoint`'s recent serializer hardening
        # doesn't apply — we never load a checkpoint from outside the
        # current process. If the HITL story ever moves to a
        # cross-process saver (Postgres, S3), this is the line to
        # revisit; either wrap PatientProfile in a Pydantic model or
        # ship a typed serializer.
        compile_kwargs["checkpointer"] = InMemorySaver(
            serde=JsonPlusSerializer(pickle_fallback=True)
        )
        compile_kwargs["interrupt_before"] = [FINALIZE_NODE]

    return builder.compile(**compile_kwargs)


__all__ = ["DEFAULT_MAX_CRITIC_ITERATIONS", "build_graph"]
