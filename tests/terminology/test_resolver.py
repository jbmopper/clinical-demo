"""Tests for the surface-form -> ConceptSet resolver.

The resolver is the cache-first orchestrator that the matcher hits
under `Settings.binding_strategy == "two_pass"`. These tests cover
its three modes -- cache-hit, cache-miss-with-fetcher, soft-fail --
across both VSAC and RxNorm bindings.

All tests run offline:
- VSAC traffic is faked via `httpx.MockTransport` (same pattern as
  `test_vsac_client.py`).
- RxNorm traffic is faked the same way.
- The cache is rooted at `tmp_path` so each test starts empty
  unless it explicitly writes a fixture in.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from clinical_demo.profile import ConceptSet
from clinical_demo.terminology import (
    ECQM_DIABETES_OID,
    RxNormBinding,
    RxNormClient,
    RxNormConcepts,
    TerminologyCache,
    TerminologyResolver,
    VSACBinding,
    VSACClient,
    VSACExpansion,
    cache_path_for_rxnorm,
    cache_path_for_vsac,
)
from clinical_demo.terminology.rxnorm_client import RXNORM_SYSTEM_URI

VSAC_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "vsac" / "diabetes_expansion.json"
RXNORM_FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "rxnorm" / "metformin_drugs.json"
)
SNOMED = "http://snomed.info/sct"


# ---------- pre-warm helpers ----------


def _prewarm_vsac(
    cache: TerminologyCache,
    *,
    oid: str = ECQM_DIABETES_OID,
    codes: frozenset[str] | None = None,
    system_filter: str | None = None,
) -> VSACExpansion:
    """Write a `StoredVSACExpansion` to disk so the resolver hits
    the cache without needing a client. Returns the expansion the
    cache will return so tests can assert on it."""
    expansion = VSACExpansion(
        oid=oid,
        version="20210220",
        concept_set=ConceptSet(
            name="Diabetes",
            system=SNOMED,
            codes=codes if codes is not None else frozenset({"44054006", "73211009"}),
        ),
    )
    cache.put_vsac_expansion(expansion, system_filter=system_filter)
    return expansion


def _prewarm_rxnorm(
    cache: TerminologyCache,
    *,
    name: str = "metformin",
    codes: frozenset[str] | None = None,
    tty_filter: frozenset[str] | None = None,
) -> RxNormConcepts:
    concepts = RxNormConcepts(
        query=name,
        concept_set=ConceptSet(
            name=name,
            system=RXNORM_SYSTEM_URI,
            codes=codes if codes is not None else frozenset({"6809"}),
        ),
        term_types=frozenset({"IN"}),
    )
    cache.put_rxnorm_concepts(concepts, tty_filter=tty_filter)
    return concepts


def _vsac_client_with_body(body: bytes) -> tuple[VSACClient, list[httpx.Request]]:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, content=body, headers={"content-type": "application/fhir+json"})

    return VSACClient(api_key="dummy-key", transport=httpx.MockTransport(handler)), captured


def _vsac_client_failing(*, status: int = 500) -> VSACClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status, content=b"upstream error", headers={"content-type": "text/plain"}
        )

    return VSACClient(api_key="dummy-key", transport=httpx.MockTransport(handler))


def _rxnorm_client_with_body(body: bytes) -> tuple[RxNormClient, list[httpx.Request]]:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    return RxNormClient(transport=httpx.MockTransport(handler)), captured


# ---------- VSAC: cache hit ----------


def test_resolve_vsac_returns_cached_concept_set_without_client(tmp_path: Path) -> None:
    """Cache hit short-circuits before the client is touched. Pass
    `vsac_client=None` so any attempt to fetch would AttributeError;
    the test passing proves the cache really did short-circuit."""
    cache = TerminologyCache(tmp_path)
    expansion = _prewarm_vsac(cache)
    resolver = TerminologyResolver(cache, vsac_client=None)

    out = resolver.resolve(VSACBinding(oid=ECQM_DIABETES_OID))

    assert out is not None
    assert out == expansion.concept_set
    assert "44054006" in out.codes


# ---------- VSAC: cache miss + fetch ----------


def test_resolve_vsac_fetches_on_cache_miss_and_caches_result(tmp_path: Path) -> None:
    """First call: cache empty -> client fetches -> cache populated.
    Second call: cache hit -> client untouched. Counting captured
    requests pins the no-double-fetch property."""
    cache = TerminologyCache(tmp_path)
    body = VSAC_FIXTURE.read_bytes()
    client, captured = _vsac_client_with_body(body)
    resolver = TerminologyResolver(cache, vsac_client=client)

    first = resolver.resolve(VSACBinding(oid=ECQM_DIABETES_OID))
    second = resolver.resolve(VSACBinding(oid=ECQM_DIABETES_OID))

    assert first is not None
    assert second is not None
    assert first == second
    # Cache miss -> one HTTP call. Second call serves from disk.
    assert len(captured) == 1
    # And the row landed on disk for future processes.
    expected = cache_path_for_vsac(ECQM_DIABETES_OID, tmp_path)
    assert expected.exists()


def test_resolve_vsac_cache_miss_with_no_client_soft_fails(tmp_path: Path) -> None:
    """No credentials, no pre-warmed cache -> resolver returns
    None and the matcher falls back to the alias table. The whole
    point of the soft-fail discipline."""
    cache = TerminologyCache(tmp_path)
    resolver = TerminologyResolver(cache, vsac_client=None)

    out = resolver.resolve(VSACBinding(oid=ECQM_DIABETES_OID))
    assert out is None


def test_resolve_vsac_fetch_error_soft_fails(tmp_path: Path) -> None:
    """Upstream 500 -> client raises VSACError -> resolver catches
    and returns None. Matcher degrades to alias / unmapped without
    the run crashing."""
    cache = TerminologyCache(tmp_path)
    resolver = TerminologyResolver(cache, vsac_client=_vsac_client_failing(status=500))

    out = resolver.resolve(VSACBinding(oid=ECQM_DIABETES_OID))
    assert out is None
    # Cache must NOT contain a poisoned entry.
    assert not cache_path_for_vsac(ECQM_DIABETES_OID, tmp_path).exists()


def test_resolve_vsac_network_error_soft_fails(tmp_path: Path) -> None:
    """`httpx.HTTPError` (DNS, connection refused, etc.) is caught
    the same as `VSACError`. The transport raises
    `httpx.ConnectError`, the client wraps it in `VSACError` per
    its own contract; the resolver swallows either."""

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated DNS failure")

    cache = TerminologyCache(tmp_path)
    client = VSACClient(api_key="dummy-key", transport=httpx.MockTransport(boom))
    resolver = TerminologyResolver(cache, vsac_client=client)

    out = resolver.resolve(VSACBinding(oid=ECQM_DIABETES_OID))
    assert out is None


def test_resolve_vsac_passes_system_filter_to_cache_and_client(tmp_path: Path) -> None:
    """A binding with `system_filter` must produce a different
    cache key than the same OID without one. Pre-warm both keys
    with disjoint code sets and assert the resolver picks the
    right one."""
    cache = TerminologyCache(tmp_path)
    _prewarm_vsac(cache, codes=frozenset({"44054006"}))  # no filter
    _prewarm_vsac(cache, codes=frozenset({"73211009"}), system_filter=SNOMED)

    resolver = TerminologyResolver(cache, vsac_client=None)
    no_filter = resolver.resolve(VSACBinding(oid=ECQM_DIABETES_OID))
    with_filter = resolver.resolve(VSACBinding(oid=ECQM_DIABETES_OID, system_filter=SNOMED))

    assert no_filter is not None and with_filter is not None
    assert no_filter.codes == frozenset({"44054006"})
    assert with_filter.codes == frozenset({"73211009"})


# ---------- RxNorm: parallel coverage ----------


def test_resolve_rxnorm_returns_cached_without_client(tmp_path: Path) -> None:
    cache = TerminologyCache(tmp_path)
    concepts = _prewarm_rxnorm(cache)
    resolver = TerminologyResolver(cache, rxnorm_client=None)

    out = resolver.resolve(RxNormBinding(name="metformin"))
    assert out == concepts.concept_set


def test_resolve_rxnorm_fetches_on_miss_and_caches(tmp_path: Path) -> None:
    cache = TerminologyCache(tmp_path)
    body = RXNORM_FIXTURE.read_bytes()
    client, captured = _rxnorm_client_with_body(body)
    resolver = TerminologyResolver(cache, rxnorm_client=client)

    first = resolver.resolve(RxNormBinding(name="metformin"))
    second = resolver.resolve(RxNormBinding(name="metformin"))

    assert first is not None
    assert first == second
    assert len(captured) == 1
    assert cache_path_for_rxnorm("metformin", tmp_path).exists()


def test_resolve_rxnorm_cache_miss_with_no_client_soft_fails(tmp_path: Path) -> None:
    cache = TerminologyCache(tmp_path)
    resolver = TerminologyResolver(cache, rxnorm_client=None)

    out = resolver.resolve(RxNormBinding(name="metformin"))
    assert out is None


def test_resolve_rxnorm_network_error_soft_fails(tmp_path: Path) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated network failure")

    cache = TerminologyCache(tmp_path)
    client = RxNormClient(transport=httpx.MockTransport(boom))
    resolver = TerminologyResolver(cache, rxnorm_client=client)

    out = resolver.resolve(RxNormBinding(name="metformin"))
    assert out is None


def test_resolve_rxnorm_tty_filter_keys_cache_independently(tmp_path: Path) -> None:
    """Same name with vs. without `tty_filter` is two different
    cache rows, mirroring the VSAC system_filter behaviour."""
    cache = TerminologyCache(tmp_path)
    _prewarm_rxnorm(cache, codes=frozenset({"6809"}))  # no filter
    _prewarm_rxnorm(cache, codes=frozenset({"99999"}), tty_filter=frozenset({"IN"}))

    resolver = TerminologyResolver(cache, rxnorm_client=None)
    no_filter = resolver.resolve(RxNormBinding(name="metformin"))
    with_filter = resolver.resolve(RxNormBinding(name="metformin", tty_filter=("IN",)))

    assert no_filter is not None and with_filter is not None
    assert no_filter.codes == frozenset({"6809"})
    assert with_filter.codes == frozenset({"99999"})


# ---------- surface-form wrappers ----------


def test_resolve_condition_uses_registry_then_cache(tmp_path: Path) -> None:
    """End-to-end: the registry's T2DM binding -> the resolver's
    cache hit -> a ConceptSet shaped exactly like the alias path
    would have produced. This is the wire-up slice 4 exists for."""
    cache = TerminologyCache(tmp_path)
    expansion = _prewarm_vsac(cache)
    resolver = TerminologyResolver(cache, vsac_client=None)

    for surface in ("type 2 diabetes", "T2DM", "  Type II Diabetes  "):
        out = resolver.resolve_condition(surface)
        assert out is not None, f"surface {surface!r} should resolve"
        assert out == expansion.concept_set


def test_resolve_condition_unregistered_surface_returns_none(tmp_path: Path) -> None:
    """Unknown surface -> `None` from the registry -> resolver
    returns `None` so the caller falls back to the alias table.
    Distinct from 'unmapped concept' (which is the matcher's
    final verdict if both bridges miss)."""
    cache = TerminologyCache(tmp_path)
    resolver = TerminologyResolver(cache, vsac_client=None)
    assert resolver.resolve_condition("acute pancreatitis") is None


def test_resolve_lab_returns_none_in_v0_empty_registry(tmp_path: Path) -> None:
    cache = TerminologyCache(tmp_path)
    resolver = TerminologyResolver(cache, vsac_client=None)
    assert resolver.resolve_lab("hba1c") is None


def test_resolve_medication_returns_none_in_v0_empty_registry(tmp_path: Path) -> None:
    cache = TerminologyCache(tmp_path)
    resolver = TerminologyResolver(cache, rxnorm_client=None)
    assert resolver.resolve_medication("metformin") is None


# ---------- defensive ----------


def test_resolve_unknown_binding_type_soft_fails(tmp_path: Path) -> None:
    """A binding type not in the dispatch table doesn't crash the
    resolver. Constructed via a Pydantic model that satisfies the
    structural shape but isn't either concrete branch."""

    class FakeBinding:
        """Future binding type the dispatch hasn't learned yet."""

    cache = TerminologyCache(tmp_path)
    resolver = TerminologyResolver(cache)
    # Bypass type-checker to simulate a future bindings.py addition
    # that landed without resolver.py learning about it.
    out = resolver.resolve(FakeBinding())  # type: ignore[arg-type]
    assert out is None
