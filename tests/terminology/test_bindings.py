"""Tests for the trial-side bindings registry.

This module is the surface-form -> (VSAC OID | RxNorm name) bridge
the matcher consults under `Settings.binding_strategy == "two_pass"`.
The tests pin three properties:

1. The seed binding (T2DM) is wired to the eCQM Diabetes OID we
   already have a test fixture for, so the resolver tests in
   `test_resolver.py` can pre-warm the cache from that fixture and
   exercise the full surface-form -> ConceptSet path.
2. Surface-form normalization matches `concept_lookup._normalize`
   exactly. A surface form that hits one bridge must hit the other
   under the same normalization, otherwise behaviour silently
   diverges between modes.
3. Lab and medication registries are intentionally empty in v0
   (slice 4 wires the plumbing; population is a separate commit
   per the bindings module docstring) -- pinned here so an
   accidental rewrite of the seed table is caught.
"""

from __future__ import annotations

import pytest

from clinical_demo.terminology.bindings import (
    CONDITION_BINDINGS,
    ECQM_DIABETES_OID,
    LAB_BINDINGS,
    MEDICATION_BINDINGS,
    Binding,
    RxNormBinding,
    VSACBinding,
    _normalize,
    lookup_condition_binding,
    lookup_lab_binding,
    lookup_medication_binding,
)

# ---------- shape ----------


def test_ecqm_diabetes_oid_is_the_canonical_cms_value_set() -> None:
    """Pin the OID byte-for-byte. CMS publishes one canonical OID
    for the eCQM Diabetes value set; if a future commit replaces it
    with a typo or a different value set, every two_pass T2DM
    lookup would silently bind to the wrong codes."""
    assert ECQM_DIABETES_OID == "2.16.840.1.113883.3.464.1003.103.12.1001"


def test_condition_bindings_seed_t2dm_to_ecqm_diabetes() -> None:
    """The slice-4 seed binding. Every T2DM surface form in the
    alias table also lives in the bindings registry pointing at
    the same OID, so a `two_pass` lookup against any of them
    resolves through the same cache row (one fetch / one cache
    line covers all five surface forms)."""
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


def test_lab_bindings_empty_pending_vsac_population() -> None:
    """Lab population lands in a separate commit (the VSAC half of
    the registry expansion), so v0 of the medication-only commit
    leaves this empty. Pin it so the next commit's diff is
    reviewable rather than silent."""
    assert LAB_BINDINGS == {}


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


def test_lookup_lab_binding_misses_everything_in_v0() -> None:
    """Empty registry -> all calls return None and dispatch falls
    through to the alias table."""
    assert lookup_lab_binding("hba1c") is None
    assert lookup_lab_binding("egfr") is None


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
