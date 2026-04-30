"""On-disk cache for terminology bindings.

The terminology APIs (VSAC, RxNorm, UMLS) sit at the edge of the
matcher: the same trial-side surface form ("type 2 diabetes",
"metformin", ...) resolves to the same `ConceptSet` for every
patient x trial x criterion pair. Re-calling NLM endpoints on every
eval pair would be wasteful (network latency, rate-limit risk) and —
more importantly for a regression-style baseline — would couple
"what the matcher decided yesterday" to "what NLM was serving
yesterday." The cache pins a binding-resolution result to disk so
re-runs are deterministic and so `--no-llm`-style offline scoring
can extend to "no live terminology calls" too.

Cache key (mirrors the D-66 extractor cache discipline)
-------------------------------------------------------
Filename embeds two things the on-disk envelope is sensitive to:

1. The query identity (e.g. for VSAC: the value-set OID + optional
   single-system filter, since the same OID expanded with and
   without a filter yields different `ConceptSet`s).
2. The on-disk envelope schema fingerprint (`vsac_envelope_fingerprint`).

Any change to either signal produces a different filename, so an old
envelope is *invisible* to the new read path rather than silently
pumping a stale or shape-mismatched binding through the matcher. Old
envelopes orphan in the same dir; gitignored, no harm.

Why fingerprint the envelope and not the underlying data model? Same
reason as D-66 for the extractor: humans should not be on the hook
to remember to bump a constant when a typed field is added. The
hash is automatic; the alternative (silent stale data) is exactly
the failure mode this cache exists to make impossible.

The cache is namespaced per terminology source. v0 supports VSAC
only; RxNorm and UMLS will land alongside their respective clients
(D-69 follow-on slice). Splitting the namespaces at the filename
level (rather than one big union envelope) keeps each source
independently revvable and lets us add a new source without
invalidating prior caches.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel

from clinical_demo.terminology.rxnorm_client import RxNormConcepts
from clinical_demo.terminology.vsac_client import VSACExpansion

log = logging.getLogger(__name__)


# ---------- on-disk envelopes ----------


class StoredVSACExpansion(BaseModel):
    """On-disk envelope for one cached VSAC `$expand` result.

    Kept separate from the in-memory `VSACExpansion` so envelope
    evolution (adding `cached_at`, `source_url`, future provenance
    fields) does not ripple through the matcher's import surface.
    The in-memory shape is what the matcher consumes; this is what
    the cache writes.
    """

    expansion: VSACExpansion
    # ISO-8601 UTC timestamp for when the row was written to disk.
    # Not part of the cache key; useful for cache-aging dashboards
    # and for "when did we last refresh this OID" debugging.
    cached_at: str
    # The `system_filter` argument that produced this row, recorded
    # so callers reading the file directly can reconstruct the
    # original query without parsing the filename. None when the
    # caller did not pass a filter.
    system_filter: str | None = None


class StoredRxNormConcepts(BaseModel):
    """On-disk envelope for one cached RxNorm `/drugs.json` result.

    Mirrors `StoredVSACExpansion`: the in-memory `RxNormConcepts`
    is what the matcher consumes; this envelope adds disk-only
    provenance (cached_at, the tty_filter argument used at fetch
    time) without polluting the matcher's import surface.

    `tty_filter` is recorded as a sorted list rather than a frozenset
    so the on-disk JSON is canonical (sorted, indented) and human-
    diffable when re-recording fixtures.
    """

    concepts: RxNormConcepts
    cached_at: str
    tty_filter: list[str] | None = None


# ---------- cache key helpers ----------


def _envelope_fingerprint(model: type[BaseModel]) -> str:
    """8-char SHA-256 over a Pydantic model's canonical JSON schema.

    Helper shared by the per-source fingerprint accessors. Truncated
    to 8 hex chars (32 bits) because the only consumer is the cache
    filename and collision risk between two manual envelope edits is
    negligible.
    """
    schema = model.model_json_schema()
    canonical = json.dumps(schema, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()[:8]


@lru_cache(maxsize=1)
def vsac_envelope_fingerprint() -> str:
    """8-char hex digest of the `StoredVSACExpansion` JSON schema.

    Mirrors `scoring.cache.schema_fingerprint`: any field-level
    addition / removal / retype on the envelope produces a different
    fingerprint, which produces a different cache filename, which
    auto-orphans every prior cache entry.
    """
    return _envelope_fingerprint(StoredVSACExpansion)


@lru_cache(maxsize=1)
def rxnorm_envelope_fingerprint() -> str:
    """8-char hex digest of the `StoredRxNormConcepts` JSON schema.

    Same auto-invalidation discipline as `vsac_envelope_fingerprint`.
    Independent fingerprint per source so an RxNorm envelope rev
    does not invalidate VSAC cache entries (and vice versa).
    """
    return _envelope_fingerprint(StoredRxNormConcepts)


def _filter_tag(system_filter: str | None) -> str:
    """Filename-safe representation of a `system_filter` value.

    `None` → `"any"`. A real URI is hashed (8 hex chars) so the
    filename stays short and OS-safe regardless of which coding
    system the caller filtered on. The hash is keyed on the filter
    string only — not on any other context — so the same filter
    always produces the same tag.
    """
    if system_filter is None:
        return "any"
    digest = hashlib.sha256(system_filter.encode("utf-8")).hexdigest()
    return digest[:8]


def _sanitize_oid(oid: str) -> str:
    """Strip the `urn:oid:` prefix VSAC sometimes emits and reject
    anything else that wouldn't be filename-safe.

    OIDs are dotted-decimal (e.g. `2.16.840.1.113883.3.464…`) so the
    base form is already filename-safe; we only need to drop the
    optional URN prefix to keep the same OID from producing two
    different cache files."""
    return oid.removeprefix("urn:oid:")


def cache_path_for_vsac(
    oid: str,
    root: Path,
    *,
    system_filter: str | None = None,
    schema_fp: str | None = None,
) -> Path:
    """Resolve the cache filename for a (oid, system_filter, envelope) tuple.

    Filename pattern: `vsac.<oid>.<filter_tag>.<schema_fp>.json`.

    The four-segment shape is intentional:

    - `vsac.` namespaces the file so RxNorm / UMLS caches can land
      alongside without colliding.
    - `<oid>.<filter_tag>` is the query identity. Same OID with and
      without a `system_filter` resolve to *different* `ConceptSet`s
      (filtered vs multi-system union), so they must cache separately.
    - `<schema_fp>` auto-invalidates on envelope shape changes
      (D-66 discipline).

    Each segment is independently revvable; at-a-glance debugging
    benefits from seeing all of them on the filename.
    """
    safe_oid = _sanitize_oid(oid)
    fp = schema_fp or vsac_envelope_fingerprint()
    return root / f"vsac.{safe_oid}.{_filter_tag(system_filter)}.{fp}.json"


def _rxnorm_query_tag(name: str) -> str:
    """Filename-safe tag for an RxNorm drug-name query.

    The query string can contain spaces, slashes, brackets, and
    other characters that would either look ugly or break on some
    filesystems (e.g. "metformin/glipizide", "acetaminophen [Tylenol]").
    A short hex digest sidesteps the whole encoding question without
    losing key uniqueness; the original query string is recorded
    inside the envelope for human inspection.
    """
    digest = hashlib.sha256(name.lower().strip().encode("utf-8")).hexdigest()
    return digest[:12]


def _rxnorm_filter_tag(tty_filter: frozenset[str] | None) -> str:
    """Stable tag for an RxNorm `tty_filter` argument.

    `None` -> `"any"`. A populated filter is hashed over the sorted
    member list so {"IN", "PIN"} and {"PIN", "IN"} produce the
    same tag. Keeping the tag short means the filename stays
    readable; the full filter list is recorded inside the envelope.
    """
    if tty_filter is None:
        return "any"
    canonical = ",".join(sorted(tty_filter))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return digest[:8]


def cache_path_for_rxnorm(
    name: str,
    root: Path,
    *,
    tty_filter: frozenset[str] | None = None,
    schema_fp: str | None = None,
) -> Path:
    """Resolve the cache filename for a (name, tty_filter, envelope) tuple.

    Filename pattern: `rxnorm.<query_tag>.<filter_tag>.<schema_fp>.json`.

    The query is hashed (rather than written verbatim) because real
    RxNorm queries contain characters that aren't filename-safe across
    every OS; the original string is recorded inside the envelope.
    Same `vsac.` / `rxnorm.` namespacing, same auto-invalidating
    `<schema_fp>` discipline.
    """
    fp = schema_fp or rxnorm_envelope_fingerprint()
    return root / (f"rxnorm.{_rxnorm_query_tag(name)}.{_rxnorm_filter_tag(tty_filter)}.{fp}.json")


# ---------- main entry point ----------


class TerminologyCache:
    """File-backed cache for resolved terminology bindings.

    One instance per cache root. Construction does *not* create the
    directory; we lazy-create on the first `put_*` call so a fresh
    checkout that never resolves a binding doesn't litter `data/`
    with empty cache dirs.

    All `get_*` methods return `None` on miss (no file, or file
    written under a different envelope schema). Read errors
    (corrupt JSON, validation failure) are intentionally propagated
    rather than silently treated as a miss: a corrupt cache file is
    a real bug and silently re-fetching would mask it. Callers that
    need degrade-don't-crash semantics (e.g. the matcher binding
    layer per D-65/D-66 soft-fail) wrap the read in try/except and
    fall through to `indeterminate(unmapped_concept)`.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        return self._root

    # ----- VSAC -----

    def get_vsac_expansion(
        self, oid: str, *, system_filter: str | None = None
    ) -> VSACExpansion | None:
        """Return the cached expansion for (oid, system_filter), or None on miss."""
        path = cache_path_for_vsac(oid, self._root, system_filter=system_filter)
        if not path.exists():
            return None
        stored = StoredVSACExpansion.model_validate_json(path.read_text())
        return stored.expansion

    def put_vsac_expansion(
        self,
        expansion: VSACExpansion,
        *,
        system_filter: str | None = None,
    ) -> Path:
        """Write a `VSACExpansion` to the cache and return the resulting path.

        Atomic: writes to a sibling temp file in the same directory
        and `os.replace`s into final position so a crash mid-write
        cannot leave a partial JSON file that the next read would
        choke on. The cache root is created if it does not exist.

        The OID stored on the envelope is taken from the expansion
        (sanitized for filename safety); the `system_filter`
        argument is recorded inside the envelope so the file is
        self-describing without filename parsing.
        """
        path = cache_path_for_vsac(expansion.oid, self._root, system_filter=system_filter)
        self._root.mkdir(parents=True, exist_ok=True)
        envelope = StoredVSACExpansion(
            expansion=expansion,
            cached_at=datetime.now(UTC).isoformat(),
            system_filter=system_filter,
        )
        # Temp filename includes a uuid suffix so concurrent writers
        # (rare but possible — two probe scripts in parallel) can't
        # clobber each other's in-flight files.
        tmp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        tmp.write_text(envelope.model_dump_json(indent=2))
        os.replace(tmp, path)
        return path

    def vsac_expansion_or_fetch(
        self,
        oid: str,
        *,
        fetch: Callable[[], VSACExpansion],
        system_filter: str | None = None,
    ) -> VSACExpansion:
        """Get a cached expansion or fetch + cache it if missing.

        The fetcher is a no-arg closure so this module stays
        decoupled from `VSACClient` (which would otherwise need to
        be imported here, creating a fetch-vs-cache import cycle as
        the API surface grows). Callers typically pass
        ``fetch=lambda: client.expand(oid, system_filter=...)``.

        On a cache hit the fetcher is not called. On a miss the
        fetcher is invoked exactly once and its result is persisted
        before being returned, so the next caller for the same key
        gets a hit. Fetcher exceptions propagate unchanged — the
        cache does not paper over upstream failures.
        """
        cached = self.get_vsac_expansion(oid, system_filter=system_filter)
        if cached is not None:
            return cached
        fresh = fetch()
        self.put_vsac_expansion(fresh, system_filter=system_filter)
        return fresh

    # ----- RxNorm -----

    def get_rxnorm_concepts(
        self, name: str, *, tty_filter: frozenset[str] | None = None
    ) -> RxNormConcepts | None:
        """Return the cached RxNorm result for (name, tty_filter), or None on miss."""
        path = cache_path_for_rxnorm(name, self._root, tty_filter=tty_filter)
        if not path.exists():
            return None
        stored = StoredRxNormConcepts.model_validate_json(path.read_text())
        return stored.concepts

    def put_rxnorm_concepts(
        self,
        concepts: RxNormConcepts,
        *,
        tty_filter: frozenset[str] | None = None,
    ) -> Path:
        """Persist an `RxNormConcepts` to the cache and return the path.

        Same atomic write + lazy root creation discipline as
        `put_vsac_expansion`. The query string the result was
        produced from is taken from `concepts.query`; the
        `tty_filter` argument is recorded as a sorted list inside
        the envelope so the file is self-describing.
        """
        path = cache_path_for_rxnorm(concepts.query, self._root, tty_filter=tty_filter)
        self._root.mkdir(parents=True, exist_ok=True)
        envelope = StoredRxNormConcepts(
            concepts=concepts,
            cached_at=datetime.now(UTC).isoformat(),
            tty_filter=sorted(tty_filter) if tty_filter is not None else None,
        )
        tmp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        tmp.write_text(envelope.model_dump_json(indent=2))
        os.replace(tmp, path)
        return path

    def rxnorm_concepts_or_fetch(
        self,
        name: str,
        *,
        fetch: Callable[[], RxNormConcepts],
        tty_filter: frozenset[str] | None = None,
    ) -> RxNormConcepts:
        """Get a cached RxNorm result or fetch + cache it on miss.

        Mirrors `vsac_expansion_or_fetch`: closure-shaped fetcher
        keeps the cache decoupled from `RxNormClient`, fetcher
        invoked exactly once per miss, fetcher exceptions
        propagate unchanged.
        """
        cached = self.get_rxnorm_concepts(name, tty_filter=tty_filter)
        if cached is not None:
            return cached
        fresh = fetch()
        self.put_rxnorm_concepts(fresh, tty_filter=tty_filter)
        return fresh


__all__ = [
    "StoredRxNormConcepts",
    "StoredVSACExpansion",
    "TerminologyCache",
    "cache_path_for_rxnorm",
    "cache_path_for_vsac",
    "rxnorm_envelope_fingerprint",
    "vsac_envelope_fingerprint",
]
