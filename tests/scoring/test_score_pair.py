"""Tests for `score_pair` and the rollup / summary helpers.

The library entry stitches the extractor and matcher together; we
test it with an injected `ExtractionResult` so no LLM call is made.
The aggregation rules (rollup, summary counts) are small but
load-bearing — the rollup is the single signal a non-clinician
consumer of the system gets, and getting it wrong silently inverts
the demo.
"""

from __future__ import annotations

from datetime import date

from clinical_demo.extractor.extractor import ExtractionResult
from clinical_demo.extractor.schema import (
    ExtractedCriteria,
    ExtractedCriterion,
    ExtractionMetadata,
    ExtractorRunMeta,
)
from clinical_demo.scoring.score_pair import _rollup, _summarize, score_pair
from tests.matcher._fixtures import (
    AS_OF,
    crit_age,
    crit_condition,
    crit_free_text,
    crit_measurement,
    make_condition,
    make_lab,
    make_patient,
    make_trial,
)


def _make_extraction(criteria: list[ExtractedCriterion]) -> ExtractionResult:
    """Bundle a list of criteria into the same envelope the extractor
    would have produced. Uses zero costs so summary numbers don't
    drift across test runs."""
    return ExtractionResult(
        extracted=ExtractedCriteria(
            criteria=criteria,
            metadata=ExtractionMetadata(notes="test fixture"),
        ),
        meta=ExtractorRunMeta(
            model="test-model",
            prompt_version="extractor-test",
            input_tokens=0,
            output_tokens=0,
            cached_input_tokens=0,
            cost_usd=0.0,
            latency_ms=0.0,
        ),
    )


# ---------- rollup ----------


def test_rollup_pass_when_all_criteria_pass() -> None:
    """All `pass` criteria → top-level pass."""
    profile_p = make_patient(
        birth=date(1990, 1, 1),
        conditions=[make_condition(code="44054006")],
    )
    extraction = _make_extraction(
        [
            crit_age(minimum_years=18.0),
            crit_condition(text="type 2 diabetes"),
        ]
    )
    result = score_pair(profile_p, make_trial(), AS_OF, extraction=extraction)
    assert result.eligibility == "pass"


def test_rollup_fail_when_any_fail_overrides_passes_and_indeterminates() -> None:
    """Conservative rule: any single `fail` flips the whole rollup
    to `fail`, even when other criteria pass or are indeterminate."""
    patient = make_patient(birth=date(2010, 1, 1))  # underage
    extraction = _make_extraction(
        [
            crit_age(minimum_years=18.0),  # fails (underage)
            crit_free_text(),  # indeterminate
        ]
    )
    result = score_pair(patient, make_trial(), AS_OF, extraction=extraction)
    assert result.eligibility == "fail"


def test_rollup_indeterminate_when_no_fail_but_at_least_one_indeterminate() -> None:
    """No fails + ≥1 indeterminate → indeterminate. Passes alone
    aren't enough to claim a positive eligibility decision."""
    patient = make_patient(birth=date(1990, 1, 1))
    extraction = _make_extraction(
        [
            crit_age(minimum_years=18.0),  # passes
            crit_free_text(),  # indeterminate
        ]
    )
    result = score_pair(patient, make_trial(), AS_OF, extraction=extraction)
    assert result.eligibility == "indeterminate"


def test_rollup_pass_on_empty_verdicts_documents_vacuous_truth() -> None:
    """Empty extraction → vacuously `pass`. This is intentional and
    documented; callers should check `summary.total_criteria == 0`
    before trusting the rollup as a positive signal."""
    extraction = _make_extraction([])
    result = score_pair(make_patient(), make_trial(), AS_OF, extraction=extraction)
    assert result.eligibility == "pass"
    assert result.summary.total_criteria == 0


def test_rollup_helper_matches_truth_table() -> None:
    """Direct unit test of the helper, mirroring the integration
    cases above so we know which layer broke when something fails."""
    from clinical_demo.matcher.matcher import _build
    from clinical_demo.matcher.verdict import MatchVerdict

    def vd(status: str) -> MatchVerdict:
        crit = crit_free_text()
        return _build(
            crit,
            verdict=status,  # type: ignore[arg-type]
            reason="ok",
            rationale="",
            evidence=[],
        )

    assert _rollup([vd("pass")]) == "pass"
    assert _rollup([vd("pass"), vd("indeterminate")]) == "indeterminate"
    assert _rollup([vd("pass"), vd("indeterminate"), vd("fail")]) == "fail"
    assert _rollup([vd("indeterminate"), vd("fail")]) == "fail"
    assert _rollup([]) == "pass"


# ---------- summary ----------


def test_summary_counts_match_verdicts() -> None:
    patient = make_patient(
        birth=date(1990, 1, 1),
        conditions=[make_condition(code="44054006")],
        observations=[make_lab(value=8.0, unit="%")],
    )
    extraction = _make_extraction(
        [
            crit_age(minimum_years=18.0),
            crit_condition(text="type 2 diabetes"),
            crit_measurement(text="hba1c", operator=">=", value=7.0, unit="%"),
            crit_free_text(),
        ]
    )
    result = score_pair(patient, make_trial(), AS_OF, extraction=extraction)
    assert result.summary.total_criteria == 4
    assert result.summary.by_verdict.get("pass") == 3
    assert result.summary.by_verdict.get("indeterminate") == 1
    assert result.summary.by_polarity.get("inclusion") == 4


def test_summarize_helper_emits_expected_shape() -> None:
    """Directly probe the helper so a regression in the keys (verdict
    name change, etc.) breaks here too, not just in integration tests."""
    from clinical_demo.matcher.matcher import _build

    crit_inc = crit_age(minimum_years=18.0, polarity="inclusion")
    crit_exc = crit_age(minimum_years=18.0, polarity="exclusion")
    verdicts = [
        _build(crit_inc, verdict="pass", reason="ok", rationale="", evidence=[]),
        _build(
            crit_exc,
            verdict="indeterminate",
            reason="no_data",
            rationale="",
            evidence=[],
        ),
    ]
    summary = _summarize(verdicts)
    assert summary.total_criteria == 2
    assert summary.by_verdict == {"pass": 1, "indeterminate": 1}
    assert summary.by_reason == {"ok": 1, "no_data": 1}
    assert summary.by_polarity == {"inclusion": 1, "exclusion": 1}


# ---------- score_pair plumbing ----------


def test_score_pair_uses_injected_extraction_when_provided() -> None:
    """No LLM call should be made when an `extraction=` argument is
    passed. The presence of any network call would be a leak — we
    verify by passing the extraction and asserting the run meta
    survives unmodified."""
    extraction = _make_extraction([crit_age(minimum_years=18.0)])
    result = score_pair(
        make_patient(birth=date(1990, 1, 1)),
        make_trial(),
        AS_OF,
        extraction=extraction,
    )
    assert result.extraction_meta.model == "test-model"
    assert result.extraction_meta.prompt_version == "extractor-test"


def test_score_pair_carries_top_level_identifiers() -> None:
    """The returned envelope must carry `patient_id`, `nct_id`, and
    `as_of` so a downstream persister doesn't have to re-derive them."""
    patient = make_patient(birth=date(1990, 1, 1))
    trial = make_trial(nct_id="NCT99999999")
    result = score_pair(
        patient,
        trial,
        AS_OF,
        extraction=_make_extraction([crit_age(minimum_years=18.0)]),
    )
    assert result.patient_id == patient.patient_id
    assert result.nct_id == "NCT99999999"
    assert result.as_of == AS_OF
