"""SQLite-backed results store for eval runs.

Two tables, append-only (D-60, D-61): `runs` for the
per-invocation header, `cases` for one row per scored pair with
the full `ScorePairResult` carried as a JSON blob (D-60). A
normalized verdicts table is *not* introduced in v0; layer 1
(2.4) will walk the JSON blob, and we add a verdicts table only
when a real query asks for one.

The schema applies idempotently: opening the DB on a fresh path
creates it, opening on an existing path is a no-op. No migration
framework — when the schema changes, we'll add an `ALTER TABLE`
guarded by a `PRAGMA user_version` bump, and only then.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .run import CaseRecord, EvalCase, RunResult

_SCHEMA_VERSION = 2
"""SQLite `PRAGMA user_version` for this build's expected schema.

Bump rules:
  - v1: initial.
  - v2: added `expected_structured_json` and `free_text_review_status`
    on `cases` so layer-1 (and any future label-aware layer) can
    operate on a self-contained persisted run without re-reading the
    seed file. See `_migrate_v1_to_v2`. Bump triggered the on-disk
    migration which is a no-op `ALTER TABLE` for fresh DBs and an
    additive `ALTER TABLE ADD COLUMN` for v1 DBs (NULL for old rows
    is the documented "no labels recorded for this run" sentinel).
"""

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    started_at    TEXT NOT NULL,
    finished_at   TEXT NOT NULL,
    dataset_path  TEXT NOT NULL,
    notes         TEXT NOT NULL DEFAULT '',
    n_cases       INTEGER NOT NULL,
    n_errors      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS cases (
    run_id                   TEXT NOT NULL REFERENCES runs(run_id),
    pair_id                  TEXT NOT NULL,
    patient_id               TEXT NOT NULL,
    nct_id                   TEXT NOT NULL,
    slice                    TEXT NOT NULL DEFAULT '',
    as_of                    TEXT NOT NULL,
    eligibility              TEXT,
    total_criteria           INTEGER,
    fail_count               INTEGER,
    pass_count               INTEGER,
    indeterminate_count      INTEGER,
    extraction_cost_usd      REAL,
    extraction_tokens        INTEGER,
    scoring_latency_ms       REAL NOT NULL,
    error                    TEXT,
    result_json              TEXT,
    expected_structured_json TEXT,
    free_text_review_status  TEXT,
    PRIMARY KEY (run_id, pair_id)
);

CREATE INDEX IF NOT EXISTS cases_run_id_idx ON cases(run_id);
"""


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """Additive migration: add the two label columns to `cases`.

    NULL on old rows is the explicit "no labels recorded for this run"
    sentinel — `load_run` decodes that as the empty list / "pending"
    string, matching what `EvalCase` defaults to. Migration is
    transactional so a half-applied schema is impossible."""
    conn.execute("ALTER TABLE cases ADD COLUMN expected_structured_json TEXT")
    conn.execute("ALTER TABLE cases ADD COLUMN free_text_review_status TEXT")


@contextmanager
def open_store(db_path: Path | str) -> Iterator[sqlite3.Connection]:
    """Open the SQLite DB at `db_path`, applying the schema if needed.

    Idempotent: re-opening on an existing DB is cheap and never
    destructive. Sets `PRAGMA foreign_keys = ON` per connection
    (SQLite's default-off is a footgun) and `user_version` so a
    future migration knows what shape it's looking at.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(_SCHEMA_SQL)
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        if current == 0:
            conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        elif current < _SCHEMA_VERSION:
            # Apply additive migrations in order. Each migration is
            # idempotent at its target version: a v1→v2 step run on a
            # v2 DB would no-op via the IF NOT EXISTS-style helpers.
            # We don't have many of these yet; when the chain grows,
            # promote to a tiny dispatch table.
            if current == 1:
                _migrate_v1_to_v2(conn)
                current = 2
            conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        elif current > _SCHEMA_VERSION:
            raise RuntimeError(
                f"runs.sqlite at {db_path} is at schema version "
                f"{current}; this build expects {_SCHEMA_VERSION}. "
                f"DB is from a newer build — upgrade your code."
            )
        conn.commit()
        yield conn
    finally:
        conn.close()


def save_run(conn: sqlite3.Connection, run: RunResult) -> None:
    """Persist a `RunResult` (and its case rows) atomically.

    Append-only (D-61): re-saving a run with the same `run_id`
    raises an `IntegrityError`. Callers that want to re-score
    should mint a new `run_id`.
    """
    conn.execute(
        "INSERT INTO runs"
        " (run_id, started_at, finished_at, dataset_path, notes,"
        "  n_cases, n_errors)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            run.run_id,
            run.started_at.isoformat(),
            run.finished_at.isoformat(),
            run.dataset_path,
            run.notes,
            run.n_cases,
            run.n_errors,
        ),
    )
    conn.executemany(
        "INSERT INTO cases"
        " (run_id, pair_id, patient_id, nct_id, slice, as_of,"
        "  eligibility, total_criteria, fail_count, pass_count,"
        "  indeterminate_count, extraction_cost_usd,"
        "  extraction_tokens, scoring_latency_ms, error, result_json,"
        "  expected_structured_json, free_text_review_status)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [_case_row(run.run_id, c) for c in run.cases],
    )
    conn.commit()


def load_run(conn: sqlite3.Connection, run_id: str) -> RunResult:
    """Hydrate a `RunResult` (with all case records) by id.

    Inverse of `save_run`: walks the rows back into pydantic
    models so layer code can call `.cases[i].result` directly
    rather than re-parsing the JSON blob itself."""
    row = conn.execute(
        "SELECT run_id, started_at, finished_at, dataset_path, notes  FROM runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"no run with run_id={run_id!r} in this DB")
    case_rows = conn.execute(
        "SELECT pair_id, patient_id, nct_id, slice, as_of,"
        "       scoring_latency_ms, error, result_json,"
        "       expected_structured_json, free_text_review_status"
        "  FROM cases WHERE run_id = ? ORDER BY pair_id",
        (run_id,),
    ).fetchall()
    cases: list[CaseRecord] = []
    for (
        pair_id,
        patient_id,
        nct_id,
        slice_,
        as_of,
        latency_ms,
        error,
        result_json,
        expected_json,
        review_status,
    ) in case_rows:
        from datetime import date as _date

        # NULL on either label column = "no labels recorded for this
        # row" (legacy v1 row, or a case the runner inserted with
        # default empty labels). Decode to the matching default rather
        # than carrying NULL into in-process pydantic models.
        expected_structured = json.loads(expected_json) if expected_json else []
        case = EvalCase(
            pair_id=pair_id,
            patient_id=patient_id,
            nct_id=nct_id,
            as_of=_date.fromisoformat(as_of),
            slice=slice_,
            expected_structured=expected_structured,
            free_text_review_status=review_status or "pending",
        )
        # ScorePairResult deserialization is the slow path, so we
        # do it lazily-but-eagerly here (callers expect a fully
        # populated CaseRecord). If a layer turns out to want
        # blob-only access, we can add a `lazy=True` flag.
        from ..scoring.score_pair import ScorePairResult

        result = (
            ScorePairResult.model_validate_json(result_json) if result_json is not None else None
        )
        cases.append(
            CaseRecord(
                case=case,
                result=result,
                error=error,
                scoring_latency_ms=latency_ms,
            )
        )
    from datetime import datetime as _dt

    return RunResult(
        run_id=row[0],
        started_at=_dt.fromisoformat(row[1]),
        finished_at=_dt.fromisoformat(row[2]),
        dataset_path=row[3],
        notes=row[4],
        cases=cases,
    )


def list_runs(conn: sqlite3.Connection) -> list[dict]:
    """Quick metadata listing for `eval report` discovery."""
    rows = conn.execute(
        "SELECT run_id, started_at, finished_at, notes, n_cases, n_errors"
        "  FROM runs ORDER BY started_at DESC"
    ).fetchall()
    return [
        {
            "run_id": r[0],
            "started_at": r[1],
            "finished_at": r[2],
            "notes": r[3],
            "n_cases": r[4],
            "n_errors": r[5],
        }
        for r in rows
    ]


def _case_row(run_id: str, c: CaseRecord) -> tuple:
    """Flatten one CaseRecord into the cases-table row tuple.

    When `result` is None (the scorer raised), the per-case
    summary columns are NULL — the `error` and
    `scoring_latency_ms` cols still carry the failure record.

    Labels (`expected_structured`, `free_text_review_status`) are
    persisted alongside the result so layer-1+ analyses don't have
    to re-read the seed file at report time. The seed remains the
    *source of truth* for ground labels — the persisted copy is a
    snapshot of what the labels looked like at run time, which is
    exactly what you want when comparing runs across label revisions.
    """
    expected_json = json.dumps(c.case.expected_structured) if c.case.expected_structured else None
    review_status = c.case.free_text_review_status or None
    if c.result is not None:
        s = c.result.summary
        meta = c.result.extraction_meta
        return (
            run_id,
            c.case.pair_id,
            c.case.patient_id,
            c.case.nct_id,
            c.case.slice,
            c.case.as_of.isoformat(),
            c.result.eligibility,
            s.total_criteria,
            s.by_verdict.get("fail", 0),
            s.by_verdict.get("pass", 0),
            s.by_verdict.get("indeterminate", 0),
            meta.cost_usd,
            (meta.input_tokens or 0) + (meta.output_tokens or 0),
            c.scoring_latency_ms,
            None,
            c.result.model_dump_json(),
            expected_json,
            review_status,
        )
    return (
        run_id,
        c.case.pair_id,
        c.case.patient_id,
        c.case.nct_id,
        c.case.slice,
        c.case.as_of.isoformat(),
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        c.scoring_latency_ms,
        c.error,
        None,
        expected_json,
        review_status,
    )


__all__ = ["list_runs", "load_run", "open_store", "save_run"]
