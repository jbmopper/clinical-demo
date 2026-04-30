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

# Canonical CMS / NCQA eCQM value-set OIDs used by the v0 registry.
# Each was validated against live VSAC `$expand` (scripts/probe_vsac.py)
# and shipped with a recorded fixture under tests/fixtures/vsac/ so
# the resolver tests stay offline-deterministic.
#
# Naming: `ECQM_<topic>_OID` for the OID, `<TOPIC>_VSAC_BINDING` for
# the canonical binding object. We keep the OIDs as module-level
# constants (not just inlined in the registry) so a regression test
# can pin each one byte-for-byte and so probe scripts / docs can
# reference them by name without re-typing the dotted string.

ECQM_DIABETES_OID = "2.16.840.1.113883.3.464.1003.103.12.1001"
"""eCQM 'Diabetes' value set. Used by CMS122 (HbA1c Poor Control),
CMS123 (Foot Exam), CMS131 (Eye Exam), etc. Authored by NCQA.
Multi-system in principle but the VSAC expansion currently returns
SNOMED only for our purposes (matched by the recorded fixture).
Covers Synthea's 44054006 (T2DM) and 73211009 (DM unspecified)."""

ECQM_HYPERTENSION_OID = "2.16.840.1.113883.3.464.1003.104.12.1011"
"""eCQM 'Essential Hypertension' value set. Used by CMS165
(Controlling High Blood Pressure). 14 SNOMED codes incl. 59621000
(Essential hypertension). Multi-system; the binding pins a SNOMED
filter so the matcher's PatientProfile (single-system per query)
gets a clean code list."""

ECQM_HBA1C_LAB_OID = "2.16.840.1.113883.3.464.1003.198.12.1013"
"""eCQM 'HbA1c Laboratory Test' value set. Used by CMS122. 5 LOINC
codes (4548-4 standard HbA1c %, 4549-2, 17855-8, 17856-6, 96595-4).
Excludes IFCC/JDS-protocol HbA1c codes by design -- aligned with
how Synthea's lab observations are coded. Pinned to LOINC via the
binding's system_filter."""


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


# Canonical FHIR coding-system URIs for use in `system_filter` on
# multi-system value sets. Repeated rather than imported from the
# matcher to keep the import graph one-directional.
SNOMED_SYSTEM = "http://snomed.info/sct"
LOINC_SYSTEM = "http://loinc.org"


# ---- conditions ----
#
# Each binding's surface-form list mirrors the alias table in
# `matcher.concept_lookup` so a `two_pass` lookup hits the registry
# under exactly the same surface forms the legacy alias path
# recognized. Hyperlipidemia and CKD are intentionally NOT in v0:
# the canonical eCQM OIDs for those concepts surfaced under
# different authorities (HL7 Patient Care WG vs. CMS) without a
# clean single-source pin during research, so they're left to a
# follow-on commit that can validate each candidate carefully
# rather than guessing here.

CONDITION_BINDINGS: dict[str, Binding] = {
    # T2DM -- canonical end-to-end test of the slice-4 wire-up.
    # All five surface forms point at the same eCQM Diabetes
    # value set, so a single cache row services every downstream
    # call. The fixture (tests/fixtures/vsac/diabetes_expansion.json)
    # ships in the repo for offline tests.
    "type 2 diabetes": VSACBinding(oid=ECQM_DIABETES_OID),
    "type 2 diabetes mellitus": VSACBinding(oid=ECQM_DIABETES_OID),
    "t2dm": VSACBinding(oid=ECQM_DIABETES_OID),
    "type ii diabetes": VSACBinding(oid=ECQM_DIABETES_OID),
    "diabetes mellitus type 2": VSACBinding(oid=ECQM_DIABETES_OID),
    # Hypertension. CMS165's Essential Hypertension value set is
    # multi-system; binding a SNOMED filter at registry time
    # avoids the matcher having to choose at lookup time.
    # Fixture: tests/fixtures/vsac/hypertension_expansion.json.
    "hypertension": VSACBinding(oid=ECQM_HYPERTENSION_OID, system_filter=SNOMED_SYSTEM),
    "essential hypertension": VSACBinding(oid=ECQM_HYPERTENSION_OID, system_filter=SNOMED_SYSTEM),
    "high blood pressure": VSACBinding(oid=ECQM_HYPERTENSION_OID, system_filter=SNOMED_SYSTEM),
    "htn": VSACBinding(oid=ECQM_HYPERTENSION_OID, system_filter=SNOMED_SYSTEM),
}


# ---- labs ----
#
# HbA1c is the only lab in v0 because it's the only one with a
# clean canonical eCQM OID we found in research and could probe
# against live VSAC successfully. eGFR, BMI, hemoglobin, platelets
# all remain on the follow-on list -- shape is one-line additions
# once each OID is validated, the slice-4 plumbing already supports
# them.

LAB_BINDINGS: dict[str, Binding] = {
    # CMS122's HbA1c Laboratory Test value set: 5 LOINC codes.
    # All surface forms in the alias table point at the same OID
    # so one cache row covers them all.
    # Fixture: tests/fixtures/vsac/hba1c_lab_expansion.json.
    "hba1c": VSACBinding(oid=ECQM_HBA1C_LAB_OID, system_filter=LOINC_SYSTEM),
    "hemoglobin a1c": VSACBinding(oid=ECQM_HBA1C_LAB_OID, system_filter=LOINC_SYSTEM),
    "haemoglobin a1c": VSACBinding(oid=ECQM_HBA1C_LAB_OID, system_filter=LOINC_SYSTEM),
    "a1c": VSACBinding(oid=ECQM_HBA1C_LAB_OID, system_filter=LOINC_SYSTEM),
    "glycated hemoglobin": VSACBinding(oid=ECQM_HBA1C_LAB_OID, system_filter=LOINC_SYSTEM),
    "glycosylated hemoglobin": VSACBinding(oid=ECQM_HBA1C_LAB_OID, system_filter=LOINC_SYSTEM),
}


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
    "ECQM_HBA1C_LAB_OID",
    "ECQM_HYPERTENSION_OID",
    "LAB_BINDINGS",
    "LOINC_SYSTEM",
    "MEDICATION_BINDINGS",
    "SNOMED_SYSTEM",
    "Binding",
    "RxNormBinding",
    "VSACBinding",
    "lookup_condition_binding",
    "lookup_lab_binding",
    "lookup_medication_binding",
]
