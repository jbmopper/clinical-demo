"""Slice-5 eval diagnostics: indeterminacy, terminology outcomes, deltas.

D-69 slice 5 needs a report that sits between the one-screen run
summary and the layer-1 structured-field report. It answers:

- Did `unmapped_concept` move relative to the D-68 baseline?
- Which failure modes dominate now?
- Did registered terminology bindings actually resolve, or did they
  still fall through to `unmapped_concept`?
- Did the structured-field layer-1 numbers move?

The module is deliberately pure-data: it consumes a persisted
`RunResult` and optional structured baseline artifacts, with no
patient/trial loading and no terminology API calls.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

from pydantic import BaseModel, Field

from clinical_demo.extractor.schema import ExtractedCriterion
from clinical_demo.terminology.bindings import (
    lookup_condition_binding,
    lookup_lab_binding,
    lookup_medication_binding,
)

from .layer_one import LayerOneReport
from .run import RunResult


class SurfaceCount(BaseModel):
    """Frequency of an unmapped trial-side surface form."""

    surface: str
    kind: str
    count: int


class EvalDiagnostics(BaseModel):
    """Run-level diagnostic rollup for D-69 slice 5."""

    run_id: str
    notes: str = ""
    n_cases: int
    n_errors: int
    scored_cases: int
    total_criteria: int
    total_scoring_latency_ms: float
    avg_scoring_latency_ms: float | None
    verdict_counts: dict[str, int] = Field(default_factory=dict)
    reason_counts: dict[str, int] = Field(default_factory=dict)
    kind_counts: dict[str, int] = Field(default_factory=dict)
    unmapped_count: int
    unmapped_rate: float | None
    indeterminate_count: int
    indeterminate_rate: float | None
    binding_registered_total: int = 0
    binding_registered_resolved: int = 0
    binding_registered_unmapped: int = 0
    binding_registered_by_kind: dict[str, dict[str, int]] = Field(default_factory=dict)
    top_unmapped_surfaces: list[SurfaceCount] = Field(default_factory=list)


def build_diagnostics(run: RunResult, *, top_n: int = 20) -> EvalDiagnostics:
    """Build D-69 slice-5 diagnostics from one persisted eval run."""

    verdict_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    kind_counts: Counter[str] = Counter()
    top_unmapped: Counter[tuple[str, str]] = Counter()
    binding_by_kind: dict[str, Counter[str]] = defaultdict(Counter)
    binding_registered_total = 0
    binding_registered_resolved = 0
    binding_registered_unmapped = 0
    scored_cases = 0
    total_criteria = 0
    total_latency = 0.0

    for record in run.cases:
        total_latency += record.scoring_latency_ms
        if record.result is None:
            continue
        scored_cases += 1
        total_criteria += len(record.result.verdicts)
        for verdict in record.result.verdicts:
            kind = str(verdict.criterion.kind)
            reason = str(verdict.reason)
            verdict_counts[str(verdict.verdict)] += 1
            reason_counts[reason] += 1
            kind_counts[kind] += 1

            if reason == "unmapped_concept":
                surface = _criterion_surface(verdict.criterion)
                if surface:
                    top_unmapped[(kind, surface)] += 1

            binding_kind = _registered_binding_kind(verdict.criterion)
            if binding_kind is not None:
                binding_registered_total += 1
                if reason == "unmapped_concept":
                    binding_registered_unmapped += 1
                    binding_by_kind[binding_kind]["unmapped"] += 1
                else:
                    binding_registered_resolved += 1
                    binding_by_kind[binding_kind]["resolved"] += 1

    unmapped_count = reason_counts.get("unmapped_concept", 0)
    indeterminate_count = verdict_counts.get("indeterminate", 0)

    return EvalDiagnostics(
        run_id=run.run_id,
        notes=run.notes,
        n_cases=run.n_cases,
        n_errors=run.n_errors,
        scored_cases=scored_cases,
        total_criteria=total_criteria,
        total_scoring_latency_ms=total_latency,
        avg_scoring_latency_ms=(total_latency / run.n_cases if run.n_cases else None),
        verdict_counts=dict(sorted(verdict_counts.items())),
        reason_counts=dict(sorted(reason_counts.items())),
        kind_counts=dict(sorted(kind_counts.items())),
        unmapped_count=unmapped_count,
        unmapped_rate=_rate(unmapped_count, total_criteria),
        indeterminate_count=indeterminate_count,
        indeterminate_rate=_rate(indeterminate_count, total_criteria),
        binding_registered_total=binding_registered_total,
        binding_registered_resolved=binding_registered_resolved,
        binding_registered_unmapped=binding_registered_unmapped,
        binding_registered_by_kind={
            kind: dict(sorted(counts.items())) for kind, counts in sorted(binding_by_kind.items())
        },
        top_unmapped_surfaces=[
            SurfaceCount(surface=surface, kind=kind, count=count)
            for (kind, surface), count in top_unmapped.most_common(top_n)
        ],
    )


def load_diagnostics(path: Path | str) -> EvalDiagnostics:
    """Load a structured diagnostic baseline from JSON."""

    return EvalDiagnostics.model_validate_json(Path(path).read_text())


def render_diagnostics(
    current: EvalDiagnostics,
    *,
    baseline: EvalDiagnostics | None = None,
    layer_one: LayerOneReport | None = None,
    baseline_layer_one: LayerOneReport | None = None,
) -> str:
    """Render the slice-5 diagnostic report as terminal-friendly text."""

    lines: list[str] = []
    lines.append(f"\nD-69 slice-5 diagnostics — run {current.run_id}")
    if current.notes:
        lines.append(f"  notes: {current.notes}")
    lines.append(
        f"  cases: {current.n_cases}  errors: {current.n_errors}  "
        f"criteria: {current.total_criteria}  "
        f"latency: {current.total_scoring_latency_ms / 1000.0:.1f}s"
    )
    if current.avg_scoring_latency_ms is not None:
        lines.append(f"  avg scoring latency: {current.avg_scoring_latency_ms:.0f}ms / case")

    lines.append("")
    lines.append("  headline:")
    lines.append(
        _metric_line(
            "indeterminate", current.indeterminate_count, current.indeterminate_rate, baseline
        )
    )
    lines.append(
        _metric_line("unmapped_concept", current.unmapped_count, current.unmapped_rate, baseline)
    )

    if layer_one is not None:
        lines.append("")
        lines.append("  layer-1 structured fields:")
        lines.append(
            "    agreement: "
            + _pct_with_delta(
                layer_one.overall_agreement, _maybe(baseline_layer_one, "overall_agreement")
            )
        )
        lines.append(
            "    coverage:  "
            + _pct_with_delta(
                layer_one.overall_coverage, _maybe(baseline_layer_one, "overall_coverage")
            )
        )

    lines.append("")
    lines.append("  verdicts:")
    lines.extend(
        _count_lines(current.verdict_counts, baseline.verdict_counts if baseline else None)
    )

    lines.append("")
    lines.append("  failure modes by reason:")
    reason_items = {
        k: v for k, v in sorted(current.reason_counts.items(), key=lambda item: (-item[1], item[0]))
    }
    baseline_reasons = baseline.reason_counts if baseline else None
    lines.extend(_count_lines(reason_items, baseline_reasons, limit=12))

    if current.binding_registered_total:
        lines.append("")
        lines.append("  registered terminology surfaces:")
        lines.append(f"    total criteria: {current.binding_registered_total}")
        lines.append(
            f"    resolved:       {current.binding_registered_resolved} "
            f"({_pct(_rate(current.binding_registered_resolved, current.binding_registered_total))})"
        )
        lines.append(f"    still unmapped: {current.binding_registered_unmapped}")
        for kind, counts in sorted(current.binding_registered_by_kind.items()):
            resolved = counts.get("resolved", 0)
            unmapped = counts.get("unmapped", 0)
            lines.append(f"    {kind:<12} resolved={resolved:<4} unmapped={unmapped:<4}")
    else:
        lines.append("")
        lines.append("  registered terminology surfaces: none observed in this run")

    if current.top_unmapped_surfaces:
        lines.append("")
        lines.append("  top unmapped surfaces:")
        for item in current.top_unmapped_surfaces[:15]:
            lines.append(f"    {item.count:>3}  {item.kind:<24} {item.surface}")

    if baseline is not None:
        lines.append("")
        lines.append(f"  baseline: {baseline.run_id} ({baseline.notes or 'no notes'})")
    lines.append("")
    return "\n".join(lines)


def _criterion_surface(criterion: ExtractedCriterion) -> str | None:
    if criterion.kind in {"condition_present", "condition_absent"} and criterion.condition:
        return criterion.condition.condition_text
    if criterion.kind in {"medication_present", "medication_absent"} and criterion.medication:
        return criterion.medication.medication_text
    if criterion.kind == "measurement_threshold" and criterion.measurement:
        return criterion.measurement.measurement_text
    if criterion.kind == "temporal_window" and criterion.temporal_window:
        return criterion.temporal_window.event_text
    return None


def _registered_binding_kind(criterion: ExtractedCriterion) -> str | None:
    surface = _criterion_surface(criterion)
    if not surface:
        return None
    if criterion.kind in {"condition_present", "condition_absent", "temporal_window"}:
        return "condition" if lookup_condition_binding(surface) is not None else None
    if criterion.kind in {"medication_present", "medication_absent"}:
        return "medication" if lookup_medication_binding(surface) is not None else None
    if criterion.kind == "measurement_threshold":
        return "lab" if lookup_lab_binding(surface) is not None else None
    return None


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _maybe(model: BaseModel | None, field: str) -> float | None:
    return getattr(model, field) if model is not None else None


def _pct_with_delta(current: float | None, baseline: float | None) -> str:
    rendered = _pct(current)
    if current is None or baseline is None:
        return rendered
    return f"{rendered} ({_signed_pct_delta(current, baseline)})"


def _signed_pct_delta(current: float, baseline: float) -> str:
    delta = (current - baseline) * 100.0
    sign = "+" if delta >= 0 else ""
    return f"{sign}{delta:.1f} pp"


def _metric_line(
    label: str,
    current_count: int,
    current_rate: float | None,
    baseline: EvalDiagnostics | None,
) -> str:
    base_count = None
    base_rate = None
    if baseline is not None:
        if label == "indeterminate":
            base_count = baseline.indeterminate_count
            base_rate = baseline.indeterminate_rate
        elif label == "unmapped_concept":
            base_count = baseline.unmapped_count
            base_rate = baseline.unmapped_rate
    rendered = f"    {label:<24} {current_count:>5}  {_pct(current_rate):>7}"
    if base_count is not None and base_rate is not None and current_rate is not None:
        rendered += (
            f"  baseline={base_count} {_pct(base_rate)}"
            f"  delta={current_count - base_count:+d}, {_signed_pct_delta(current_rate, base_rate)}"
        )
    return rendered


def _count_lines(
    counts: dict[str, int],
    baseline_counts: dict[str, int] | None = None,
    *,
    limit: int | None = None,
) -> list[str]:
    out: list[str] = []
    items = list(counts.items())
    if limit is not None:
        items = items[:limit]
    for key, count in items:
        line = f"    {key:<32} {count:>5}"
        if baseline_counts is not None and key in baseline_counts:
            line += f"  delta={count - baseline_counts[key]:+d}"
        out.append(line)
    return out


def write_diagnostics(path: Path | str, diagnostics: EvalDiagnostics) -> None:
    """Persist diagnostics JSON with stable formatting for baselines."""

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(diagnostics.model_dump_json(indent=2) + "\n")


def load_layer_one(path: Path | str) -> LayerOneReport:
    return LayerOneReport.model_validate_json(Path(path).read_text())


def diagnostics_to_json(diagnostics: EvalDiagnostics) -> str:
    """Small helper for CLI stdout paths."""

    return json.dumps(diagnostics.model_dump(mode="json"), indent=2)


__all__ = [
    "EvalDiagnostics",
    "SurfaceCount",
    "build_diagnostics",
    "diagnostics_to_json",
    "load_diagnostics",
    "load_layer_one",
    "render_diagnostics",
    "write_diagnostics",
]
