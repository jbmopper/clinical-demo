"""Curate a working patient cohort for the demo.

Pure functions over an in-memory list of `Patient` objects. The companion
script (`scripts/curate_cohort.py`) handles loading, persistence, and
logging.

Policy summary (see PLAN.md D-15):

- Eligible pool = patients with at least one *cardiometabolic* SNOMED
  Condition active as-of the curation date (`age_years` evaluated on
  the same date).
- Score each eligible patient. Higher = richer matching candidate:

      score = 2 * (# distinct non-prediabetes cardiometabolic conditions)
            +     (# distinct prediabetes-only conditions)

  The 2x weight rewards conditions that yield positive matches against
  our curated trials (T2DM, HTN, hyperlipidemia). Prediabetes counts at
  half-weight: useful as near-miss / exclusion-trigger cases without
  dominating the cohort.

- Adult & non-centenarian: 18 ≤ age ≤ 95.

- Take the top `target_size` by `(score desc, age desc, patient_id asc)`
  for deterministic ordering. Age desc as tiebreaker because older
  patients tend to have richer longitudinal records.

Why these specific codes are the cardiometabolic set: D-15 in PLAN.md
documents the rationale, including why CKD codes are excluded (Synthea
emits ~12 CKD patients across the entire sample, not enough to slice).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from clinical_demo.domain import Patient

# SNOMED codes for the cardiometabolic phenotype we curate against.
# Two buckets: "core" conditions yield 2x scoring weight; prediabetes is
# half-weight to keep it represented but not dominant. These are the same
# codes used by the marimo exploration notebook and the data profile
# captured in PLAN.md.
CORE_CARDIOMETABOLIC: dict[str, str] = {
    "44054006": "Type 2 diabetes mellitus",
    "73211009": "Diabetes mellitus (unspecified)",
    "59621000": "Essential hypertension",
    "38341003": "Hypertensive disorder",
    "55822004": "Hyperlipidemia",
    "267432004": "Pure hypercholesterolemia",
}
PREDIABETES: dict[str, str] = {
    "15777000": "Prediabetes",
}
ALL_CARDIOMETABOLIC: dict[str, str] = {**CORE_CARDIOMETABOLIC, **PREDIABETES}

DEFAULT_MIN_AGE = 18
DEFAULT_MAX_AGE = 95


@dataclass(frozen=True)
class CohortMember:
    """A patient selected into the working cohort, plus the bookkeeping
    needed to render an offline-readable manifest."""

    patient_id: str
    age: int
    sex: str
    score: int
    cardiometabolic_codes: list[str]
    cardiometabolic_labels: list[str]
    has_hba1c: bool
    has_ldl: bool
    has_egfr: bool
    has_systolic_bp: bool


def cardiometabolic_codes(patient: Patient, as_of: date) -> set[str]:
    """Return the set of cardiometabolic SNOMED codes the patient has
    active as-of the given date."""
    return {
        c.concept.code
        for c in patient.active_conditions(as_of)
        if c.concept.code in ALL_CARDIOMETABOLIC
    }


def score_patient(codes: set[str]) -> int:
    """Score a patient by the cardiometabolic codes they carry.

    Pure function so it's trivial to test and to reason about ranking
    differences when we tune the policy.
    """
    core = len(codes & set(CORE_CARDIOMETABOLIC))
    prediabetes = len(codes & set(PREDIABETES))
    return 2 * core + prediabetes


def curate(
    patients: Iterable[Patient],
    *,
    as_of: date,
    target_size: int = 150,
    min_age: int = DEFAULT_MIN_AGE,
    max_age: int = DEFAULT_MAX_AGE,
) -> list[CohortMember]:
    """Select up to `target_size` patients for the working cohort.

    Patients with no cardiometabolic conditions or out of age range are
    dropped. Remaining patients are ranked by `(score, age, patient_id)`
    descending-descending-ascending and the top `target_size` returned.
    Returning fewer than `target_size` is allowed and expected if the
    input pool is small (e.g., the test fixtures).
    """
    candidates: list[CohortMember] = []
    for p in patients:
        codes = cardiometabolic_codes(p, as_of)
        if not codes:
            continue
        age = p.age_years(as_of)
        if age < min_age or age > max_age:
            continue
        candidates.append(_build_member(p, codes, age, as_of))

    candidates.sort(
        key=lambda m: (-m.score, -m.age, m.patient_id),
    )
    return candidates[:target_size]


def _build_member(patient: Patient, codes: set[str], age: int, as_of: date) -> CohortMember:
    sorted_codes = sorted(codes)
    return CohortMember(
        patient_id=patient.patient_id,
        age=age,
        sex=patient.sex,
        score=score_patient(codes),
        cardiometabolic_codes=sorted_codes,
        cardiometabolic_labels=[ALL_CARDIOMETABOLIC[c] for c in sorted_codes],
        has_hba1c=patient.latest_observation("4548-4", as_of) is not None,
        has_ldl=patient.latest_observation("18262-6", as_of) is not None,
        has_egfr=patient.latest_observation("33914-3", as_of) is not None,
        has_systolic_bp=patient.latest_observation("8480-6", as_of) is not None,
    )
