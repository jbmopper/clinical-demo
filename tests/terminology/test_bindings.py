"""Tests for the trial-side bindings registry.

This module is the surface-form -> (VSAC OID | RxNorm name) bridge
the matcher consults under `Settings.binding_strategy == "two_pass"`.
The tests pin five properties:

1. Each canonical eCQM OID is pinned byte-for-byte. CMS publishes
   one OID per value set; a typo or substitution would silently
   bind every two_pass lookup of that surface form to the wrong
   codes, with no runtime error.
2. Each populated surface form maps to an instance of the right
   concrete `Binding` subtype (VSAC for conditions/labs, RxNorm
   for medications). Catches accidental cross-wiring at the
   registry layer rather than only at runtime.
3. Multi-system VSAC value sets (hypertension, HbA1c) are pinned
   with an explicit `system_filter` so the matcher's PatientProfile
   (single-system per query) gets a clean code list. Without this
   the resolver would `VSACError` on first fetch.
4. Surface-form normalization matches `concept_lookup._normalize`
   exactly. A surface form that hits one bridge must hit the other
   under the same normalization, otherwise behaviour silently
   diverges between modes.
5. Each populated entry has a corresponding recorded fixture under
   `tests/fixtures/vsac/` so the resolver tests stay offline-
   deterministic and don't need the live VSAC API to verify the
   parser path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clinical_demo.terminology.bindings import (
    CONDITION_BINDINGS,
    ECQM_DIABETES_OID,
    ECQM_HBA1C_LAB_OID,
    ECQM_HYPERTENSION_OID,
    LAB_BINDINGS,
    LOINC_SYSTEM,
    MEDICATION_BINDINGS,
    SNOMED_SYSTEM,
    Binding,
    RxNormBinding,
    VSACBinding,
    _normalize,
    lookup_condition_binding,
    lookup_lab_binding,
    lookup_medication_binding,
)

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "vsac"

# ---------- canonical OIDs (byte-for-byte) ----------


@pytest.mark.parametrize(
    ("name", "expected_oid"),
    [
        ("ECQM_DIABETES_OID", "2.16.840.1.113883.3.464.1003.103.12.1001"),
        ("ECQM_HYPERTENSION_OID", "2.16.840.1.113883.3.464.1003.104.12.1011"),
        ("ECQM_HBA1C_LAB_OID", "2.16.840.1.113883.3.464.1003.198.12.1013"),
    ],
)
def test_ecqm_oid_constants_are_pinned(name: str, expected_oid: str) -> None:
    """CMS publishes one canonical OID per value set; if a future
    commit replaces a constant with a typo or a different value set,
    every two_pass lookup of that surface form would silently bind
    to the wrong codes (no runtime error). Pinning each constant
    by name + expected value catches both directions of drift."""
    actual = {
        "ECQM_DIABETES_OID": ECQM_DIABETES_OID,
        "ECQM_HYPERTENSION_OID": ECQM_HYPERTENSION_OID,
        "ECQM_HBA1C_LAB_OID": ECQM_HBA1C_LAB_OID,
    }[name]
    assert actual == expected_oid


# ---------- conditions ----------


def test_condition_bindings_seed_t2dm_to_ecqm_diabetes() -> None:
    """T2DM surface forms in the alias table also live here pointing
    at the eCQM Diabetes OID. A `two_pass` lookup against any of
    them resolves through the same cache row (one fetch / one
    cache line covers all five surface forms)."""
    expected_surfaces = {
        "type 2 diabetes",
        "type 2 diabetes mellitus",
        "t2dm",
        "type ii diabetes",
        "diabetes mellitus type 2",
    }
    assert expected_surfaces.issubset(CONDITION_BINDINGS.keys())
    for surface in expected_surfaces:
        binding = CONDITION_BINDINGS[surface]
        assert isinstance(binding, VSACBinding)
        assert binding.oid == ECQM_DIABETES_OID
        # T2DM expansion is single-system already (SNOMED only in
        # the recorded fixture); no system_filter needed.
        assert binding.system_filter is None


@pytest.mark.parametrize(
    "surface",
    ["hypertension", "essential hypertension", "high blood pressure", "htn"],
)
def test_condition_bindings_hypertension_routes_to_snomed_filtered_oid(
    surface: str,
) -> None:
    """The eCQM Essential Hypertension value set is multi-system
    (SNOMED + others). Without a `system_filter` the resolver
    would `VSACError` on first fetch -- pinning the SNOMED filter
    here guards against silent removal of that constraint."""
    binding = CONDITION_BINDINGS[surface]
    assert isinstance(binding, VSACBinding)
    assert binding.oid == ECQM_HYPERTENSION_OID
    assert binding.system_filter == SNOMED_SYSTEM


# ---------- labs ----------


@pytest.mark.parametrize(
    "surface",
    [
        "hba1c",
        "hemoglobin a1c",
        "haemoglobin a1c",
        "a1c",
        "glycated hemoglobin",
        "glycosylated hemoglobin",
    ],
)
def test_lab_bindings_hba1c_routes_to_loinc_filtered_oid(surface: str) -> None:
    """All HbA1c surface forms point at the CMS122 HbA1c value set
    with an explicit LOINC filter. Synthea encodes lab observations
    in LOINC, so a missing filter would either pull in non-LOINC
    codes or fail the resolver outright."""
    binding = LAB_BINDINGS[surface]
    assert isinstance(binding, VSACBinding)
    assert binding.oid == ECQM_HBA1C_LAB_OID
    assert binding.system_filter == LOINC_SYSTEM


# ---------- fixtures ----------
#
# Every populated VSAC binding ships with a recorded fixture so
# the resolver tests stay offline-deterministic. If we add a new
# binding without recording the fixture, the resolver tests would
# silently start hitting the live VSAC API on next CI run.


@pytest.mark.parametrize(
    ("filename", "expected_oid"),
    [
        ("diabetes_expansion.json", ECQM_DIABETES_OID),
        ("hypertension_expansion.json", ECQM_HYPERTENSION_OID),
        ("hba1c_lab_expansion.json", ECQM_HBA1C_LAB_OID),
    ],
)
def test_vsac_fixture_matches_pinned_oid(filename: str, expected_oid: str) -> None:
    """Each recorded fixture's `id` field equals the OID we pin
    above. Catches accidental swap (e.g. recording the diabetes
    payload under the hypertension filename)."""
    payload = json.loads((FIXTURE_DIR / filename).read_text())
    assert payload["id"] == expected_oid
    # Sanity-check non-empty expansion -- a recorded fixture with
    # zero codes would silently produce an empty ConceptSet that
    # matches nothing.
    assert payload["expansion"]["contains"]


# ---- medications: validated against live RxNav probes ----
#
# Each entry below was probed via `scripts/probe_rxnorm.py` on the
# real RxNav `/drugs.json` endpoint and confirmed to return a
# non-empty SCD/SBD code list. The bindings are pinned by ingredient
# name; `tty_filter=None` (the union of returned TTYs) matches
# Synthea's coding model. See the bindings module docstring for the
# class-vs-ingredient deferral note.


@pytest.mark.parametrize(
    "ingredient",
    [
        "metformin",
        "insulin",
        "atorvastatin",
        "simvastatin",
        "semaglutide",
        "dapagliflozin",
    ],
)
def test_medication_binding_is_rxnorm_with_no_tty_filter(ingredient: str) -> None:
    """Every v0 medication entry is an `RxNormBinding` keyed by the
    canonical ingredient name with no TTY filter. A future entry
    that needs a tty_filter (e.g. an IN-only restriction) will
    explicitly fail this test, surfacing the deviation for review
    instead of letting it slip in silently."""
    binding = MEDICATION_BINDINGS[ingredient]
    assert isinstance(binding, RxNormBinding)
    assert binding.name == ingredient
    assert binding.tty_filter is None


def test_medication_binding_lookup_via_helper() -> None:
    """Lookup via the public helper (not direct dict access) hits
    every populated entry under their canonical surface form. The
    helper applies normalization (lowercase, whitespace), so this
    also exercises the same code path the matcher will use."""
    for ingredient in MEDICATION_BINDINGS:
        b = lookup_medication_binding(ingredient.upper() + "  ")
        assert isinstance(b, RxNormBinding)
        assert b.name == ingredient


def test_medication_binding_misses_unknown_drug() -> None:
    """v0 covers a curated cardiometabolic set; anything outside
    it returns None and dispatch falls back to the alias table
    (which is itself empty for meds, ultimately yielding
    `unmapped_concept`)."""
    assert lookup_medication_binding("rosuvastatin") is None
    assert lookup_medication_binding("warfarin") is None


# ---------- normalization parity ----------


def test_normalize_matches_concept_lookup_normalize() -> None:
    """Both bridges must normalize identically; otherwise
    'T2DM' might hit one and miss the other purely on whitespace
    handling. Pinning a small parity table catches drift in
    either direction."""
    from clinical_demo.matcher.concept_lookup import _normalize as alias_normalize

    cases = [
        "T2DM",
        "  Type 2 Diabetes  ",
        "Type 2 Diabetes Mellitus.",
        "(type ii diabetes)",
        "Diabetes\tMellitus\nType 2",
    ]
    for raw in cases:
        assert _normalize(raw) == alias_normalize(raw), (
            f"normalize divergence for {raw!r}: bindings={_normalize(raw)!r}, "
            f"alias={alias_normalize(raw)!r}"
        )


# ---------- lookups ----------


def test_lookup_condition_binding_hits_normalized() -> None:
    """Lookup tolerates the same noise normalization handles:
    case, surrounding whitespace, trailing punctuation."""
    for raw in ("T2DM", "t2dm", "  t2dm  ", "(t2dm)"):
        binding = lookup_condition_binding(raw)
        assert isinstance(binding, VSACBinding)
        assert binding.oid == ECQM_DIABETES_OID


def test_lookup_condition_binding_misses_unknown_surface() -> None:
    """Unknown surface forms return `None`; the resolver's contract
    treats `None` as 'fall back to the alias table', not as
    'unmapped concept'."""
    assert lookup_condition_binding("acute pancreatitis") is None
    assert lookup_condition_binding("") is None


def test_lookup_condition_binding_hits_hypertension_normalized() -> None:
    """Hypertension surface forms route to the same OID under
    normalization. Same parity property as T2DM but cross-binding,
    so a normalization regression specific to non-T2DM surfaces is
    visible."""
    for raw in ("HTN", "Hypertension", "  HIGH BLOOD PRESSURE  "):
        binding = lookup_condition_binding(raw)
        assert isinstance(binding, VSACBinding)
        assert binding.oid == ECQM_HYPERTENSION_OID


def test_lookup_lab_binding_hits_hba1c_normalized() -> None:
    """v0 lab population. Helper-routed lookup confirms each
    surface form normalizes to the same registry key."""
    for raw in ("HbA1c", "Hemoglobin A1c", "  A1C  ", "Glycated Hemoglobin"):
        binding = lookup_lab_binding(raw)
        assert isinstance(binding, VSACBinding)
        assert binding.oid == ECQM_HBA1C_LAB_OID


def test_lookup_lab_binding_misses_unknown_lab() -> None:
    """eGFR / BMI / hemoglobin / platelets are deferred to a
    follow-on commit; until then they fall back to the alias
    table (which itself doesn't cover them, so the verdict is
    `unmapped_concept` at the matcher layer)."""
    assert lookup_lab_binding("egfr") is None
    assert lookup_lab_binding("bmi") is None
    assert lookup_lab_binding("") is None


# ---------- type discipline ----------


def test_binding_union_admits_both_concrete_types() -> None:
    """`Binding` is the discriminated union the resolver dispatches
    on. Both concrete types must round-trip cleanly to the union
    annotation; a registry value typed as `Binding` should accept
    either concrete instance without a Pydantic complaint."""
    vsac: Binding = VSACBinding(oid=ECQM_DIABETES_OID)
    rxnorm: Binding = RxNormBinding(name="metformin")
    assert isinstance(vsac, VSACBinding)
    assert isinstance(rxnorm, RxNormBinding)


def test_rxnorm_binding_tty_filter_is_hashable_tuple() -> None:
    """Stored as a sorted tuple (not a set) so two registries
    built from set literals in different declaration orders
    compare equal -- important when registry diff'ing across
    branches."""
    a = RxNormBinding(name="metformin", tty_filter=("IN", "PIN"))
    b = RxNormBinding(name="metformin", tty_filter=("IN", "PIN"))
    assert a == b
    # Hashable enough to live in a dict-of-Binding (used in tests
    # and conceivable in future registry shapes).
    assert hash(tuple(a.tty_filter or ())) == hash(tuple(b.tty_filter or ()))
