"""Tests for the eval dataset adapter and orchestrator-agnostic runner.

The runner takes a Scorer callable; tests use synthetic scorers
so no LLM, no graph, no patient/trial loading. We pin:

  - dataset round-trip: seed JSON → EvalCases without losing data
  - filtering: pair_ids and limit
  - runner success path: every case scored, latency populated
  - runner failure tolerance: one bad case doesn't tank the run
  - on_case_done callback: invoked once per case, in order
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from clinical_demo.evals.run import (
    CaseRecord,
    EvalCase,
    load_dataset,
    run_eval,
)

from ._fixtures import make_score_pair_result


def _seed_payload() -> dict:
    return {
        "cohort_manifest_path": "data/curated/cohort_manifest.json",
        "trials_manifest_path": "data/curated/trials_manifest.json",
        "as_of": "2025-01-01",
        "generated_at": "2026-04-20T21:15:49.986143",
        "selection_policy": {
            "target_pairs": 3,
            "pairs_per_slice_target": 1,
            "max_pairs_per_patient": 2,
            "cohort_score_min": 1,
            "require_lab_coverage_for_threshold_trials": False,
            "description": "test fixture",
        },
        "pairs": [
            {
                "pair_id": "p1__T1",
                "patient_id": "p1",
                "nct_id": "T1",
                "slice": "slice-a",
                "structured_verdicts": [
                    {
                        "criterion": {
                            "field": "min_age",
                            "expected": ">= 18 Years",
                            "source_text": "18 Years",
                        },
                        "verdict": "pass",
                        "rationale": "patient age 51 vs. min 18",
                        "method": "mechanical",
                        "reviewed_by": None,
                        "reviewed_at": None,
                    },
                ],
                "free_text_criteria_count": 4,
                "free_text_review_status": "pending",
                "notes": "",
            },
            {
                "pair_id": "p2__T2",
                "patient_id": "p2",
                "nct_id": "T2",
                "slice": "slice-b",
                "structured_verdicts": [],
                "free_text_criteria_count": 1,
                "free_text_review_status": "complete",
                "notes": "",
            },
            {
                "pair_id": "p3__T3",
                "patient_id": "p3",
                "nct_id": "T3",
                "slice": "slice-a",
                "structured_verdicts": [],
                "free_text_criteria_count": 0,
                "free_text_review_status": "pending",
                "notes": "",
            },
        ],
    }


@pytest.fixture
def seed_path(tmp_path: Path) -> Path:
    p = tmp_path / "eval_seed.json"
    p.write_text(json.dumps(_seed_payload()))
    return p


# ---------------- dataset


def test_load_dataset_yields_every_pair(seed_path: Path) -> None:
    cases = load_dataset(seed_path)
    assert len(cases) == 3
    assert [c.pair_id for c in cases] == ["p1__T1", "p2__T2", "p3__T3"]
    assert all(c.as_of == date(2025, 1, 1) for c in cases)


def test_load_dataset_carries_expected_structured(seed_path: Path) -> None:
    """The expected verdicts are forwarded as opaque dicts so the
    layer-1 reporter can parse them without coupling to seed types."""
    cases = load_dataset(seed_path)
    p1 = next(c for c in cases if c.pair_id == "p1__T1")
    assert len(p1.expected_structured) == 1
    assert p1.expected_structured[0]["criterion"]["field"] == "min_age"
    assert p1.expected_structured[0]["verdict"] == "pass"


def test_load_dataset_pair_ids_filter(seed_path: Path) -> None:
    cases = load_dataset(seed_path, pair_ids={"p2__T2", "p3__T3"})
    assert {c.pair_id for c in cases} == {"p2__T2", "p3__T3"}


def test_load_dataset_limit_truncates_after_filter(seed_path: Path) -> None:
    cases = load_dataset(seed_path, limit=2)
    assert [c.pair_id for c in cases] == ["p1__T1", "p2__T2"]


def test_load_dataset_filter_then_limit(seed_path: Path) -> None:
    cases = load_dataset(seed_path, pair_ids={"p2__T2", "p3__T3"}, limit=1)
    assert len(cases) == 1
    assert cases[0].pair_id in {"p2__T2", "p3__T3"}


# ---------------- runner


def _ok_scorer(case: EvalCase):
    return make_score_pair_result(patient_id=case.patient_id, nct_id=case.nct_id)


def test_run_eval_success_populates_every_case(seed_path: Path) -> None:
    cases = load_dataset(seed_path)
    run = run_eval(_ok_scorer, cases, dataset_path=seed_path)
    assert run.n_cases == 3
    assert run.n_errors == 0
    assert all(c.result is not None for c in run.cases)
    assert all(c.error is None for c in run.cases)
    assert run.dataset_path == str(seed_path)


def test_run_eval_records_scoring_latency(seed_path: Path) -> None:
    cases = load_dataset(seed_path)
    run = run_eval(_ok_scorer, cases, dataset_path=seed_path)
    assert all(c.scoring_latency_ms >= 0 for c in run.cases)


def test_run_eval_isolates_per_case_failures(seed_path: Path) -> None:
    """A single bad case must not tank the rest of the run."""

    def _flaky(case: EvalCase):
        if case.pair_id == "p2__T2":
            raise RuntimeError("induced failure")
        return _ok_scorer(case)

    cases = load_dataset(seed_path)
    run = run_eval(_flaky, cases, dataset_path=seed_path)
    assert run.n_cases == 3
    assert run.n_errors == 1
    failed = next(c for c in run.cases if c.case.pair_id == "p2__T2")
    assert failed.result is None
    assert failed.error is not None
    assert "induced failure" in failed.error
    ok_ids = {c.case.pair_id for c in run.cases if c.result is not None}
    assert ok_ids == {"p1__T1", "p3__T3"}


def test_on_case_done_called_in_order(seed_path: Path) -> None:
    seen: list[str] = []

    def _cb(record: CaseRecord) -> None:
        seen.append(record.case.pair_id)

    cases = load_dataset(seed_path)
    run_eval(_ok_scorer, cases, dataset_path=seed_path, on_case_done=_cb)
    assert seen == ["p1__T1", "p2__T2", "p3__T3"]


def test_run_eval_propagates_notes(seed_path: Path) -> None:
    cases = load_dataset(seed_path)
    run = run_eval(_ok_scorer, cases, dataset_path=seed_path, notes="smoke run")
    assert run.notes == "smoke run"


def test_run_eval_assigns_unique_run_id(seed_path: Path) -> None:
    cases = load_dataset(seed_path)
    r1 = run_eval(_ok_scorer, cases, dataset_path=seed_path)
    r2 = run_eval(_ok_scorer, cases, dataset_path=seed_path)
    assert r1.run_id != r2.run_id
    assert len(r1.run_id) == 12
