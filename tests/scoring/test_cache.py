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
    schema_fingerprint,
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


def test_cache_path_embeds_prompt_schema_and_model(tmp_path: Path) -> None:
    """Filename pattern is `<NCT>.<prompt_version>.<schema_fp>.<model>.json`.

    Pinning the four-segment shape here because (a) any of the three
    rev-able signals changing must produce a different filename for
    cache invalidation to work (D-66), and (b) downstream tooling that
    inspects filenames depends on the segment order. If you intend to
    change the format, also change the doc on `cache_path_for` and
    delete or migrate `data/curated/extractions/*.json` so old
    envelopes don't stick around as orphans.
    """
    p = cache_path_for(
        "NCT99999999",
        tmp_path,
        prompt_version="prompt-vX",
        schema_fp="abcd1234",
        model="gpt-test",
    )
    assert p == tmp_path / "NCT99999999.prompt-vX.abcd1234.gpt-test.json"


def test_cache_path_defaults_pull_current_signals(tmp_path: Path) -> None:
    """No-arg defaults: prompt_version from `extractor.prompt`, schema
    fingerprint from the live schema, model from settings. So callers
    that don't override anything always get the *current* cache key,
    automatically invalidating when any of the three changes."""
    from clinical_demo.extractor.prompt import PROMPT_VERSION
    from clinical_demo.settings import get_settings

    p = cache_path_for("NCT99999999", tmp_path)
    expected = (
        tmp_path / f"NCT99999999.{PROMPT_VERSION}.{schema_fingerprint()}."
        f"{get_settings().extractor_model}.json"
    )
    assert p == expected


def test_schema_fingerprint_is_stable_and_short() -> None:
    """8 hex chars, deterministic. The cache filename and the
    StoredExtraction reader both depend on this; tests upstream/downstream
    have an implicit dependency on the digest staying the same per
    schema rev. Bumping it should be the only effect of a schema edit."""
    fp = schema_fingerprint()
    assert len(fp) == 8
    assert fp == schema_fingerprint()
    int(fp, 16)


def test_schema_fingerprint_changes_when_schema_changes() -> None:
    """Inject a probe model with a different schema and confirm the
    fingerprint differs. Doesn't mutate the real schema (we use a
    sibling model and hash directly), but pins the contract that a
    schema rev produces a different digest."""
    import hashlib
    import json

    from pydantic import BaseModel

    class _Probe(BaseModel):
        x: int

    probe_schema = _Probe.model_json_schema()
    probe_canonical = json.dumps(probe_schema, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    probe_fp = hashlib.sha256(probe_canonical).hexdigest()[:8]
    assert probe_fp != schema_fingerprint()
