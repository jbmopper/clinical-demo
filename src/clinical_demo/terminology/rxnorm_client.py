"""RxNorm REST client.

RxNorm (https://www.nlm.nih.gov/research/umls/rxnorm/) is the NLM's
normalized naming system for clinical drugs; every drug concept --
ingredient, brand, clinical dose form, packaged product -- gets an
RxCUI. Synthea patient bundles already code medications in RxNorm,
so resolving a trial-side surface form ("metformin", "Glucophage")
into the matcher-shaped `ConceptSet` of RxCUIs lets the existing
`PatientProfile.medications` path work without per-criterion
hand-curated aliases.

Auth model is different from VSAC. The RxNav REST surface
(https://rxnav.nlm.nih.gov/REST/) is **public, no API key**;
authentication is gated only on the rate limit (~20 rps per IP).
That removes the auth/secrets ceremony VSAC needs (D-31 / D-69)
and means a fresh checkout can call this client without any NLM
account at all -- the hand-curated alias path remains the runtime
default until the resolver is wired in slice 4, but live probing
and offline fixture refreshes work out of the box.

This v0 client returns a `RxNormConcepts` envelope that pairs the
matcher-shaped `ConceptSet` with the original query string and the
set of RxNorm term types contributing codes. The set of TTYs is
recorded so eval rollups can split "ingredient-only matches" from
"branded-product matches" without re-querying RxNorm. Versioning
(RxNorm publishes a monthly data set) is intentionally **not**
recorded on the envelope here -- the per-call response does not
carry one, and we'd need a separate `/version.json` fetch at
construction time to pin it. When that becomes the bottleneck
(e.g. an eval re-run produces a different result and we want to
explain the drift), the right move is to add a `data_version`
field to the envelope and let `vsac_envelope_fingerprint`'s
sibling `rxnorm_envelope_fingerprint` auto-orphan the existing
cache. Cheap to add later; ceremony to require now.
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel

from clinical_demo.profile import ConceptSet

DEFAULT_BASE_URL = "https://rxnav.nlm.nih.gov/REST"

# Canonical RxNorm coding-system URI. Patient FHIR data from Synthea
# uses this exact URI on Medication resources, so the matcher's
# coded-fact comparison works without further normalization.
RXNORM_SYSTEM_URI = "http://www.nlm.nih.gov/research/umls/rxnorm"

# RxNav `/drugs.json?name=...` accepts no `search` parameter; it
# applies its own broader matching internally. Brand names and
# ingredient names both work.
_DRUGS_PATH = "/drugs.json"


class RxNormError(RuntimeError):
    """Anything RxNorm-side that prevented a usable concept set.

    Caught by callers (and eventually by the matcher's binding-
    dispatch path) so a transient terminology-server failure
    degrades to `indeterminate(unmapped_concept)` rather than
    crashing a scoring run -- same shape as D-65/D-66's soft-fail
    discipline, applied at the binding layer (mirrors VSACError)."""


class RxNormConcepts(BaseModel):
    """One RxNorm drug-name lookup as the matcher will consume it.

    `query` is the exact surface form the caller asked about,
    preserved for traceability when the same RxCUI shows up in
    several `RxNormConcepts` objects under different surface
    aliases. `term_types` records which RxNorm TTYs (IN, PIN, SCD,
    SBD, ...) contributed at least one code so eval rollups can
    distinguish "ingredient-only resolution" from "matched all the
    way down to specific branded products."
    """

    query: str
    concept_set: ConceptSet
    term_types: frozenset[str]


class RxNormClient:
    """Thin sync wrapper over the RxNav `/drugs.json` endpoint.

    Sync (not async) for the same reason VSACClient is sync: v0
    callers are CLI scripts and a single-process eval runner; the
    surface area an async client would unlock is the same surface
    area `httpx.AsyncClient` would have given us.

    No constructor-time API key. Tests inject a `transport=` to
    return canned responses without hitting the network.
    """

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._transport = transport

    def find_drug_concepts(
        self,
        name: str,
        *,
        tty_filter: frozenset[str] | None = None,
    ) -> RxNormConcepts:
        """Resolve an RxNorm drug surface form into a ConceptSet of RxCUIs.

        Calls `GET /drugs.json?name=<name>` and unions the `rxcui`
        from every populated `conceptGroup` into a single
        `ConceptSet`. The default behavior unions across *all*
        returned term types (IN, PIN, SCD, SCDC, SBD, ...) because
        Synthea patient bundles can be coded at any TTY level
        (typically SCD); a narrower binding would silently drop
        valid patient evidence.

        `tty_filter` restricts the result to a chosen set of term
        types (e.g. `frozenset({"IN", "PIN"})` to bind only to the
        ingredient level). Pass it when the caller has explicit
        evidence that the patient data is coded at that level --
        rarely needed in v0, but cheap to support and useful for
        slice-4 ablations.

        `name` is sent verbatim; RxNav's `/drugs` endpoint applies
        its own normalized matching internally (handles
        capitalization, salt forms, and common abbreviations like
        "hctz" -> "hydrochlorothiazide").

        Raises `RxNormError` when the response is unusable
        (network/HTTP error, non-JSON body, missing `drugGroup`,
        zero codes after parsing/filtering). Failing loud here is
        the same default as VSAC: an empty `ConceptSet` would tell
        the matcher "no codes count" -- exactly the wrong default.
        """
        try:
            with httpx.Client(
                timeout=self._timeout,
                transport=self._transport,
            ) as client:
                response = client.get(
                    f"{self._base_url}{_DRUGS_PATH}",
                    params={"name": name},
                )
        except httpx.HTTPError as exc:
            raise RxNormError(f"RxNorm request failed for {name!r}: {exc}") from exc

        if response.status_code != 200:
            raise RxNormError(
                f"RxNorm returned {response.status_code} for {name!r}: {response.text[:200]}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise RxNormError(f"RxNorm response for {name!r} was not JSON: {exc}") from exc

        return _parse_drug_group(payload, query=name, tty_filter=tty_filter)


def _parse_drug_group(
    payload: dict[str, Any],
    *,
    query: str,
    tty_filter: frozenset[str] | None,
) -> RxNormConcepts:
    """Pull the `drugGroup.conceptGroup` rows out of a RxNav response
    and shape them into a `RxNormConcepts`.

    Empty conceptGroup buckets (e.g. `{"tty": "BPCK"}` with no
    `conceptProperties`) are normal in RxNav responses and are
    skipped silently. Parse errors are loud: a malformed payload
    means the API contract changed, not "no codes for this drug."
    """
    drug_group = payload.get("drugGroup")
    if not isinstance(drug_group, dict):
        raise RxNormError(
            f"RxNorm payload for {query!r} has no `drugGroup` object; "
            f"keys present: {sorted(payload.keys())}"
        )

    concept_groups = drug_group.get("conceptGroup")
    if not isinstance(concept_groups, list) or not concept_groups:
        # No conceptGroup at all (vs. populated-but-empty groups) is
        # the API's "no match" response and signals an unmapped
        # surface form. The matcher's caller wants this loud so it
        # can route to `unmapped_concept` rather than treat the
        # silent empty result as "no codes count" (the wrong
        # default; see find_drug_concepts).
        raise RxNormError(
            f"RxNorm response for {query!r} contains no `conceptGroup` "
            "rows (no drug matched this surface form)."
        )

    codes: set[str] = set()
    term_types: set[str] = set()
    for group in concept_groups:
        if not isinstance(group, dict):
            continue
        properties = group.get("conceptProperties")
        if not isinstance(properties, list):
            continue
        for prop in properties:
            if not isinstance(prop, dict):
                continue
            tty = prop.get("tty")
            rxcui = prop.get("rxcui")
            if not isinstance(tty, str) or not isinstance(rxcui, str):
                continue
            if tty_filter is not None and tty not in tty_filter:
                continue
            codes.add(rxcui)
            term_types.add(tty)

    if not codes:
        if tty_filter is not None:
            raise RxNormError(
                f"RxNorm response for {query!r} contained no codes "
                f"under TTYs {sorted(tty_filter)}; available TTYs: "
                f"{_available_ttys(concept_groups)}."
            )
        raise RxNormError(
            f"RxNorm response for {query!r} parsed zero usable "
            f"(tty, rxcui) rows from {len(concept_groups)} groups."
        )

    return RxNormConcepts(
        query=query,
        concept_set=ConceptSet(
            name=query,
            system=RXNORM_SYSTEM_URI,
            codes=frozenset(codes),
        ),
        term_types=frozenset(term_types),
    )


def _available_ttys(concept_groups: list[Any]) -> list[str]:
    """Return the term types present in a RxNav conceptGroup list.

    Used in the no-match-after-filter error message so the
    operator sees which TTYs were actually returned."""
    ttys: set[str] = set()
    for group in concept_groups:
        if not isinstance(group, dict):
            continue
        properties = group.get("conceptProperties")
        if not isinstance(properties, list) or not properties:
            continue
        tty = group.get("tty")
        if isinstance(tty, str):
            ttys.add(tty)
    return sorted(ttys)
