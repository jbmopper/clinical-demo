"""Patient Profiler v0.

A `PatientProfile` is a thin, as-of-date-anchored view over a `Patient`
that gives the matcher (and the seed-set labeler) the small set of
semantic primitives it actually uses, instead of forcing every caller
to re-implement the FHIR-y plumbing themselves.

Why a wrapper rather than a materialized snapshot? The Patient model is
already immutable Pydantic and queries against it are cheap; a
materialized snapshot would just add a copy-step and a "which view do
I trust" question. The wrapper approach also keeps the profile
trivially in sync if the underlying Patient is updated for whatever
reason (new lab arrives, etc.).

The primitives intentionally fall into three buckets, each a tri-state:
- conditions   → bool (present / absent)
- medications  → bool (active / inactive)
- thresholds   → `ThresholdResult` (meets / does_not_meet / no_data /
                                    stale_data / unit_mismatch)

Tri-state on thresholds matters: silently treating a missing lab as
"fail" would make the matcher hallucinate a confident verdict. Real
matcher output should distinguish "we know this fails" from "we don't
have the data to decide" — the latter is `indeterminate` from the
eval's perspective and triggers human review.

Unit aliases are handled per-LOINC. Real Synthea data uses `mL/min`
and `mL/min/{1.73_m2}` interchangeably for eGFR; we normalize these
inside `latest_lab`, but threshold checks fail-closed on units we
don't know about (so the matcher doesn't accidentally compare 7.0%
HbA1c to a 53 mmol/mol HbA1c without explicit conversion).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel

from clinical_demo.domain.patient import (
    Condition,
    LabObservation,
    Medication,
    Patient,
)

ThresholdOp = Literal["<", "<=", ">", ">=", "=="]


class ThresholdResult(StrEnum):
    """Outcome of a numeric threshold check.

    String-valued so it serializes cleanly into the manifest the
    seed-set labeler and the matcher both produce.
    """

    MEETS = "meets"
    DOES_NOT_MEET = "does_not_meet"
    NO_DATA = "no_data"
    STALE_DATA = "stale_data"
    UNIT_MISMATCH = "unit_mismatch"


class ConceptSet(BaseModel):
    """A named set of coded concepts (e.g., 'all T2DM SNOMED codes').

    `system` is the URI of the coding system the codes live in; we
    require it because the same numeric code can mean different things
    in different systems (a SNOMED 73211000 is not an ICD 73211000).
    """

    name: str
    system: str
    codes: frozenset[str]


# ---- unit normalization (LOINC-scoped aliases) ----

# Map of LOINC code -> {raw_unit -> canonical_unit}. Keep this small
# and explicit; expand only when we hit a real mismatch in the data.
# The empty-string entry is for units the source forgot to record.
_UNIT_ALIASES: dict[str, dict[str, str]] = {
    # eGFR — Synthea uses both UCUM canonical and the abbreviated form.
    "33914-3": {
        "mL/min/{1.73_m2}": "mL/min/1.73m2",
        "mL/min": "mL/min/1.73m2",  # treat as the same in this dataset
    },
    # HbA1c — only ever observed as percent in our data, but we log
    # mmol/mol explicitly so the matcher knows to refuse it.
    "4548-4": {"%": "%"},
    # LDL — only mg/dL in our data; mmol/L would need conversion.
    "18262-6": {"mg/dL": "mg/dL"},
    # Systolic / diastolic blood pressure.
    "8480-6": {"mm[Hg]": "mmHg"},
    "8462-4": {"mm[Hg]": "mmHg"},
}


def canonical_unit(loinc_code: str, raw_unit: str) -> str | None:
    """Return the canonical unit for `raw_unit` under `loinc_code`.

    Returns None when we have no alias mapping — the caller should
    treat this as `UNIT_MISMATCH` rather than silently comparing
    incompatible quantities.
    """
    aliases = _UNIT_ALIASES.get(loinc_code)
    if aliases is None:
        return None
    return aliases.get(raw_unit)


# ---- profile ----


class PatientProfile:
    """Anchored as-of view over a Patient.

    Construct with the patient and the date the eligibility decision
    is being evaluated *as of*. All query methods are pure and
    idempotent; nothing on the underlying Patient is mutated.
    """

    def __init__(self, patient: Patient, as_of: date) -> None:
        self.patient = patient
        self.as_of = as_of

    @property
    def patient_id(self) -> str:
        return self.patient.patient_id

    @property
    def sex(self) -> str:
        return self.patient.sex

    @property
    def age_years(self) -> int:
        return self.patient.age_years(self.as_of)

    @property
    def active_conditions(self) -> list[Condition]:
        return self.patient.active_conditions(self.as_of)

    @property
    def active_medications(self) -> list[Medication]:
        return self.patient.active_medications(self.as_of)

    # ---- condition primitives ----

    def has_active_condition_in(self, codes: Iterable[str] | ConceptSet) -> bool:
        """True iff any active clinical condition's code is in `codes`.

        Accepts either a raw iterable of code strings or a `ConceptSet`
        (the matcher path); when a ConceptSet is passed we additionally
        require the system URI to match so a SNOMED 73211009 doesn't
        accidentally match an ICD 73211009 of the same numeric code.
        """
        if isinstance(codes, ConceptSet):
            return any(
                c.concept.code in codes.codes and c.concept.system == codes.system
                for c in self.active_conditions
            )
        wanted = set(codes)
        return any(c.concept.code in wanted for c in self.active_conditions)

    def matching_active_conditions(self, codes: Iterable[str] | ConceptSet) -> list[Condition]:
        """All active conditions whose code is in `codes` (for evidence display).

        Same system-URI semantics as `has_active_condition_in`.
        """
        if isinstance(codes, ConceptSet):
            return [
                c
                for c in self.active_conditions
                if c.concept.code in codes.codes and c.concept.system == codes.system
            ]
        wanted = set(codes)
        return [c for c in self.active_conditions if c.concept.code in wanted]

    # ---- medication primitives ----

    def has_active_medication_in(self, codes: Iterable[str] | ConceptSet) -> bool:
        """True iff any active medication's code is in `codes`."""
        if isinstance(codes, ConceptSet):
            return any(
                m.concept.code in codes.codes and m.concept.system == codes.system
                for m in self.active_medications
            )
        wanted = set(codes)
        return any(m.concept.code in wanted for m in self.active_medications)

    # ---- lab primitives ----

    def latest_lab(
        self,
        loinc_code: str,
        *,
        max_age_days: int | None = None,
    ) -> LabObservation | None:
        """Most recent observation for `loinc_code` on/before `as_of`.

        If `max_age_days` is provided, results older than that window
        are *not* returned (use `latest_lab_with_freshness` to
        distinguish "no lab" from "stale lab"). The unit on the
        returned observation is the source unit; pair this with
        `canonical_unit` if you need to compare numerically.
        """
        result, _is_stale = self._latest_lab_with_freshness(loinc_code, max_age_days)
        if _is_stale:
            return None
        return result

    def _latest_lab_with_freshness(
        self,
        loinc_code: str,
        max_age_days: int | None,
    ) -> tuple[LabObservation | None, bool]:
        """Return (latest_obs_or_None, is_stale).

        `is_stale=True` means an observation exists but it's older
        than the freshness window. `is_stale=False` with `obs=None`
        means no observation exists at all.
        """
        obs = self.patient.latest_observation(loinc_code, self.as_of)
        if obs is None:
            return None, False
        if max_age_days is None:
            return obs, False
        age = (self.as_of - obs.effective_date).days
        if age > max_age_days:
            return obs, True
        return obs, False

    def meets_threshold(
        self,
        loinc_code: str,
        op: ThresholdOp,
        value: float,
        unit: str,
        *,
        max_age_days: int | None = None,
    ) -> ThresholdResult:
        """Tri-state numeric threshold check.

        Returns:
        - `MEETS` / `DOES_NOT_MEET`: value comparison succeeded
        - `NO_DATA`: no observation exists on/before as_of
        - `STALE_DATA`: observation exists but is past `max_age_days`
        - `UNIT_MISMATCH`: observation's unit can't be normalized to
          match the threshold's unit (e.g. lab is mmol/mol, threshold
          is %); the matcher should not silently coerce.

        The function fails closed on units it doesn't recognize. Only
        when *both* the observation unit and the threshold unit
        canonicalize to the same string under the same LOINC do we
        actually compare values.
        """
        obs, is_stale = self._latest_lab_with_freshness(loinc_code, max_age_days)
        if obs is None:
            return ThresholdResult.NO_DATA
        if is_stale:
            return ThresholdResult.STALE_DATA
        obs_canonical = canonical_unit(loinc_code, obs.unit)
        threshold_canonical = canonical_unit(loinc_code, unit)
        if obs_canonical is None or threshold_canonical is None:
            return ThresholdResult.UNIT_MISMATCH
        if obs_canonical != threshold_canonical:
            return ThresholdResult.UNIT_MISMATCH
        if _compare(obs.value, op, value):
            return ThresholdResult.MEETS
        return ThresholdResult.DOES_NOT_MEET


def _compare(left: float, op: ThresholdOp, right: float) -> bool:
    if op == "<":
        return left < right
    if op == "<=":
        return left <= right
    if op == ">":
        return left > right
    if op == ">=":
        return left >= right
    return left == right


# ---- helpers for tests / scripts ----


def days_between(earlier: date, later: date) -> int:
    """Inclusive day count, useful when authoring freshness windows."""
    return (later - earlier).days


def freshness_window_days(*, days: int = 0, weeks: int = 0, months: int = 0) -> int:
    """Convenience for stating freshness in clinical terms.

    Months are treated as 30-day buckets — close enough for matcher
    coarse-grained windows, and a clinician reviewing the policy can
    spot a rounding-induced edge case if it ever matters.
    """
    return days + weeks * 7 + months * 30


__all__ = [
    "ConceptSet",
    "PatientProfile",
    "ThresholdOp",
    "ThresholdResult",
    "canonical_unit",
    "days_between",
    "freshness_window_days",
]
