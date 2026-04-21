"""Unit tests for the eval seed-set: mechanical labeler + helpers.

Selection-script tests live alongside; they exercise the slice-ranking
and per-patient cap logic via small synthetic cohort + trial fixtures
without needing a full data dir on disk.
"""

from __future__ import annotations

from datetime import date

import pytest

from clinical_demo.domain.patient import (
    CodedConcept,
    Condition,
    Patient,
)
from clinical_demo.domain.trial import Trial
from clinical_demo.evals.seed import (
    estimate_free_text_criteria,
    label_healthy_volunteers,
    label_max_age,
    label_min_age,
    label_sex,
    mechanical_verdicts,
    parse_age_years,
)
from clinical_demo.profile import PatientProfile

AS_OF = date(2025, 1, 1)


def _patient(
    *,
    birth: date = date(1990, 6, 15),
    sex: str = "male",
    conditions: list[Condition] | None = None,
) -> Patient:
    return Patient(
        patient_id="X",
        birth_date=birth,
        sex=sex,  # type: ignore[arg-type]
        conditions=conditions or [],
    )


def _profile(
    *,
    birth: date = date(1990, 6, 15),
    sex: str = "male",
    conditions: list[Condition] | None = None,
) -> PatientProfile:
    return PatientProfile(_patient(birth=birth, sex=sex, conditions=conditions), AS_OF)


def _trial(
    *,
    minimum_age: str | None = None,
    maximum_age: str | None = None,
    sex: str = "ALL",
    healthy_volunteers: bool = False,
    eligibility_text: str = "",
) -> Trial:
    return Trial(
        nct_id="NCT0",
        title="t",
        overall_status="RECRUITING",
        sponsor_name="s",
        sponsor_class="INDUSTRY",
        eligibility_text=eligibility_text,
        minimum_age=minimum_age,
        maximum_age=maximum_age,
        sex=sex,
        healthy_volunteers=healthy_volunteers,
    )


def _condition(code: str = "44054006", display: str = "T2DM") -> Condition:
    """A small clinical condition with no end date (active indefinitely)."""
    return Condition(
        concept=CodedConcept(system="http://snomed.info/sct", code=code, display=display),
        onset_date=date(2010, 1, 1),
        is_clinical=True,
    )


# ---------- parse_age_years ----------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("18 Years", 18),
        ("21 Year", 21),
        ("  12 years ", 12),
        ("6 Months", 0),
        ("36 Months", 3),
        ("3 Weeks", 0),
        ("N/A", None),
        ("", None),
        (None, None),
        ("eighty", None),
    ],
)
def test_parse_age_years(raw: str | None, expected: int | None) -> None:
    assert parse_age_years(raw) == expected


# ---------- min/max age ----------


def test_label_min_age_pass() -> None:
    pr = _profile(birth=date(1990, 1, 1))  # age 35 on AS_OF
    t = _trial(minimum_age="18 Years")
    v = label_min_age(pr, t)
    assert v is not None
    assert v.verdict == "pass"
    assert v.criterion.field == "min_age"
    assert v.method == "mechanical"


def test_label_min_age_fail_for_underage_patient() -> None:
    pr = _profile(birth=date(2010, 1, 1))  # age 15
    t = _trial(minimum_age="18 Years")
    v = label_min_age(pr, t)
    assert v is not None
    assert v.verdict == "fail"


def test_label_min_age_returns_none_when_trial_has_no_min() -> None:
    assert label_min_age(_profile(), _trial(minimum_age=None)) is None


def test_label_min_age_indeterminate_when_unparseable() -> None:
    v = label_min_age(_profile(), _trial(minimum_age="N/A"))
    assert v is not None
    assert v.verdict == "indeterminate"


def test_label_max_age_fail_when_patient_too_old() -> None:
    pr = _profile(birth=date(1940, 1, 1))  # age ~85
    v = label_max_age(pr, _trial(maximum_age="75 Years"))
    assert v is not None
    assert v.verdict == "fail"


# ---------- sex ----------


def test_label_sex_returns_none_for_all() -> None:
    assert label_sex(_profile(sex="male"), _trial(sex="ALL")) is None


def test_label_sex_pass_when_match() -> None:
    v = label_sex(_profile(sex="female"), _trial(sex="FEMALE"))
    assert v is not None and v.verdict == "pass"


def test_label_sex_fail_when_mismatch() -> None:
    v = label_sex(_profile(sex="male"), _trial(sex="FEMALE"))
    assert v is not None and v.verdict == "fail"


def test_label_sex_indeterminate_for_unknown_patient_sex() -> None:
    v = label_sex(_profile(sex="unknown"), _trial(sex="MALE"))
    assert v is not None and v.verdict == "indeterminate"


# ---------- healthy_volunteers ----------


def test_healthy_volunteers_returns_none_when_flag_off() -> None:
    pr = _profile(conditions=[_condition()])
    assert label_healthy_volunteers(pr, _trial(healthy_volunteers=False)) is None


def test_healthy_volunteers_pass_when_no_active_conditions() -> None:
    v = label_healthy_volunteers(_profile(conditions=[]), _trial(healthy_volunteers=True))
    assert v is not None and v.verdict == "pass"


def test_healthy_volunteers_fail_when_patient_has_active_condition() -> None:
    pr = _profile(conditions=[_condition()])
    v = label_healthy_volunteers(pr, _trial(healthy_volunteers=True))
    assert v is not None and v.verdict == "fail"
    assert "1 active clinical condition" in v.rationale


# ---------- mechanical_verdicts aggregation ----------


def test_mechanical_verdicts_returns_only_applicable_labels() -> None:
    """A trial with no constraints produces no verdicts."""
    assert mechanical_verdicts(_profile(), _trial()) == []


def test_mechanical_verdicts_combines_multiple_constraints() -> None:
    pr = _profile(birth=date(1990, 1, 1), sex="female")
    t = _trial(
        minimum_age="18 Years",
        maximum_age="75 Years",
        sex="FEMALE",
        healthy_volunteers=False,
    )
    fields = {v.criterion.field for v in mechanical_verdicts(pr, t)}
    assert fields == {"min_age", "max_age", "sex"}


# ---------- free-text criterion estimate ----------


def test_estimate_free_text_counts_bullets() -> None:
    text = """
    Inclusion Criteria:
    - Age 18 or older
    - HbA1c >= 7.0%
    - Willing to comply with study procedures
    """
    assert estimate_free_text_criteria(text) == 3


def test_estimate_free_text_counts_numbered_lines() -> None:
    text = "1. age >= 18\n2. female\n3) BP < 140"
    assert estimate_free_text_criteria(text) == 3


def test_estimate_free_text_falls_back_to_nonblank_lines() -> None:
    """When no bullets are present we over-count rather than under-count."""
    text = "patient must be ambulatory\npatient must consent\n"
    assert estimate_free_text_criteria(text) == 2


def test_estimate_free_text_handles_empty_string() -> None:
    assert estimate_free_text_criteria("") == 0
