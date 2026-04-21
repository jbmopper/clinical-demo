"""Tests for the Patient Profiler primitives.

Hand-built tiny patients so each test exercises one concern; the FHIR
loader has its own tests in tests/data/test_synthea.py and we don't
need to repeat that work here.
"""

from __future__ import annotations

from datetime import date

import pytest

from clinical_demo.domain.patient import (
    CodedConcept,
    Condition,
    LabObservation,
    Medication,
    Patient,
)
from clinical_demo.profile import (
    ConceptSet,
    PatientProfile,
    ThresholdResult,
    canonical_unit,
    days_between,
    freshness_window_days,
)
from clinical_demo.profile.concept_sets import HBA1C, T2DM

AS_OF = date(2025, 1, 1)
SNOMED = "http://snomed.info/sct"
LOINC = "http://loinc.org"
RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm"


def _condition(
    code: str,
    *,
    onset: date = date(2010, 1, 1),
    abatement: date | None = None,
    is_clinical: bool = True,
) -> Condition:
    return Condition(
        concept=CodedConcept(system=SNOMED, code=code, display=code),
        onset_date=onset,
        abatement_date=abatement,
        is_clinical=is_clinical,
    )


def _lab(
    loinc: str,
    value: float,
    *,
    unit: str,
    on_date: date,
) -> LabObservation:
    return LabObservation(
        concept=CodedConcept(system=LOINC, code=loinc, display=loinc),
        value=value,
        unit=unit,
        effective_date=on_date,
    )


def _med(rxnorm: str, *, start: date = date(2020, 1, 1)) -> Medication:
    return Medication(
        concept=CodedConcept(system=RXNORM, code=rxnorm, display=rxnorm),
        start_date=start,
    )


def _patient(
    *,
    sex: str = "female",
    birth: date = date(1970, 6, 1),
    conditions: list[Condition] | None = None,
    observations: list[LabObservation] | None = None,
    medications: list[Medication] | None = None,
) -> Patient:
    return Patient(
        patient_id="P",
        birth_date=birth,
        sex=sex,  # type: ignore[arg-type]
        conditions=conditions or [],
        observations=observations or [],
        medications=medications or [],
    )


# ---------- as-of view ----------


def test_profile_exposes_age_and_demographics() -> None:
    prof = PatientProfile(_patient(birth=date(1995, 1, 2), sex="male"), AS_OF)
    assert prof.age_years == 29  # birthday hasn't passed on Jan 1
    assert prof.sex == "male"
    assert prof.patient_id == "P"


def test_profile_active_conditions_filters_by_as_of() -> None:
    """Conditions abated before as_of should not appear as active."""
    p = _patient(
        conditions=[
            _condition("44054006", onset=date(2010, 1, 1)),  # active
            _condition("38341003", onset=date(2020, 1, 1), abatement=date(2024, 1, 1)),
        ]
    )
    prof = PatientProfile(p, AS_OF)
    active = prof.active_conditions
    assert len(active) == 1
    assert active[0].concept.code == "44054006"


# ---------- condition primitives ----------


def test_has_active_condition_in_returns_true_for_member() -> None:
    p = _patient(conditions=[_condition("44054006")])
    prof = PatientProfile(p, AS_OF)
    assert prof.has_active_condition_in({"44054006", "73211009"}) is True


def test_has_active_condition_in_returns_false_when_no_match() -> None:
    p = _patient(conditions=[_condition("38341003")])  # hypertension only
    prof = PatientProfile(p, AS_OF)
    assert prof.has_active_condition_in({"44054006"}) is False


def test_matching_active_conditions_returns_evidence_list() -> None:
    p = _patient(
        conditions=[
            _condition("44054006"),
            _condition("38341003"),
            _condition("55822004"),
        ]
    )
    prof = PatientProfile(p, AS_OF)
    matches = prof.matching_active_conditions({"44054006", "55822004"})
    assert {c.concept.code for c in matches} == {"44054006", "55822004"}


def test_inactive_conditions_are_excluded_from_match() -> None:
    """An abated condition should not register as a match even if its
    code is in the requested set."""
    p = _patient(conditions=[_condition("44054006", abatement=date(2020, 1, 1))])
    prof = PatientProfile(p, AS_OF)
    assert prof.has_active_condition_in({"44054006"}) is False


# ---------- ConceptSet path ----------


def test_has_active_condition_in_accepts_concept_set() -> None:
    """The curated T2DM ConceptSet matches the patient's T2DM condition."""
    p = _patient(conditions=[_condition("44054006")])
    prof = PatientProfile(p, AS_OF)
    assert prof.has_active_condition_in(T2DM) is True


def test_concept_set_path_requires_matching_system_uri() -> None:
    """Same numeric code in a different coding system must not match.

    This is the whole point of carrying `system` on a ConceptSet —
    a SNOMED 73211009 (diabetes mellitus) should not match an
    ICD-10 73211009 (which doesn't even exist as a valid code, but
    the principle is what matters)."""
    icd_diabetes = Condition(
        concept=CodedConcept(system="http://hl7.org/fhir/sid/icd-10", code="44054006", display="x"),
        onset_date=date(2010, 1, 1),
        is_clinical=True,
    )
    p = _patient(conditions=[icd_diabetes])
    prof = PatientProfile(p, AS_OF)
    assert prof.has_active_condition_in(T2DM) is False
    assert prof.has_active_condition_in({"44054006"}) is True  # raw code, no URI check


def test_concept_set_round_trip_through_matching_helper() -> None:
    """`matching_active_conditions` returns the evidence the matcher
    will display in its rationale."""
    p = _patient(
        conditions=[
            _condition("44054006"),  # T2DM
            _condition("38341003"),  # HTN, not in T2DM set
        ]
    )
    prof = PatientProfile(p, AS_OF)
    matches = prof.matching_active_conditions(T2DM)
    assert {c.concept.code for c in matches} == {"44054006"}


def test_lab_concept_set_documents_loinc() -> None:
    """Lab ConceptSets carry the LOINC system URI and the codes the
    matcher will look up; the API surface itself takes the LOINC
    string for now (single-code sets), but having the ConceptSet on
    hand keeps the policy auditable in one file."""
    assert HBA1C.system.endswith("loinc.org")
    assert HBA1C.codes == frozenset({"4548-4"})


def test_concept_set_is_frozen_and_hashable() -> None:
    """Pydantic's frozenset codes ensure ConceptSets are safe to use
    as dict keys / set members in the matcher's rule book."""
    cs1 = ConceptSet(name="x", system="http://snomed.info/sct", codes=frozenset({"a", "b"}))
    cs2 = ConceptSet(name="x", system="http://snomed.info/sct", codes=frozenset({"b", "a"}))
    assert cs1.codes == cs2.codes


# ---------- medication primitives ----------


def test_has_active_medication_in_basic() -> None:
    p = _patient(medications=[_med("860975")])  # metformin RxNorm
    prof = PatientProfile(p, AS_OF)
    assert prof.has_active_medication_in({"860975"}) is True
    assert prof.has_active_medication_in({"999999"}) is False


# ---------- canonical_unit ----------


@pytest.mark.parametrize(
    "loinc, raw, expected",
    [
        ("33914-3", "mL/min/{1.73_m2}", "mL/min/1.73m2"),
        ("33914-3", "mL/min", "mL/min/1.73m2"),  # alias to canonical
        ("4548-4", "%", "%"),
        ("4548-4", "mmol/mol", None),  # not in our alias table → fail-closed
        ("8480-6", "mm[Hg]", "mmHg"),
        ("99999-9", "anything", None),  # unknown LOINC
    ],
)
def test_canonical_unit_normalizes_known_aliases(
    loinc: str, raw: str, expected: str | None
) -> None:
    assert canonical_unit(loinc, raw) == expected


# ---------- latest_lab ----------


def test_latest_lab_returns_most_recent() -> None:
    p = _patient(
        observations=[
            _lab("4548-4", 7.5, unit="%", on_date=date(2020, 6, 1)),
            _lab("4548-4", 8.0, unit="%", on_date=date(2024, 11, 1)),
            _lab("4548-4", 7.0, unit="%", on_date=date(2023, 1, 1)),
        ]
    )
    prof = PatientProfile(p, AS_OF)
    obs = prof.latest_lab("4548-4")
    assert obs is not None
    assert obs.effective_date == date(2024, 11, 1)
    assert obs.value == 8.0


def test_latest_lab_respects_freshness_window() -> None:
    """Lab outside the freshness window should not be returned.

    The window is inclusive: a lab exactly N days old is still in the
    window for `max_age_days=N`. Using 367 here for a lab that is
    exactly 366 days old (2024 was a leap year) so the test isn't
    riding the edge of off-by-one debates.
    """
    p = _patient(
        observations=[_lab("4548-4", 7.5, unit="%", on_date=date(2024, 1, 1))],
    )
    prof = PatientProfile(p, AS_OF)
    assert prof.latest_lab("4548-4", max_age_days=367) is not None
    assert prof.latest_lab("4548-4", max_age_days=90) is None


def test_latest_lab_returns_none_when_no_observation() -> None:
    prof = PatientProfile(_patient(), AS_OF)
    assert prof.latest_lab("4548-4") is None


# ---------- meets_threshold ----------


def test_meets_threshold_pass_when_lab_satisfies() -> None:
    p = _patient(observations=[_lab("4548-4", 7.5, unit="%", on_date=date(2024, 12, 1))])
    prof = PatientProfile(p, AS_OF)
    assert prof.meets_threshold("4548-4", ">=", 7.0, "%") == ThresholdResult.MEETS


def test_meets_threshold_fail_when_lab_does_not_satisfy() -> None:
    p = _patient(observations=[_lab("4548-4", 6.5, unit="%", on_date=date(2024, 12, 1))])
    prof = PatientProfile(p, AS_OF)
    assert prof.meets_threshold("4548-4", ">=", 7.0, "%") == ThresholdResult.DOES_NOT_MEET


def test_meets_threshold_no_data_when_lab_missing() -> None:
    prof = PatientProfile(_patient(), AS_OF)
    assert prof.meets_threshold("4548-4", ">=", 7.0, "%") == ThresholdResult.NO_DATA


def test_meets_threshold_stale_when_lab_too_old() -> None:
    """Stale-data is distinct from no-data: the matcher's verdict should
    be 'indeterminate (stale)' rather than silently using a 5-year-old
    HbA1c."""
    p = _patient(observations=[_lab("4548-4", 7.5, unit="%", on_date=date(2018, 1, 1))])
    prof = PatientProfile(p, AS_OF)
    result = prof.meets_threshold("4548-4", ">=", 7.0, "%", max_age_days=90)
    assert result == ThresholdResult.STALE_DATA


def test_meets_threshold_unit_mismatch_when_units_disagree() -> None:
    """HbA1c reported as mmol/mol vs. threshold in %: the matcher must
    not silently coerce — return UNIT_MISMATCH."""
    p = _patient(observations=[_lab("4548-4", 53.0, unit="mmol/mol", on_date=date(2024, 12, 1))])
    prof = PatientProfile(p, AS_OF)
    result = prof.meets_threshold("4548-4", ">=", 7.0, "%")
    assert result == ThresholdResult.UNIT_MISMATCH


def test_meets_threshold_unit_aliases_recognized() -> None:
    """Synthea's 'mL/min' for eGFR is treated as the same physical
    quantity as the canonical 'mL/min/{1.73_m2}'."""
    p = _patient(observations=[_lab("33914-3", 65.0, unit="mL/min", on_date=date(2024, 12, 1))])
    prof = PatientProfile(p, AS_OF)
    result = prof.meets_threshold("33914-3", ">=", 60.0, "mL/min/{1.73_m2}")
    assert result == ThresholdResult.MEETS


@pytest.mark.parametrize(
    "op, value, expected",
    [
        (">=", 7.0, ThresholdResult.MEETS),
        (">", 7.5, ThresholdResult.DOES_NOT_MEET),
        ("<", 8.0, ThresholdResult.MEETS),
        ("<=", 7.5, ThresholdResult.MEETS),
        ("==", 7.5, ThresholdResult.MEETS),
        ("==", 7.4, ThresholdResult.DOES_NOT_MEET),
    ],
)
def test_meets_threshold_handles_all_operators(
    op: str, value: float, expected: ThresholdResult
) -> None:
    p = _patient(observations=[_lab("4548-4", 7.5, unit="%", on_date=date(2024, 12, 1))])
    prof = PatientProfile(p, AS_OF)
    assert prof.meets_threshold("4548-4", op, value, "%") == expected  # type: ignore[arg-type]


# ---------- date helpers ----------


def test_days_between_basic() -> None:
    assert days_between(date(2025, 1, 1), date(2025, 1, 31)) == 30


def test_freshness_window_combines_units() -> None:
    assert freshness_window_days(months=3) == 90
    assert freshness_window_days(weeks=2, days=1) == 15
    assert freshness_window_days(months=6, weeks=1) == 187
