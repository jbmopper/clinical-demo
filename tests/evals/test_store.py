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
        # Bumped to 2 by the v1→v2 migration that added
        # `expected_structured_json` and `free_text_review_status`
        # so layer-1+ analyses can read self-contained persisted
        # runs without re-loading the seed file.
        assert version == 2


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


# ---------------- migrations


def test_v1_to_v2_migration_adds_label_columns_and_bumps_version(
    tmp_path: Path,
) -> None:
    """A DB synthesized at the v1 shape opens to v2 with the two new
    columns added and existing rows preserved (with NULL labels —
    the documented "no labels recorded for this row" sentinel).

    Pin: nobody breaks the migration ladder by silently rewriting
    `_SCHEMA_SQL` without bumping `_SCHEMA_VERSION` and providing a
    migration step. If this test fails, write the migration."""
    db = tmp_path / "runs.sqlite"
    # Hand-roll a v1 DB: cases table without the v2 columns.
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE runs (
            run_id        TEXT PRIMARY KEY,
            started_at    TEXT NOT NULL,
            finished_at   TEXT NOT NULL,
            dataset_path  TEXT NOT NULL,
            notes         TEXT NOT NULL DEFAULT '',
            n_cases       INTEGER NOT NULL,
            n_errors      INTEGER NOT NULL
        );
        CREATE TABLE cases (
            run_id              TEXT NOT NULL REFERENCES runs(run_id),
            pair_id             TEXT NOT NULL,
            patient_id          TEXT NOT NULL,
            nct_id              TEXT NOT NULL,
            slice               TEXT NOT NULL DEFAULT '',
            as_of               TEXT NOT NULL,
            eligibility         TEXT,
            total_criteria      INTEGER,
            fail_count          INTEGER,
            pass_count          INTEGER,
            indeterminate_count INTEGER,
            extraction_cost_usd REAL,
            extraction_tokens   INTEGER,
            scoring_latency_ms  REAL NOT NULL,
            error               TEXT,
            result_json         TEXT,
            PRIMARY KEY (run_id, pair_id)
        );
        """
    )
    conn.execute("PRAGMA user_version = 1")
    conn.execute(
        "INSERT INTO runs (run_id, started_at, finished_at, dataset_path,"
        " notes, n_cases, n_errors)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("legacy", "2025-01-01T00:00:00", "2025-01-01T00:00:01", "x", "", 1, 0),
    )
    conn.execute(
        "INSERT INTO cases (run_id, pair_id, patient_id, nct_id, slice,"
        " as_of, scoring_latency_ms)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("legacy", "p1__T1", "p1", "T1", "s", "2025-01-01", 0.0),
    )
    conn.commit()
    conn.close()

    with open_store(db) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == 2
        cols = {r[1] for r in conn.execute("PRAGMA table_info(cases)").fetchall()}
        assert "expected_structured_json" in cols
        assert "free_text_review_status" in cols
        legacy = conn.execute(
            "SELECT pair_id, expected_structured_json, free_text_review_status"
            " FROM cases WHERE run_id = ?",
            ("legacy",),
        ).fetchone()
        assert legacy == ("p1__T1", None, None)


def test_open_store_rejects_newer_db_than_build_expects(tmp_path: Path) -> None:
    """If a teammate runs a future build that bumps to v3, then this
    older build re-opens that DB, refuse rather than silently
    truncating columns we don't know about."""
    db = tmp_path / "runs.sqlite"
    with open_store(db) as conn:
        conn.execute("PRAGMA user_version = 999")
        conn.commit()
    with pytest.raises(RuntimeError, match="newer build"), open_store(db):
        pass


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


def test_save_then_load_round_trips_labels(tmp_path: Path) -> None:
    """`expected_structured` and `free_text_review_status` round-trip
    through the v2 columns, so layer-1+ analyses don't have to
    re-load the seed file at report time. Cases without labels
    (the v0 default for an unlabeled pair) survive as the empty
    list / 'pending' string after the NULL→default decode."""
    labeled = EvalCase(
        pair_id="p1__T1",
        patient_id="p1",
        nct_id="T1",
        as_of=AS_OF,
        slice="s",
        expected_structured=[
            {
                "criterion": {"field": "min_age", "expected": ">= 18 Years"},
                "verdict": "pass",
            }
        ],
        free_text_review_status="complete",
    )
    unlabeled = _case("p2__T2")
    run = run_eval(_ok_scorer, [labeled, unlabeled], dataset_path="seed.json")
    db = tmp_path / "runs.sqlite"
    with open_store(db) as conn:
        save_run(conn, run)
    with open_store(db) as conn:
        loaded = load_run(conn, run.run_id)
    by_pair = {c.case.pair_id: c.case for c in loaded.cases}
    assert by_pair["p1__T1"].expected_structured == labeled.expected_structured
    assert by_pair["p1__T1"].free_text_review_status == "complete"
    # Default round-trips as empty list / "pending" — same as a
    # freshly-built EvalCase from the seed for an unlabeled pair.
    assert by_pair["p2__T2"].expected_structured == []
    assert by_pair["p2__T2"].free_text_review_status == "pending"


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
