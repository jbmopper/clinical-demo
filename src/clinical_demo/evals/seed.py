"""Eval seed-set: domain types + the mechanical pre-labeler.

The seed set is the ground truth backbone for the matcher's evaluation.
For each (patient, trial) pair we record per-criterion verdicts. Two
sources of verdicts coexist:

- **Mechanical**: produced by this module by checking *structured*
  fields the trial source already gives us (minimum_age,
  maximum_age, sex, conditions, healthy_volunteers) against the
  patient's typed record. These are the easy, defensible labels.
- **Human review**: required for criteria that live in the trial's
  free-text eligibility blob (e.g. "investigator deems suitable",
  "active substance use", clinical judgement criteria). The seed set
  records *how many* of these are owed per pair so the eval consumer
  can weight pairs honestly and the writeup can be transparent about
  what was and wasn't human-validated.

This split is deliberate. Pretending we labeled judgement criteria
without a clinician is the single worst thing we could do to the
project's credibility — the matcher would learn to game our wrong
labels, and any reviewer with a medical background would spot it.

The mechanical labeler is intentionally narrow. It only emits verdicts
for fields the trial source structures for us. Free-text criteria are
*counted*, not labeled.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel

from clinical_demo.domain.patient import Patient
from clinical_demo.domain.trial import Trial

CriterionField = Literal[
    "min_age",
    "max_age",
    "sex",
    "required_condition",
    "healthy_volunteers",
]
Verdict = Literal["pass", "fail", "indeterminate"]
LabelMethod = Literal["mechanical", "human_review"]
ReviewStatus = Literal["pending", "complete"]


class StructuredCriterion(BaseModel):
    """A criterion derived directly from a trial's structured fields.

    `source_text` quotes the verbatim source value (e.g. `"18 Years"`)
    so a reviewer can audit the verdict without re-fetching the API.
    Free-text criteria are not represented here — they are out of
    scope for the mechanical pass.
    """

    field: CriterionField
    expected: str
    source_text: str


class CriterionVerdict(BaseModel):
    """A per-criterion verdict for one (patient, trial) pair.

    `method` records how the verdict was produced. Eval consumers
    that want strict ground truth should filter to
    `method == "human_review"`; consumers that want the broadest
    coverage can include both.
    """

    criterion: StructuredCriterion
    verdict: Verdict
    rationale: str
    method: LabelMethod
    reviewed_by: str | None = None
    reviewed_at: date | None = None


class EvalPair(BaseModel):
    """One (patient, trial) pair in the eval seed set.

    `free_text_criteria_count` is a coarse estimate (one per
    bullet/line in the inclusion+exclusion text) of how many criteria
    a real reviewer still owes labels for. `free_text_review_status`
    flips to `"complete"` once those labels are in place.
    """

    pair_id: str
    patient_id: str
    nct_id: str
    slice: str
    structured_verdicts: list[CriterionVerdict]
    free_text_criteria_count: int
    free_text_review_status: ReviewStatus = "pending"
    notes: str = ""


class SelectionPolicy(BaseModel):
    """Captured in the manifest so seed-set construction is reproducible."""

    target_pairs: int
    pairs_per_slice_target: int
    max_pairs_per_patient: int
    cohort_score_min: int
    require_lab_coverage_for_threshold_trials: bool
    description: str


class EvalSeed(BaseModel):
    """Top-level seed-set manifest persisted to disk."""

    cohort_manifest_path: str
    trials_manifest_path: str
    as_of: date
    generated_at: datetime
    selection_policy: SelectionPolicy
    pairs: list[EvalPair]


# ---------- mechanical pre-labeler ----------

_AGE_PATTERN = re.compile(
    r"^(\d+)\s*(year|years|month|months|week|weeks|day|days)?\s*$",
    re.IGNORECASE,
)


def parse_age_years(raw: str | None) -> int | None:
    """Parse a CT.gov age string ('18 Years', '6 Months', None) to whole years.

    Returns None for missing / unparseable values so callers can record
    an `indeterminate` verdict rather than crash. Sub-year units round
    down to 0 — clinically defensible for adult-trial use, and we
    never rely on this for pediatric trials anyway.
    """
    if not raw:
        return None
    m = _AGE_PATTERN.match(raw.strip())
    if not m:
        return None
    n = int(m.group(1))
    unit = (m.group(2) or "years").lower().rstrip("s")
    if unit == "year":
        return n
    if unit == "month":
        return n // 12
    if unit in {"week", "day"}:
        return 0
    return None


def label_min_age(patient: Patient, trial: Trial, as_of: date) -> CriterionVerdict | None:
    """Verdict for the trial's minimum-age requirement, if it has one."""
    if not trial.minimum_age:
        return None
    expected_years = parse_age_years(trial.minimum_age)
    crit = StructuredCriterion(
        field="min_age",
        expected=f">= {trial.minimum_age}",
        source_text=trial.minimum_age,
    )
    if expected_years is None:
        return CriterionVerdict(
            criterion=crit,
            verdict="indeterminate",
            rationale=f"could not parse minimum_age={trial.minimum_age!r}",
            method="mechanical",
        )
    patient_age = patient.age_years(as_of)
    passed = patient_age >= expected_years
    return CriterionVerdict(
        criterion=crit,
        verdict="pass" if passed else "fail",
        rationale=f"patient age {patient_age} vs. min {expected_years}",
        method="mechanical",
    )


def label_max_age(patient: Patient, trial: Trial, as_of: date) -> CriterionVerdict | None:
    """Verdict for the trial's maximum-age requirement, if it has one."""
    if not trial.maximum_age:
        return None
    expected_years = parse_age_years(trial.maximum_age)
    crit = StructuredCriterion(
        field="max_age",
        expected=f"<= {trial.maximum_age}",
        source_text=trial.maximum_age,
    )
    if expected_years is None:
        return CriterionVerdict(
            criterion=crit,
            verdict="indeterminate",
            rationale=f"could not parse maximum_age={trial.maximum_age!r}",
            method="mechanical",
        )
    patient_age = patient.age_years(as_of)
    passed = patient_age <= expected_years
    return CriterionVerdict(
        criterion=crit,
        verdict="pass" if passed else "fail",
        rationale=f"patient age {patient_age} vs. max {expected_years}",
        method="mechanical",
    )


def label_sex(patient: Patient, trial: Trial) -> CriterionVerdict | None:
    """Verdict for the trial's sex restriction.

    Trials with `sex=ALL` get no verdict (no constraint to check).
    Trials restricted to MALE or FEMALE produce pass/fail; any other
    patient sex (e.g. 'unknown', 'other') is `indeterminate`.
    """
    trial_sex = trial.sex.upper()
    if trial_sex == "ALL":
        return None
    crit = StructuredCriterion(
        field="sex",
        expected=trial_sex,
        source_text=trial.sex,
    )
    patient_sex = patient.sex.lower()
    if trial_sex in {"MALE", "FEMALE"} and patient_sex in {"male", "female"}:
        passed = patient_sex == trial_sex.lower()
        return CriterionVerdict(
            criterion=crit,
            verdict="pass" if passed else "fail",
            rationale=f"patient sex {patient_sex!r} vs. trial requires {trial_sex!r}",
            method="mechanical",
        )
    return CriterionVerdict(
        criterion=crit,
        verdict="indeterminate",
        rationale=(
            f"non-binary or unknown patient sex {patient_sex!r}; trial restricts to {trial_sex!r}"
        ),
        method="mechanical",
    )


def label_healthy_volunteers(
    patient: Patient, trial: Trial, as_of: date
) -> CriterionVerdict | None:
    """Verdict for the 'healthy volunteers only' flag.

    If `healthy_volunteers=True`, any active clinical condition
    disqualifies the patient. We only emit a verdict when the trial
    actually sets the flag — most trials have it as False (default).
    """
    if not trial.healthy_volunteers:
        return None
    crit = StructuredCriterion(
        field="healthy_volunteers",
        expected="no active clinical conditions",
        source_text="healthy_volunteers=True",
    )
    active = patient.active_conditions(as_of)
    if not active:
        return CriterionVerdict(
            criterion=crit,
            verdict="pass",
            rationale="no active clinical conditions on as_of date",
            method="mechanical",
        )
    return CriterionVerdict(
        criterion=crit,
        verdict="fail",
        rationale=(
            f"patient has {len(active)} active clinical condition(s); "
            f"trial requires healthy volunteers"
        ),
        method="mechanical",
    )


def mechanical_verdicts(patient: Patient, trial: Trial, as_of: date) -> list[CriterionVerdict]:
    """All structured-field verdicts the mechanical labeler can produce.

    Returns an empty list if the trial has no structured restrictions
    that apply (e.g. no age bounds, sex=ALL, healthy_volunteers=False).
    """
    out: list[CriterionVerdict] = []
    for v in (
        label_min_age(patient, trial, as_of),
        label_max_age(patient, trial, as_of),
        label_sex(patient, trial),
        label_healthy_volunteers(patient, trial, as_of),
    ):
        if v is not None:
            out.append(v)
    return out


# ---------- free-text criterion accounting ----------

_BULLET_PATTERN = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+", re.MULTILINE)


def estimate_free_text_criteria(eligibility_text: str) -> int:
    """Crude estimate: count bullet/numbered lines in the eligibility blob.

    The estimate is intentionally conservative — the mechanical pass
    handled structured fields, and the count surfaces *how much*
    free-text the human reviewer still owes labels on. If no bullets
    are detected we fall back to one criterion per non-blank line,
    which over-counts but never under-counts (useful for honesty).
    """
    bullets = _BULLET_PATTERN.findall(eligibility_text)
    if bullets:
        return len(bullets)
    return sum(1 for line in eligibility_text.splitlines() if line.strip())
