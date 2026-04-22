"""On-disk cache for extractor results.

The extractor is the only LLM call in the pipeline. Caching the
result keyed by NCT id lets the matcher and the scoring CLI iterate
without re-spending tokens. The on-disk format is the same one
`scripts/extract_criteria.py` writes; this module is the loader.

Cache key (D-66)
----------------
The filename embeds three things the on-disk envelope is sensitive
to: the prompt version, the response-schema fingerprint, and the
extractor model. Any change to any of those three produces a
different filename, so an old envelope is *invisible* to the new
read path rather than silently pumping stale criteria through the
matcher. Old envelopes become orphans in the same dir; gitignored,
no harm. Operator can `rm` them at leisure.

The schema fingerprint hashes `ExtractedCriteria.model_json_schema()`,
which is exactly what OpenAI receives as the `response_format`.
That captures every field-level constraint that could shift model
behavior, automatically — so adding/renaming/retyping a field on the
extractor schema invalidates the cache for free.

Why all three signals and not just a manual prompt-version bump?
Because the schema rev is the easy one to forget. A new field on
`ExtractedCriterion` is a typed, IDE-supported change that should
"just work"; humans should not be on the hook to remember to bump a
string constant in a sibling module. Automation here is cheap (one
hash) and the failure mode of *not* doing it (a stale cache pumping
schema-mismatched JSON through the matcher) is precisely what
caused the NCT05268237 invariant-violation incident.
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel

from ..extractor.extractor import ExtractionResult
from ..extractor.prompt import PROMPT_VERSION
from ..extractor.schema import ExtractedCriteria, ExtractorRunMeta
from ..settings import Settings, get_settings


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


@lru_cache(maxsize=1)
def schema_fingerprint() -> str:
    """8-char hex digest of the extractor's response schema.

    Hashes the canonical JSON of `ExtractedCriteria.model_json_schema()`
    — i.e. the exact `response_format` payload OpenAI sees. Stable
    across runs, automatically catches any schema rev. We truncate to
    8 hex chars (32 bits) because the cache filename is the only
    consumer; collision risk between two manual schema edits is
    negligible at that surface area, and the shorter string keeps
    filenames readable.
    """
    schema = ExtractedCriteria.model_json_schema()
    canonical = json.dumps(schema, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()[:8]


def _sanitize_model_name(model: str) -> str:
    """OpenAI model strings can contain `/` (e.g. fine-tuned suffixes).
    Convert to a filename-safe form."""
    return model.replace("/", "_")


def cache_path_for(
    nct_id: str,
    root: Path,
    *,
    prompt_version: str | None = None,
    schema_fp: str | None = None,
    model: str | None = None,
    settings: Settings | None = None,
) -> Path:
    """Resolve the cache filename for a given (nct_id, prompt, schema, model) tuple.

    Default: pull prompt_version from `extractor.prompt`, schema
    fingerprint from `schema_fingerprint()`, and model from settings.
    Each can be overridden for tests or for cross-model probing.

    Filename pattern: `<NCT>.<prompt_version>.<schema_fp>.<model>.json`.
    The four-segment shape is intentional: each segment is a separately
    revvable signal (D-66), and at-a-glance debugging benefits from
    seeing all three on the filename.
    """
    s = settings or get_settings()
    prompt_v = prompt_version or PROMPT_VERSION
    schema_v = schema_fp or schema_fingerprint()
    model_v = _sanitize_model_name(model or s.extractor_model)
    return root / f"{nct_id}.{prompt_v}.{schema_v}.{model_v}.json"


__all__ = [
    "StoredExtraction",
    "cache_path_for",
    "load_cached_extraction",
    "schema_fingerprint",
]
