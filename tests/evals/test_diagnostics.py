"""Tests for D-69 slice-5 diagnostic rollups."""

from __future__ import annotations

from datetime import datetime

from clinical_demo.evals.diagnostics import build_diagnostics, render_diagnostics
from clinical_demo.evals.run import CaseRecord, EvalCase, RunResult
from clinical_demo.matcher import MATCHER_VERSION
from clinical_demo.matcher.verdict import MatchVerdict
from tests.matcher._fixtures import crit_condition, crit_medication

from ._fixtures import AS_OF, make_score_pair_result


def _case(pair_id: str = "p1__T1") -> EvalCase:
    return EvalCase(
        pair_id=pair_id,
        patient_id="p1",
        nct_id="T1",
        as_of=AS_OF,
        slice="slice-a",
    )


def _run(records: list[CaseRecord], *, notes: str = "diagnostic test") -> RunResult:
    return RunResult(
        started_at=datetime(2025, 1, 1, 0, 0, 0),
        finished_at=datetime(2025, 1, 1, 0, 0, 1),
        dataset_path="seed.json",
        notes=notes,
        cases=records,
    )


def _verdict(
    criterion,
    *,
    verdict: str = "indeterminate",
    reason: str = "unmapped_concept",
) -> MatchVerdict:
    return MatchVerdict(
        criterion=criterion,
        verdict=verdict,  # type: ignore[arg-type]
        reason=reason,  # type: ignore[arg-type]
        rationale="test",
        evidence=[],
        matcher_version=MATCHER_VERSION,
    )


def test_build_diagnostics_counts_verdicts_reasons_and_unmapped_surfaces() -> None:
    result = make_score_pair_result(
        verdicts=[
            _verdict(crit_condition(text="rare disease")),
            _verdict(crit_condition(text="rare disease")),
            _verdict(crit_condition(text="type 2 diabetes"), verdict="fail", reason="ok"),
        ]
    )
    run = _run([CaseRecord(case=_case(), result=result, scoring_latency_ms=25.0)])

    report = build_diagnostics(run)

    assert report.total_criteria == 3
    assert report.unmapped_count == 2
    assert report.unmapped_rate == 2 / 3
    assert report.reason_counts["ok"] == 1
    assert report.top_unmapped_surfaces[0].surface == "rare disease"
    assert report.top_unmapped_surfaces[0].count == 2


def test_build_diagnostics_tracks_registered_binding_resolution() -> None:
    result = make_score_pair_result(
        verdicts=[
            _verdict(crit_condition(text="type 2 diabetes"), verdict="fail", reason="ok"),
            _verdict(crit_medication(text="metformin")),
            _verdict(crit_medication(text="unknown drug")),
        ]
    )
    run = _run([CaseRecord(case=_case(), result=result)])

    report = build_diagnostics(run)

    assert report.binding_registered_total == 2
    assert report.binding_registered_resolved == 1
    assert report.binding_registered_unmapped == 1
    assert report.binding_registered_by_kind == {
        "condition": {"resolved": 1},
        "medication": {"unmapped": 1},
    }


def test_render_diagnostics_includes_baseline_deltas() -> None:
    current = build_diagnostics(
        _run(
            [
                CaseRecord(
                    case=_case(),
                    result=make_score_pair_result(
                        verdicts=[
                            _verdict(crit_condition(text="rare disease")),
                            _verdict(
                                crit_condition(text="type 2 diabetes"), verdict="fail", reason="ok"
                            ),
                        ]
                    ),
                )
            ],
            notes="current",
        )
    )
    baseline = build_diagnostics(
        _run(
            [
                CaseRecord(
                    case=_case(),
                    result=make_score_pair_result(
                        verdicts=[
                            _verdict(crit_condition(text="rare disease")),
                            _verdict(crit_condition(text="another disease")),
                            _verdict(
                                crit_condition(text="type 2 diabetes"), verdict="fail", reason="ok"
                            ),
                        ]
                    ),
                )
            ],
            notes="baseline",
        )
    )

    out = render_diagnostics(current, baseline=baseline)

    assert "D-69 slice-5 diagnostics" in out
    assert "unmapped_concept" in out
    assert "delta=-1" in out
    assert "registered terminology surfaces" in out
