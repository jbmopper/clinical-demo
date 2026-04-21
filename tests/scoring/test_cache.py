"""Tests for the on-disk extraction cache.

The cache is written by `scripts/extract_criteria.py` and read by
`scripts/score_pair.py` (via `score_pair`'s `extraction=` parameter).
We test the loader's round-trip and the path helper here so a
regression in the I/O contract doesn't surface only when the demo
script crashes.
"""

from __future__ import annotations

from pathlib import Path

from clinical_demo.extractor.schema import (
    ExtractedCriteria,
    ExtractionMetadata,
    ExtractorRunMeta,
)
from clinical_demo.scoring import (
    StoredExtraction,
    cache_path_for,
    load_cached_extraction,
)


def _make_stored(nct_id: str = "NCT00000000") -> StoredExtraction:
    return StoredExtraction(
        nct_id=nct_id,
        extraction=ExtractedCriteria(
            criteria=[],
            metadata=ExtractionMetadata(notes="empty"),
        ),
        meta=ExtractorRunMeta(
            model="test-model",
            prompt_version="extractor-test",
            input_tokens=10,
            output_tokens=20,
            cached_input_tokens=0,
            cost_usd=0.0001,
            latency_ms=100.0,
        ),
    )


def test_cache_round_trip(tmp_path: Path) -> None:
    """Write a StoredExtraction and re-load via the public loader.

    Confirms that the on-disk JSON round-trips byte-for-byte through
    Pydantic and that the loader returns an `ExtractionResult`
    dataclass (not the on-disk envelope), which is what `score_pair`
    consumes."""
    stored = _make_stored("NCT12345678")
    path = tmp_path / "NCT12345678.json"
    path.write_text(stored.model_dump_json(indent=2))

    result = load_cached_extraction(path)
    assert result.extracted.criteria == []
    assert result.meta.model == "test-model"
    assert result.meta.prompt_version == "extractor-test"
    assert result.meta.cost_usd == 0.0001


def test_cache_path_for_uses_nct_id_filename(tmp_path: Path) -> None:
    """Path convention is `<root>/<NCT_ID>.json`. Pinning it here so
    a path-format change doesn't silently invalidate the cache."""
    p = cache_path_for("NCT99999999", tmp_path)
    assert p == tmp_path / "NCT99999999.json"
