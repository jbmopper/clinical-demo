"""Surface form -> ConceptSet resolver via the bindings registry + cache.

Stitches three pieces together for D-69 slice 4:

1. The trial-side bindings registry (`terminology.bindings`) maps a
   surface form to a `VSACBinding` or `RxNormBinding`.
2. The on-disk `TerminologyCache` (`terminology.cache`) services
   subsequent calls without paying NLM round-trip cost.
3. The live `VSACClient` / `RxNormClient` (`terminology.vsac_client` /
   `.rxnorm_client`) fetch on cache miss when credentials are set.

The resolver is what `matcher.concept_lookup.lookup_*` calls when
`Settings.binding_strategy == "two_pass"`. Its return contract is
intentionally identical to the alias table's: `ConceptSet` on
success, `None` on any kind of miss or soft-fail. The matcher's
existing `unmapped_concept` branch consumes `None` unchanged, so a
terminology outage degrades to the same `indeterminate` verdict the
alias path produces today (D-65 / D-66 soft-fail discipline applied
at the binding layer).

Soft-fail rules
---------------

- Surface form not in the bindings registry -> `None` (caller falls
  back to the alias table).
- Binding present, cache hit -> `ConceptSet`.
- Binding present, cache miss, credentials available, fetch
  succeeds -> `ConceptSet` (and the cache now has the row for next
  time).
- Binding present, cache miss, no credentials / no client -> `None`
  + warning log. Lets a fresh checkout without `UMLS_API_KEY` opt
  into `two_pass` and still produce useful output for any binding
  whose cache is pre-warmed (e.g. shipped with the repo for tests).
- Binding present, fetch raises (network, auth, rate limit,
  schema drift) -> caught, warning logged, `None` returned. The
  matcher's downstream verdict is the same shape as an alias miss;
  the warning is the only externally visible signal that a degrade
  happened.

Lifetime
--------
A `TerminologyResolver` is cheap to construct (no I/O); the
`get_resolver()` accessor keeps a process-wide singleton so the
matcher's hot path doesn't re-instantiate clients per criterion.
Tests build their own resolvers against a temp cache and inject
clients (or `None`) directly.
"""

from __future__ import annotations

import logging
from functools import lru_cache

import httpx

from clinical_demo.profile import ConceptSet
from clinical_demo.settings import Settings, get_settings
from clinical_demo.terminology.bindings import (
    Binding,
    RxNormBinding,
    VSACBinding,
    lookup_condition_binding,
    lookup_lab_binding,
    lookup_medication_binding,
)
from clinical_demo.terminology.cache import TerminologyCache
from clinical_demo.terminology.rxnorm_client import RxNormClient, RxNormError
from clinical_demo.terminology.vsac_client import VSACClient, VSACError

log = logging.getLogger(__name__)


class TerminologyResolver:
    """Cache-first surface-form -> ConceptSet resolver.

    Constructed with a `TerminologyCache` and (optionally) live
    clients. When a client is `None`, cache-miss for that source
    short-circuits to `None` instead of raising -- the matcher
    fall-through to the alias table is the intended behavior."""

    def __init__(
        self,
        cache: TerminologyCache,
        *,
        vsac_client: VSACClient | None = None,
        rxnorm_client: RxNormClient | None = None,
    ) -> None:
        self._cache = cache
        self._vsac = vsac_client
        self._rxnorm = rxnorm_client

    # ----- per-binding-type primitives -----

    def resolve(self, binding: Binding) -> ConceptSet | None:
        """Dispatch on binding type. Soft-fails to `None` per the
        module docstring."""
        if isinstance(binding, VSACBinding):
            return self._resolve_vsac(binding)
        if isinstance(binding, RxNormBinding):
            return self._resolve_rxnorm(binding)
        # Defensive: a future binding type added without a resolver
        # branch would fall through here. Soft-fail rather than
        # crash the matcher.
        log.warning("TerminologyResolver: unknown binding type %r", type(binding).__name__)
        return None

    def _resolve_vsac(self, binding: VSACBinding) -> ConceptSet | None:
        """Cache-first VSAC expansion. Cache miss + no client ->
        `None`. Fetch error -> `None` + warning."""
        cached = self._cache.get_vsac_expansion(binding.oid, system_filter=binding.system_filter)
        if cached is not None:
            return cached.concept_set

        if self._vsac is None:
            log.warning(
                "TerminologyResolver: VSAC cache miss for OID %s and no client "
                "configured (UMLS_API_KEY unset?); falling through.",
                binding.oid,
            )
            return None

        try:
            expansion = self._cache.vsac_expansion_or_fetch(
                binding.oid,
                system_filter=binding.system_filter,
                fetch=lambda: self._vsac.expand(  # type: ignore[union-attr]
                    binding.oid, system_filter=binding.system_filter
                ),
            )
        except (VSACError, httpx.HTTPError) as exc:
            log.warning(
                "TerminologyResolver: VSAC fetch for OID %s failed (%s); falling through.",
                binding.oid,
                exc,
            )
            return None
        return expansion.concept_set

    def _resolve_rxnorm(self, binding: RxNormBinding) -> ConceptSet | None:
        """Cache-first RxNorm name lookup. Same soft-fail discipline
        as `_resolve_vsac`. The binding stores `tty_filter` as a
        sorted tuple for hashability; the client + cache layer want
        a `frozenset[str] | None`, so we convert here."""
        tty: frozenset[str] | None = (
            frozenset(binding.tty_filter) if binding.tty_filter is not None else None
        )

        cached = self._cache.get_rxnorm_concepts(binding.name, tty_filter=tty)
        if cached is not None:
            return cached.concept_set

        if self._rxnorm is None:
            log.warning(
                "TerminologyResolver: RxNorm cache miss for name %r and no client "
                "configured; falling through.",
                binding.name,
            )
            return None

        try:
            concepts = self._cache.rxnorm_concepts_or_fetch(
                binding.name,
                tty_filter=tty,
                fetch=lambda: self._rxnorm.find_drug_concepts(  # type: ignore[union-attr]
                    binding.name, tty_filter=tty
                ),
            )
        except (RxNormError, httpx.HTTPError) as exc:
            log.warning(
                "TerminologyResolver: RxNorm fetch for %r failed (%s); falling through.",
                binding.name,
                exc,
            )
            return None
        return concepts.concept_set

    # ----- surface-form convenience wrappers -----
    #
    # Mirror the three `lookup_*` entry points in
    # `matcher.concept_lookup` so the matcher-side switch is a
    # one-line delegation.

    def resolve_condition(self, surface: str) -> ConceptSet | None:
        binding = lookup_condition_binding(surface)
        if binding is None:
            return None
        return self.resolve(binding)

    def resolve_lab(self, surface: str) -> ConceptSet | None:
        binding = lookup_lab_binding(surface)
        if binding is None:
            return None
        return self.resolve(binding)

    def resolve_medication(self, surface: str) -> ConceptSet | None:
        binding = lookup_medication_binding(surface)
        if binding is None:
            return None
        return self.resolve(binding)


# ---------- process-wide singleton accessor ----------


def _build_default_resolver(settings: Settings) -> TerminologyResolver:
    """Construct the singleton resolver from settings.

    `VSACClient` requires `UMLS_API_KEY`; if it's unset, the client
    is `None` and the resolver soft-fails on VSAC cache misses
    (intentional -- a fresh checkout without a UMLS account can
    still opt into `two_pass` and benefit from any pre-warmed cache
    rows). `RxNormClient` is unconditional -- the RxNav surface is
    public, no API key required."""
    cache = TerminologyCache(settings.terminology_cache_dir)

    vsac_client: VSACClient | None
    if settings.umls_api_key is not None:
        try:
            vsac_client = VSACClient(api_key=settings.umls_api_key.get_secret_value())
        except VSACError as exc:
            log.warning(
                "TerminologyResolver: VSACClient construction failed (%s); "
                "VSAC cache misses will soft-fail.",
                exc,
            )
            vsac_client = None
    else:
        vsac_client = None

    rxnorm_client = RxNormClient()

    return TerminologyResolver(
        cache,
        vsac_client=vsac_client,
        rxnorm_client=rxnorm_client,
    )


@lru_cache(maxsize=1)
def get_resolver() -> TerminologyResolver:
    """Process-wide singleton. Cleared by tests via
    `get_resolver.cache_clear()` after monkey-patching settings."""
    return _build_default_resolver(get_settings())


__all__ = [
    "TerminologyResolver",
    "get_resolver",
]
