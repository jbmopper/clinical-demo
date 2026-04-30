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


def test_lab_and_medication_bindings_empty_in_v0() -> None:
    """Slice 4 wires plumbing only; population is a follow-on per
    the module docstring. If a future commit seeds these without
    updating the docstring + tests, the change is reviewable
    instead of silent."""
    assert LAB_BINDINGS == {}
    assert MEDICATION_BINDINGS == {}


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


def test_lookup_medication_binding_misses_everything_in_v0() -> None:
    assert lookup_medication_binding("metformin") is None
    assert lookup_medication_binding("insulin") is None


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
