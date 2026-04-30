"""Text renderer for layer-2 Chia entity-mention F1 reports."""

from __future__ import annotations

from .layer_two import LayerTwoReport

_TYPE_LIMIT = 16
_DOC_LIMIT = 12
_MISS_LIMIT = 8


def render_layer_two(report: LayerTwoReport) -> str:
    lines: list[str] = []
    lines.append("\nLayer-2 Chia entity-mention report")
    lines.append(
        f"  documents: {report.n_documents}"
        f"  gold: {report.gold}"
        f"  predicted: {report.predicted}"
        f"  true_positive: {report.true_positive}"
    )
    lines.append(
        f"  micro precision: {_pct(report.precision)}"
        f"  recall: {_pct(report.recall)}"
        f"  f1: {_pct(report.f1)}"
        f"  macro f1: {_pct(report.macro_f1)}"
    )
    if report.skipped_gold_unsupported:
        skipped = "  ".join(
            f"{kind}={count}" for kind, count in sorted(report.skipped_gold_unsupported.items())
        )
        lines.append(f"  skipped gold entity types unsupported by extractor schema: {skipped}")

    if report.by_type:
        lines.append("")
        lines.append("  per entity type:")
        lines.append(
            f"    {'type':<18} {'gold':>5} {'pred':>5} {'tp':>5}"
            f"  {'precision':>9} {'recall':>8} {'f1':>8}"
        )
        for stat in sorted(report.by_type, key=lambda s: (-s.gold, s.type))[:_TYPE_LIMIT]:
            lines.append(
                f"    {stat.type:<18} {stat.gold:>5} {stat.predicted:>5} {stat.true_positive:>5}"
                f"  {_pct(stat.precision):>9} {_pct(stat.recall):>8} {_pct(stat.f1):>8}"
            )

    if report.documents:
        lines.append("")
        lines.append(f"  documents (showing up to {_DOC_LIMIT} lowest F1):")
        for doc in sorted(
            report.documents, key=lambda d: (d.f1 if d.f1 is not None else -1, d.doc_id)
        )[:_DOC_LIMIT]:
            lines.append(
                f"    {doc.doc_id:<20} {doc.section:<9}"
                f" gold={doc.gold:<4} pred={doc.predicted:<4} tp={doc.true_positive:<4}"
                f" f1={_pct(doc.f1)}"
            )
            if doc.false_negatives:
                misses = ", ".join(
                    f"{m.type}:{m.text} x{m.count}" for m in doc.false_negatives[:_MISS_LIMIT]
                )
                lines.append(f"      missed: {misses}")

    lines.append("")
    return "\n".join(lines)


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


__all__ = ["render_layer_two"]
