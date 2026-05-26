"""Tests for deterministic criterion fixing."""

from __future__ import annotations

from clinical_demo.extractor.fix import CRITERION_FIX_NOTE_PREFIX, fix_extracted_criteria
from clinical_demo.extractor.schema import EntityMention, ExtractedCriteria, ExtractionMetadata
from tests.matcher._fixtures import (
    crit_condition,
    crit_free_text,
    crit_measurement,
    crit_temporal_window,
)


def _extracted(*criteria) -> ExtractedCriteria:
    return ExtractedCriteria(
        criteria=list(criteria),
        metadata=ExtractionMetadata(notes=""),
    )


def test_fix_normalizes_known_condition_and_measurement_surfaces() -> None:
    out = fix_extracted_criteria(
        _extracted(
            crit_condition(text="mild to moderate hypertension"),
            crit_measurement(text="C-peptide concentrations"),
        )
    )

    condition = out.criteria[0]
    measurement = out.criteria[1]
    assert condition.condition is not None
    assert condition.condition.condition_text == "hypertension"
    assert measurement.measurement is not None
    assert measurement.measurement.measurement_text == "C-peptide"
    assert CRITERION_FIX_NOTE_PREFIX in out.metadata.notes


def test_fix_infers_blood_pressure_specificity_from_source_text() -> None:
    criterion = crit_measurement(text="blood pressure", operator="<=", value=140, unit="mmHg")
    criterion = criterion.model_copy(update={"source_text": "Systolic blood pressure <= 140 mmHg"})

    out = fix_extracted_criteria(_extracted(criterion))

    assert out.criteria[0].measurement is not None
    assert out.criteria[0].measurement.measurement_text == "systolic blood pressure"


def test_fix_converts_diagnosis_temporal_window_to_condition() -> None:
    out = fix_extracted_criteria(
        _extracted(
            crit_temporal_window(event_text="T1D diagnosis", window_days=0, direction="within_past")
        )
    )

    assert out.criteria[0].kind == "condition_present"
    assert out.criteria[0].condition is not None
    assert out.criteria[0].condition.condition_text == "type 1 diabetes"
    assert out.criteria[0].source_text == "temporal criterion"


def test_fix_routes_unsafe_composite_to_free_text_review() -> None:
    original = crit_condition(text="pregnant or breastfeeding", kind="condition_absent")

    out = fix_extracted_criteria(_extracted(original))

    assert len(out.criteria) == 1
    fixed = out.criteria[0]
    assert fixed.kind == "free_text"
    assert fixed.source_text == original.source_text
    assert fixed.free_text is not None
    assert "unsafe composite" in fixed.free_text.note
    assert "condition_absent" in fixed.free_text.note


def test_fix_marks_clinical_trial_participation_as_interview_required() -> None:
    original = crit_free_text(
        source_text=(
            "Patient is participating in a clinical trial of another investigational "
            "drug or device."
        )
    )

    out = fix_extracted_criteria(_extracted(original))

    fixed = out.criteria[0]
    assert fixed.kind == "free_text"
    assert fixed.free_text is not None
    assert "interview_required" in fixed.free_text.note
    assert "interview_required" in out.metadata.notes


def test_fix_emits_native_composite_groups_for_explicit_or_bundle() -> None:
    original = crit_free_text().model_copy(
        update={
            "source_text": (
                "Hyperglycemia (HbA1c >= 6.5%; OR fasting plasma glucose >= "
                "126 mg/dL; OR random plasma glucose >= 200 mg/dL)"
            )
        }
    )

    out = fix_extracted_criteria(_extracted(original))

    assert out.criteria == [original]
    assert len(out.composite_groups) == 1
    group = out.composite_groups[0]
    assert group.group_id == "criterion:0:group:001"
    assert group.operator == "any_of"
    assert group.parent_criterion_index == 0
    assert [subcheck.subcheck_id for subcheck in group.subchecks] == [
        "criterion:0:group:001:subcheck:001",
        "criterion:0:group:001:subcheck:002",
        "criterion:0:group:001:subcheck:003",
    ]
    assert group.subchecks[0].criterion.kind == "measurement_threshold"
    assert group.subchecks[1].criterion.kind == "free_text"
    assert "native composite" in out.metadata.notes


def test_fix_promotes_parenthetical_condition_mentions_to_any_of_group() -> None:
    original = crit_free_text(
        polarity="exclusion",
        source_text=("Disorders of bone metabolism (pregnancy, malignancy, breastfeeding)"),
        mentions=[
            EntityMention(text="pregnancy", type="Condition"),
            EntityMention(text="malignancy", type="Condition"),
            EntityMention(text="breastfeeding", type="Condition"),
        ],
    )

    out = fix_extracted_criteria(_extracted(original))

    assert out.criteria == [original]
    group = out.composite_groups[0]
    assert group.operator == "any_of"
    assert group.parent_source_text == original.source_text
    assert [subcheck.source_text for subcheck in group.subchecks] == [
        "pregnancy",
        "malignancy",
        "breastfeeding",
    ]
    assert [subcheck.criterion.kind for subcheck in group.subchecks] == [
        "condition_present",
        "condition_present",
        "condition_present",
    ]
    assert all(subcheck.criterion.polarity == "exclusion" for subcheck in group.subchecks)
    assert all(subcheck.criterion.mentions for subcheck in group.subchecks)


def test_fix_promotes_inline_disjunction_with_temporal_qualifier() -> None:
    original = crit_free_text(
        polarity="exclusion",
        source_text="Pregnancy or breastfeeding within 6 months",
        mentions=[
            EntityMention(text="Pregnancy", type="Condition"),
            EntityMention(text="breastfeeding", type="Condition"),
            EntityMention(text="within 6 months", type="Temporal"),
        ],
    ).model_copy(update={"mood": "historical"})

    out = fix_extracted_criteria(_extracted(original))

    group = out.composite_groups[0]
    assert group.operator == "any_of"
    assert [subcheck.source_text for subcheck in group.subchecks] == [
        "Pregnancy",
        "breastfeeding",
    ]
    condition_texts = []
    for subcheck in group.subchecks:
        condition = subcheck.criterion.condition
        assert condition is not None
        condition_texts.append(condition.condition_text)
    assert condition_texts == [
        "Pregnancy",
        "breastfeeding",
    ]
    assert all(subcheck.criterion.mood == "historical" for subcheck in group.subchecks)
    assert all(subcheck.criterion.negated is False for subcheck in group.subchecks)


def test_fix_promotes_treatment_with_any_following_drug_mentions() -> None:
    original = crit_free_text(
        polarity="exclusion",
        source_text=(
            "Treatment with any of the following drugs in past year: "
            "immunosuppressants, calcitonin, bisphosphonate treatment."
        ),
        mentions=[
            EntityMention(text="immunosuppressants", type="Drug"),
            EntityMention(text="calcitonin", type="Drug"),
            EntityMention(text="bisphosphonate treatment", type="Drug"),
        ],
    )

    out = fix_extracted_criteria(_extracted(original))

    group = out.composite_groups[0]
    assert group.operator == "any_of"
    assert [subcheck.criterion.kind for subcheck in group.subchecks] == [
        "medication_present",
        "medication_present",
        "medication_present",
    ]
    medication_texts = []
    for subcheck in group.subchecks:
        medication = subcheck.criterion.medication
        assert medication is not None
        medication_texts.append(medication.medication_text)
    assert medication_texts == [
        "immunosuppressants",
        "calcitonin",
        "bisphosphonate treatment",
    ]
    assert all(subcheck.criterion.free_text is None for subcheck in group.subchecks)
