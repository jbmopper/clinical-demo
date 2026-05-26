from __future__ import annotations

from clinical_demo.compiler import compile_extracted_criteria
from clinical_demo.compiler.reviewer_queue import compiler_gap_queue, compiler_gap_queue_object
from clinical_demo.extractor.schema import (
    CompositeCriterionGroup,
    CompositeCriterionSubcheck,
    ConditionCriterion,
    ExtractedCriteria,
    ExtractedCriterion,
    ExtractionMetadata,
    FreeTextCriterion,
    MeasurementCriterion,
)


def _condition(text: str) -> ExtractedCriterion:
    return ExtractedCriterion(
        kind="condition_present",
        polarity="inclusion",
        source_text=f"History of {text}",
        negated=False,
        mood="historical",
        age=None,
        sex=None,
        condition=ConditionCriterion(condition_text=text),
        medication=None,
        measurement=None,
        temporal_window=None,
        free_text=None,
        mentions=[],
    )


def _measurement(text: str, unit: str | None = "%") -> ExtractedCriterion:
    return ExtractedCriterion(
        kind="measurement_threshold",
        polarity="inclusion",
        source_text=f"{text} >= 7{unit or ''}",
        negated=False,
        mood="actual",
        age=None,
        sex=None,
        condition=None,
        medication=None,
        measurement=MeasurementCriterion(
            measurement_text=text,
            operator=">=",
            value=7.0,
            value_low=None,
            value_high=None,
            unit=unit,
        ),
        temporal_window=None,
        free_text=None,
        mentions=[],
    )


def _free_text(text: str = "Narrative-only criterion") -> ExtractedCriterion:
    return ExtractedCriterion(
        kind="free_text",
        polarity="inclusion",
        source_text=text,
        negated=False,
        mood="actual",
        age=None,
        sex=None,
        condition=None,
        medication=None,
        measurement=None,
        temporal_window=None,
        free_text=FreeTextCriterion(note="not structured yet"),
        mentions=[],
    )


def test_unmapped_structured_condition_emits_review_mapping_action() -> None:
    result = compile_extracted_criteria([_condition("rare unknown syndrome")])

    items = compiler_gap_queue(result)

    assert len(items) == 1
    item = items[0]
    assert item.item_id == f"queue:{item.gap_id}"
    assert item.source_criterion_id == "criterion:0"
    assert item.source_index == 0
    assert item.criterion_kind == "condition_present"
    assert item.gap_kind == "unmapped_concept"
    assert item.stage == "concept_resolution"
    assert item.domain == "condition"
    assert item.surface == "rare unknown syndrome"
    assert item.resolver_policy == "cached_only"
    assert item.recommended_action == "review_mapping"
    assert item.severity == "high"


def test_unknown_measurement_missing_unit_emits_mapping_and_unit_queue_items() -> None:
    result = compile_extracted_criteria([_measurement("BNP", unit=None)])

    items = compiler_gap_queue(result)

    assert [item.gap_kind for item in items] == ["unmapped_concept", "missing_unit"]
    assert [item.recommended_action for item in items] == [
        "review_mapping",
        "add_unit_mapping",
    ]
    assert [item.domain for item in items] == ["measurement", "unit"]
    assert [item.surface for item in items] == ["BNP", "BNP"]


def test_measurement_taxonomy_gaps_stay_compiler_logic_actions() -> None:
    result = compile_extracted_criteria(
        [
            _measurement("fasting plasma glucose", unit="mg/dL"),
            _measurement("clinical laboratory values", unit="normal ranges"),
        ]
    )

    items = compiler_gap_queue(result)

    assert [item.gap_kind for item in items] == [
        "provenance_required",
        "normal_range_unknown",
    ]
    assert [item.recommended_action for item in items] == [
        "implement_compiler_logic",
        "implement_compiler_logic",
    ]
    assert [item.priority for item in items] == [55, 55]
    assert [item.severity for item in items] == ["medium", "medium"]


def test_unsupported_compound_emits_decompose_compound_logic_action() -> None:
    parent = _free_text("HbA1c >= 7% with conflicting composite groups")
    subcheck = _measurement("HbA1c")
    extracted = ExtractedCriteria(
        criteria=[parent],
        composite_groups=[
            CompositeCriterionGroup(
                group_id="criterion:0:group:001",
                operator="any_of",
                parent_criterion_index=0,
                parent_source_text=parent.source_text,
                subchecks=[
                    CompositeCriterionSubcheck(
                        subcheck_id="criterion:0:group:001:subcheck:001",
                        operator="any_of",
                        source_text=subcheck.source_text,
                        criterion=subcheck,
                    )
                ],
            ),
            CompositeCriterionGroup(
                group_id="criterion:0:group:002",
                operator="all_of",
                parent_criterion_index=0,
                parent_source_text=parent.source_text,
                subchecks=[
                    CompositeCriterionSubcheck(
                        subcheck_id="criterion:0:group:002:subcheck:001",
                        operator="all_of",
                        source_text=subcheck.source_text,
                        criterion=subcheck,
                    )
                ],
            ),
        ],
        metadata=ExtractionMetadata(notes=""),
    )

    result = compile_extracted_criteria(extracted)
    items = compiler_gap_queue(result)

    assert len(items) == 1
    assert items[0].gap_kind == "unsupported_compound"
    assert items[0].domain == "compound"
    assert items[0].recommended_action == "decompose_compound_logic"
    assert items[0].severity == "critical"


def test_free_text_without_unresolved_gaps_does_not_create_queue_item() -> None:
    result = compile_extracted_criteria([_free_text()])

    assert compiler_gap_queue(result) == []
    assert compiler_gap_queue_object(result).items == []


def test_interview_required_gap_emits_interview_action() -> None:
    result = compile_extracted_criteria(
        [
            _free_text(
                "Patient is participating in a clinical trial of another investigational drug."
            )
        ]
    )

    items = compiler_gap_queue(result)

    assert len(items) == 1
    assert items[0].gap_kind == "interview_required"
    assert items[0].recommended_action == "collect_interview_answer"
    assert items[0].priority == 75
    assert items[0].severity == "low"


def test_queue_ordering_is_deterministic() -> None:
    parent = _free_text("conflicting groups")
    unknown_condition = _condition("rare unknown syndrome")
    unknown_measurement = _measurement("BNP", unit=None)
    extracted = ExtractedCriteria(
        criteria=[unknown_measurement, parent, unknown_condition],
        composite_groups=[
            CompositeCriterionGroup(
                group_id="criterion:1:group:001",
                operator="any_of",
                parent_criterion_index=1,
                parent_source_text=parent.source_text,
                subchecks=[
                    CompositeCriterionSubcheck(
                        subcheck_id="criterion:1:group:001:subcheck:001",
                        operator="any_of",
                        source_text=unknown_measurement.source_text,
                        criterion=unknown_measurement,
                    )
                ],
            ),
            CompositeCriterionGroup(
                group_id="criterion:1:group:002",
                operator="all_of",
                parent_criterion_index=1,
                parent_source_text=parent.source_text,
                subchecks=[
                    CompositeCriterionSubcheck(
                        subcheck_id="criterion:1:group:002:subcheck:001",
                        operator="all_of",
                        source_text=unknown_condition.source_text,
                        criterion=unknown_condition,
                    )
                ],
            ),
        ],
        metadata=ExtractionMetadata(notes=""),
    )

    first = compiler_gap_queue(compile_extracted_criteria(extracted))
    second = compiler_gap_queue(compile_extracted_criteria(extracted))

    assert [item.item_id for item in first] == [item.item_id for item in second]
    assert [item.recommended_action for item in first] == [
        "decompose_compound_logic",
        "review_mapping",
        "review_mapping",
        "add_unit_mapping",
    ]
    assert [item.source_index for item in first] == [1, 0, 2, 0]
