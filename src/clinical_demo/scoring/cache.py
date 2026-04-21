"""On-disk cache for extractor results.

The extractor is the only LLM call in the pipeline. Caching the
result keyed by NCT id lets the matcher and the scoring CLI iterate
without re-spending tokens. The on-disk format is the same one
`scripts/extract_criteria.py` writes; this module is the loader.

The cache is intentionally write-once-read-many at v0: the curator
script writes; the scoring path reads. When the prompt or model
changes, the operator deletes the stale envelopes and re-extracts
(see `scripts/extract_criteria.py --force`). A versioned cache key
(prompt+model) is a Phase-2 upgrade.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from ..extractor.extractor import ExtractionResult
from ..extractor.schema import ExtractedCriteria, ExtractorRunMeta


class StoredExtraction(BaseModel):
    """On-disk envelope: trial id + extraction + run metadata."""

    nct_id: str
    extraction: ExtractedCriteria
    meta: ExtractorRunMeta


def load_cached_extraction(path: Path) -> ExtractionResult:
    """Load a StoredExtraction from disk and adapt to the in-process
    `ExtractionResult` dataclass that `score_pair` accepts."""
    stored = StoredExtraction.model_validate_json(path.read_text())
    return ExtractionResult(extracted=stored.extraction, meta=stored.meta)


def cache_path_for(nct_id: str, root: Path) -> Path:
    return root / f"{nct_id}.json"


__all__ = [
    "StoredExtraction",
    "cache_path_for",
    "load_cached_extraction",
]
