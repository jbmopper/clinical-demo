"""Layer-2 eval: extractor entity-mention F1 against Chia.

Chia's gold labels are BRAT entity spans plus relations and equivalence
groups. The current extractor schema intentionally does not emit a
relation graph; it emits matcher-ready criteria with an audit-only
`mentions` list. Layer 2 therefore starts with the comparable slice:
entity mention `(type, normalized surface)` precision / recall / F1.

This is a real reference-based signal, but a deliberately bounded one.
It should not be described as full Chia graph F1 until the extractor
emits relation-shaped output.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import get_args

from pydantic import BaseModel, Field

from clinical_demo.data.chia import ChiaDocument
from clinical_demo.extractor.schema import EntityType, ExtractedCriteria

SUPPORTED_ENTITY_TYPES: frozenset[str] = frozenset(get_args(EntityType))
"""Chia entity labels the extractor schema can emit today."""


class MentionKey(BaseModel):
    """Comparable unit for layer-2 entity scoring."""

    type: str
    text: str
    count: int = 1


class MentionTypeStats(BaseModel):
    """Per-entity-type precision / recall / F1."""

    type: str
    gold: int
    predicted: int
    true_positive: int
    precision: float | None
    recall: float | None
    f1: float | None


class LayerTwoDocumentReport(BaseModel):
    """Layer-2 metrics for one Chia inclusion/exclusion document."""

    doc_id: str
    nct_id: str
    section: str
    gold: int
    predicted: int
    true_positive: int
    precision: float | None
    recall: float | None
    f1: float | None
    skipped_gold_unsupported: dict[str, int] = Field(default_factory=dict)
    by_type: list[MentionTypeStats] = Field(default_factory=list)
    false_positives: list[MentionKey] = Field(default_factory=list)
    false_negatives: list[MentionKey] = Field(default_factory=list)


class LayerTwoReport(BaseModel):
    """Aggregate Chia entity-mention F1 report."""

    documents: list[LayerTwoDocumentReport]
    gold: int
    predicted: int
    true_positive: int
    precision: float | None
    recall: float | None
    f1: float | None
    macro_f1: float | None
    by_type: list[MentionTypeStats] = Field(default_factory=list)
    skipped_gold_unsupported: dict[str, int] = Field(default_factory=dict)

    @property
    def n_documents(self) -> int:
        return len(self.documents)


def score_chia_document(
    document: ChiaDocument,
    extraction: ExtractedCriteria,
    *,
    nct_id: str,
    section: str,
    sample_limit: int = 20,
) -> LayerTwoDocumentReport:
    """Compare one extractor output against one Chia document."""

    gold, skipped = _gold_mentions(document)
    predicted = _predicted_mentions(extraction)
    return _document_report(
        doc_id=document.doc_id,
        nct_id=nct_id,
        section=section,
        gold=gold,
        predicted=predicted,
        skipped=skipped,
        sample_limit=sample_limit,
    )


def build_layer_two_report(documents: list[LayerTwoDocumentReport]) -> LayerTwoReport:
    """Aggregate per-document layer-2 metrics."""

    gold_total = sum(d.gold for d in documents)
    predicted_total = sum(d.predicted for d in documents)
    tp_total = sum(d.true_positive for d in documents)
    by_type_counts: dict[str, Counter[str]] = defaultdict(Counter)
    skipped: Counter[str] = Counter()

    for doc in documents:
        skipped.update(doc.skipped_gold_unsupported)
        for stat in doc.by_type:
            by_type_counts[stat.type]["gold"] += stat.gold
            by_type_counts[stat.type]["predicted"] += stat.predicted
            by_type_counts[stat.type]["true_positive"] += stat.true_positive

    f1_values = [d.f1 for d in documents if d.f1 is not None]

    return LayerTwoReport(
        documents=documents,
        gold=gold_total,
        predicted=predicted_total,
        true_positive=tp_total,
        precision=_rate(tp_total, predicted_total),
        recall=_rate(tp_total, gold_total),
        f1=_f1(tp_total, predicted_total, gold_total),
        macro_f1=(sum(f1_values) / len(f1_values) if f1_values else None),
        by_type=[
            _type_stats(
                type_=type_,
                gold=counts["gold"],
                predicted=counts["predicted"],
                true_positive=counts["true_positive"],
            )
            for type_, counts in sorted(by_type_counts.items())
        ],
        skipped_gold_unsupported=dict(sorted(skipped.items())),
    )


def _document_report(
    *,
    doc_id: str,
    nct_id: str,
    section: str,
    gold: Counter[tuple[str, str]],
    predicted: Counter[tuple[str, str]],
    skipped: Counter[str],
    sample_limit: int,
) -> LayerTwoDocumentReport:
    overlap = gold & predicted
    false_pos = predicted - gold
    false_neg = gold - predicted
    gold_total = gold.total()
    predicted_total = predicted.total()
    tp_total = overlap.total()

    types = sorted({type_ for type_, _ in gold} | {type_ for type_, _ in predicted})
    return LayerTwoDocumentReport(
        doc_id=doc_id,
        nct_id=nct_id,
        section=section,
        gold=gold_total,
        predicted=predicted_total,
        true_positive=tp_total,
        precision=_rate(tp_total, predicted_total),
        recall=_rate(tp_total, gold_total),
        f1=_f1(tp_total, predicted_total, gold_total),
        skipped_gold_unsupported=dict(sorted(skipped.items())),
        by_type=[
            _type_stats(
                type_=type_,
                gold=sum(count for (t, _), count in gold.items() if t == type_),
                predicted=sum(count for (t, _), count in predicted.items() if t == type_),
                true_positive=sum(count for (t, _), count in overlap.items() if t == type_),
            )
            for type_ in types
        ],
        false_positives=_mention_samples(false_pos, sample_limit),
        false_negatives=_mention_samples(false_neg, sample_limit),
    )


def _gold_mentions(document: ChiaDocument) -> tuple[Counter[tuple[str, str]], Counter[str]]:
    out: Counter[tuple[str, str]] = Counter()
    skipped: Counter[str] = Counter()
    for entity in document.entities.values():
        if entity.type not in SUPPORTED_ENTITY_TYPES:
            skipped[entity.type] += 1
            continue
        normalized = normalize_mention_text(entity.text)
        if normalized:
            out[(entity.type, normalized)] += 1
    return out, skipped


def _predicted_mentions(extraction: ExtractedCriteria) -> Counter[tuple[str, str]]:
    out: Counter[tuple[str, str]] = Counter()
    for criterion in extraction.criteria:
        for mention in criterion.mentions:
            normalized = normalize_mention_text(mention.text)
            if normalized:
                out[(mention.type, normalized)] += 1
    return out


def normalize_mention_text(value: str) -> str:
    """Normalize mention surfaces enough for fair exact matching.

    Keep this intentionally conservative: lowercasing, punctuation at
    the edges, whitespace, and common unicode comparison marks. We do
    not stem, synonym-map, or code-normalize here because layer 2 is
    measuring extraction fidelity, not terminology expansion.
    """

    text = value.casefold()
    text = text.replace("≤", "<=").replace("≥", ">=")
    text = re.sub(r"\s+", " ", text)
    return text.strip(" \t\r\n.,;:()[]{}\"'")


def _mention_samples(counter: Counter[tuple[str, str]], limit: int) -> list[MentionKey]:
    return [
        MentionKey(type=type_, text=text, count=count)
        for (type_, text), count in counter.most_common(limit)
    ]


def _type_stats(
    *,
    type_: str,
    gold: int,
    predicted: int,
    true_positive: int,
) -> MentionTypeStats:
    return MentionTypeStats(
        type=type_,
        gold=gold,
        predicted=predicted,
        true_positive=true_positive,
        precision=_rate(true_positive, predicted),
        recall=_rate(true_positive, gold),
        f1=_f1(true_positive, predicted, gold),
    )


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _f1(true_positive: int, predicted: int, gold: int) -> float | None:
    precision = _rate(true_positive, predicted)
    recall = _rate(true_positive, gold)
    if precision is None or recall is None:
        return None
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


__all__ = [
    "SUPPORTED_ENTITY_TYPES",
    "LayerTwoDocumentReport",
    "LayerTwoReport",
    "MentionKey",
    "MentionTypeStats",
    "build_layer_two_report",
    "normalize_mention_text",
    "score_chia_document",
]
