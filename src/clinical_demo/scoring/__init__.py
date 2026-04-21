"""End-to-end scoring: extract criteria, run the matcher, roll up.

Importable wrapper around the extractor + matcher + profile that the
CLI script and any future API surface both call.
"""

from .cache import StoredExtraction, cache_path_for, load_cached_extraction
from .score_pair import (
    EligibilityRollup,
    ScorePairResult,
    ScoringSummary,
    score_pair,
)

__all__ = [
    "EligibilityRollup",
    "ScorePairResult",
    "ScoringSummary",
    "StoredExtraction",
    "cache_path_for",
    "load_cached_extraction",
    "score_pair",
]
