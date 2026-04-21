"""End-to-end scoring entry: extractor + matcher → per-criterion verdicts.

`score_pair(patient, trial, as_of)` is the seam the CLI script and
the eventual web/API surface both call. It does the smallest useful
amount of orchestration — extract criteria, match each one, roll up
to a top-level eligibility — and returns a structured envelope that
the caller renders / persists / sends downstream.

Why a top-level rollup at all?
------------------------------
The matcher emits per-criterion verdicts. Reviewers (and any caller
that wants a single answer) need a "what's the bottom line" signal.
v0 uses a deliberately conservative rule (D-38):

  - Any `fail` criterion → eligibility = `fail`.
  - Otherwise, any `indeterminate` → eligibility = `indeterminate`.
  - Otherwise → `pass`.

This mirrors the clinical screening reality (one missed exclusion is
disqualifying) and is exactly the surface a Phase-2 critic loop will
refine ("override an unmapped-concept indeterminate with a
high-confidence textual match," etc.).

Why ScorePairResult is a single envelope, not a tuple
-----------------------------------------------------
Every consumer wants the verdicts plus the run metadata: the CLI
needs cost to print, the eval harness needs prompt+matcher version
to attribute regressions, the reviewer UI needs the trial+patient
ids to render headers. Bundling them in one Pydantic model means
each consumer picks what it needs without an ad-hoc tuple unpacking
contract.
"""

from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Literal

from pydantic import BaseModel

from ..domain.patient import Patient
from ..domain.trial import Trial
from ..extractor.extractor import ExtractionResult, extract_criteria
from ..extractor.schema import ExtractedCriteria, ExtractorRunMeta
from ..matcher import MatchVerdict, match_extracted
from ..profile import PatientProfile

EligibilityRollup = Literal["pass", "fail", "indeterminate"]


class ScoringSummary(BaseModel):
    """Counts derived from the per-criterion verdicts.

    Persisted alongside the verdicts so a regression dashboard can
    pivot on summary counts (e.g. "matcher's `unmapped_concept` rate
    on this slice jumped 30% after extractor-v0.2") without
    re-aggregating from raw verdict lists every time.
    """

    total_criteria: int
    by_verdict: dict[str, int]
    by_reason: dict[str, int]
    by_polarity: dict[str, int]


class ScorePairResult(BaseModel):
    """The full result of scoring one (patient, trial) pair."""

    patient_id: str
    nct_id: str
    as_of: date
    extraction: ExtractedCriteria
    extraction_meta: ExtractorRunMeta
    verdicts: list[MatchVerdict]
    summary: ScoringSummary
    eligibility: EligibilityRollup


def score_pair(
    patient: Patient,
    trial: Trial,
    as_of: date,
    *,
    extraction: ExtractionResult | None = None,
) -> ScorePairResult:
    """Score one patient against one trial end-to-end.

    Parameters
    ----------
    patient : Patient
        Domain patient (loaded via `data.synthea.load_bundle` or
        equivalent).
    trial : Trial
        Domain trial (loaded via `data.clinicaltrials.trial_from_raw`).
    as_of : date
        The date the eligibility decision is being evaluated against.
        Drives age, lab freshness, condition activity, etc.
    extraction : ExtractionResult, optional
        Pre-computed extraction. If provided, skip the LLM call —
        useful for replay / caching, evals, and offline tests. If
        None, calls `extract_criteria(trial.eligibility_text)`.
    """
    if extraction is None:
        extraction = extract_criteria(trial.eligibility_text)

    profile = PatientProfile(patient, as_of)
    verdicts = match_extracted(extraction.extracted.criteria, profile, trial)

    return ScorePairResult(
        patient_id=patient.patient_id,
        nct_id=trial.nct_id,
        as_of=as_of,
        extraction=extraction.extracted,
        extraction_meta=extraction.meta,
        verdicts=verdicts,
        summary=_summarize(verdicts),
        eligibility=_rollup(verdicts),
    )


def _rollup(verdicts: list[MatchVerdict]) -> EligibilityRollup:
    """Conservative top-level eligibility:
    any fail wins; else any indeterminate wins; else pass.

    Empty verdict lists collapse to `pass` — vacuously true, but
    callers should check for the empty case themselves before
    trusting that as a positive signal."""
    statuses = {v.verdict for v in verdicts}
    if "fail" in statuses:
        return "fail"
    if "indeterminate" in statuses:
        return "indeterminate"
    return "pass"


def _summarize(verdicts: list[MatchVerdict]) -> ScoringSummary:
    """Roll the per-criterion verdicts into the counts the dashboard
    and the CLI summary printer want."""
    # Cast the Counter keys to plain str on the way out so the
    # ScoringSummary's API doesn't leak the closed Literal types of
    # the upstream enums into every consumer's type signature.
    by_verdict: Counter[str] = Counter(str(v.verdict) for v in verdicts)
    by_reason: Counter[str] = Counter(str(v.reason) for v in verdicts)
    by_polarity: Counter[str] = Counter(str(v.criterion.polarity) for v in verdicts)
    return ScoringSummary(
        total_criteria=len(verdicts),
        by_verdict=dict(by_verdict),
        by_reason=dict(by_reason),
        by_polarity=dict(by_polarity),
    )


__all__ = [
    "EligibilityRollup",
    "ScorePairResult",
    "ScoringSummary",
    "score_pair",
]
