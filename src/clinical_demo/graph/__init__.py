"""Graph orchestration for the scoring pipeline.

Public surface is `score_pair_graph(patient, trial, as_of, ...)`,
the LangGraph mirror of `clinical_demo.scoring.score_pair()`. The
graph itself is exposed via `build_graph()` for callers that want
to invoke it directly (eval harness, streaming demo).
"""

from .graph import build_graph
from .nodes.llm_match import LLM_MATCHER_VERSION
from .score_pair_graph import score_pair_graph
from .state import ScoringState, ScoringStateInput

__all__ = [
    "LLM_MATCHER_VERSION",
    "ScoringState",
    "ScoringStateInput",
    "build_graph",
    "score_pair_graph",
]
