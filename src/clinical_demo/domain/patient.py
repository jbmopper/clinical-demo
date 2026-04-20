"""Patient-side domain model.

These types are the canonical internal representation that all downstream
components (matchers, evaluators, UI) consume. They are deliberately simpler
than FHIR: only the fields we actually use, named for clinical clarity, and
with helpers that make as-of-date queries cheap.

Eligibility decisions are always evaluated *as of* a point in time. Every
query helper accepts an `as_of` parameter so the system can replay screenings
against historical states (e.g., for evals on retrospective trial cohorts).
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

Sex = Literal["male", "female", "other", "unknown"]


class CodedConcept(BaseModel):
    """A single coded clinical concept (SNOMED, LOINC, RxNorm, etc.).

    `code` and `system` together uniquely identify the concept. `display`
    is the human-readable label as recorded by the source; it is informational
    only and must not be relied on for matching.
    """

    system: str
    code: str
    display: str | None = None


class Condition(BaseModel):
    """A patient diagnosis.

    Synthea (and other FHIR sources) sometimes record social findings as
    Conditions; callers that want only clinical diagnoses should filter on
    `is_clinical`.
    """

    concept: CodedConcept
    onset_date: date | None = None
    abatement_date: date | None = None
    is_clinical: bool = True

    def is_active(self, as_of: date) -> bool:
        """True iff the condition was active on `as_of` (start <= as_of <= end)."""
        if self.onset_date is not None and self.onset_date > as_of:
            return False
        if self.abatement_date is not None and self.abatement_date < as_of:
            return False
        return True


class LabObservation(BaseModel):
    """A numeric clinical observation (lab value, vital sign).

    Categorical observations are intentionally out of scope for v0; add a
    sibling `CategoricalObservation` model when an eligibility criterion
    actually requires one.
    """

    concept: CodedConcept
    value: float
    unit: str
    effective_date: date


class Medication(BaseModel):
    """A medication that was prescribed/ordered.

    `end_date` of None means the medication is still active (the source did
    not record a stop date).
    """

    concept: CodedConcept
    start_date: date
    end_date: date | None = None

    def is_active(self, as_of: date) -> bool:
        if self.start_date > as_of:
            return False
        if self.end_date is not None and self.end_date < as_of:
            return False
        return True


class Patient(BaseModel):
    """A single patient and their longitudinal record."""

    patient_id: str
    birth_date: date
    sex: Sex
    conditions: list[Condition] = Field(default_factory=list)
    observations: list[LabObservation] = Field(default_factory=list)
    medications: list[Medication] = Field(default_factory=list)

    def age_years(self, as_of: date) -> int:
        """Age in completed years on `as_of`."""
        years = as_of.year - self.birth_date.year
        # Birthday hasn't happened yet this year?
        if (as_of.month, as_of.day) < (self.birth_date.month, self.birth_date.day):
            years -= 1
        return years

    def active_conditions(self, as_of: date) -> list[Condition]:
        """Conditions active as of the given date (clinical only)."""
        return [c for c in self.conditions if c.is_clinical and c.is_active(as_of)]

    def active_medications(self, as_of: date) -> list[Medication]:
        return [m for m in self.medications if m.is_active(as_of)]

    def latest_observation(
        self,
        loinc_code: str,
        as_of: date,
    ) -> LabObservation | None:
        """Most recent observation with the given LOINC code on or before `as_of`.

        Returns None if no such observation exists.
        """
        candidates = [
            o
            for o in self.observations
            if o.concept.code == loinc_code
            and o.concept.system.endswith("loinc.org")
            and o.effective_date <= as_of
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda o: o.effective_date)
