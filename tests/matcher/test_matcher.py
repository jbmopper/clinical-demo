"""Per-kind matcher tests.

Each `CriterionKind` has its own pass / fail / indeterminate paths;
we cover them explicitly so a regression in one kind doesn't hide
behind a high aggregate pass rate.

Tests organised by kind, then by outcome class. Polarity / negation
interactions are smoke-tested at the top-level dispatcher in
`test_dispatch.py`; the truth table for the polarity helper is in
`test_polarity.py`.
"""

from __future__ import annotations

from datetime import date

from clinical_demo.matcher import MATCHER_VERSION, match_criterion
from tests.matcher._fixtures import (
    AS_OF,
    crit_age,
    crit_condition,
    crit_free_text,
    crit_measurement,
    crit_medication,
    crit_sex,
    crit_temporal_window,
    make_condition,
    make_lab,
    make_profile,
    make_trial,
)

# ---------- age ----------


def test_age_pass_within_lower_bound() -> None:
    """Patient at minimum age satisfies a `>= min` criterion."""
    profile = make_profile(birth=date(2007, 1, 1))  # turns 18 on 2025-01-01
    v = match_criterion(crit_age(minimum_years=18.0), profile, make_trial())
    assert v.verdict == "pass"
    assert v.reason == "ok"
    assert v.matcher_version == MATCHER_VERSION


def test_age_fail_below_lower_bound() -> None:
    """Underage patient fails an inclusion-side adult criterion."""
    profile = make_profile(birth=date(2010, 1, 1))  # age 14 on 2025-01-01
    v = match_criterion(crit_age(minimum_years=18.0), profile, make_trial())
    assert v.verdict == "fail"
    assert v.reason == "ok"


def test_age_fail_above_upper_bound() -> None:
    profile = make_profile(birth=date(1940, 1, 1))  # age 85
    v = match_criterion(crit_age(minimum_years=18.0, maximum_years=80.0), profile, make_trial())
    assert v.verdict == "fail"


def test_age_evidence_includes_trial_field_when_present() -> None:
    """Trial-side structured age fields are cited as auxiliary
    evidence so the reviewer can compare the LLM's restatement to
    the source."""
    profile = make_profile(birth=date(1990, 1, 1))
    trial = make_trial(minimum_age="18 Years", maximum_age="80 Years")
    v = match_criterion(crit_age(minimum_years=18.0), profile, trial)
    kinds = {e.kind for e in v.evidence}
    assert "demographics" in kinds
    assert "trial_field" in kinds


# ---------- sex ----------


def test_sex_all_passes_regardless() -> None:
    """`ALL` short-circuits to pass; reason stays `ok`."""
    profile = make_profile(sex="female")
    v = match_criterion(crit_sex(sex="ALL"), profile, make_trial())
    assert v.verdict == "pass"


def test_sex_match_passes() -> None:
    profile = make_profile(sex="female")
    v = match_criterion(crit_sex(sex="FEMALE"), profile, make_trial())
    assert v.verdict == "pass"


def test_sex_mismatch_fails() -> None:
    profile = make_profile(sex="male")
    v = match_criterion(crit_sex(sex="FEMALE"), profile, make_trial())
    assert v.verdict == "fail"


def test_sex_unknown_patient_returns_indeterminate() -> None:
    """`other`/`unknown` patient sex against a specific MALE/FEMALE
    requirement is indeterminate — matcher doesn't guess."""
    profile = make_profile(sex="unknown")
    v = match_criterion(crit_sex(sex="MALE"), profile, make_trial())
    assert v.verdict == "indeterminate"
    assert v.reason == "no_data"


# ---------- condition ----------


def test_condition_present_pass() -> None:
    """T2DM-coded condition active on `as_of` satisfies a present
    criterion."""
    profile = make_profile(conditions=[make_condition(code="44054006", display="T2DM")])
    v = match_criterion(crit_condition(text="type 2 diabetes"), profile, make_trial())
    assert v.verdict == "pass"
    assert any(e.kind == "condition" for e in v.evidence)


def test_condition_present_fail_without_match() -> None:
    """No matching active condition → fail (with a MissingEvidence
    row citing what we looked for)."""
    profile = make_profile(conditions=[])
    v = match_criterion(crit_condition(text="type 2 diabetes"), profile, make_trial())
    assert v.verdict == "fail"
    assert any(e.kind == "missing" for e in v.evidence)


def test_condition_unmapped_text_indeterminate() -> None:
    """Surface form not in the lookup table → indeterminate."""
    profile = make_profile(conditions=[make_condition()])
    v = match_criterion(crit_condition(text="rare unknown disease"), profile, make_trial())
    assert v.verdict == "indeterminate"
    assert v.reason == "unmapped_concept"


def test_condition_absent_with_no_match_still_passes_after_polarity() -> None:
    """`condition_absent` polarity='inclusion' negated=True means the
    patient must NOT have the condition. Raw "no T2DM" → fail; the
    inclusion-side single negation flips it to pass."""
    profile = make_profile(conditions=[])  # no T2DM
    v = match_criterion(
        crit_condition(
            text="type 2 diabetes",
            kind="condition_absent",
            polarity="inclusion",
            negated=True,
        ),
        profile,
        make_trial(),
    )
    assert v.verdict == "pass"


# ---------- medication ----------


def test_medication_always_indeterminate_in_v0() -> None:
    """Med lookup table is intentionally empty; this is the design
    contract. If you start populating that table, this test should
    fail and you should rewrite it deliberately."""
    profile = make_profile()
    v = match_criterion(crit_medication(text="metformin"), profile, make_trial())
    assert v.verdict == "indeterminate"
    assert v.reason == "unmapped_concept"


# ---------- measurement_threshold ----------


def test_measurement_threshold_meets_passes() -> None:
    """HbA1c 7.5% satisfies `>= 7.0%`."""
    profile = make_profile(
        observations=[make_lab(loinc="4548-4", value=7.5, unit="%")],
    )
    v = match_criterion(
        crit_measurement(text="hba1c", operator=">=", value=7.0, unit="%"), profile, make_trial()
    )
    assert v.verdict == "pass"
    assert any(e.kind == "lab" for e in v.evidence)


def test_measurement_threshold_does_not_meet_fails() -> None:
    """HbA1c 6.0% fails `>= 7.0%`."""
    profile = make_profile(
        observations=[make_lab(loinc="4548-4", value=6.0, unit="%")],
    )
    v = match_criterion(
        crit_measurement(text="hba1c", operator=">=", value=7.0, unit="%"), profile, make_trial()
    )
    assert v.verdict == "fail"


def test_measurement_no_lab_returns_no_data() -> None:
    """Honest no-data signal — the matcher's job is to surface this,
    not to silently fail the criterion."""
    profile = make_profile(observations=[])
    v = match_criterion(
        crit_measurement(text="hba1c", operator=">=", value=7.0, unit="%"), profile, make_trial()
    )
    assert v.verdict == "indeterminate"
    assert v.reason == "no_data"
    assert any(e.kind == "missing" for e in v.evidence)


def test_measurement_unit_mismatch() -> None:
    """Threshold unit `mmol/mol` doesn't canonicalize against our
    HbA1c LOINC table → unit_mismatch (matcher fails closed)."""
    profile = make_profile(
        observations=[make_lab(loinc="4548-4", value=7.5, unit="%")],
    )
    v = match_criterion(
        crit_measurement(text="hba1c", operator=">=", value=53.0, unit="mmol/mol"),
        profile,
        make_trial(),
    )
    assert v.verdict == "indeterminate"
    assert v.reason == "unit_mismatch"


def test_measurement_equality_operator_translates_to_profile() -> None:
    """The extractor's clinical-style `=` must be translated to the
    profile's Pythonic `==`; this is the small but load-bearing glue
    between the two modules."""
    profile = make_profile(
        observations=[make_lab(loinc="4548-4", value=7.0, unit="%")],
    )
    v = match_criterion(
        crit_measurement(text="hba1c", operator="=", value=7.0, unit="%"),
        profile,
        make_trial(),
    )
    assert v.verdict == "pass"


def test_measurement_unmapped_lab_indeterminate() -> None:
    profile = make_profile()
    v = match_criterion(
        crit_measurement(text="bnp", operator=">=", value=100.0, unit="pg/mL"),
        profile,
        make_trial(),
    )
    assert v.verdict == "indeterminate"
    assert v.reason == "unmapped_concept"


def test_measurement_in_range_pass() -> None:
    """HbA1c 7.5% is in [7.0, 9.0]."""
    profile = make_profile(
        observations=[make_lab(loinc="4548-4", value=7.5, unit="%")],
    )
    v = match_criterion(
        crit_measurement(
            text="hba1c",
            operator="in_range",
            value=None,
            value_low=7.0,
            value_high=9.0,
            unit="%",
        ),
        profile,
        make_trial(),
    )
    assert v.verdict == "pass"


def test_measurement_in_range_outside_fails() -> None:
    profile = make_profile(
        observations=[make_lab(loinc="4548-4", value=10.5, unit="%")],
    )
    v = match_criterion(
        crit_measurement(
            text="hba1c",
            operator="in_range",
            value=None,
            value_low=7.0,
            value_high=9.0,
            unit="%",
        ),
        profile,
        make_trial(),
    )
    assert v.verdict == "fail"


def test_measurement_range_missing_bound_is_ambiguous() -> None:
    profile = make_profile(
        observations=[make_lab(loinc="4548-4", value=7.5, unit="%")],
    )
    v = match_criterion(
        crit_measurement(
            text="hba1c",
            operator="in_range",
            value=None,
            value_low=7.0,
            value_high=None,
            unit="%",
        ),
        profile,
        make_trial(),
    )
    assert v.verdict == "indeterminate"
    assert v.reason == "ambiguous_criterion"


def test_measurement_one_sided_op_missing_value_is_ambiguous() -> None:
    profile = make_profile()
    v = match_criterion(
        crit_measurement(text="hba1c", operator=">=", value=None, unit="%"), profile, make_trial()
    )
    assert v.verdict == "indeterminate"
    assert v.reason == "ambiguous_criterion"


def test_measurement_missing_unit_is_ambiguous() -> None:
    profile = make_profile()
    v = match_criterion(
        crit_measurement(text="hba1c", operator=">=", value=7.0, unit=None), profile, make_trial()
    )
    assert v.verdict == "indeterminate"
    assert v.reason == "ambiguous_criterion"


# ---------- temporal_window ----------


def test_temporal_window_recent_event_passes() -> None:
    """Recent T2DM diagnosis falls within a 365-day window."""
    profile = make_profile(
        conditions=[make_condition(code="44054006", display="T2DM", onset=date(2024, 6, 1))]
    )
    v = match_criterion(
        crit_temporal_window(event_text="type 2 diabetes", window_days=365),
        profile,
        make_trial(),
    )
    assert v.verdict == "pass"


def test_temporal_window_old_event_fails() -> None:
    """Diagnosis 5 years before `as_of` is outside a 90-day window."""
    profile = make_profile(
        conditions=[make_condition(code="44054006", display="T2DM", onset=date(2020, 1, 1))]
    )
    v = match_criterion(
        crit_temporal_window(event_text="type 2 diabetes", window_days=90),
        profile,
        make_trial(),
    )
    assert v.verdict == "fail"


def test_temporal_window_future_direction_unsupported() -> None:
    """`within_future` lands as `unsupported_mood` — patients have no
    planned-event records in v0."""
    profile = make_profile()
    v = match_criterion(
        crit_temporal_window(
            event_text="type 2 diabetes",
            window_days=30,
            direction="within_future",
        ),
        profile,
        make_trial(),
    )
    assert v.verdict == "indeterminate"
    assert v.reason == "unsupported_mood"


def test_temporal_window_unmapped_event() -> None:
    profile = make_profile()
    v = match_criterion(
        crit_temporal_window(event_text="liver transplant", window_days=365),
        profile,
        make_trial(),
    )
    assert v.verdict == "indeterminate"
    assert v.reason == "unmapped_concept"


# ---------- free_text ----------


def test_free_text_always_human_review() -> None:
    """Free-text criteria honestly defer to human review; the
    aggregator decides what to do with that signal."""
    profile = make_profile()
    v = match_criterion(crit_free_text(), profile, make_trial())
    assert v.verdict == "indeterminate"
    assert v.reason == "human_review_required"


# ---------- mood gate ----------


def test_hypothetical_mood_short_circuits_to_indeterminate() -> None:
    """`mood='hypothetical'` is rejected before dispatch — no patient
    data on planned events."""
    profile = make_profile(conditions=[make_condition()])
    v = match_criterion(
        crit_condition(text="type 2 diabetes", mood="hypothetical"),
        profile,
        make_trial(),
    )
    assert v.verdict == "indeterminate"
    assert v.reason == "unsupported_mood"


# ---------- matcher version ----------


def test_matcher_version_stamped_on_every_verdict() -> None:
    profile = make_profile()
    v = match_criterion(crit_free_text(), profile, make_trial())
    assert v.matcher_version == MATCHER_VERSION
    assert v.matcher_version.startswith("matcher-v")


# ---------- as_of awareness ----------


def test_matcher_uses_profile_as_of_for_age() -> None:
    """Profile carries `as_of`; matcher must use it (not today)."""
    profile = make_profile(birth=date(1990, 6, 15))
    # AS_OF = 2025-01-01 in conftest; turn-of-year birthday is 6/15
    # so age on 2025-01-01 is 34 (35th birthday hasn't happened).
    assert profile.age_years == 34
    v = match_criterion(crit_age(minimum_years=35.0), profile, make_trial())
    assert v.verdict == "fail"


def test_matcher_uses_as_of_for_temporal_window() -> None:
    """Cutoff is `as_of - window_days`, not today."""
    profile = make_profile(
        conditions=[make_condition(code="44054006", display="T2DM", onset=date(2024, 12, 15))]
    )
    # AS_OF = 2025-01-01; 30 days back = 2024-12-02; onset 2024-12-15 → in window
    v = match_criterion(
        crit_temporal_window(event_text="type 2 diabetes", window_days=30),
        profile,
        make_trial(),
    )
    assert v.verdict == "pass"
    assert profile.as_of == AS_OF
