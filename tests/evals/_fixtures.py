"""Shared fixtures for the evals tests: synthetic ScorePairResult."""

from __future__ import annotations

from datetime import date

from clinical_demo.extractor.schema import (
    ExtractedCriteria,
    ExtractionMetadata,
    ExtractorRunMeta,
)
from clinical_demo.scoring.score_pair import (
    EligibilityRollup,
    ScorePairResult,
    ScoringSummary,
)

AS_OF = date(2025, 1, 1)


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
        verdicts=[],
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
