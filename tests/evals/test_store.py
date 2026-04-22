"""Tests for the SQLite results store.

Pin: schema applies idempotently on a fresh DB and on re-open;
save/load is a true round-trip (run + every case + the full
ScorePairResult); append-only contract is enforced (re-saving
the same run_id raises); listing returns runs newest-first;
case rows for failed scorers persist the error and NULL out the
per-case columns."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from clinical_demo.evals.run import CaseRecord, EvalCase, RunResult, run_eval
from clinical_demo.evals.store import (
    list_runs,
    load_run,
    open_store,
    save_run,
)

from ._fixtures import AS_OF, make_score_pair_result


def _case(pair_id: str = "p1__T1") -> EvalCase:
    return EvalCase(
        pair_id=pair_id,
        patient_id="p1",
        nct_id="T1",
        as_of=AS_OF,
        slice="slice-a",
    )


def _ok_scorer(case: EvalCase):
    return make_score_pair_result(patient_id=case.patient_id, nct_id=case.nct_id)


# ---------------- schema


def test_open_store_creates_db_and_sets_user_version(tmp_path: Path) -> None:
    db = tmp_path / "runs.sqlite"
    assert not db.exists()
    with open_store(db) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == 1
    assert db.exists()


def test_open_store_is_idempotent(tmp_path: Path) -> None:
    """Re-opening an existing DB is a no-op (schema CREATE IF NOT
    EXISTS, version unchanged)."""
    db = tmp_path / "runs.sqlite"
    with open_store(db) as conn:
        conn.execute(
            "INSERT INTO runs (run_id, started_at, finished_at,"
            " dataset_path, notes, n_cases, n_errors)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("r1", "2025-01-01T00:00:00", "2025-01-01T00:00:01", "x", "", 0, 0),
        )
        conn.commit()
    with open_store(db) as conn:
        rows = conn.execute("SELECT run_id FROM runs").fetchall()
        assert rows == [("r1",)]


def test_open_store_creates_parent_dirs(tmp_path: Path) -> None:
    db = tmp_path / "deeper" / "still" / "runs.sqlite"
    with open_store(db):
        pass
    assert db.exists()


# ---------------- save / load round-trip


def test_save_then_load_round_trips_a_run(tmp_path: Path) -> None:
    cases = [_case("p1__T1"), _case("p2__T2")]
    run = run_eval(_ok_scorer, cases, dataset_path="seed.json", notes="rt")
    db = tmp_path / "runs.sqlite"
    with open_store(db) as conn:
        save_run(conn, run)
    with open_store(db) as conn:
        loaded = load_run(conn, run.run_id)
    assert loaded.run_id == run.run_id
    assert loaded.notes == "rt"
    assert loaded.dataset_path == "seed.json"
    assert loaded.n_cases == 2
    assert loaded.n_errors == 0
    by_pair = {c.case.pair_id: c for c in loaded.cases}
    assert set(by_pair) == {"p1__T1", "p2__T2"}
    assert by_pair["p1__T1"].result is not None
    assert by_pair["p1__T1"].result.eligibility == "pass"
    # extraction_meta survives the JSON round-trip
    assert by_pair["p1__T1"].result.extraction_meta.cost_usd == 0.0001


def test_save_persists_case_summary_columns(tmp_path: Path) -> None:
    """Per-case summary columns (eligibility, counts, cost) populate
    even though the layer reporters will mostly read result_json.
    These columns let an operator do quick SQL eyeballing without
    json_extract gymnastics."""
    cases = [_case("p1__T1")]
    run = run_eval(_ok_scorer, cases, dataset_path="seed.json")
    db = tmp_path / "runs.sqlite"
    with open_store(db) as conn:
        save_run(conn, run)
        row = conn.execute(
            "SELECT eligibility, total_criteria, pass_count,"
            " extraction_cost_usd, error"
            " FROM cases WHERE run_id = ?",
            (run.run_id,),
        ).fetchone()
    assert row == ("pass", 3, 3, 0.0001, None)


def test_save_append_only_rejects_duplicate_run_id(tmp_path: Path) -> None:
    cases = [_case()]
    run = run_eval(_ok_scorer, cases, dataset_path="seed.json")
    db = tmp_path / "runs.sqlite"
    with open_store(db) as conn:
        save_run(conn, run)
        with pytest.raises(sqlite3.IntegrityError):
            save_run(conn, run)


def test_failed_case_persists_error_with_null_summary(tmp_path: Path) -> None:
    def _bad(case: EvalCase):
        raise ValueError("nope")

    cases = [_case("p1__T1")]
    run = run_eval(_bad, cases, dataset_path="seed.json")
    db = tmp_path / "runs.sqlite"
    with open_store(db) as conn:
        save_run(conn, run)
        row = conn.execute(
            "SELECT eligibility, total_criteria, error, result_json FROM cases WHERE run_id = ?",
            (run.run_id,),
        ).fetchone()
    assert row[0] is None
    assert row[1] is None
    assert "nope" in row[2]
    assert row[3] is None
    with open_store(db) as conn:
        loaded = load_run(conn, run.run_id)
    assert loaded.cases[0].result is None
    assert "nope" in (loaded.cases[0].error or "")


def test_load_run_unknown_id_raises_keyerror(tmp_path: Path) -> None:
    db = tmp_path / "runs.sqlite"
    with open_store(db) as conn, pytest.raises(KeyError):
        load_run(conn, "does-not-exist")


# ---------------- listing


def test_list_runs_returns_newest_first(tmp_path: Path) -> None:
    db = tmp_path / "runs.sqlite"
    cases = [_case()]
    older = run_eval(_ok_scorer, cases, dataset_path="seed.json", notes="old")
    newer = RunResult(
        started_at=datetime(2099, 1, 1, 0, 0, 0),
        finished_at=datetime(2099, 1, 1, 0, 0, 1),
        dataset_path="seed.json",
        notes="new",
        cases=[CaseRecord(case=cases[0], result=None, error="x")],
    )
    with open_store(db) as conn:
        save_run(conn, older)
        save_run(conn, newer)
        rows = list_runs(conn)
    assert [r["notes"] for r in rows] == ["new", "old"]
    assert rows[0]["n_errors"] == 1
    assert rows[1]["n_errors"] == 0
