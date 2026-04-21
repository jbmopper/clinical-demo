"""Tests for the cohort curation policy.

The policy is a pure function over a list of `Patient`. Tests build
small synthetic patients in-memory rather than depending on Synthea
fixtures so the contract is testable without the multi-MB dataset.
"""

from __future__ import annotations

from datetime import date

from clinical_demo.data.cohort import (
    cardiometabolic_codes,
    curate,
    score_patient,
)
from clinical_demo.domain import CodedConcept, Condition, LabObservation, Patient

SNOMED = "http://snomed.info/sct"
LOINC = "http://loinc.org"

T2DM = "44054006"
ESSENTIAL_HTN = "59621000"
HYPERLIPIDEMIA = "55822004"
PREDIABETES = "15777000"
UNRELATED_BACK_PAIN = "161891005"

AS_OF = date(2025, 1, 1)


def _condition(code: str, onset: date = date(2010, 1, 1)) -> Condition:
    return Condition(
        concept=CodedConcept(system=SNOMED, code=code, display=code),
        onset_date=onset,
        abatement_date=None,
        is_clinical=True,
    )


def _obs(loinc: str, value: float, unit: str = "") -> LabObservation:
    return LabObservation(
        concept=CodedConcept(system=LOINC, code=loinc, display=loinc),
        value=value,
        unit=unit,
        effective_date=date(2024, 6, 1),
    )


def _patient(
    pid: str,
    *,
    birth_year: int = 1970,
    sex: str = "female",
    conditions: list[Condition] | None = None,
    observations: list[LabObservation] | None = None,
) -> Patient:
    return Patient(
        patient_id=pid,
        birth_date=date(birth_year, 1, 1),
        sex=sex,  # type: ignore[arg-type]
        conditions=conditions or [],
        observations=observations or [],
        medications=[],
    )


# ---------- score_patient ----------


def test_score_rewards_core_conditions_2x_over_prediabetes() -> None:
    assert score_patient({T2DM}) == 2
    assert score_patient({PREDIABETES}) == 1
    assert score_patient({T2DM, ESSENTIAL_HTN}) == 4
    assert score_patient({T2DM, PREDIABETES}) == 3
    assert score_patient(set()) == 0


def test_score_ignores_unrelated_codes() -> None:
    assert score_patient({UNRELATED_BACK_PAIN}) == 0
    assert score_patient({T2DM, UNRELATED_BACK_PAIN}) == 2


# ---------- cardiometabolic_codes ----------


def test_cardiometabolic_codes_filters_to_known_set() -> None:
    p = _patient(
        "p1",
        conditions=[_condition(T2DM), _condition(UNRELATED_BACK_PAIN)],
    )
    assert cardiometabolic_codes(p, AS_OF) == {T2DM}


def test_cardiometabolic_codes_respects_active_as_of() -> None:
    """A condition with onset after the as-of date must not count."""
    p = _patient(
        "p1",
        conditions=[
            _condition(T2DM, onset=date(2030, 1, 1)),
        ],
    )
    assert cardiometabolic_codes(p, AS_OF) == set()


# ---------- curate ----------


def test_curate_drops_patients_with_no_cardiometabolic_conditions() -> None:
    members = curate(
        [
            _patient("with-t2dm", conditions=[_condition(T2DM)]),
            _patient("with-back-pain", conditions=[_condition(UNRELATED_BACK_PAIN)]),
        ],
        as_of=AS_OF,
    )
    ids = [m.patient_id for m in members]
    assert ids == ["with-t2dm"]


def test_curate_drops_patients_outside_age_range() -> None:
    members = curate(
        [
            _patient("child", birth_year=2015, conditions=[_condition(T2DM)]),
            _patient("centenarian", birth_year=1900, conditions=[_condition(T2DM)]),
            _patient("adult", birth_year=1980, conditions=[_condition(T2DM)]),
        ],
        as_of=AS_OF,
    )
    ids = {m.patient_id for m in members}
    assert ids == {"adult"}


def test_curate_orders_by_score_then_age_desc() -> None:
    members = curate(
        [
            _patient(
                "younger-richer",
                birth_year=1990,
                conditions=[_condition(T2DM), _condition(ESSENTIAL_HTN)],
            ),
            _patient("older-poorer", birth_year=1950, conditions=[_condition(PREDIABETES)]),
            _patient(
                "older-richer",
                birth_year=1950,
                conditions=[
                    _condition(T2DM),
                    _condition(ESSENTIAL_HTN),
                    _condition(HYPERLIPIDEMIA),
                ],
            ),
        ],
        as_of=AS_OF,
    )
    assert [m.patient_id for m in members] == [
        "older-richer",  # score 6
        "younger-richer",  # score 4, age 35
        "older-poorer",  # score 1
    ]


def test_curate_caps_at_target_size() -> None:
    patients = [
        _patient(f"p{i}", birth_year=1980, conditions=[_condition(T2DM)]) for i in range(10)
    ]
    members = curate(patients, as_of=AS_OF, target_size=3)
    assert len(members) == 3


def test_curate_returns_fewer_than_target_when_pool_is_small() -> None:
    members = curate(
        [_patient("p1", conditions=[_condition(T2DM)])],
        as_of=AS_OF,
        target_size=150,
    )
    assert len(members) == 1


def test_cohort_member_records_lab_availability() -> None:
    """Lab flags are what hand-labelers and the matcher use to know
    whether a numeric criterion can even be evaluated."""
    p = _patient(
        "rich",
        conditions=[_condition(T2DM)],
        observations=[
            _obs("4548-4", 7.2, "%"),  # HbA1c
            _obs("8480-6", 145, "mm[Hg]"),  # SBP
        ],
    )
    members = curate([p], as_of=AS_OF)
    assert len(members) == 1
    m = members[0]
    assert m.has_hba1c is True
    assert m.has_systolic_bp is True
    assert m.has_ldl is False
    assert m.has_egfr is False


def test_cohort_member_carries_labels_for_offline_review() -> None:
    p = _patient(
        "p1",
        conditions=[_condition(T2DM), _condition(ESSENTIAL_HTN)],
    )
    members = curate([p], as_of=AS_OF)
    assert sorted(members[0].cardiometabolic_codes) == sorted([T2DM, ESSENTIAL_HTN])
    assert "Type 2 diabetes mellitus" in members[0].cardiometabolic_labels
    assert "Essential hypertension" in members[0].cardiometabolic_labels
