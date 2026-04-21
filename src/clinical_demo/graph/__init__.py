"""Graph orchestration for the scoring pipeline.

Public surface is `score_pair_graph(patient, trial, as_of, ...)`,
the LangGraph mirror of `clinical_demo.scoring.score_pair()`. The
graph itself is exposed via `build_graph()` for callers that want
to invoke it directly (eval harness, streaming demo).
"""

from .critic_types import CriticFinding, CriticRevision
from .graph import DEFAULT_MAX_CRITIC_ITERATIONS, build_graph
from .nodes.critic import LLM_CRITIC_VERSION
from .nodes.llm_match import LLM_MATCHER_VERSION
from .score_pair_graph import score_pair_graph
from .state import ScoringState, ScoringStateInput

__all__ = [
    "DEFAULT_MAX_CRITIC_ITERATIONS",
    "LLM_CRITIC_VERSION",
    "LLM_MATCHER_VERSION",
    "CriticFinding",
    "CriticRevision",
    "ScoringState",
    "ScoringStateInput",
    "build_graph",
    "score_pair_graph",
]
