"""Shared fixtures for the evals tests: synthetic ScorePairResult."""

from __future__ import annotations

from datetime import date

from clinical_demo.extractor.schema import (
    ExtractedCriteria,
    ExtractionMetadata,
    ExtractorRunMeta,
)
from clinical_demo.matcher import MATCHER_VERSION
from clinical_demo.matcher.verdict import MatchVerdict, Verdict
from clinical_demo.scoring.score_pair import (
    EligibilityRollup,
    ScorePairResult,
    ScoringSummary,
)
from tests.matcher._fixtures import crit_age, crit_sex

AS_OF = date(2025, 1, 1)


def make_age_verdict(
    *,
    minimum_years: float | None = 18.0,
    maximum_years: float | None = None,
    verdict: Verdict = "pass",
) -> MatchVerdict:
    return MatchVerdict(
        criterion=crit_age(minimum_years=minimum_years, maximum_years=maximum_years),
        verdict=verdict,
        reason="ok",
        rationale="age check",
        evidence=[],
        matcher_version=MATCHER_VERSION,
    )


def make_sex_verdict(*, sex: str = "MALE", verdict: Verdict = "pass") -> MatchVerdict:
    return MatchVerdict(
        criterion=crit_sex(sex=sex),
        verdict=verdict,
        reason="ok",
        rationale="sex check",
        evidence=[],
        matcher_version=MATCHER_VERSION,
    )


def make_score_pair_result(
    *,
    patient_id: str = "P-1",
    nct_id: str = "NCT00000001",
    eligibility: EligibilityRollup = "pass",
    total: int = 3,
    pass_count: int = 3,
    fail_count: int = 0,
    indeterminate_count: int = 0,
    cost_usd: float | None = 0.0001,
    input_tokens: int = 100,
    output_tokens: int = 50,
    verdicts: list[MatchVerdict] | None = None,
) -> ScorePairResult:
    """Synthetic ScorePairResult sufficient for evals tests.

    Verdicts are intentionally empty: the eval harness summarises
    via the `summary` counts (which the matcher would normally
    compute), so we hand-build the counts to match the totals
    rather than constructing real verdict pydantics."""
    return ScorePairResult(
        patient_id=patient_id,
        nct_id=nct_id,
        as_of=AS_OF,
        extraction=ExtractedCriteria(
            criteria=[],
            metadata=ExtractionMetadata(notes="test fixture"),
        ),
        extraction_meta=ExtractorRunMeta(
            model="test-model",
            prompt_version="extractor-test",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=0,
            cost_usd=cost_usd,
            latency_ms=12.5,
        ),
        verdicts=verdicts or [],
        summary=ScoringSummary(
            total_criteria=total,
            by_verdict={
                "pass": pass_count,
                "fail": fail_count,
                "indeterminate": indeterminate_count,
            },
            by_reason={},
            by_polarity={},
        ),
        eligibility=eligibility,
    )
