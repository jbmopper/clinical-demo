"""Tests for layer-2 Chia entity-mention F1."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from clinical_demo.data.chia import load_document
from clinical_demo.evals.layer_two import (
    SUPPORTED_ENTITY_TYPES,
    build_layer_two_report,
    normalize_mention_text,
    score_chia_document,
)
from clinical_demo.evals.report_layer_two import render_layer_two
from clinical_demo.extractor.schema import (
    EntityMention,
    EntityType,
    ExtractedCriteria,
    ExtractedCriterion,
    ExtractionMetadata,
    FreeTextCriterion,
)

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "chia"


def test_normalize_mention_text_is_conservative() -> None:
    assert normalize_mention_text("  HbA1c ≥ 7.0%; ") == "hba1c >= 7.0%"


def test_score_chia_document_counts_entity_type_surface_matches() -> None:
    doc = load_document(FIXTURE_DIR / "NCT00050349_inc.txt")
    first_gold = next(e for e in doc.entities.values() if e.type in SUPPORTED_ENTITY_TYPES)
    extraction = _extraction(
        [
            EntityMention(
                text=f"  {first_gold.text.upper()}  ",
                type=cast(EntityType, first_gold.type),
            ),
            EntityMention(text="not in the gold annotations", type="Condition"),
        ]
    )

    report = score_chia_document(doc, extraction, nct_id="NCT00050349", section="inclusion")

    assert report.doc_id == "NCT00050349_inc"
    assert report.gold > 30
    assert report.predicted == 2
    assert report.true_positive == 1
    assert report.precision == 0.5
    assert report.false_positives[0].text == "not in the gold annotations"
    assert report.false_negatives


def test_build_layer_two_report_micro_and_macro_f1() -> None:
    doc = load_document(FIXTURE_DIR / "NCT00050349_inc.txt")
    first_two = [e for e in doc.entities.values() if e.type in SUPPORTED_ENTITY_TYPES][:2]
    perfect = score_chia_document(
        doc,
        _extraction([EntityMention(text=e.text, type=cast(EntityType, e.type)) for e in first_two]),
        nct_id="NCT00050349",
        section="inclusion",
    )
    empty = score_chia_document(
        doc,
        _extraction([]),
        nct_id="NCT00050349",
        section="inclusion",
    )

    report = build_layer_two_report([perfect, empty])

    assert report.n_documents == 2
    assert report.gold == perfect.gold + empty.gold
    assert report.predicted == 2
    assert report.true_positive == 2
    assert report.f1 is not None
    assert report.macro_f1 is not None
    assert report.by_type


def test_render_layer_two_mentions_lowest_scoring_document() -> None:
    doc = load_document(FIXTURE_DIR / "NCT00050349_inc.txt")
    doc_report = score_chia_document(
        doc,
        _extraction([]),
        nct_id="NCT00050349",
        section="inclusion",
    )

    text = render_layer_two(build_layer_two_report([doc_report]))

    assert "Layer-2 Chia entity-mention report" in text
    assert "NCT00050349_inc" in text
    assert "missed:" in text


def _extraction(mentions: list[EntityMention]) -> ExtractedCriteria:
    return ExtractedCriteria(
        criteria=[
            ExtractedCriterion(
                kind="free_text",
                polarity="inclusion",
                source_text="fixture",
                negated=False,
                mood="actual",
                age=None,
                sex=None,
                condition=None,
                medication=None,
                measurement=None,
                temporal_window=None,
                free_text=FreeTextCriterion(note=""),
                mentions=mentions,
            )
        ],
        metadata=ExtractionMetadata(notes=""),
    )
