"""Concept-lookup tests.

The lookup tables drive the matcher's recall: a missed alias means a
verdict drops to `indeterminate (unmapped_concept)` even when the
patient data would otherwise resolve it. These tests pin the major
condition / lab aliases we rely on; the medication table is
deliberately empty in v0 (see concept_lookup.py docstring)."""

from __future__ import annotations

import pytest

from clinical_demo.matcher.concept_lookup import (
    lookup_condition,
    lookup_lab,
    lookup_medication,
)
from clinical_demo.profile.concept_sets import (
    CHRONIC_KIDNEY_DISEASE,
    EGFR,
    HBA1C,
    HYPERLIPIDEMIA,
    HYPERTENSION,
    LDL_CHOLESTEROL,
    PREDIABETES,
    SYSTOLIC_BP,
    T2DM,
)


@pytest.mark.parametrize(
    "surface,expected",
    [
        ("type 2 diabetes", T2DM),
        ("Type 2 Diabetes", T2DM),
        ("  T2DM  ", T2DM),
        ("type ii diabetes", T2DM),
        ("prediabetes", PREDIABETES),
        ("pre-diabetes", PREDIABETES),
        ("hypertension", HYPERTENSION),
        ("HTN", HYPERTENSION),
        ("hyperlipidemia", HYPERLIPIDEMIA),
        ("dyslipidemia", HYPERLIPIDEMIA),
        ("chronic kidney disease", CHRONIC_KIDNEY_DISEASE),
        ("CKD", CHRONIC_KIDNEY_DISEASE),
    ],
)
def test_lookup_condition_known_aliases(surface: str, expected: object) -> None:
    """Common cardiometabolic aliases must hit; case + whitespace
    are normalized so the LLM's surface form flows through."""
    assert lookup_condition(surface) is expected


def test_lookup_condition_unknown_returns_none() -> None:
    """Anything not in the table must return None — that's how the
    matcher distinguishes 'no evidence' from 'concept not recognized'."""
    assert lookup_condition("Sjogren's syndrome") is None
    assert lookup_condition("") is None


@pytest.mark.parametrize(
    "surface,expected",
    [
        ("hba1c", HBA1C),
        ("HbA1c", HBA1C),
        ("a1c", HBA1C),
        ("glycated hemoglobin", HBA1C),
        ("LDL", LDL_CHOLESTEROL),
        ("ldl-c", LDL_CHOLESTEROL),
        ("low-density lipoprotein cholesterol", LDL_CHOLESTEROL),
        ("eGFR", EGFR),
        ("estimated glomerular filtration rate", EGFR),
        ("systolic blood pressure", SYSTOLIC_BP),
        ("SBP", SYSTOLIC_BP),
    ],
)
def test_lookup_lab_known_aliases(surface: str, expected: object) -> None:
    assert lookup_lab(surface) is expected


def test_lookup_lab_unknown_returns_none() -> None:
    assert lookup_lab("BNP") is None


def test_lookup_medication_v0_returns_none_for_everything() -> None:
    """v0's medication table is intentionally empty; pin that
    behaviour so we notice when it changes."""
    for s in ("metformin", "insulin", "statins", "aspirin"):
        assert lookup_medication(s) is None


# ---------- D-69 slice 4: terminology dispatch ----------
#
# These tests cover the strategy switch on `Settings.binding_strategy`.
# The terminology resolver itself is tested in
# `tests/terminology/test_resolver.py`; here we only pin the
# *dispatch* -- alias mode never touches the resolver, two_pass
# mode tries it first and falls back to alias on miss.


@pytest.fixture
def two_pass_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force `binding_strategy="two_pass"` for the duration of one
    test without writing to the global env or .env file."""
    from clinical_demo.settings import Settings, get_settings

    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.setattr(
        "clinical_demo.matcher.concept_lookup.get_settings",
        lambda: Settings(binding_strategy="two_pass"),
    )
    # Make sure the singleton resolver isn't carried over from an
    # earlier test that used real settings.
    get_settings.cache_clear()


def _resolver_returning(value: object) -> object:
    """Build a stand-in resolver whose three resolve_* methods all
    return `value`. Lets us inject 'always-hit' or 'always-miss'
    behaviour without standing up a TerminologyCache."""

    class StubResolver:
        def resolve_condition(self, surface: str) -> object:
            return value

        def resolve_lab(self, surface: str) -> object:
            return value

        def resolve_medication(self, surface: str) -> object:
            return value

    return StubResolver()


def test_alias_mode_does_not_consult_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default mode -- the resolver must not be reached. We trip-wire
    the resolver factory with an exception so any accidental call
    fails the test loudly."""
    from clinical_demo.settings import Settings

    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.setattr(
        "clinical_demo.matcher.concept_lookup.get_settings",
        lambda: Settings(binding_strategy="alias"),
    )

    def explode() -> object:
        raise AssertionError("get_resolver() called under alias mode")

    monkeypatch.setattr(
        "clinical_demo.matcher.concept_lookup.get_resolver",
        explode,
    )

    # Alias hit + alias miss both fine -- neither path consults the
    # resolver under `alias` mode.
    assert lookup_condition("type 2 diabetes") is T2DM
    assert lookup_condition("acute pancreatitis") is None


def test_two_pass_mode_uses_resolver_first(two_pass_settings: None) -> None:
    """When the resolver returns a ConceptSet, the matcher takes
    it -- alias table is not consulted for that surface form."""
    from clinical_demo.profile import ConceptSet

    sentinel = ConceptSet(
        name="from-resolver",
        system="http://snomed.info/sct",
        codes=frozenset({"99999999"}),
    )

    # Even an alias-hit surface form should yield the resolver's
    # ConceptSet under two_pass.
    out = lookup_condition("type 2 diabetes", resolver=_resolver_returning(sentinel))  # type: ignore[arg-type]
    assert out is sentinel


def test_two_pass_mode_falls_back_to_alias_on_resolver_miss(two_pass_settings: None) -> None:
    """Resolver returning None means 'not in the registry' or
    'soft-failed'; either way the matcher must consult the alias
    table next."""
    out = lookup_condition("type 2 diabetes", resolver=_resolver_returning(None))  # type: ignore[arg-type]
    assert out is T2DM


def test_two_pass_mode_returns_none_when_both_bridges_miss(two_pass_settings: None) -> None:
    """Final 'unmapped concept' shape: resolver miss + alias miss
    -> None, which the matcher renders as
    `indeterminate(unmapped_concept)`. The whole point of keeping
    the alias path during migration is that this set shrinks
    monotonically as the bindings registry grows."""
    out = lookup_condition("acute pancreatitis", resolver=_resolver_returning(None))  # type: ignore[arg-type]
    assert out is None


def test_two_pass_mode_lab_falls_back_to_alias(two_pass_settings: None) -> None:
    """Lab registry is empty in v0; alias hit must still work
    under two_pass thanks to the fallback chain."""
    out = lookup_lab("hba1c", resolver=_resolver_returning(None))  # type: ignore[arg-type]
    assert out is HBA1C


def test_two_pass_mode_medication_resolver_can_populate(two_pass_settings: None) -> None:
    """The medication alias table is empty in v0 -- two_pass mode
    is the only path that can return a non-None ConceptSet for
    meds. Verifies the resolver's return is honored even when the
    alias table has nothing to fall back to."""
    from clinical_demo.profile import ConceptSet

    rx = ConceptSet(
        name="metformin (RxNorm)",
        system="http://www.nlm.nih.gov/research/umls/rxnorm",
        codes=frozenset({"6809"}),
    )
    out = lookup_medication("metformin", resolver=_resolver_returning(rx))  # type: ignore[arg-type]
    assert out is rx
