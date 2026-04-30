"""Trial-side bindings: surface form -> VSAC OID or RxNorm name.

Companion to `clinical_demo.matcher.concept_lookup` (the legacy
alias table). Where the alias table maps a surface form *directly*
to a `ConceptSet` of codes, this module maps a surface form to a
*pointer* into a terminology API:

- A `VSACBinding` carries a value-set OID (and optional single-system
  filter) that the resolver expands via `VSACClient.expand`.
- An `RxNormBinding` carries a drug name (and optional TTY filter)
  that the resolver looks up via `RxNormClient.find_drug_concepts`.

The resolver (in `terminology/resolver.py`) consumes these and
returns a `ConceptSet` shaped exactly like the alias table's values
-- so the matcher's downstream `PatientProfile.has_condition` /
`.is_taking_medication` / `.meets_threshold` calls do not care
which bridge produced the codes.

Why a separate registry instead of inlining the OIDs into
`concept_lookup.py`?
-------------------------------------------------------------------
1. Provenance. The alias table is "I (the developer) decided these
   strings count as T2DM." The bindings table is "CMS publishes
   value set 2.16.840.1.113883.3.464.1003.103.12.1001 as Diabetes
   for eCQM 2026 reporting; we trust that authority." Different
   things, different trust models, different review cadences.
2. Cache-key honesty. Bindings are stable identifiers; the
   `TerminologyCache` filename embeds them verbatim. Mixing them
   with surface-form aliases would either widen the cache key
   pointlessly or smear two different concepts together.
3. Migration sequencing. We can grow the bindings table without
   touching the aliases (and vice versa); during the D-69
   migration the matcher consults the bindings first and the
   aliases as fallback (see `Settings.binding_strategy`).

Population discipline
---------------------
v0 deliberately seeds **one** binding (T2DM via the eCQM Diabetes
OID) to prove the wire-up end-to-end and exercise the cache -> API
-> ConceptSet path under tests. Real expansion of the registry
runs as a separate commit so each addition can be validated against
its source authority (VSAC search UI / RxNav probe scripts) without
the slice-4 plumbing diff being noisy.
"""

from __future__ import annotations

from pydantic import BaseModel

# v0 single-binding seed. The eCQM "Diabetes" value set
# (2.16.840.1.113883.3.464.1003.103.12.1001) is the canonical CMS
# diabetes code list for quality reporting -- broad enough to cover
# Synthea's diabetes coding (44054006 type 2, 73211009 unspecified)
# and explicitly authored by NCQA, so the binding is defensible
# against a clinical reviewer ("we used the same code list CMS uses
# for Diabetes Care").
ECQM_DIABETES_OID = "2.16.840.1.113883.3.464.1003.103.12.1001"


class VSACBinding(BaseModel):
    """Pointer to a VSAC value set, resolved via FHIR `$expand`.

    `system_filter` (optional) restricts the expansion to a single
    coding system when the value set is multi-system and the matcher
    only cares about one (rare for eCQM lists in practice; carried
    here so the registry can grow into multi-system value sets
    without re-shaping the type)."""

    oid: str
    system_filter: str | None = None


class RxNormBinding(BaseModel):
    """Pointer to an RxNorm drug name, resolved via `/drugs.json`.

    `name` is the surface form to send to RxNav (e.g. `"metformin"`,
    `"glucophage"`); the API does its own broader matching so
    ingredient and brand names both work.

    `tty_filter` restricts the returned codes to specific RxNorm
    term types (IN, PIN, SCD, SBD, BPCK, GPCK). Stored as a sorted
    tuple so two registries built from set literals in different
    orders compare equal in tests; the `RxNormClient` accepts a
    `frozenset[str] | None` and the resolver converts on the way in.
    """

    name: str
    tty_filter: tuple[str, ...] | None = None


Binding = VSACBinding | RxNormBinding
"""Discriminated by class. The resolver dispatches on `isinstance`."""


def _normalize(s: str) -> str:
    """Mirror `concept_lookup._normalize` exactly so a surface form
    that hits the alias table also hits the bindings table (and
    vice versa). Duplicating the four-line helper rather than
    importing it sideways keeps the matcher's import graph
    one-directional: matcher -> terminology, never the reverse."""
    return " ".join(s.lower().strip(".,;:()[]{}\"'").split())


# ---- conditions ----

CONDITION_BINDINGS: dict[str, Binding] = {
    # T2DM -- the canonical end-to-end test of the slice-4 wire-up.
    # All five surface forms in the alias table point at the same
    # eCQM Diabetes value set, so a `two_pass` lookup that misses
    # the cache will fetch + cache once and then service every
    # downstream call from disk.
    "type 2 diabetes": VSACBinding(oid=ECQM_DIABETES_OID),
    "type 2 diabetes mellitus": VSACBinding(oid=ECQM_DIABETES_OID),
    "t2dm": VSACBinding(oid=ECQM_DIABETES_OID),
    "type ii diabetes": VSACBinding(oid=ECQM_DIABETES_OID),
    "diabetes mellitus type 2": VSACBinding(oid=ECQM_DIABETES_OID),
}


# ---- labs ----
#
# Empty in v0; the next commit populates HbA1c (LOINC value set),
# eGFR, BMI per the D-68 baseline's top-unmapped-labs ranking.
LAB_BINDINGS: dict[str, Binding] = {}


# ---- medications ----
#
# Each entry was validated against live RxNav `/drugs.json`
# (scripts/probe_rxnorm.py) and confirmed to return non-empty
# SCD/SBD code lists -- the exact TTYs Synthea uses for
# `MedicationRequest.medicationCodeableConcept.coding`. A
# representative Synthea medication code (the one a smoke test
# would expect to land in the patient profile) is noted on each
# row as a sanity check against future RxNav data drift.
#
# `tty_filter` is left `None` on every entry: the matcher is
# coding-system-agnostic *within* RxNorm, so unioning SCD + SBD
# gives the broadest hit rate without cross-system noise. If a
# future Synthea update starts emitting IN/PIN codes (currently
# it does not), the right move is to *add* those TTYs to the
# union -- not to drop SCD/SBD.
#
# Class-level coverage ("any GLP-1 agonist", "any SGLT2 inhibitor")
# is intentionally NOT modeled here. RxNav `/drugs.json?name=...`
# is an ingredient/brand lookup, not a class lookup; representing
# a class would mean either querying RxClass (separate API surface)
# or unioning multiple ingredient bindings. Defer until trial
# eligibility text actually demands class-level matching.
MEDICATION_BINDINGS: dict[str, Binding] = {
    # Diabetes first-line. Already has a recorded fixture under
    # tests/fixtures/rxnorm/metformin_drugs.json so the resolver
    # tests exercise the full parser path here.
    "metformin": RxNormBinding(name="metformin"),
    # Diabetes maintenance. RxNav also returns BPCK/GPCK pack
    # codes for insulin, but Synthea encodes only individual
    # SCD products ("insulin glargine 100 UNT/ML Injectable"),
    # so unioning SCD/SBD is sufficient and tty_filter stays None.
    "insulin": RxNormBinding(name="insulin"),
    # Statins: Synthea cohort includes both atorvastatin
    # (RxCUI 259255 in the curated bundle sample) and simvastatin
    # (RxCUI 312961). Add both ingredient names so trial-side
    # surface forms hit either Synthea drug.
    "atorvastatin": RxNormBinding(name="atorvastatin"),
    "simvastatin": RxNormBinding(name="simvastatin"),
    # GLP-1 representative. Surface forms in trial eligibility
    # text often say "GLP-1 agonist" (a class); we cover the
    # canonical ingredient and let the alias-class gap surface
    # in slice-5 eval as a known follow-up rather than papering
    # over it with class hardcoding.
    "semaglutide": RxNormBinding(name="semaglutide"),
    # SGLT2 representative. Same class-vs-ingredient note.
    "dapagliflozin": RxNormBinding(name="dapagliflozin"),
}


def lookup_condition_binding(surface: str) -> Binding | None:
    """Return the registered binding for a condition surface form,
    or None if no binding is registered. None means "fall through
    to the alias table" -- not the same as "unmapped concept",
    which is the matcher's verdict if both bridges miss."""
    return CONDITION_BINDINGS.get(_normalize(surface))


def lookup_lab_binding(surface: str) -> Binding | None:
    return LAB_BINDINGS.get(_normalize(surface))


def lookup_medication_binding(surface: str) -> Binding | None:
    return MEDICATION_BINDINGS.get(_normalize(surface))


__all__ = [
    "CONDITION_BINDINGS",
    "ECQM_DIABETES_OID",
    "LAB_BINDINGS",
    "MEDICATION_BINDINGS",
    "Binding",
    "RxNormBinding",
    "VSACBinding",
    "lookup_condition_binding",
    "lookup_lab_binding",
    "lookup_medication_binding",
]
