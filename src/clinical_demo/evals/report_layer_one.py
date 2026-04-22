"""Minimal text renderer for a `LayerOneReport`.

Pure formatting; metrics live in `layer_one.py`. Output is one
screen of summary stats plus a list of disagreements (the
actionable failures) and missing cells (extractor coverage gaps),
truncated so a 50-pair run still fits a terminal."""

from __future__ import annotations

from .layer_one import LayerOneReport

_DISAGREE_LIMIT = 20
_MISSING_LIMIT = 20


def _pct(num: float | None) -> str:
    return "n/a" if num is None else f"{num * 100:.1f}%"


def render_layer_one(report: LayerOneReport) -> str:
    lines: list[str] = []
    lines.append(f"\nLayer-1 report — run {report.run_id}")
    if report.notes:
        lines.append(f"  notes: {report.notes}")
    lines.append(
        f"  cells: {report.n_cells}"
        f"  overall agreement: {_pct(report.overall_agreement)}"
        f"  overall coverage: {_pct(report.overall_coverage)}"
    )
    if report.skipped_failed_cases:
        lines.append(f"  skipped (scorer failed): {report.skipped_failed_cases} case(s)")
    if report.skipped_uncoverable:
        skip = "  ".join(f"{f}={n}" for f, n in sorted(report.skipped_uncoverable.items()))
        lines.append(f"  skipped (uncoverable in v0): {skip}")

    lines.append("")
    lines.append("  per-field:")
    lines.append(
        f"    {'field':<20} {'agree':>5} {'disagree':>9} {'missing':>8}"
        f"  {'agreement':>10}  {'coverage':>9}"
    )
    for s in report.field_stats:
        lines.append(
            f"    {s.field:<20} {s.agree:>5} {s.disagree:>9} {s.missing:>8}"
            f"  {_pct(s.agreement_rate):>10}  {_pct(s.coverage_rate):>9}"
        )

    disagreements = [c for c in report.cells if c.status == "disagree"]
    if disagreements:
        lines.append("")
        lines.append(f"  disagreements ({len(disagreements)}; showing up to {_DISAGREE_LIMIT}):")
        for c in disagreements[:_DISAGREE_LIMIT]:
            lines.append(
                f"    {c.pair_id:<28} {c.field:<18}"
                f"  seed={c.seed_verdict:<13} matcher={c.matcher_verdict}"
            )

    missing = [c for c in report.cells if c.status == "missing"]
    if missing:
        lines.append("")
        lines.append(f"  missing extractions ({len(missing)}; showing up to {_MISSING_LIMIT}):")
        for c in missing[:_MISSING_LIMIT]:
            lines.append(f"    {c.pair_id:<28} {c.field:<18}  expected={c.seed_expected!r}")

    lines.append("")
    return "\n".join(lines)


__all__ = ["render_layer_one"]
