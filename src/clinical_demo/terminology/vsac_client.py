"""VSAC FHIR `$expand` client.

VSAC (Value Set Authority Center, https://vsac.nlm.nih.gov) hosts
CMS/NCQA-authored eCQM value sets — pre-vetted code lists like
"all SNOMED codes that count as Diabetes for quality measurement."
The FHIR endpoint at https://cts.nlm.nih.gov/fhir lets us resolve a
value-set OID into its full code list without standing up our own
terminology server (D-69 rejected (d)).

Auth is HTTP Basic with username `"apikey"` and the user's UMLS UTS
key (https://uts.nlm.nih.gov/uts/profile) as the password — the same
key gates RxNorm and UMLS REST too. The key lives in
`Settings.umls_api_key` (D-31 / D-69).

This v0 client returns a `VSACExpansion` envelope that pairs the
matcher-shaped `ConceptSet` (name + system + frozenset of codes)
with the value-set OID and *version* the VSAC server expanded
against, so the eval store can pin the version per run (D-69's
"VSAC value-set version pinned per eval run" requirement). A v0
expansion is single-system in practice for the OIDs we care about
(eCQM diabetes is SNOMED-only, the eCQM med lists are RxNorm-only,
etc.); when that stops being true, the envelope grows a per-system
breakdown rather than the matcher learning to reason across systems.
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel

from clinical_demo.profile import ConceptSet
from clinical_demo.settings import Settings, get_settings

DEFAULT_BASE_URL = "https://cts.nlm.nih.gov/fhir"

# Code-system URI ↔ FHIR canonical URL. VSAC reports the URL form on
# expansion rows; ConceptSet.system uses the URI form to match the
# patient profile's coding (Patient resources from Synthea use
# `http://snomed.info/sct`, not the OID). One direction is enough
# for v0; expand when a non-SNOMED expansion comes through.
_SYSTEM_URI_FROM_URL: dict[str, str] = {
    "http://snomed.info/sct": "http://snomed.info/sct",
    "http://loinc.org": "http://loinc.org",
    "http://www.nlm.nih.gov/research/umls/rxnorm": ("http://www.nlm.nih.gov/research/umls/rxnorm"),
}


class VSACError(RuntimeError):
    """Anything VSAC-side that prevented a usable expansion.

    Caught by callers (and eventually by the matcher's binding-
    dispatch path) so a transient terminology-server failure
    degrades to `indeterminate(unmapped_concept)` rather than
    crashing a scoring run — same shape as D-65/D-66's soft-fail
    discipline, applied at the binding layer."""


class VSACExpansion(BaseModel):
    """One value-set expansion as the matcher will consume it.

    `version` is the VSAC-reported version of the value set this
    expansion came from; it lands on the eval `runs` row so a CMS
    re-publish can't silently shift a baseline (D-69 architecture
    note).
    """

    oid: str
    version: str
    concept_set: ConceptSet


class VSACClient:
    """Thin sync wrapper over VSAC FHIR `$expand`.

    Sync (not async) because v0 callers are CLI scripts and a
    single-process eval runner; the surface area an async client
    would unlock is the same surface area `httpx.AsyncClient` would
    have given us — not a v0 concern.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if api_key is None:
            settings: Settings = get_settings()
            secret = settings.umls_api_key
            if secret is None:
                raise VSACError(
                    "UMLS_API_KEY is not set; cannot call VSAC. "
                    "Set it in .env or pass api_key= explicitly."
                )
            api_key = secret.get_secret_value()
        self._base_url = base_url.rstrip("/")
        # `apikey` is VSAC's literal expected username when
        # Basic-auth'ing with a UTS key.
        self._auth = ("apikey", api_key)
        self._timeout = timeout_seconds
        self._transport = transport

    def expand(
        self,
        oid: str,
        *,
        name: str | None = None,
        system_filter: str | None = None,
    ) -> VSACExpansion:
        """Expand a VSAC value set by OID.

        `name` defaults to the OID; pass a human-readable name when
        the caller knows one (e.g., "Diabetes" for the eCQM diabetes
        value set) so the resulting `ConceptSet.name` reads sensibly
        in matcher evidence rows.

        `system_filter` restricts the parsed codes to a single coding
        system (e.g., `"http://snomed.info/sct"`). Many eCQM value
        sets — including the Diabetes one — span SNOMED + ICD-10-CM;
        the matcher's `PatientProfile` is single-system per query
        (D-25), so filtering at expansion time keeps the resulting
        `ConceptSet` aligned with the codes the patient is actually
        coded in. Omit the filter only when you've confirmed the
        value set is single-system; otherwise multi-system expansions
        raise `VSACError` so silent code-system mismatches can't slip
        through.
        """
        # Use the FHIR read-by-id form `GET /ValueSet/<OID>/$expand`.
        # The alternative (`?url=<canonical>`) requires a fully-
        # qualified canonical URL — `urn:oid:` is rejected with 404
        # by VSAC's $expand. Read-by-id sidesteps the URL-shape
        # question entirely and is what VSAC's own docs lean on.
        bare_oid = oid.removeprefix("urn:oid:")
        try:
            with httpx.Client(
                auth=self._auth,
                timeout=self._timeout,
                transport=self._transport,
            ) as client:
                response = client.get(f"{self._base_url}/ValueSet/{bare_oid}/$expand")
        except httpx.HTTPError as exc:  # network / timeout / DNS
            raise VSACError(f"VSAC request failed for OID {oid}: {exc}") from exc

        if response.status_code != 200:
            raise VSACError(
                f"VSAC returned {response.status_code} for OID {oid}: {response.text[:200]}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise VSACError(f"VSAC response for OID {oid} was not JSON: {exc}") from exc

        return _parse_expansion(
            payload,
            oid=bare_oid,
            name=name or bare_oid,
            system_filter=system_filter,
        )


def _parse_expansion(
    payload: dict[str, Any], *, oid: str, name: str, system_filter: str | None
) -> VSACExpansion:
    """Pull the `expansion.contains` list out of a FHIR ValueSet
    response and shape it into a `VSACExpansion`.

    Raises `VSACError` if the response is missing the fields v0
    requires; we'd rather fail loud here than silently emit an
    empty ConceptSet that the matcher would then treat as
    "no codes count" — exactly the wrong default.
    """
    expansion = payload.get("expansion")
    if not isinstance(expansion, dict):
        raise VSACError(
            f"VSAC payload for OID {oid} has no `expansion` object; "
            f"keys present: {sorted(payload.keys())}"
        )
    contains = expansion.get("contains")
    if not isinstance(contains, list) or not contains:
        raise VSACError(
            f"VSAC expansion for OID {oid} contains no concepts "
            f"(empty or malformed `expansion.contains`)."
        )

    systems_seen: set[str] = set()
    codes_by_system: dict[str, set[str]] = {}
    for entry in contains:
        if not isinstance(entry, dict):
            continue
        raw_system = entry.get("system")
        raw_code = entry.get("code")
        if not isinstance(raw_system, str) or not isinstance(raw_code, str):
            continue
        system_uri = _SYSTEM_URI_FROM_URL.get(raw_system, raw_system)
        if system_filter is not None and system_uri != system_filter:
            continue
        systems_seen.add(system_uri)
        codes_by_system.setdefault(system_uri, set()).add(raw_code)

    if not codes_by_system:
        if system_filter is not None:
            raise VSACError(
                f"VSAC expansion for OID {oid} contained no codes from "
                f"system {system_filter!r}; available systems: "
                f"{_available_systems(contains)}."
            )
        raise VSACError(
            f"VSAC expansion for OID {oid} parsed zero usable "
            f"(system, code) rows from {len(contains)} entries."
        )
    if len(systems_seen) > 1:
        # v0 ConceptSet is single-system by construction (D-25). When
        # we hit a multi-system value set without a system_filter we
        # surface it explicitly rather than silently dropping codes
        # from one system. Pass `system_filter=...` to slice cleanly.
        raise VSACError(
            f"VSAC expansion for OID {oid} spans multiple coding "
            f"systems ({sorted(systems_seen)}); v0 ConceptSet is "
            "single-system. Filter at the value-set level or extend "
            "the envelope to per-system breakdowns."
        )

    (system,) = systems_seen
    codes = codes_by_system[system]
    version = expansion.get("identifier") or payload.get("version") or "unknown"
    if not isinstance(version, str):
        version = str(version)

    return VSACExpansion(
        oid=oid,
        version=version,
        concept_set=ConceptSet(
            name=name,
            system=system,
            codes=frozenset(codes),
        ),
    )


def _available_systems(entries: list[Any]) -> list[str]:
    """Return normalized coding systems present in a FHIR contains list."""
    systems: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        raw_system = entry.get("system")
        if isinstance(raw_system, str):
            systems.add(_SYSTEM_URI_FROM_URL.get(raw_system, raw_system))
    return sorted(systems)
