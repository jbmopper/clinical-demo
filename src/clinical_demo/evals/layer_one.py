"""Layer-1 eval: deterministic agreement vs. the seed's mechanical labels.

Pure-data module: takes a `RunResult` (from `evals.store.load_run`)
plus the seed's `expected_structured` cells already attached to
each `EvalCase`, and produces:

- `LayerOneCell`: one alignment outcome per (pair, seed_field).
- `LayerOneReport`: per-field agreement + coverage rates plus the
  cell list, suitable for both rendering and JSON dump.

Alignment is per-field hardcoded (`min_age` / `max_age` / `sex`)
because the seed's `CriterionField` is a closed enum of four and
a generic alignment engine costs more than it saves.
`healthy_volunteers` is intentionally not coverable in v0 — the
extractor schema has no kind that represents "healthy volunteers
only," so those seed cells are dropped before alignment with a
recorded count of how many were dropped. Document, move on.

Status enum is deliberately tiny: `agree | disagree | missing`.
Multi-match is resolved deterministically (first match wins);
"uncoverable" cells aren't emitted at all (they show up in the
report's "skipped" line). Ambiguity flags, slice pivots, and
confusion matrices are deferred until baseline numbers say
they're worth surfacing.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Literal

from pydantic import BaseModel, Field

from ..matcher.verdict import MatchVerdict, Verdict
from ..scoring.score_pair import ScorePairResult
from .run import EvalCase, RunResult

CellStatus = Literal["agree", "disagree", "missing"]

# Seed fields layer-1 actually scores. `healthy_volunteers` and
# `required_condition` are intentionally out of scope for v0:
# neither maps cleanly to a single ExtractedCriterion kind, and
# guessing the bridge would make the metric lie. Track skipped
# counts in the report so the gap is visible, not hidden.
COVERED_FIELDS: tuple[str, ...] = ("min_age", "max_age", "sex")


class LayerOneCell(BaseModel):
    """One alignment outcome between a seed cell and the matcher.

    `matcher_verdict` is `None` when `status == "missing"` (no
    extracted criterion of the right kind survived to the matcher).
    Carries enough context — the seed's expected string, the
    extracted criterion's source text — to eyeball failures
    without re-loading the run.
    """

    pair_id: str
    field: str
    seed_verdict: Verdict
    seed_expected: str
    matcher_verdict: Verdict | None = None
    matcher_source_text: str | None = None
    status: CellStatus


class FieldStats(BaseModel):
    """Per-field rollup. `agreement` excludes missing cells —
    extractor coverage and matcher accuracy are different
    failure modes and shouldn't be averaged together."""

    field: str
    agree: int = 0
    disagree: int = 0
    missing: int = 0

    @property
    def n_scored(self) -> int:
        return self.agree + self.disagree

    @property
    def n_total(self) -> int:
        return self.agree + self.disagree + self.missing

    @property
    def agreement_rate(self) -> float | None:
        return self.agree / self.n_scored if self.n_scored else None

    @property
    def coverage_rate(self) -> float | None:
        return self.n_scored / self.n_total if self.n_total else None


class LayerOneReport(BaseModel):
    """Top-level layer-1 report: cells + per-field rollups +
    skipped-cell accounting for the v0 uncoverable fields."""

    run_id: str
    notes: str = ""
    cells: list[LayerOneCell] = Field(default_factory=list)
    field_stats: list[FieldStats] = Field(default_factory=list)
    skipped_uncoverable: dict[str, int] = Field(default_factory=dict)
    skipped_failed_cases: int = 0

    @property
    def n_cells(self) -> int:
        return len(self.cells)

    @property
    def overall_agreement(self) -> float | None:
        agree = sum(s.agree for s in self.field_stats)
        scored = sum(s.n_scored for s in self.field_stats)
        return agree / scored if scored else None

    @property
    def overall_coverage(self) -> float | None:
        scored = sum(s.n_scored for s in self.field_stats)
        total = sum(s.n_total for s in self.field_stats)
        return scored / total if total else None


# --------------------- alignment


def _pick_match(
    field: str, seed_expected: str, verdicts: list[MatchVerdict]
) -> MatchVerdict | None:
    """Pick the matcher verdict that most likely answers a seed cell.

    First-match-wins rule per field, with one tiebreak nod to the
    seed's expected value when there are multiple candidates. We
    don't try to be clever about polarity — the matcher already
    bakes polarity into the verdict, so an extractor that mislabels
    inclusion/exclusion shows up as a `disagree` rather than a
    silent win for the wrong cell."""
    if field in {"min_age", "max_age"}:
        bound_attr = "minimum_years" if field == "min_age" else "maximum_years"
        with_bound = [
            v
            for v in verdicts
            if v.criterion.kind == "age"
            and v.criterion.age is not None
            and getattr(v.criterion.age, bound_attr) is not None
        ]
        return with_bound[0] if with_bound else None
    if field == "sex":
        candidates = [v for v in verdicts if v.criterion.kind == "sex"]
        if not candidates:
            return None
        seed_norm = seed_expected.strip().upper()
        for v in candidates:
            if v.criterion.sex is not None and v.criterion.sex.sex == seed_norm:
                return v
        return candidates[0]
    return None


def _cell_for(
    pair_id: str,
    field: str,
    seed_expected: str,
    seed_verdict: Verdict,
    result: ScorePairResult,
) -> LayerOneCell:
    match = _pick_match(field, seed_expected, result.verdicts)
    if match is None:
        return LayerOneCell(
            pair_id=pair_id,
            field=field,
            seed_verdict=seed_verdict,
            seed_expected=seed_expected,
            status="missing",
        )
    status: CellStatus = "agree" if match.verdict == seed_verdict else "disagree"
    return LayerOneCell(
        pair_id=pair_id,
        field=field,
        seed_verdict=seed_verdict,
        seed_expected=seed_expected,
        matcher_verdict=match.verdict,
        matcher_source_text=match.criterion.source_text,
        status=status,
    )


# --------------------- public entry


def build_layer_one_report(run: RunResult) -> LayerOneReport:
    """Walk a persisted run, build the layer-1 report.

    Cases whose scorer raised (`record.result is None`) are skipped
    with a count; layer-1 can't say anything about a pair that
    didn't produce verdicts. Seed cells in uncoverable fields are
    counted into `skipped_uncoverable[field]` and otherwise
    ignored."""
    cells: list[LayerOneCell] = []
    skipped_uncoverable: dict[str, int] = defaultdict(int)
    skipped_failed = 0
    for record in run.cases:
        if record.result is None:
            skipped_failed += 1
            continue
        for raw in record.case.expected_structured:
            crit = raw.get("criterion") or {}
            field = crit.get("field")
            if field not in COVERED_FIELDS:
                if field is not None:
                    skipped_uncoverable[field] += 1
                continue
            cells.append(
                _cell_for(
                    pair_id=record.case.pair_id,
                    field=field,
                    seed_expected=str(crit.get("expected", "")),
                    seed_verdict=raw["verdict"],
                    result=record.result,
                )
            )

    by_field: dict[str, FieldStats] = {f: FieldStats(field=f) for f in COVERED_FIELDS}
    for c in cells:
        s = by_field[c.field]
        if c.status == "agree":
            s.agree += 1
        elif c.status == "disagree":
            s.disagree += 1
        else:
            s.missing += 1

    return LayerOneReport(
        run_id=run.run_id,
        notes=run.notes,
        cells=cells,
        field_stats=[by_field[f] for f in COVERED_FIELDS],
        skipped_uncoverable=dict(skipped_uncoverable),
        skipped_failed_cases=skipped_failed,
    )


# Re-exported for the score-pair entry tests (which need to
# construct EvalCase->ScorePairResult fixtures); not load-bearing.
__all__ = [
    "COVERED_FIELDS",
    "CellStatus",
    "EvalCase",
    "FieldStats",
    "LayerOneCell",
    "LayerOneReport",
    "build_layer_one_report",
]
