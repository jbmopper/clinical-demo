"""Eval harness: dataset adapter + scorer-agnostic runner.

The eval harness is the foundation for layers 1-3 (tasks
2.4-2.6). This module is **pure plumbing**:

- `EvalCase`: one (patient, trial, as_of) target with the seed's
  expected verdicts attached.
- `RunResult`: aggregate output of one run, plus the per-case
  `ScorePairResult` envelopes.
- `load_dataset()`: reads the existing `eval_seed.json` and
  yields cases. We deliberately re-use the seed format rather
  than inventing a parallel one.
- `run_eval()`: orchestrator-agnostic runner (D-59). Takes a
  `Callable[[EvalCase], ScorePairResult]` so the imperative
  `score_pair()`, the LangGraph `score_pair_graph()`, and any
  future variants are all "just a scorer."

Per-case failures are caught and recorded on the case row so a
single bad pair doesn't tank a 50-pair run (D-62). No layer-1/2/3
knowledge lives here — those layers will consume `runs.sqlite`
in 2.4-2.6 (D-63).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable
from datetime import date, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from ..scoring.score_pair import ScorePairResult


class EvalCase(BaseModel):
    """One (patient, trial) target the runner will score.

    Mirrors the seed's `EvalPair` but only the fields the runner
    actually needs to score + attribute. The expected verdicts are
    carried opaquely (`expected_structured` is a list of dicts
    matching the seed's `CriterionVerdict` shape) so we don't
    re-import the seed module's pydantic types and accidentally
    couple the harness to its internal evolution. Layer-1 (2.4)
    will parse this list when it's ready to compute accuracy.
    """

    pair_id: str
    patient_id: str
    nct_id: str
    as_of: date
    slice: str = ""
    expected_structured: list[dict] = Field(default_factory=list)
    free_text_review_status: Literal["pending", "complete"] = "pending"


class CaseRecord(BaseModel):
    """One row of a `RunResult`: scoring outcome for one EvalCase.

    Holds the full `ScorePairResult` (or `None` if the scorer
    raised) plus the wall-clock latency of the scorer call. Layer
    consumers walk `result` for verdicts; the harness itself only
    cares about `error` and `scoring_latency_ms`.
    """

    case: EvalCase
    result: ScorePairResult | None = None
    error: str | None = None
    scoring_latency_ms: float = 0.0


class RunResult(BaseModel):
    """Aggregate output of one `run_eval` invocation.

    Persisted by `evals.store.save_run`. The `notes` field is the
    operator's free-text record of what this run *was* (e.g.
    `"score_pair_graph, gpt-4o-mini, critic enabled"`); v0
    deliberately keeps run configuration out of the schema and
    lets structured columns earn their place by being queried.
    """

    run_id: str = Field(default_factory=lambda: uuid4().hex[:12])
    started_at: datetime
    finished_at: datetime
    dataset_path: str
    notes: str = ""
    cases: list[CaseRecord]

    @property
    def n_cases(self) -> int:
        return len(self.cases)

    @property
    def n_errors(self) -> int:
        return sum(1 for c in self.cases if c.error is not None)


def load_dataset(
    seed_path: Path | str,
    *,
    pair_ids: Iterable[str] | None = None,
    limit: int | None = None,
) -> list[EvalCase]:
    """Read `eval_seed.json` and produce `EvalCase`s.

    `pair_ids` filters to a specific subset (useful for smoke
    runs); `limit` truncates after filtering. Both are optional;
    omitting both yields every pair in the seed in source order.
    The seed's own `as_of` date is applied uniformly to every
    case — eligibility evaluation is always anchored to one date
    per run, by design.
    """
    seed = json.loads(Path(seed_path).read_text())
    seed_as_of = date.fromisoformat(seed["as_of"])
    wanted = set(pair_ids) if pair_ids is not None else None

    out: list[EvalCase] = []
    for raw in seed["pairs"]:
        if wanted is not None and raw["pair_id"] not in wanted:
            continue
        out.append(
            EvalCase(
                pair_id=raw["pair_id"],
                patient_id=raw["patient_id"],
                nct_id=raw["nct_id"],
                as_of=seed_as_of,
                slice=raw.get("slice", ""),
                expected_structured=list(raw.get("structured_verdicts", [])),
                free_text_review_status=raw.get("free_text_review_status", "pending"),
            )
        )
        if limit is not None and len(out) >= limit:
            break
    return out


Scorer = Callable[[EvalCase], ScorePairResult]
"""Type alias: a function that scores one case and returns the result.

Concrete scorers wrap `score_pair()` or `score_pair_graph()` plus
the I/O of loading the patient + trial. The harness deliberately
doesn't know how — it just calls the function (D-59)."""


def run_eval(
    scorer: Scorer,
    cases: Iterable[EvalCase],
    *,
    dataset_path: str | Path,
    notes: str = "",
    on_case_done: Callable[[CaseRecord], None] | None = None,
) -> RunResult:
    """Run `scorer` over every case; return a `RunResult`.

    Per-case exceptions are caught and recorded on the case's
    `error` field (D-62); the run as a whole keeps going. The
    `on_case_done` callback (optional) is invoked after every
    case, useful for CLI progress bars and tests; it should not
    raise.
    """
    started_at = datetime.now()
    records: list[CaseRecord] = []
    for case in cases:
        t0 = time.perf_counter()
        record: CaseRecord
        try:
            result = scorer(case)
            record = CaseRecord(
                case=case,
                result=result,
                scoring_latency_ms=(time.perf_counter() - t0) * 1000.0,
            )
        except Exception as exc:
            record = CaseRecord(
                case=case,
                error=f"{type(exc).__name__}: {exc}",
                scoring_latency_ms=(time.perf_counter() - t0) * 1000.0,
            )
        records.append(record)
        if on_case_done is not None:
            on_case_done(record)
    finished_at = datetime.now()
    return RunResult(
        started_at=started_at,
        finished_at=finished_at,
        dataset_path=str(dataset_path),
        notes=notes,
        cases=records,
    )


__all__ = [
    "CaseRecord",
    "EvalCase",
    "RunResult",
    "Scorer",
    "load_dataset",
    "run_eval",
]
