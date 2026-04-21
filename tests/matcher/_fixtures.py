"""Shared fixtures and small builders for matcher tests.

Kept in conftest so each test module can import the builders without
re-defining them. The patterns follow the eval-seed test fixtures so
the test layer stays consistent across the codebase.
"""

from __future__ import annotations

from datetime import date

from clinical_demo.domain.patient import (
    CodedConcept,
    Condition,
    LabObservation,
    Medication,
    Patient,
)
from clinical_demo.domain.trial import Trial
from clinical_demo.extractor.schema import (
    AgeCriterion,
    ConditionCriterion,
    ExtractedCriterion,
    FreeTextCriterion,
    MeasurementCriterion,
    MedicationCriterion,
    SexCriterion,
    TemporalWindowCriterion,
)
from clinical_demo.profile import PatientProfile

AS_OF = date(2025, 1, 1)
SNOMED = "http://snomed.info/sct"
LOINC = "http://loinc.org"


def make_patient(
    *,
    birth: date = date(1990, 6, 15),
    sex: str = "male",
    conditions: list[Condition] | None = None,
    observations: list[LabObservation] | None = None,
    medications: list[Medication] | None = None,
) -> Patient:
    return Patient(
        patient_id="P-test",
        birth_date=birth,
        sex=sex,  # type: ignore[arg-type]
        conditions=conditions or [],
        observations=observations or [],
        medications=medications or [],
    )


def make_profile(**kwargs: object) -> PatientProfile:
    return PatientProfile(make_patient(**kwargs), AS_OF)  # type: ignore[arg-type]


def make_trial(
    *,
    nct_id: str = "NCT00000000",
    minimum_age: str | None = None,
    maximum_age: str | None = None,
    sex: str = "ALL",
    eligibility_text: str = "",
) -> Trial:
    return Trial(
        nct_id=nct_id,
        title="Test Trial",
        overall_status="RECRUITING",
        sponsor_name="Test Sponsor",
        sponsor_class="INDUSTRY",
        eligibility_text=eligibility_text,
        minimum_age=minimum_age,
        maximum_age=maximum_age,
        sex=sex,
    )


def make_condition(
    *,
    code: str = "44054006",
    system: str = SNOMED,
    display: str = "T2DM",
    onset: date | None = date(2015, 1, 1),
    abatement: date | None = None,
) -> Condition:
    return Condition(
        concept=CodedConcept(system=system, code=code, display=display),
        onset_date=onset,
        abatement_date=abatement,
        is_clinical=True,
    )


def make_lab(
    *,
    loinc: str = "4548-4",
    value: float = 7.5,
    unit: str = "%",
    date_: date = date(2024, 12, 1),
) -> LabObservation:
    return LabObservation(
        concept=CodedConcept(system=LOINC, code=loinc, display=None),
        value=value,
        unit=unit,
        effective_date=date_,
    )


def make_medication(
    *,
    code: str = "rx-1",
    system: str = "http://www.nlm.nih.gov/research/umls/rxnorm",
    start: date = date(2020, 1, 1),
    end: date | None = None,
) -> Medication:
    return Medication(
        concept=CodedConcept(system=system, code=code, display="Rx"),
        start_date=start,
        end_date=end,
    )


# ---------- ExtractedCriterion builders ----------


def crit_age(
    *,
    minimum_years: float | None = 18.0,
    maximum_years: float | None = None,
    polarity: str = "inclusion",
    negated: bool = False,
    mood: str = "actual",
) -> ExtractedCriterion:
    return ExtractedCriterion(
        kind="age",
        polarity=polarity,  # type: ignore[arg-type]
        source_text="age criterion",
        negated=negated,
        mood=mood,  # type: ignore[arg-type]
        age=AgeCriterion(minimum_years=minimum_years, maximum_years=maximum_years),
        sex=None,
        condition=None,
        medication=None,
        measurement=None,
        temporal_window=None,
        free_text=None,
        mentions=[],
    )


def crit_sex(
    *,
    sex: str = "MALE",
    polarity: str = "inclusion",
    negated: bool = False,
) -> ExtractedCriterion:
    return ExtractedCriterion(
        kind="sex",
        polarity=polarity,  # type: ignore[arg-type]
        source_text="sex criterion",
        negated=negated,
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


def crit_condition(
    *,
    text: str = "type 2 diabetes",
    kind: str = "condition_present",
    polarity: str = "inclusion",
    negated: bool = False,
    mood: str = "actual",
) -> ExtractedCriterion:
    return ExtractedCriterion(
        kind=kind,  # type: ignore[arg-type]
        polarity=polarity,  # type: ignore[arg-type]
        source_text=f"{text} criterion",
        negated=negated,
        mood=mood,  # type: ignore[arg-type]
        age=None,
        sex=None,
        condition=ConditionCriterion(condition_text=text),
        medication=None,
        measurement=None,
        temporal_window=None,
        free_text=None,
        mentions=[],
    )


def crit_medication(
    *,
    text: str = "metformin",
    kind: str = "medication_present",
    polarity: str = "inclusion",
) -> ExtractedCriterion:
    return ExtractedCriterion(
        kind=kind,  # type: ignore[arg-type]
        polarity=polarity,  # type: ignore[arg-type]
        source_text=f"{text} criterion",
        negated=False,
        mood="actual",
        age=None,
        sex=None,
        condition=None,
        medication=MedicationCriterion(medication_text=text),
        measurement=None,
        temporal_window=None,
        free_text=None,
        mentions=[],
    )


def crit_measurement(
    *,
    text: str = "hba1c",
    operator: str = ">=",
    value: float | None = 7.0,
    value_low: float | None = None,
    value_high: float | None = None,
    unit: str | None = "%",
    polarity: str = "inclusion",
    negated: bool = False,
) -> ExtractedCriterion:
    return ExtractedCriterion(
        kind="measurement_threshold",
        polarity=polarity,  # type: ignore[arg-type]
        source_text=f"{text} {operator} {value}{unit or ''}",
        negated=negated,
        mood="actual",
        age=None,
        sex=None,
        condition=None,
        medication=None,
        measurement=MeasurementCriterion(
            measurement_text=text,
            operator=operator,  # type: ignore[arg-type]
            value=value,
            value_low=value_low,
            value_high=value_high,
            unit=unit,
        ),
        temporal_window=None,
        free_text=None,
        mentions=[],
    )


def crit_temporal_window(
    *,
    event_text: str = "type 2 diabetes",
    window_days: int = 365,
    direction: str = "within_past",
    polarity: str = "inclusion",
) -> ExtractedCriterion:
    return ExtractedCriterion(
        kind="temporal_window",
        polarity=polarity,  # type: ignore[arg-type]
        source_text="temporal criterion",
        negated=False,
        mood="historical",
        age=None,
        sex=None,
        condition=None,
        medication=None,
        measurement=None,
        temporal_window=TemporalWindowCriterion(
            event_text=event_text,
            window_days=window_days,
            direction=direction,  # type: ignore[arg-type]
        ),
        free_text=None,
        mentions=[],
    )


def crit_free_text(*, polarity: str = "inclusion") -> ExtractedCriterion:
    return ExtractedCriterion(
        kind="free_text",
        polarity=polarity,  # type: ignore[arg-type]
        source_text="willing to follow protocol",
        negated=False,
        mood="actual",
        age=None,
        sex=None,
        condition=None,
        medication=None,
        measurement=None,
        temporal_window=None,
        free_text=FreeTextCriterion(note="behavioural"),
        mentions=[],
    )
