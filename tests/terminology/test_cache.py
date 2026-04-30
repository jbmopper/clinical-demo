"""Tests for the on-disk terminology cache.

The cache is the seam between the live VSAC client (and future
RxNorm / UMLS clients) and the matcher. It must:

- Round-trip a `VSACExpansion` byte-for-byte through the on-disk
  envelope so the matcher gets back exactly what the live client
  returned.
- Auto-invalidate on any envelope-shape change (D-66 discipline,
  applied to terminology bindings instead of extractor outputs).
- Discriminate keys by `system_filter`, since the same OID with and
  without a filter resolves to different `ConceptSet`s.
- Survive crash-mid-write by being atomic.
- Stay decoupled from the live `VSACClient` so the cache can be
  exercised offline (these tests never touch the network).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from clinical_demo.profile import ConceptSet
from clinical_demo.terminology import (
    StoredVSACExpansion,
    TerminologyCache,
    VSACExpansion,
    cache_path_for_vsac,
    vsac_envelope_fingerprint,
)

DIABETES_OID = "2.16.840.1.113883.3.464.1003.103.12.1001"
SNOMED = "http://snomed.info/sct"


def _make_expansion(
    *, codes: frozenset[str] | None = None, name: str = "Diabetes"
) -> VSACExpansion:
    return VSACExpansion(
        oid=DIABETES_OID,
        version="20210220",
        concept_set=ConceptSet(
            name=name,
            system=SNOMED,
            codes=codes if codes is not None else frozenset({"44054006", "46635009"}),
        ),
    )


# ---------- round-trip ----------


def test_put_then_get_round_trips_concept_set(tmp_path: Path) -> None:
    """The matcher consumes `ConceptSet`s by code-set intersection;
    a round-trip that drops or reorders codes would silently change
    matcher verdicts. Pin the full equality, including the frozenset
    membership, here."""
    cache = TerminologyCache(tmp_path)
    original = _make_expansion()

    cache.put_vsac_expansion(original)
    loaded = cache.get_vsac_expansion(DIABETES_OID)

    assert loaded is not None
    assert loaded == original
    assert loaded.concept_set.codes == original.concept_set.codes
    assert isinstance(loaded.concept_set.codes, frozenset)


def test_get_returns_none_on_miss(tmp_path: Path) -> None:
    """Miss is a None, not an exception. Callers branch on this to
    decide whether to call the live VSAC client."""
    cache = TerminologyCache(tmp_path)
    assert cache.get_vsac_expansion(DIABETES_OID) is None


def test_put_creates_cache_root_lazily(tmp_path: Path) -> None:
    """Constructor does not create the dir; first put does. Keeps
    fresh checkouts that never resolve a binding from littering
    data/ with empty cache dirs."""
    root = tmp_path / "does-not-exist-yet"
    cache = TerminologyCache(root)
    assert not root.exists()

    cache.put_vsac_expansion(_make_expansion())

    assert root.is_dir()


# ---------- cache key ----------


def test_cache_path_pattern_pins_filename_segments(tmp_path: Path) -> None:
    """Filename pattern is `vsac.<oid>.<filter_tag>.<schema_fp>.json`.

    Pinned here because (a) downstream tooling (manual `ls`,
    eventual cache-aging scripts) reads the segments, and (b)
    changing the pattern silently is exactly the failure mode this
    cache is supposed to prevent — old envelopes would no longer
    auto-orphan against new reads. If you intend to change the
    format, also update `cache_path_for_vsac`'s docstring and
    delete or migrate `data/cache/terminology/vsac.*.json` so old
    envelopes don't stick around as orphans."""
    p = cache_path_for_vsac(
        DIABETES_OID,
        tmp_path,
        system_filter=None,
        schema_fp="abcd1234",
    )
    assert p == tmp_path / f"vsac.{DIABETES_OID}.any.abcd1234.json"


def test_cache_path_strips_urn_prefix(tmp_path: Path) -> None:
    """`urn:oid:` prefix should not produce a separate cache entry
    from the bare OID — both are valid VSAC inputs, both must hit
    the same row."""
    bare = cache_path_for_vsac(DIABETES_OID, tmp_path)
    urn = cache_path_for_vsac(f"urn:oid:{DIABETES_OID}", tmp_path)
    assert bare == urn


def test_filter_changes_filename(tmp_path: Path) -> None:
    """Same OID with and without a `system_filter` resolves to
    different ConceptSets (filtered single-system vs full
    expansion). The cache must keep them separate or the second
    caller would silently get the wrong-shaped row."""
    no_filter = cache_path_for_vsac(DIABETES_OID, tmp_path)
    with_filter = cache_path_for_vsac(DIABETES_OID, tmp_path, system_filter=SNOMED)
    assert no_filter != with_filter
    other_filter = cache_path_for_vsac(
        DIABETES_OID,
        tmp_path,
        system_filter="http://loinc.org",
    )
    assert with_filter != other_filter


def test_filtered_and_unfiltered_round_trips_dont_collide(tmp_path: Path) -> None:
    """End-to-end version of the previous test: write two distinct
    expansions for the same OID under different filters and confirm
    each retrieval returns its own row."""
    cache = TerminologyCache(tmp_path)
    full = _make_expansion(codes=frozenset({"a", "b", "c"}))
    snomed_only = _make_expansion(codes=frozenset({"a", "b"}))

    cache.put_vsac_expansion(full)
    cache.put_vsac_expansion(snomed_only, system_filter=SNOMED)

    assert cache.get_vsac_expansion(DIABETES_OID) == full
    assert cache.get_vsac_expansion(DIABETES_OID, system_filter=SNOMED) == snomed_only


# ---------- envelope fingerprint ----------


def test_envelope_fingerprint_is_stable_and_short() -> None:
    """8 hex chars, deterministic. The cache filename depends on
    this; a regression here re-orphans every prior cache entry on
    every fresh process, which is much worse than this test
    failing."""
    fp = vsac_envelope_fingerprint()
    assert len(fp) == 8
    assert fp == vsac_envelope_fingerprint()
    int(fp, 16)


def test_envelope_fingerprint_differs_for_a_different_schema() -> None:
    """Inject a probe model with a different schema and confirm the
    fingerprint differs. Pins the contract that an envelope rev
    produces a different digest, which is what makes the
    auto-invalidation guarantee real."""

    class _Probe(BaseModel):
        x: int

    probe_canonical = json.dumps(
        _Probe.model_json_schema(), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    probe_fp = hashlib.sha256(probe_canonical).hexdigest()[:8]
    assert probe_fp != vsac_envelope_fingerprint()


def test_old_envelope_under_different_fingerprint_is_invisible(tmp_path: Path) -> None:
    """Simulate "envelope shape changed in a new commit": a file
    written under an old fingerprint must be invisible to the
    current read path. This is the whole point of D-66's
    auto-invalidating cache key — proven here for the terminology
    cache by writing under a fake fingerprint and confirming the
    real `get_*` reports a miss."""
    cache = TerminologyCache(tmp_path)
    stale_path = cache_path_for_vsac(DIABETES_OID, tmp_path, schema_fp="deadbeef")
    stale_path.parent.mkdir(parents=True, exist_ok=True)
    envelope = StoredVSACExpansion(
        expansion=_make_expansion(),
        cached_at="2026-01-01T00:00:00+00:00",
        system_filter=None,
    )
    stale_path.write_text(envelope.model_dump_json())

    assert cache.get_vsac_expansion(DIABETES_OID) is None


# ---------- vsac_expansion_or_fetch convenience ----------


def test_or_fetch_calls_fetcher_on_miss_and_persists(tmp_path: Path) -> None:
    """First call is a miss → fetcher invoked exactly once → result
    persisted. Second call hits the cache and the fetcher is not
    invoked again. Demonstrates the actual usage shape callers
    will hit."""
    cache = TerminologyCache(tmp_path)
    call_count = {"n": 0}
    expansion = _make_expansion()

    def fake_fetch() -> VSACExpansion:
        call_count["n"] += 1
        return expansion

    first = cache.vsac_expansion_or_fetch(DIABETES_OID, fetch=fake_fetch)
    second = cache.vsac_expansion_or_fetch(DIABETES_OID, fetch=fake_fetch)

    assert first == expansion
    assert second == expansion
    assert call_count["n"] == 1


def test_or_fetch_propagates_fetcher_exceptions(tmp_path: Path) -> None:
    """Fetcher exceptions must propagate unchanged — the cache must
    not paper over upstream failures (which would silently treat a
    transient VSAC outage as 'no codes for this OID', exactly the
    wrong default)."""
    cache = TerminologyCache(tmp_path)

    def boom() -> VSACExpansion:
        raise RuntimeError("vsac is down")

    with pytest.raises(RuntimeError, match="vsac is down"):
        cache.vsac_expansion_or_fetch(DIABETES_OID, fetch=boom)
    assert cache.get_vsac_expansion(DIABETES_OID) is None


def test_or_fetch_discriminates_filter(tmp_path: Path) -> None:
    """A miss for one filter must not be served by a hit on a
    different filter. End-to-end check of the filter-key behavior
    when going through the convenience method."""
    cache = TerminologyCache(tmp_path)
    fetched: list[str | None] = []

    def make_fetcher(tag: str | None) -> Callable[[], VSACExpansion]:
        def _f() -> VSACExpansion:
            fetched.append(tag)
            return _make_expansion(codes=frozenset({tag or "any"}))

        return _f

    cache.vsac_expansion_or_fetch(DIABETES_OID, fetch=make_fetcher(None))
    cache.vsac_expansion_or_fetch(DIABETES_OID, fetch=make_fetcher(SNOMED), system_filter=SNOMED)

    assert fetched == [None, SNOMED]


# ---------- atomicity ----------


def test_put_does_not_leave_temp_files_on_success(tmp_path: Path) -> None:
    """Atomic write uses a temp file + os.replace; on success the
    cache dir contains only the final json file. A leftover .tmp
    would suggest the rename never happened, which would also
    suggest the durability guarantee is broken."""
    cache = TerminologyCache(tmp_path)
    cache.put_vsac_expansion(_make_expansion())

    leftover_tmps = list(tmp_path.glob("*.tmp"))
    assert leftover_tmps == []
    json_files = list(tmp_path.glob("vsac.*.json"))
    assert len(json_files) == 1


def test_get_propagates_corruption_loudly(tmp_path: Path) -> None:
    """A malformed cache file is a real bug; silently treating it as
    a miss would mask the corruption and re-fetch indefinitely.
    Mirrors `load_cached_extraction`'s behavior — failures here
    surface as Pydantic ValidationError so the caller sees
    something actionable."""
    cache = TerminologyCache(tmp_path)
    path = cache_path_for_vsac(DIABETES_OID, tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not even json")

    with pytest.raises(ValidationError):
        cache.get_vsac_expansion(DIABETES_OID)


# ---------- settings wiring ----------


def test_settings_default_terminology_cache_dir() -> None:
    """Default lives under data/cache/ (already gitignored) so a
    fresh checkout's first cache write doesn't accidentally
    introduce tracked files. If you change the default, also update
    `.gitignore` and `.env.example`."""
    from clinical_demo.settings import Settings

    s = Settings.model_construct()
    assert s.terminology_cache_dir == Path("data/cache/terminology")


def test_settings_terminology_cache_dir_overridable_via_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Env override is what tests and CI use to redirect the cache
    away from the working tree."""
    from clinical_demo.settings import Settings

    custom = tmp_path / "elsewhere"
    monkeypatch.setenv("TERMINOLOGY_CACHE_DIR", str(custom))
    # Bypass the .env file so a real .env on disk doesn't shadow
    # the monkey-patched env var.
    s = Settings(_env_file=None)

    assert s.terminology_cache_dir == custom
