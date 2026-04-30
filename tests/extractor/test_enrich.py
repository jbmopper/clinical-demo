"""Tests for extractor post-processing enrichment from CT.gov fields.

Covers `enrich_with_structured_fields` and the `_parse_ctgov_age_string`
helper. The integration with `score_pair` is exercised in
`tests/scoring/test_score_pair.py::test_score_pair_enriches_age_sex_from_ctgov`.

Determinism is the load-bearing property: we want to *backfill* gaps,
never override what the LLM saw, and never invent bounds when the
structured field is uninformative ('N/A', categorical labels). Each
test pins one branch of that contract.
"""

from __future__ import annotations

import pytest

from clinical_demo.domain.trial import Trial
from clinical_demo.extractor.enrich import (
    INJECTED_SOURCE_PREFIX,
    _parse_ctgov_age_string,
    enrich_with_structured_fields,
)
from clinical_demo.extractor.schema import (
    AgeCriterion,
    ConditionCriterion,
    ExtractedCriteria,
    ExtractedCriterion,
    ExtractionMetadata,
    SexCriterion,
)

# ---------- fixture builders ----------


def _trial(**overrides: object) -> Trial:
    """Minimal CT.gov-shaped Trial with sensible defaults; tests
    override the fields they care about."""
    base: dict[str, object] = {
        "nct_id": "NCT00000001",
        "title": "Test trial",
        "phase": [],
        "overall_status": "RECRUITING",
        "conditions": [],
        "sponsor_name": "Test Sponsor",
        "sponsor_class": "INDUSTRY",
        "intervention_types": [],
        "eligibility_text": "Inclusion: adults",
        "minimum_age": "18 Years",
        "maximum_age": "75 Years",
        "sex": "ALL",
        "healthy_volunteers": False,
    }
    base.update(overrides)
    return Trial(**base)  # type: ignore[arg-type]


def _empty_extracted() -> ExtractedCriteria:
    return ExtractedCriteria(criteria=[], metadata=ExtractionMetadata(notes=""))


def _criterion_age(min_years: float = 21.0) -> ExtractedCriterion:
    """LLM-extracted age criterion. Used to verify the
    no-override branch."""
    return ExtractedCriterion(
        kind="age",
        polarity="inclusion",
        source_text="Adults aged 21 years or older",
        negated=False,
        mood="actual",
        age=AgeCriterion(minimum_years=min_years, maximum_years=None),
        sex=None,
        condition=None,
        medication=None,
        measurement=None,
        temporal_window=None,
        free_text=None,
        mentions=[],
    )


def _criterion_sex(sex: str = "FEMALE") -> ExtractedCriterion:
    return ExtractedCriterion(
        kind="sex",
        polarity="inclusion",
        source_text="Female patients",
        negated=False,
        mood="actual",
        age=None,
        sex=SexCriterion(sex=sex),  # type: ignore[arg-type]
        condition=None,
        medication=None,
        measurement=None,
        temporal_window=None,
        free_text=None,
        mentions=[],
    )


def _criterion_condition() -> ExtractedCriterion:
    return ExtractedCriterion(
        kind="condition_present",
        polarity="inclusion",
        source_text="Diagnosed with diabetes",
        negated=False,
        mood="actual",
        age=None,
        sex=None,
        condition=ConditionCriterion(condition_text="diabetes"),
        medication=None,
        measurement=None,
        temporal_window=None,
        free_text=None,
        mentions=[],
    )


# ---------- _parse_ctgov_age_string ----------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("18 Years", 18.0),
        ("65 Years", 65.0),
        ("99 Years", 99.0),
        ("1 Year", 1.0),
        ("12 Months", 1.0),
        ("6 Months", 0.5),
        ("52 Weeks", 1.0),
        ("365 Days", 1.0),
    ],
)
def test_parse_ctgov_age_string_recognized_units(raw: str, expected: float) -> None:
    """All four CT.gov pediatric/adult units must round-trip to
    years. Pinning the exact float values rather than just
    'not None' so a unit-conversion bug (e.g. months/30 instead
    of months/12) is caught."""
    parsed = _parse_ctgov_age_string(raw)
    assert parsed is not None
    assert parsed == pytest.approx(expected, rel=1e-6)


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "N/A",
        "n/a",
        "Child",
        "Adult",
        "Senior",
        "18",
        "Years",
        "eighteen years",
        "18 Decades",
        "18 Years old",
    ],
)
def test_parse_ctgov_age_string_rejects_ambiguous(raw: str | None) -> None:
    """Anything we can't pin to a numeric year value returns None.
    The matcher's 'no bound' code path is well-tested; silently
    inventing a bound from 'Adult' is the failure mode we are
    avoiding (mirrors the soft-fail discipline from D-65/D-66)."""
    assert _parse_ctgov_age_string(raw) is None


# ---------- enrich_with_structured_fields: age branch ----------


def test_injects_age_when_extractor_did_not() -> None:
    """The headline use case: trial has structured age bounds, the
    extractor missed them, enrichment fills the gap."""
    extracted = _empty_extracted()
    trial = _trial(minimum_age="18 Years", maximum_age="75 Years")

    out = enrich_with_structured_fields(extracted, trial)

    age_rows = [c for c in out.criteria if c.kind == "age"]
    assert len(age_rows) == 1
    assert age_rows[0].age is not None
    assert age_rows[0].age.minimum_years == 18.0
    assert age_rows[0].age.maximum_years == 75.0
    assert age_rows[0].polarity == "inclusion"
    assert age_rows[0].mood == "actual"
    assert age_rows[0].negated is False
    assert age_rows[0].source_text.startswith(INJECTED_SOURCE_PREFIX)
    # Provenance: the original CT.gov strings appear verbatim so
    # reviewers can see what was injected from where.
    assert "minimumAge=18 Years" in age_rows[0].source_text
    assert "maximumAge=75 Years" in age_rows[0].source_text


def test_injects_age_with_only_min_when_max_is_missing() -> None:
    """One-sided bounds: minimum_age set, maximum_age None.
    Encodes as `maximum_years=None` rather than fabricating a
    high bound."""
    extracted = _empty_extracted()
    trial = _trial(minimum_age="18 Years", maximum_age=None)

    out = enrich_with_structured_fields(extracted, trial)
    age_rows = [c for c in out.criteria if c.kind == "age"]
    assert len(age_rows) == 1
    assert age_rows[0].age is not None
    assert age_rows[0].age.minimum_years == 18.0
    assert age_rows[0].age.maximum_years is None


def test_injects_age_with_only_max_when_min_is_na() -> None:
    """`'N/A'` is the CT.gov convention for 'no bound on this side'.
    It must parse to None (not be silently treated as 0 or
    inherit the other bound)."""
    extracted = _empty_extracted()
    trial = _trial(minimum_age="N/A", maximum_age="65 Years")

    out = enrich_with_structured_fields(extracted, trial)
    age_rows = [c for c in out.criteria if c.kind == "age"]
    assert len(age_rows) == 1
    assert age_rows[0].age is not None
    assert age_rows[0].age.minimum_years is None
    assert age_rows[0].age.maximum_years == 65.0


def test_does_not_inject_age_when_both_bounds_unparseable() -> None:
    """If neither side parses, no age row at all -- the matcher
    never sees one. Beats the alternative of an `age` row with
    both bounds None, which would be a vacuous criterion."""
    extracted = _empty_extracted()
    trial = _trial(minimum_age=None, maximum_age=None)

    out = enrich_with_structured_fields(extracted, trial)
    assert all(c.kind != "age" for c in out.criteria)


def test_does_not_override_extractor_age() -> None:
    """If the LLM extracted *any* `kind='age'` row, enrichment
    leaves it alone -- the LLM may have nuanced the bounds the
    structured field can't capture (e.g. exception clauses)."""
    extracted = ExtractedCriteria(
        criteria=[_criterion_age(min_years=21.0)],
        metadata=ExtractionMetadata(notes=""),
    )
    trial = _trial(minimum_age="18 Years", maximum_age="75 Years")

    out = enrich_with_structured_fields(extracted, trial)
    age_rows = [c for c in out.criteria if c.kind == "age"]
    assert len(age_rows) == 1
    # LLM bound preserved verbatim -- structured-field bounds NOT
    # appended.
    assert age_rows[0].age is not None
    assert age_rows[0].age.minimum_years == 21.0
    assert age_rows[0].source_text == "Adults aged 21 years or older"


# ---------- enrich_with_structured_fields: sex branch ----------


@pytest.mark.parametrize("sex_value", ["MALE", "FEMALE"])
def test_injects_sex_when_constraining(sex_value: str) -> None:
    """Sex='MALE' or 'FEMALE' actually constrains -- inject."""
    extracted = _empty_extracted()
    trial = _trial(sex=sex_value)

    out = enrich_with_structured_fields(extracted, trial)
    sex_rows = [c for c in out.criteria if c.kind == "sex"]
    assert len(sex_rows) == 1
    assert sex_rows[0].sex is not None
    assert sex_rows[0].sex.sex == sex_value
    assert sex_rows[0].source_text.startswith(INJECTED_SOURCE_PREFIX)
    assert f"sex={sex_value}" in sex_rows[0].source_text


def test_does_not_inject_sex_when_all() -> None:
    """Sex='ALL' is vacuous -- the matcher's ALL branch always
    passes. Injecting a row would just clutter the verdict list
    with a guaranteed `pass`."""
    extracted = _empty_extracted()
    trial = _trial(sex="ALL")

    out = enrich_with_structured_fields(extracted, trial)
    assert all(c.kind != "sex" for c in out.criteria)


def test_does_not_inject_sex_when_unrecognized() -> None:
    """Defensive: anything other than MALE/FEMALE/ALL is treated
    as 'don't inject.' Beats coercing weird values into a
    Literal-typed slot."""
    extracted = _empty_extracted()
    trial = _trial(sex="OTHER")

    out = enrich_with_structured_fields(extracted, trial)
    assert all(c.kind != "sex" for c in out.criteria)


def test_does_not_override_extractor_sex() -> None:
    """LLM-extracted sex wins, structured field is ignored
    (parallel to the age-override-not-allowed rule)."""
    extracted = ExtractedCriteria(
        criteria=[_criterion_sex(sex="FEMALE")],
        metadata=ExtractionMetadata(notes=""),
    )
    trial = _trial(sex="MALE")  # would inject MALE if extractor were silent

    out = enrich_with_structured_fields(extracted, trial)
    sex_rows = [c for c in out.criteria if c.kind == "sex"]
    assert len(sex_rows) == 1
    assert sex_rows[0].sex is not None
    assert sex_rows[0].sex.sex == "FEMALE"


# ---------- combined behaviour ----------


def test_preserves_existing_criteria_and_appends_injected() -> None:
    """Original criteria stay in their original order; injected
    criteria land at the end. Citation indices on the
    extractor-extracted rows stay stable across enrichment."""
    cond = _criterion_condition()
    extracted = ExtractedCriteria(
        criteria=[cond],
        metadata=ExtractionMetadata(notes="original notes"),
    )
    trial = _trial(minimum_age="18 Years", maximum_age=None, sex="MALE")

    out = enrich_with_structured_fields(extracted, trial)

    assert len(out.criteria) == 3
    assert out.criteria[0] is cond  # exact identity, original first
    assert out.criteria[1].kind == "age"
    assert out.criteria[2].kind == "sex"
    # Metadata carried through unchanged.
    assert out.metadata.notes == "original notes"


def test_no_op_returns_same_object_when_nothing_to_inject() -> None:
    """Cheap-path optimization: when there's nothing to add,
    return the input unchanged so callers can rely on identity
    to detect 'no work happened.' (`extract_node` uses this to
    skip a `dataclasses.replace`.)"""
    cond = _criterion_condition()
    extracted = ExtractedCriteria(
        criteria=[cond],
        metadata=ExtractionMetadata(notes=""),
    )
    # Nothing parseable, sex=ALL.
    trial = _trial(minimum_age="N/A", maximum_age="N/A", sex="ALL")

    out = enrich_with_structured_fields(extracted, trial)
    assert out is extracted


def test_injected_criterion_passes_pydantic_validation() -> None:
    """Round-trip through JSON to confirm the synthetic rows are
    schema-valid -- if a future schema change broke this, eval
    persistence would silently drop the injected bounds."""
    extracted = _empty_extracted()
    trial = _trial(minimum_age="18 Years", maximum_age="65 Years", sex="FEMALE")

    out = enrich_with_structured_fields(extracted, trial)
    roundtripped = ExtractedCriteria.model_validate_json(out.model_dump_json())
    assert roundtripped == out
