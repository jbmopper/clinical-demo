"""Reviewer queue projection for unresolved compiler gaps."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from clinical_demo.extractor.schema import CriterionKind
from clinical_demo.settings import ResolverExecutionPolicy

from .schema import (
    CriterionCompilationResult,
    ResolutionDomain,
    ResolutionGap,
    ResolutionGapKind,
    ResolutionStage,
)

RecommendedAction = Literal[
    "review_mapping",
    "choose_candidate",
    "add_unit_mapping",
    "implement_compiler_logic",
    "decompose_compound_logic",
    "collect_interview_answer",
    "review_gap",
]

Severity = Literal["critical", "high", "medium", "low"]

_ACTION_BY_GAP_KIND: dict[ResolutionGapKind, RecommendedAction] = {
    "unmapped_concept": "review_mapping",
    "ambiguous_mapping": "choose_candidate",
    "missing_unit": "add_unit_mapping",
    "unsupported_predicate": "implement_compiler_logic",
    "normal_range_unknown": "implement_compiler_logic",
    "provenance_required": "implement_compiler_logic",
    "unsupported_compound": "decompose_compound_logic",
    "insufficient_source": "review_gap",
    "interview_required": "collect_interview_answer",
    "not_attempted": "review_gap",
}

_PRIORITY_BY_GAP_KIND: dict[ResolutionGapKind, int] = {
    "unsupported_compound": 10,
    "unmapped_concept": 20,
    "ambiguous_mapping": 30,
    "missing_unit": 40,
    "normal_range_unknown": 55,
    "provenance_required": 55,
    "unsupported_predicate": 60,
    "interview_required": 75,
    "insufficient_source": 80,
    "not_attempted": 90,
}

_SEVERITY_BY_GAP_KIND: dict[ResolutionGapKind, Severity] = {
    "unsupported_compound": "critical",
    "unmapped_concept": "high",
    "ambiguous_mapping": "high",
    "missing_unit": "high",
    "normal_range_unknown": "medium",
    "provenance_required": "medium",
    "unsupported_predicate": "medium",
    "interview_required": "low",
    "insufficient_source": "low",
    "not_attempted": "low",
}


class CompilerGapQueueItem(BaseModel):
    """One reviewer-actionable item generated from a compiler gap."""

    item_id: str = Field(description="Stable queue item id derived from the gap id.")
    source_criterion_id: str = Field(description="Criterion id this queue item belongs to.")
    source_index: int = Field(description="Zero-based criterion index in the source criteria.")
    criterion_kind: CriterionKind = Field(description="Original extractor criterion kind.")
    gap_id: str = Field(description="Compiler gap id that produced this item.")
    gap_kind: ResolutionGapKind = Field(description="Machine-readable compiler gap kind.")
    stage: ResolutionStage = Field(description="Compiler stage that emitted the gap.")
    domain: ResolutionDomain = Field(description="Clinical or compiler domain with the gap.")
    surface: str | None = Field(description="Original surface text, if available.")
    message: str = Field(description="Reviewer-facing gap message.")
    resolver_policy: ResolverExecutionPolicy = Field(
        description="Resolver policy in effect when the gap was produced."
    )
    recommended_action: RecommendedAction = Field(
        description="Suggested offline reviewer action for this gap."
    )
    priority: int = Field(description="Lower numbers sort ahead in reviewer queues.")
    severity: Severity = Field(description="Coarse reviewer severity bucket.")


class CompilerGapQueue(BaseModel):
    """Sorted reviewer queue for unresolved compiler gaps."""

    items: list[CompilerGapQueueItem] = Field(default_factory=list)


def compiler_gap_queue(compilation: CriterionCompilationResult) -> list[CompilerGapQueueItem]:
    """Return sorted reviewer queue items for unresolved compiler gaps.

    Queue items are generated from criterion-local gaps so source index and
    criterion kind are preserved even when the aggregate gap list is reordered.
    """

    items = [
        _queue_item_for_gap(
            gap,
            source_index=criterion.source_index,
            criterion_kind=criterion.criterion_kind,
        )
        for criterion in compilation.criteria
        for gap in criterion.unresolved_gaps
    ]
    return sorted(items, key=_sort_key)


def compiler_gap_queue_object(compilation: CriterionCompilationResult) -> CompilerGapQueue:
    """Return a typed wrapper around the sorted compiler gap queue."""

    return CompilerGapQueue(items=compiler_gap_queue(compilation))


def _queue_item_for_gap(
    gap: ResolutionGap,
    *,
    source_index: int,
    criterion_kind: CriterionKind,
) -> CompilerGapQueueItem:
    return CompilerGapQueueItem(
        item_id=f"queue:{gap.gap_id}",
        source_criterion_id=gap.source_criterion_id,
        source_index=source_index,
        criterion_kind=criterion_kind,
        gap_id=gap.gap_id,
        gap_kind=gap.kind,
        stage=gap.stage,
        domain=gap.domain,
        surface=gap.surface,
        message=gap.message,
        resolver_policy=gap.resolver_policy,
        recommended_action=_ACTION_BY_GAP_KIND.get(gap.kind, "review_gap"),
        priority=_PRIORITY_BY_GAP_KIND.get(gap.kind, 100),
        severity=_SEVERITY_BY_GAP_KIND.get(gap.kind, "low"),
    )


def _sort_key(item: CompilerGapQueueItem) -> tuple[int, int, str, str]:
    return (item.priority, item.source_index, item.gap_id, item.item_id)


__all__ = [
    "CompilerGapQueue",
    "CompilerGapQueueItem",
    "RecommendedAction",
    "Severity",
    "compiler_gap_queue",
    "compiler_gap_queue_object",
]
