"""Text renderer for Layer-3 LLM-as-judge reports."""

from __future__ import annotations

from .layer_three import LayerThreeReport

_JUDGMENT_LIMIT = 12


def render_layer_three(report: LayerThreeReport) -> str:
    lines: list[str] = []
    lines.append("\nLayer-3 LLM-as-judge report")
    lines.append(f"  judgments: {report.total_judgments}")
    if report.total_cost_usd is not None:
        lines.append(f"  judge cost: ${report.total_cost_usd:.4f}")
    if report.label_counts:
        labels = "  ".join(f"{label}={count}" for label, count in report.label_counts.items())
        lines.append(f"  labels: {labels}")
    if report.confidence_counts:
        confidence = "  ".join(
            f"{label}={count}" for label, count in report.confidence_counts.items()
        )
        lines.append(f"  confidence: {confidence}")
    if report.error_category_counts:
        errors = "  ".join(
            f"{label}={count}" for label, count in report.error_category_counts.items()
        )
        lines.append(f"  error categories: {errors}")

    if report.agreement is not None:
        agreement = report.agreement
        lines.append("")
        lines.append(
            "  calibration: "
            f"compared={agreement.compared} "
            f"agreement={_pct(agreement.agreement_rate)} "
            f"kappa={_num(agreement.cohen_kappa)} "
            f"missing_judgments={agreement.missing_judgments} "
            f"missing_human_labels={agreement.missing_human_labels}"
        )

    if report.judgments:
        lines.append("")
        lines.append(f"  judgments (showing up to {_JUDGMENT_LIMIT}):")
        for judgment in report.judgments[:_JUDGMENT_LIMIT]:
            errors = ",".join(judgment.error_categories) if judgment.error_categories else "-"
            lines.append(
                f"    {judgment.pair_id} [{judgment.criterion_index}] "
                f"matcher={judgment.matcher_verdict:<13} "
                f"judge={judgment.judge_label:<11} "
                f"confidence={judgment.confidence:<6} errors={errors}"
            )
            lines.append(f"      {judgment.rationale}")

    lines.append("")
    return "\n".join(lines)


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _num(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


__all__ = ["render_layer_three"]
