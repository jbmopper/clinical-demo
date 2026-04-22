"""Tests for layer-1 alignment + agreement/coverage metrics.

The unit-of-meaning is `LayerOneCell`. Tests pin:
  - per-field alignment rules pick the right MatchVerdict
  - missing extractions become `missing` cells (not silent zeros)
  - multi-match: deterministic first-match-wins (with bound/sex tiebreaks)
  - uncoverable seed fields are skipped + counted (`healthy_volunteers`)
  - failed scorer cases are skipped + counted, never crash the report
  - agreement excludes missing; coverage includes them
  - renderer produces something that contains the expected numbers
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from clinical_demo.evals.layer_one import (
    COVERED_FIELDS,
    LayerOneCell,
    build_layer_one_report,
)
from clinical_demo.evals.report_layer_one import render_layer_one
from clinical_demo.evals.run import CaseRecord, EvalCase, RunResult

from ._fixtures import (
    AS_OF,
    make_age_verdict,
    make_score_pair_result,
    make_sex_verdict,
)


def _seed_cell(field: str, expected: str, verdict: str) -> dict[str, Any]:
    return {
        "criterion": {"field": field, "expected": expected, "source_text": expected},
        "verdict": verdict,
        "rationale": "test",
        "method": "mechanical",
    }


def _case(
    pair_id: str,
    expected_structured: list[dict[str, Any]],
    *,
    patient_id: str = "P",
    nct_id: str = "NCT",
) -> EvalCase:
    return EvalCase(
        pair_id=pair_id,
        patient_id=patient_id,
        nct_id=nct_id,
        as_of=AS_OF,
        slice="test",
        expected_structured=expected_structured,
    )


def _run(records: list[CaseRecord]) -> RunResult:
    return RunResult(
        started_at=datetime(2025, 1, 1, 0, 0, 0),
        finished_at=datetime(2025, 1, 1, 0, 0, 1),
        dataset_path="seed.json",
        notes="layer-1 test",
        cases=records,
    )


# ---------------- per-field alignment


def test_min_age_agree_when_matcher_passes() -> None:
    case = _case("p1", [_seed_cell("min_age", ">= 18 Years", "pass")])
    result = make_score_pair_result(verdicts=[make_age_verdict(minimum_years=18.0, verdict="pass")])
    report = build_layer_one_report(_run([CaseRecord(case=case, result=result)]))
    cells = report.cells
    assert len(cells) == 1
    assert cells[0].status == "agree"
    assert cells[0].matcher_verdict == "pass"


def test_min_age_disagree_when_matcher_fails() -> None:
    case = _case("p1", [_seed_cell("min_age", ">= 18 Years", "pass")])
    result = make_score_pair_result(verdicts=[make_age_verdict(minimum_years=18.0, verdict="fail")])
    report = build_layer_one_report(_run([CaseRecord(case=case, result=result)]))
    assert report.cells[0].status == "disagree"


def test_min_age_missing_when_no_age_criterion_extracted() -> None:
    case = _case("p1", [_seed_cell("min_age", ">= 18 Years", "pass")])
    result = make_score_pair_result(verdicts=[])
    report = build_layer_one_report(_run([CaseRecord(case=case, result=result)]))
    assert report.cells[0].status == "missing"
    assert report.cells[0].matcher_verdict is None


def test_max_age_picks_age_with_max_bound_over_min_only() -> None:
    """Multi-age extraction: if one criterion only has minimum_years
    and another has maximum_years, max_age cell aligns to the one
    with maximum_years (not blindly the first)."""
    case = _case("p1", [_seed_cell("max_age", "<= 75 Years", "pass")])
    result = make_score_pair_result(
        verdicts=[
            make_age_verdict(minimum_years=18.0, maximum_years=None, verdict="pass"),
            make_age_verdict(minimum_years=None, maximum_years=75.0, verdict="fail"),
        ]
    )
    report = build_layer_one_report(_run([CaseRecord(case=case, result=result)]))
    assert report.cells[0].status == "disagree"
    assert report.cells[0].matcher_verdict == "fail"


def test_sex_prefers_matching_value_then_first() -> None:
    """When multiple sex criteria are extracted, prefer the one
    whose .sex.sex matches the seed's expected; otherwise first."""
    case_match = _case("p1", [_seed_cell("sex", "FEMALE", "pass")])
    result_match = make_score_pair_result(
        verdicts=[
            make_sex_verdict(sex="MALE", verdict="fail"),
            make_sex_verdict(sex="FEMALE", verdict="pass"),
        ]
    )
    report = build_layer_one_report(_run([CaseRecord(case=case_match, result=result_match)]))
    assert report.cells[0].status == "agree"
    assert report.cells[0].matcher_verdict == "pass"

    case_nomatch = _case("p2", [_seed_cell("sex", "FEMALE", "pass")])
    result_nomatch = make_score_pair_result(verdicts=[make_sex_verdict(sex="MALE", verdict="fail")])
    report2 = build_layer_one_report(_run([CaseRecord(case=case_nomatch, result=result_nomatch)]))
    assert report2.cells[0].status == "disagree"


# ---------------- skipped cells


def test_healthy_volunteers_is_uncoverable_in_v0() -> None:
    case = _case(
        "p1",
        [
            _seed_cell("healthy_volunteers", "no active conditions", "fail"),
            _seed_cell("min_age", ">= 18 Years", "pass"),
        ],
    )
    result = make_score_pair_result(verdicts=[make_age_verdict(minimum_years=18.0, verdict="pass")])
    report = build_layer_one_report(_run([CaseRecord(case=case, result=result)]))
    assert len(report.cells) == 1  # the min_age cell
    assert report.cells[0].field == "min_age"
    assert report.skipped_uncoverable == {"healthy_volunteers": 1}


def test_failed_scorer_cases_are_skipped_with_count() -> None:
    case = _case("p1", [_seed_cell("min_age", ">= 18 Years", "pass")])
    record = CaseRecord(case=case, result=None, error="boom")
    report = build_layer_one_report(_run([record]))
    assert report.cells == []
    assert report.skipped_failed_cases == 1


# ---------------- aggregate metrics


def test_overall_agreement_excludes_missing() -> None:
    """Missing cells should land in coverage, not depress agreement."""
    case = _case(
        "p1",
        [
            _seed_cell("min_age", ">= 18 Years", "pass"),  # agree
            _seed_cell("max_age", "<= 75 Years", "pass"),  # missing
            _seed_cell("sex", "MALE", "pass"),  # disagree
        ],
    )
    result = make_score_pair_result(
        verdicts=[
            make_age_verdict(minimum_years=18.0, verdict="pass"),
            make_sex_verdict(sex="MALE", verdict="fail"),
        ]
    )
    report = build_layer_one_report(_run([CaseRecord(case=case, result=result)]))
    statuses = {c.field: c.status for c in report.cells}
    assert statuses == {"min_age": "agree", "max_age": "missing", "sex": "disagree"}
    assert report.overall_agreement == 0.5  # 1 agree / 2 scored
    assert report.overall_coverage == 2 / 3


def test_field_stats_present_for_every_covered_field() -> None:
    """Even when no cells exist for a field, the per-field row is
    present so the report shows the empty field instead of silently
    omitting it."""
    case = _case("p1", [_seed_cell("min_age", ">= 18 Years", "pass")])
    result = make_score_pair_result(verdicts=[make_age_verdict(minimum_years=18.0, verdict="pass")])
    report = build_layer_one_report(_run([CaseRecord(case=case, result=result)]))
    fields = [s.field for s in report.field_stats]
    assert fields == list(COVERED_FIELDS)
    rates = {s.field: s.agreement_rate for s in report.field_stats}
    assert rates["min_age"] == 1.0
    assert rates["max_age"] is None
    assert rates["sex"] is None


def test_field_stats_counts_aggregate_correctly() -> None:
    case_a = _case("p1", [_seed_cell("min_age", ">= 18 Years", "pass")])
    case_b = _case("p2", [_seed_cell("min_age", ">= 18 Years", "fail")])
    case_c = _case("p3", [_seed_cell("min_age", ">= 18 Years", "pass")])
    report = build_layer_one_report(
        _run(
            [
                CaseRecord(
                    case=case_a,
                    result=make_score_pair_result(verdicts=[make_age_verdict(verdict="pass")]),
                ),
                CaseRecord(
                    case=case_b,
                    result=make_score_pair_result(verdicts=[make_age_verdict(verdict="fail")]),
                ),
                CaseRecord(
                    case=case_c,
                    result=make_score_pair_result(verdicts=[]),
                ),
            ]
        )
    )
    min_age = next(s for s in report.field_stats if s.field == "min_age")
    assert (min_age.agree, min_age.disagree, min_age.missing) == (2, 0, 1)
    assert min_age.agreement_rate == 1.0
    assert min_age.coverage_rate == 2 / 3


# ---------------- renderer smoke


def test_render_layer_one_includes_summary_and_disagreements() -> None:
    case = _case(
        "p1",
        [
            _seed_cell("min_age", ">= 18 Years", "pass"),
            _seed_cell("sex", "MALE", "pass"),
            _seed_cell("healthy_volunteers", "n/a", "fail"),
        ],
    )
    result = make_score_pair_result(
        verdicts=[
            make_age_verdict(minimum_years=18.0, verdict="pass"),
            make_sex_verdict(sex="MALE", verdict="fail"),
        ]
    )
    report = build_layer_one_report(_run([CaseRecord(case=case, result=result)]))
    out = render_layer_one(report)
    assert "Layer-1 report" in out
    assert "min_age" in out
    assert "sex" in out
    assert "max_age" in out
    assert "disagreements" in out
    assert "healthy_volunteers=1" in out


def test_render_layer_one_handles_empty_report() -> None:
    report = build_layer_one_report(_run([]))
    out = render_layer_one(report)
    assert "Layer-1 report" in out
    # All three covered fields appear with n/a for empty rates.
    assert "n/a" in out


# ---------------- LayerOneCell shape sanity


def test_cell_carries_seed_and_matcher_context_for_eyeballing() -> None:
    case = _case("p1", [_seed_cell("min_age", ">= 18 Years", "pass")])
    result = make_score_pair_result(verdicts=[make_age_verdict(minimum_years=18.0, verdict="fail")])
    report = build_layer_one_report(_run([CaseRecord(case=case, result=result)]))
    cell = report.cells[0]
    assert isinstance(cell, LayerOneCell)
    assert cell.seed_expected == ">= 18 Years"
    assert cell.matcher_source_text == "age criterion"
