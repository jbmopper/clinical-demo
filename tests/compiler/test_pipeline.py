from __future__ import annotations

from clinical_demo.compiler import compile_extracted_criteria
from clinical_demo.extractor.schema import (
    AgeCriterion,
    CompositeCriterionGroup,
    CompositeCriterionSubcheck,
    ConditionCriterion,
    EntityMention,
    ExtractedCriteria,
    ExtractedCriterion,
    ExtractionMetadata,
    FreeTextCriterion,
    MeasurementCriterion,
    MedicationCriterion,
    TemporalWindowCriterion,
    ThresholdOperator,
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


def _measurement_with_value(
    text: str,
    *,
    operator: ThresholdOperator = ">=",
    value: float | None,
    unit: str | None,
) -> ExtractedCriterion:
    return ExtractedCriterion(
        kind="measurement_threshold",
        polarity="inclusion",
        source_text=f"{text} {operator} {value if value is not None else ''}{unit or ''}",
        negated=False,
        mood="actual",
        age=None,
        sex=None,
        condition=None,
        medication=None,
        measurement=MeasurementCriterion(
            measurement_text=text,
            operator=operator,
            value=value,
            value_low=None,
            value_high=None,
            unit=unit,
        ),
        temporal_window=None,
        free_text=None,
        mentions=[],
    )


def _medication(text: str, source_text: str | None = None) -> ExtractedCriterion:
    return ExtractedCriterion(
        kind="medication_present",
        polarity="inclusion",
        source_text=source_text or f"Receiving {text}",
        negated=False,
        mood="actual",
        age=None,
        sex=None,
        condition=None,
        medication=MedicationCriterion(medication_text=text),
        measurement=None,
        temporal_window=None,
        free_text=None,
        mentions=[],
    )


def _free_text(text: str) -> ExtractedCriterion:
    return ExtractedCriterion(
        kind="free_text",
        polarity="exclusion",
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


def _age(minimum_years: float = 18.0) -> ExtractedCriterion:
    return ExtractedCriterion(
        kind="age",
        polarity="inclusion",
        source_text=f"Age >= {minimum_years:g} years",
        negated=False,
        mood="actual",
        age=AgeCriterion(minimum_years=minimum_years, maximum_years=None),
        sex=None,
        condition=None,
        medication=None,
        measurement=None,
        temporal_window=None,
        free_text=None,
        mentions=[],
    )


def _temporal(event_text: str, window_days: int = 365) -> ExtractedCriterion:
    return ExtractedCriterion(
        kind="temporal_window",
        polarity="exclusion",
        source_text=f"{event_text} within {window_days} days",
        negated=False,
        mood="historical",
        age=None,
        sex=None,
        condition=None,
        medication=None,
        measurement=None,
        temporal_window=TemporalWindowCriterion(
            event_text=event_text,
            window_days=window_days,
            direction="within_past",
        ),
        free_text=None,
        mentions=[],
    )


def test_pipeline_preserves_count_order_text_kind_and_matcher_inputs() -> None:
    criteria = [_condition("type 2 diabetes"), _measurement("HbA1c"), _free_text("Unable")]
    extracted = ExtractedCriteria(criteria=criteria, metadata=ExtractionMetadata(notes=""))

    result = compile_extracted_criteria(extracted)

    assert result.source_criteria_count == 3
    assert result.matcher_inputs == criteria
    assert [item.source_text for item in result.criteria] == [item.source_text for item in criteria]
    assert [item.criterion_kind for item in result.criteria] == [item.kind for item in criteria]


def test_pipeline_assigns_stable_ids() -> None:
    criteria = [_condition("type 2 diabetes"), _measurement("HbA1c")]

    first = compile_extracted_criteria(criteria, resolver_policy="cached_only")
    second = compile_extracted_criteria(criteria, resolver_policy="cached_only")

    assert [item.source_criterion_id for item in first.criteria] == ["criterion:0", "criterion:1"]
    assert [item.compiled_id for item in first.criteria] == [
        "compiled:criterion:0",
        "compiled:criterion:1",
    ]
    assert [item.compiled_id for item in first.criteria] == [
        item.compiled_id for item in second.criteria
    ]


def test_exact_reviewed_condition_mapping_wins_over_generated_variants() -> None:
    criterion = _condition("previous malignancies")

    result = compile_extracted_criteria([criterion], resolver_policy="cached_only")
    compiled = result.criteria[0]

    assert compiled.unresolved_gaps == []
    assert compiled.predicate.status == "resolved"
    assert compiled.checkable_predicates[0].target_codes == frozenset({"266987004"})


def test_empty_input_compiles_to_empty_result() -> None:
    result = compile_extracted_criteria(
        ExtractedCriteria(criteria=[], metadata=ExtractionMetadata(notes="empty"))
    )

    assert result.source_criteria_count == 0
    assert result.criteria == []
    assert result.matcher_inputs == []
    assert result.unresolved_gaps == []
    assert result.diagnostics == []


def test_compound_logic_compiles_without_changing_matcher_input() -> None:
    parent = _free_text("HbA1c >= 7% or fasting glucose >= 126 mg/dL")
    subcheck = _measurement("HbA1c", unit="%")
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
            )
        ],
        metadata=ExtractionMetadata(notes=""),
    )

    result = compile_extracted_criteria(extracted, resolver_policy="live_allowed")
    compiled = result.criteria[0]

    assert result.matcher_inputs == [parent]
    assert compiled.resolver_policy == "live_allowed"
    assert compiled.compound_logic.status == "resolved"
    assert compiled.compound_logic.operator == "any_of"
    assert compiled.compound_logic.source_group_ids == ["criterion:0:group:001"]
    assert compiled.compound_logic.subcheck_ids == ["criterion:0:group:001:subcheck:001"]
    assert compiled.unit_normalization.status == "skipped"
    assert compiled.predicate.status == "resolved"
    assert compiled.predicate.predicate_kind == "compound"
    assert compiled.predicate.predicate_ids == [
        "criterion:0:group:001:subcheck:001:predicate:measurement"
    ]
    assert compiled.checkable_predicates[0].source_criterion_id == (
        "criterion:0:group:001:subcheck:001"
    )
    assert compiled.checkable_predicates[0].predicate_kind == "measurement_threshold"


def test_measurement_unit_resolution_builds_checkable_predicate() -> None:
    criterion = _measurement("HbA1c", unit="%")

    result = compile_extracted_criteria([criterion])
    compiled = result.criteria[0]

    assert compiled.unit_normalization.status == "resolved"
    assert compiled.unit_normalization.measurement_surface == "HbA1c"
    assert compiled.unit_normalization.source_unit == "%"
    assert compiled.unit_normalization.conventional_unit == "%"
    assert compiled.predicate.predicate_kind == "measurement_threshold"
    assert compiled.predicate.status == "resolved"
    assert compiled.predicate.predicate_ids == ["criterion:0:predicate:measurement"]
    assert [predicate.predicate_id for predicate in result.checkable_predicates] == [
        "criterion:0:predicate:measurement"
    ]
    predicate = compiled.checkable_predicates[0]
    assert predicate.target_system == "http://loinc.org"
    assert predicate.target_codes == frozenset({"4548-4"})
    assert predicate.operator == ">="
    assert predicate.value == 7.0
    assert predicate.unit == "%"
    assert result.resolved_supports == compiled.resolved_supports


def test_measurement_missing_threshold_value_blocks_checkable_predicate() -> None:
    criterion = _measurement_with_value(
        "aspartate aminotransferase",
        operator="<=",
        value=None,
        unit=None,
    )

    result = compile_extracted_criteria([criterion])
    compiled = result.criteria[0]

    assert compiled.unit_normalization.status == "resolved"
    assert compiled.predicate.status == "unresolved"
    assert compiled.checkable_predicates == []
    assert [gap.kind for gap in compiled.unresolved_gaps] == ["insufficient_source"]
    assert compiled.unresolved_gaps[0].stage == "predicate_translation"


def test_condition_compiler_maps_reviewed_fracture_surface_after_variant_cleanup() -> None:
    criterion = _condition(
        "Bone fractures (excluding skull, facial bones, metacarpals, fingers, toes "
        "and spontaneous fractures associated with severe trauma) within the past 12 months"
    )

    result = compile_extracted_criteria([criterion], resolver_policy="cached_only")
    compiled = result.criteria[0]

    assert result.matcher_inputs == [criterion]
    assert compiled.expansion.status == "resolved"
    assert compiled.expansion.strategy == "reviewed_code_list"
    assert compiled.predicate.status == "resolved"
    assert compiled.predicate.predicate_ids == ["criterion:0:predicate:condition"]
    assert compiled.checkable_predicates[0].predicate_kind == "condition_presence"
    assert "263102004" in compiled.checkable_predicates[0].target_codes
    assert compiled.resolved_supports[0].normalized_surface == "bone fractures"
    assert {support.stage for support in compiled.resolved_supports} == {
        "concept_resolution",
        "expansion",
    }
    assert result.checkable_predicates == compiled.checkable_predicates
    assert result.unresolved_gaps == []


def test_condition_surface_reviewed_as_procedure_history_compiles_to_procedure_predicate() -> None:
    result = compile_extracted_criteria([_condition("history of full pneumonectomy")])

    compiled = result.criteria[0]

    assert compiled.expansion.domain == "procedure"
    assert compiled.expansion.status == "resolved"
    assert compiled.predicate.predicate_kind == "procedure_history"
    assert compiled.predicate.status == "resolved"
    assert compiled.checkable_predicates[0].predicate_kind == "procedure_history"
    assert compiled.checkable_predicates[0].target_codes == frozenset({"49795001", "232647000"})
    assert compiled.resolved_supports[0].domain == "procedure"


def test_free_text_procedure_mention_promotes_to_procedure_history_predicate() -> None:
    criterion = _free_text("History of full pneumonectomy").model_copy(
        update={
            "mentions": [
                EntityMention(text="full pneumonectomy", type="Procedure"),
            ]
        }
    )

    result = compile_extracted_criteria([criterion])
    compiled = result.criteria[0]

    assert compiled.expansion.domain == "procedure"
    assert compiled.predicate.predicate_kind == "procedure_history"
    assert compiled.checkable_predicates[0].predicate_kind == "procedure_history"
    assert compiled.diagnostics[0].code == "free_text.promoted.procedure"


def test_condition_typed_free_text_medication_exposure_promotes_to_medication() -> None:
    criterion = _free_text(
        "Received intravenous inotropes (e.g., dobutamine, dopamine, norepinephrine, "
        "vasopressin) within 30 days prior to the screening visit"
    ).model_copy(
        update={
            "mentions": [
                EntityMention(text="Received intravenous inotropes", type="Condition"),
                EntityMention(text="30 days prior to the screening visit", type="Temporal"),
            ]
        }
    )

    result = compile_extracted_criteria([criterion], resolver_policy="cached_only")
    compiled = result.criteria[0]
    predicate = compiled.checkable_predicates[0]

    assert compiled.predicate.status == "resolved"
    assert compiled.predicate.predicate_kind == "medication_exposure"
    assert compiled.expansion.domain == "medication"
    assert predicate.predicate_kind == "medication_exposure"
    assert predicate.surface == "Received intravenous inotropes"
    assert predicate.window_days == 30
    assert predicate.target_codes == frozenset({"242969"})
    assert compiled.diagnostics[0].code == "free_text.promoted.medication"


def test_condition_shaped_anticoagulation_exposure_promotes_to_medication() -> None:
    criterion = _condition("chronic anticoagulation therapy")

    result = compile_extracted_criteria([criterion], resolver_policy="cached_only")
    compiled = result.criteria[0]
    predicate = compiled.checkable_predicates[0]

    assert compiled.predicate.status == "resolved"
    assert compiled.predicate.predicate_kind == "medication_exposure"
    assert compiled.expansion.domain == "medication"
    assert compiled.expansion.strategy == "patient_vocabulary_closure"
    assert predicate.predicate_kind == "medication_exposure"
    assert predicate.surface == "chronic anticoagulation therapy"
    assert predicate.target_codes == frozenset({"855332", "854235", "854252", "1659263"})
    assert compiled.diagnostics[0].code == "condition.promoted.medication"


def test_reviewed_coronary_procedure_surface_compiles_to_procedure_history() -> None:
    result = compile_extracted_criteria([_condition("percutaneous coronary intervention")])

    compiled = result.criteria[0]

    assert compiled.expansion.domain == "procedure"
    assert compiled.expansion.status == "resolved"
    assert compiled.predicate.predicate_kind == "procedure_history"
    assert compiled.checkable_predicates[0].predicate_kind == "procedure_history"
    assert compiled.checkable_predicates[0].target_codes == frozenset({"415070008"})


def test_dialysis_dependent_ckd_condition_decomposes_to_condition_and_procedure_all_of() -> None:
    criterion = _condition("advanced CKD requiring chronic dialysis")

    result = compile_extracted_criteria([criterion], resolver_policy="cached_only")
    compiled = result.criteria[0]

    assert compiled.predicate.status == "resolved"
    assert compiled.predicate.predicate_kind == "compound"
    assert compiled.compound_logic.status == "resolved"
    assert compiled.compound_logic.operator == "all_of"
    assert compiled.compound_logic.subcheck_ids == [
        "criterion:0:renal-dialysis:condition",
        "criterion:0:renal-dialysis:procedure",
    ]
    assert [predicate.predicate_kind for predicate in compiled.checkable_predicates] == [
        "condition_presence",
        "procedure_history",
    ]
    assert "433146000" in compiled.checkable_predicates[0].target_codes
    assert compiled.checkable_predicates[1].target_codes == frozenset({"265764009", "302497006"})
    assert compiled.diagnostics[0].code == "condition.promoted.renal-dialysis-dependence"


def test_dialysis_dependent_end_stage_renal_failure_uses_esrd_condition_atom() -> None:
    criterion = _condition("end-stage renal failure on dialysis")

    result = compile_extracted_criteria([criterion], resolver_policy="cached_only")
    compiled = result.criteria[0]

    assert compiled.predicate.predicate_kind == "compound"
    assert compiled.checkable_predicates[0].predicate_kind == "condition_presence"
    assert compiled.checkable_predicates[0].surface == "end-stage renal disease"
    assert compiled.checkable_predicates[0].target_codes == frozenset({"46177005"})
    assert compiled.checkable_predicates[1].predicate_kind == "procedure_history"


def test_condition_compiler_preserves_raw_hyphenated_lookup_surface() -> None:
    criterion = _condition("end-stage renal disease")

    result = compile_extracted_criteria([criterion], resolver_policy="cached_only")
    compiled = result.criteria[0]

    assert compiled.predicate.status == "resolved"
    assert compiled.resolved_supports[0].normalized_surface == "end-stage renal disease"
    assert compiled.checkable_predicates[0].target_codes == frozenset({"46177005"})


def test_reviewed_condition_nonmapped_emits_typed_gap() -> None:
    criterion = _condition("Uncontrolled systemic hypertension")

    result = compile_extracted_criteria([criterion], resolver_policy="cached_only")
    compiled = result.criteria[0]

    assert compiled.predicate.status == "unresolved"
    assert compiled.expansion.status == "unsupported"
    assert compiled.unresolved_gaps[0].gap_id == (
        "criterion:0:condition:gap:reviewed-composite_unhandled"
    )
    assert compiled.unresolved_gaps[0].kind == "unsupported_predicate"
    assert compiled.diagnostics[0].code == "condition.reviewed.composite_unhandled"


def test_reviewed_nonclinical_condition_routes_to_free_text_review_plan() -> None:
    criterion = _condition("structured exercise program")

    result = compile_extracted_criteria([criterion], resolver_policy="cached_only")
    compiled = result.criteria[0]

    assert compiled.predicate.status == "unsupported"
    assert compiled.predicate.predicate_kind == "free_text_review"
    assert compiled.expansion.status == "skipped"
    assert compiled.expansion.domain == "free_text"
    assert compiled.checkable_predicates == []
    assert compiled.unresolved_gaps[0].gap_id == (
        "criterion:0:free-text-review:gap:reviewed-out_of_scope"
    )
    assert compiled.unresolved_gaps[0].domain == "free_text"
    assert compiled.unresolved_gaps[0].kind == "unsupported_predicate"
    assert compiled.diagnostics[0].code == "condition.reviewed.out_of_scope.free_text_review"


def test_free_text_reviewed_nonclinical_condition_mention_routes_to_review_plan() -> None:
    criterion = _free_text("Participation in a structured exercise program").model_copy(
        update={"mentions": [EntityMention(text="structured exercise program", type="Condition")]}
    )

    result = compile_extracted_criteria([criterion], resolver_policy="cached_only")
    compiled = result.criteria[0]

    assert compiled.criterion_kind == "free_text"
    assert compiled.predicate.status == "unsupported"
    assert compiled.predicate.predicate_kind == "free_text_review"
    assert compiled.expansion.domain == "free_text"
    assert compiled.checkable_predicates == []
    assert compiled.unresolved_gaps[0].gap_id == (
        "criterion:0:free-text:condition:free-text-review:gap:reviewed-out_of_scope"
    )
    assert compiled.unresolved_gaps[0].domain == "free_text"
    assert [diagnostic.code for diagnostic in compiled.diagnostics] == [
        "free_text.promoted.free-text-review",
        "condition.reviewed.out_of_scope.free_text_review",
    ]


def test_study_compliance_condition_phrase_routes_to_free_text_review_plan() -> None:
    criterion = _condition(
        "Ability to adhere to study visit schedule and understand and comply with "
        "all protocol requirements"
    )

    result = compile_extracted_criteria([criterion], resolver_policy="cached_only")
    compiled = result.criteria[0]

    assert compiled.predicate.status == "unsupported"
    assert compiled.predicate.predicate_kind == "free_text_review"
    assert compiled.expansion.domain == "free_text"
    assert compiled.unresolved_gaps[0].gap_id == "criterion:0:free-text-review:gap:study-compliance"
    assert compiled.unresolved_gaps[0].domain == "free_text"
    assert compiled.checkable_predicates == []
    assert [diagnostic.code for diagnostic in compiled.diagnostics] == [
        "condition.promoted.free-text-review",
        "condition_phrase.free_text_review",
    ]


def test_acuity_sensitive_reviewed_condition_stays_condition_gap() -> None:
    criterion = _condition("acutely decompensated heart failure")

    result = compile_extracted_criteria([criterion], resolver_policy="cached_only")
    compiled = result.criteria[0]

    assert compiled.predicate.status == "unresolved"
    assert compiled.predicate.predicate_kind == "condition_presence"
    assert compiled.expansion.domain == "condition"
    assert compiled.unresolved_gaps[0].gap_id == "criterion:0:condition:gap:reviewed-out_of_scope"
    assert compiled.unresolved_gaps[0].domain == "condition"
    assert compiled.diagnostics[0].code == "condition.reviewed.out_of_scope"


def test_reviewed_condition_mapping_can_use_specific_code_list() -> None:
    criterion = _condition("chronic kidney disease (CKD) stage 3-4")

    result = compile_extracted_criteria([criterion], resolver_policy="cached_only")
    compiled = result.criteria[0]

    assert compiled.predicate.status == "resolved"
    assert compiled.expansion.strategy == "reviewed_code_list"
    assert compiled.resolved_supports[0].target_label == "Chronic kidney disease stage 3 or 4"
    assert compiled.checkable_predicates[0].target_codes == frozenset({"433144002", "431857002"})


def test_reviewed_condition_mapping_can_use_descendant_expansion() -> None:
    criterion = _condition("cardiovascular disease")

    result = compile_extracted_criteria([criterion], resolver_policy="cached_only")
    compiled = result.criteria[0]

    assert compiled.unresolved_gaps == []
    assert compiled.predicate.status == "resolved"
    assert compiled.expansion.status == "resolved"
    assert compiled.expansion.strategy == "descendants"
    assert "49601007" in compiled.checkable_predicates[0].target_codes
    assert "84114007" in compiled.checkable_predicates[0].target_codes
    assert "22298006" in compiled.checkable_predicates[0].target_codes


def test_cardiovascular_event_phrase_promotes_to_reviewed_condition_without_alias(
    monkeypatch,
) -> None:
    def fail_alias_lookup(surface: str):
        raise AssertionError(f"legacy alias lookup should not be used for {surface!r}")

    monkeypatch.setattr(
        "clinical_demo.compiler.pipeline.lookup_condition_alias",
        fail_alias_lookup,
    )
    criterion = _condition("major adverse cardiovascular events")

    result = compile_extracted_criteria([criterion], resolver_policy="cached_only")
    compiled = result.criteria[0]

    assert compiled.predicate.status == "resolved"
    assert compiled.expansion.strategy == "descendants"
    assert compiled.resolved_supports[0].surface == "cardiovascular disease"
    assert compiled.diagnostics[0].code == "condition.promoted.cardiovascular-event"
    assert "22298006" in compiled.checkable_predicates[0].target_codes
    assert "230690007" in compiled.checkable_predicates[0].target_codes


def test_plural_cardiovascular_disease_phrase_promotes_to_reviewed_condition() -> None:
    criterion = _condition("clinically significant cardiovascular diseases")

    result = compile_extracted_criteria([criterion], resolver_policy="cached_only")
    compiled = result.criteria[0]

    assert compiled.predicate.status == "resolved"
    assert compiled.expansion.strategy == "descendants"
    assert compiled.resolved_supports[0].surface == "cardiovascular disease"
    assert compiled.diagnostics[0].code == "condition.promoted.cardiovascular-event"


def test_ph_ild_phrase_decomposes_to_pulmonary_hypertension_and_ild() -> None:
    criterion = _condition("PH-ILD")

    result = compile_extracted_criteria([criterion], resolver_policy="cached_only")
    compiled = result.criteria[0]

    assert compiled.predicate.status == "resolved"
    assert compiled.predicate.predicate_kind == "compound"
    assert compiled.compound_logic.status == "resolved"
    assert compiled.compound_logic.operator == "all_of"
    assert compiled.compound_logic.subcheck_ids == [
        "criterion:0:condition-phrase:001",
        "criterion:0:condition-phrase:002",
    ]
    assert [predicate.surface for predicate in compiled.checkable_predicates] == [
        "PH",
        "interstitial lung disease",
    ]
    assert {
        code for predicate in compiled.checkable_predicates for code in predicate.target_codes
    } >= {
        "70995007",
        "233703007",
    }


def test_named_cardiovascular_event_bundle_compiles_explicit_subchecks() -> None:
    criterion = _condition("myocardial infarction, stroke, or transient ischemic attack")

    result = compile_extracted_criteria([criterion], resolver_policy="cached_only")
    compiled = result.criteria[0]

    assert compiled.predicate.status == "resolved"
    assert compiled.predicate.predicate_kind == "compound"
    assert compiled.compound_logic.operator == "any_of"
    assert compiled.compound_logic.subcheck_ids == [
        "criterion:0:condition-phrase:001",
        "criterion:0:condition-phrase:002",
        "criterion:0:condition-phrase:003",
    ]
    assert compiled.unresolved_gaps == []
    assert [predicate.surface for predicate in compiled.checkable_predicates] == [
        "myocardial infarction",
        "stroke",
        "transient ischemic attack",
    ]
    assert {
        code for predicate in compiled.checkable_predicates for code in predicate.target_codes
    } >= {
        "22298006",
        "230690007",
        "266257000",
    }


def test_left_sided_heart_disease_emits_typed_unsupported_gap() -> None:
    criterion = _condition("clinically significant left-sided heart disease")

    result = compile_extracted_criteria([criterion], resolver_policy="cached_only")
    compiled = result.criteria[0]

    assert compiled.predicate.status == "unsupported"
    assert compiled.checkable_predicates == []
    assert compiled.unresolved_gaps[0].gap_id == ("criterion:0:condition-phrase:gap:unsupported")
    assert compiled.unresolved_gaps[0].kind == "unsupported_predicate"
    assert compiled.diagnostics[1].code == "condition_phrase.unsupported"


def test_single_nested_cardiovascular_event_atom_does_not_decompose_recursively() -> None:
    criterion = _condition("acute pulmonary embolism")

    result = compile_extracted_criteria([criterion], resolver_policy="cached_only")
    compiled = result.criteria[0]

    assert compiled.predicate.status == "resolved"
    assert compiled.predicate.predicate_kind == "condition_presence"
    assert compiled.compound_logic.status == "skipped"
    assert [predicate.surface for predicate in compiled.checkable_predicates] == [
        "acute pulmonary embolism"
    ]


def test_nyha_heart_failure_compiles_heart_failure_with_unsupported_qualifier() -> None:
    criterion = _condition("HF New York Heart Association Class II-III")

    result = compile_extracted_criteria([criterion], resolver_policy="cached_only")
    compiled = result.criteria[0]

    assert compiled.predicate.status == "unsupported"
    assert compiled.predicate.predicate_kind == "compound"
    assert compiled.compound_logic.status == "unresolved"
    assert compiled.compound_logic.operator == "all_of"
    assert compiled.checkable_predicates == []
    assert compiled.resolved_supports[0].surface == "heart failure"
    assert compiled.unresolved_gaps[-1].kind == "unsupported_predicate"
    assert compiled.diagnostics[-1].code == "condition_phrase.unsupported_qualifier"


def test_condition_contraindication_phrase_emits_typed_gap() -> None:
    criterion = _condition("contraindication to RHC")

    result = compile_extracted_criteria([criterion], resolver_policy="cached_only")
    compiled = result.criteria[0]

    assert compiled.predicate.status == "unsupported"
    assert compiled.checkable_predicates == []
    assert compiled.unresolved_gaps[0].kind == "unsupported_predicate"
    assert compiled.diagnostics[1].code == "condition_phrase.unsupported"


def test_prognostic_life_expectancy_condition_phrase_emits_typed_gap() -> None:
    criterion = _condition("concomitant disease with life expectancy <6 months")

    result = compile_extracted_criteria([criterion], resolver_policy="cached_only")
    compiled = result.criteria[0]

    assert compiled.predicate.status == "unsupported"
    assert compiled.unresolved_gaps[0].kind == "unsupported_predicate"
    assert "Life-expectancy criteria" in compiled.unresolved_gaps[0].message


def test_qualified_arrhythmia_phrase_emits_typed_gap() -> None:
    criterion = _condition("ongoing cardiac dysrhythmias")

    result = compile_extracted_criteria([criterion], resolver_policy="cached_only")
    compiled = result.criteria[0]

    assert compiled.predicate.status == "unsupported"
    assert compiled.unresolved_gaps[0].kind == "unsupported_predicate"
    assert "Qualified arrhythmia" in compiled.unresolved_gaps[0].message


def test_free_text_condition_mention_compiles_to_condition_predicate() -> None:
    criterion = _free_text("Bone fractures within the past 12 months").model_copy(
        update={
            "mentions": [
                EntityMention(text="Bone fractures", type="Condition"),
                EntityMention(text="12 months", type="Temporal"),
            ]
        }
    )

    result = compile_extracted_criteria([criterion], resolver_policy="cached_only")
    compiled = result.criteria[0]

    assert compiled.criterion_kind == "free_text"
    assert compiled.predicate.status == "resolved"
    assert compiled.predicate.predicate_kind == "condition_presence"
    assert compiled.checkable_predicates[0].predicate_kind == "condition_presence"
    assert compiled.diagnostics[0].code == "free_text.promoted.condition"


def test_free_text_composite_condition_mention_stays_human_review() -> None:
    criterion = _free_text("Pregnant or breastfeeding females").model_copy(
        update={
            "mentions": [EntityMention(text="Pregnant or breastfeeding females", type="Condition")]
        }
    )

    result = compile_extracted_criteria([criterion], resolver_policy="cached_only")
    compiled = result.criteria[0]

    assert compiled.criterion_kind == "free_text"
    assert compiled.predicate.status == "not_attempted"
    assert compiled.checkable_predicates == []
    assert result.unresolved_gaps == []


def test_free_text_blood_pressure_thresholds_compile_to_measurement_compound() -> None:
    criterion = _free_text(
        "Uncontrolled systemic hypertension as evidenced by sitting systolic BP > 160 mmHg "
        "or sitting diastolic BP > 100 mmHg during screening visit after a period of rest; "
        "Baseline systolic BP < 90 mmHg at screening"
    ).model_copy(
        update={
            "mentions": [
                EntityMention(text="Uncontrolled systemic hypertension", type="Condition"),
                EntityMention(text="sitting systolic BP > 160 mmHg", type="Value"),
                EntityMention(text="sitting diastolic BP > 100 mmHg", type="Value"),
                EntityMention(text="Baseline systolic BP < 90 mmHg", type="Value"),
            ]
        }
    )

    result = compile_extracted_criteria([criterion], resolver_policy="cached_only")
    compiled = result.criteria[0]

    assert compiled.criterion_kind == "free_text"
    assert compiled.predicate.status == "resolved"
    assert compiled.predicate.predicate_kind == "compound"
    assert compiled.compound_logic.operator == "any_of"
    assert compiled.unresolved_gaps == []
    assert compiled.diagnostics[0].code == "free_text.promoted.blood-pressure-thresholds"
    assert [
        (predicate.surface, predicate.operator, predicate.value, predicate.unit)
        for predicate in compiled.checkable_predicates
    ] == [
        ("systolic blood pressure", ">", 160.0, "mmHg"),
        ("diastolic blood pressure", ">", 100.0, "mmHg"),
        ("systolic blood pressure", "<", 90.0, "mmHg"),
    ]
    assert compiled.checkable_predicates[0].target_codes == frozenset({"8480-6"})
    assert compiled.checkable_predicates[1].target_codes == frozenset({"8462-4"})


def test_free_text_fasting_plasma_glucose_promotes_to_measurement_provenance_gap() -> None:
    criterion = _free_text("Fasting plasma glucose >= 126 mg/dL at screening")

    result = compile_extracted_criteria([criterion], resolver_policy="cached_only")
    compiled = result.criteria[0]

    assert compiled.criterion_kind == "free_text"
    assert compiled.predicate.status == "unsupported"
    assert compiled.predicate.predicate_kind == "measurement_threshold"
    assert compiled.checkable_predicates == []
    assert compiled.unit_normalization.status == "unsupported"
    assert compiled.unit_normalization.measurement_surface == "fasting plasma glucose"
    assert compiled.unresolved_gaps[0].domain == "measurement"
    assert compiled.unresolved_gaps[0].kind == "provenance_required"
    assert "not collapsed to ordinary glucose" in compiled.unresolved_gaps[0].message
    assert [diagnostic.code for diagnostic in compiled.diagnostics[:3]] == [
        "free_text.promoted.plasma-glucose-thresholds",
        "measurement.provenance_sensitive_glucose.unsupported",
        "measurement.provenance_sensitive_glucose.threshold_extracted",
    ]


def test_free_text_plasma_glucose_thresholds_compile_to_unsupported_measurement_compound() -> None:
    criterion = _free_text(
        "Hyperglycemia: fasting plasma glucose >= 126 mg/dL; "
        "2-hour plasma glucose >= 200 mg/dL during an oral glucose tolerance test; "
        "or random plasma glucose >= 200 mg/dL with classic symptoms"
    )

    result = compile_extracted_criteria([criterion], resolver_policy="cached_only")
    compiled = result.criteria[0]

    assert compiled.criterion_kind == "free_text"
    assert compiled.predicate.status == "unresolved"
    assert compiled.predicate.predicate_kind == "compound"
    assert compiled.compound_logic.status == "unresolved"
    assert compiled.compound_logic.operator == "any_of"
    assert compiled.checkable_predicates == []
    assert compiled.diagnostics[0].code == "free_text.promoted.plasma-glucose-thresholds"
    assert [(gap.domain, gap.kind, gap.surface) for gap in compiled.unresolved_gaps] == [
        ("measurement", "provenance_required", "fasting plasma glucose"),
        ("measurement", "provenance_required", "2-hour plasma glucose"),
        ("measurement", "provenance_required", "random plasma glucose"),
    ]
    assert {diagnostic.code for diagnostic in compiled.diagnostics} >= {
        "measurement.provenance_sensitive_glucose.unsupported",
        "measurement.provenance_sensitive_glucose.threshold_extracted",
    }


def test_direct_provenance_glucose_measurement_plan_is_unsupported() -> None:
    criterion = _measurement_with_value(
        "fasting plasma glucose",
        operator=">=",
        value=126.0,
        unit="mg/dL",
    )

    result = compile_extracted_criteria([criterion], resolver_policy="cached_only")
    compiled = result.criteria[0]

    assert compiled.predicate.status == "unsupported"
    assert compiled.predicate.predicate_kind == "measurement_threshold"
    assert compiled.checkable_predicates == []
    assert compiled.unresolved_gaps[0].kind == "provenance_required"


def test_direct_normal_range_measurement_plan_is_unsupported() -> None:
    criterion = _measurement_with_value(
        "hemoglobin",
        operator="in_range",
        value=None,
        unit="normal limits",
    ).model_copy(update={"source_text": "Hemoglobin within normal limits"})

    result = compile_extracted_criteria([criterion], resolver_policy="cached_only")
    compiled = result.criteria[0]

    assert compiled.predicate.status == "unsupported"
    assert compiled.predicate.predicate_kind == "measurement_threshold"
    assert compiled.checkable_predicates == []
    assert compiled.unresolved_gaps[0].kind == "normal_range_unknown"


def test_generic_blood_pressure_pair_measurement_decomposes_to_systolic_diastolic() -> None:
    criterion = _measurement_with_value(
        "blood pressure",
        operator=">",
        value=160.0,
        unit="mmHg",
    ).model_copy(
        update={
            "source_text": "Uncontrolled hypertension at screening (blood pressure >160/100 mmHg)."
        }
    )

    result = compile_extracted_criteria([criterion], resolver_policy="cached_only")
    compiled = result.criteria[0]

    assert compiled.predicate.status == "resolved"
    assert compiled.predicate.predicate_kind == "compound"
    assert compiled.compound_logic.operator == "any_of"
    assert compiled.unresolved_gaps == []
    assert compiled.diagnostics[0].code == "measurement.promoted.blood-pressure-thresholds"
    assert [
        (predicate.surface, predicate.operator, predicate.value, predicate.unit)
        for predicate in compiled.checkable_predicates
    ] == [
        ("systolic blood pressure", ">", 160.0, "mmHg"),
        ("diastolic blood pressure", ">", 100.0, "mmHg"),
    ]


def test_sbp_dbp_pair_measurement_decomposes_to_systolic_diastolic() -> None:
    criterion = _measurement_with_value(
        "SBP",
        operator=">=",
        value=160.0,
        unit="mmHg",
    ).model_copy(
        update={"source_text": "Poorly controlled hypertension (SBP≥160 mmHg or DBP≥100 mmHg)"}
    )

    result = compile_extracted_criteria([criterion], resolver_policy="cached_only")
    compiled = result.criteria[0]

    assert compiled.predicate.status == "resolved"
    assert compiled.predicate.predicate_kind == "compound"
    assert compiled.compound_logic.operator == "any_of"
    assert [
        (predicate.surface, predicate.operator, predicate.value, predicate.unit)
        for predicate in compiled.checkable_predicates
    ] == [
        ("systolic blood pressure", ">=", 160.0, "mmHg"),
        ("diastolic blood pressure", ">=", 100.0, "mmHg"),
    ]


def test_controlled_blood_pressure_pair_uses_all_of_semantics() -> None:
    criterion = _measurement_with_value(
        "blood pressure",
        operator="<",
        value=140.0,
        unit="mmHg",
    ).model_copy(update={"source_text": "Blood pressure controlled to <140/90 mmHg at screening."})

    result = compile_extracted_criteria([criterion], resolver_policy="cached_only")
    compiled = result.criteria[0]

    assert compiled.predicate.status == "resolved"
    assert compiled.compound_logic.operator == "all_of"
    assert [
        (predicate.surface, predicate.operator, predicate.value)
        for predicate in compiled.checkable_predicates
    ] == [
        ("systolic blood pressure", "<", 140.0),
        ("diastolic blood pressure", "<", 90.0),
    ]


def test_ast_alt_uln_pair_compiles_to_reference_limit_compound() -> None:
    criterion = _measurement_with_value(
        "aspartate aminotransferase",
        operator="<=",
        value=3.0,
        unit="x upper limit of normal (ULN)",
    ).model_copy(
        update={
            "source_text": (
                "Aspartate aminotransferase (AST) and alanine aminotransferase (ALT): "
                "<=3.0 x upper limit of normal (ULN)."
            )
        }
    )

    result = compile_extracted_criteria([criterion], resolver_policy="cached_only")
    compiled = result.criteria[0]

    assert compiled.predicate.status == "resolved"
    assert compiled.predicate.predicate_kind == "compound"
    assert compiled.compound_logic.operator == "all_of"
    assert compiled.unresolved_gaps == []
    assert [
        (predicate.surface, predicate.operator, predicate.value, predicate.unit)
        for predicate in compiled.checkable_predicates
    ] == [
        ("aspartate aminotransferase", "<=", 120.0, "U/L"),
        ("alanine aminotransferase", "<=", 120.0, "U/L"),
    ]
    assert [predicate.target_codes for predicate in compiled.checkable_predicates] == [
        frozenset({"1920-8"}),
        frozenset({"1742-6"}),
    ]


def test_ast_alt_uln_exclusion_pair_uses_any_of_semantics() -> None:
    criterion = _measurement_with_value(
        "aspartate aminotransferase",
        operator=">",
        value=3.0,
        unit="ULN",
    ).model_copy(
        update={
            "source_text": (
                "Aspartate aminotransferase (AST) >3x upper limit of normal (ULN) "
                "and/or alanine aminotransferase (ALT) >3x ULN."
            ),
            "polarity": "exclusion",
        }
    )

    result = compile_extracted_criteria([criterion], resolver_policy="cached_only")
    compiled = result.criteria[0]

    assert compiled.predicate.status == "resolved"
    assert compiled.compound_logic.operator == "any_of"
    assert [
        (predicate.surface, predicate.operator, predicate.value, predicate.unit)
        for predicate in compiled.checkable_predicates
    ] == [
        ("aspartate aminotransferase", ">", 120.0, "U/L"),
        ("alanine aminotransferase", ">", 120.0, "U/L"),
    ]


def test_free_text_trial_exposure_compiles_to_internal_predicate() -> None:
    criterion = _free_text("Use of other investigational agents within 3 months").model_copy(
        update={
            "mentions": [
                EntityMention(text="other investigational agents", type="Drug"),
                EntityMention(text="3 months", type="Temporal"),
            ]
        }
    )

    result = compile_extracted_criteria([criterion], resolver_policy="cached_only")
    compiled = result.criteria[0]

    assert compiled.predicate.status == "resolved"
    assert compiled.predicate.predicate_kind == "trial_exposure"
    assert compiled.checkable_predicates[0].window_days == 90
    assert compiled.diagnostics[0].code == "free_text.promoted.trial-exposure"


def test_free_text_trial_participation_routes_to_interview_required_gap() -> None:
    criterion = _free_text(
        "Patient is participating in a clinical trial of another investigational drug or device."
    )

    result = compile_extracted_criteria([criterion], resolver_policy="cached_only")
    compiled = result.criteria[0]

    assert compiled.predicate.status == "unsupported"
    assert compiled.predicate.predicate_kind == "free_text_review"
    assert compiled.expansion.status == "skipped"
    assert compiled.unresolved_gaps[0].kind == "interview_required"
    assert compiled.unresolved_gaps[0].gap_id == (
        "criterion:0:free-text-review:gap:interview-required"
    )
    assert compiled.diagnostics[0].code == "free_text.promoted.interview-required"
    assert compiled.diagnostics[1].code == "free_text.interview_required"


def test_medication_predicate_carries_exposure_window_and_minimum_duration() -> None:
    criterion = _medication(
        "metformin",
        source_text="On a stable dose of metformin for at least 30 days within previous 2 months",
    )

    result = compile_extracted_criteria([criterion], resolver_policy="cached_only")
    predicate = result.criteria[0].checkable_predicates[0]

    assert predicate.predicate_kind == "medication_exposure"
    assert predicate.window_days == 60
    assert predicate.min_duration_days == 30
    expression = result.criteria[0].predicate.expression
    assert expression is not None
    assert "window=60d" in expression
    assert "min_duration=30d" in expression


def test_temporal_drug_event_reroutes_to_medication_exposure_predicate() -> None:
    criterion = _temporal("lipid-lowering therapies", window_days=30).model_copy(
        update={
            "source_text": (
                "Participants on lipid-lowering therapies must be on a stable dose "
                "for ≥30 days before screening"
            ),
            "mentions": [
                EntityMention(text="lipid-lowering therapies", type="Drug"),
                EntityMention(text="≥30 days before screening", type="Temporal"),
            ],
        }
    )

    result = compile_extracted_criteria([criterion], resolver_policy="cached_only")
    compiled = result.criteria[0]

    assert compiled.predicate.status == "resolved"
    assert compiled.predicate.predicate_kind == "medication_exposure"
    assert compiled.expansion.domain == "medication"
    assert compiled.checkable_predicates[0].predicate_kind == "medication_exposure"
    assert compiled.checkable_predicates[0].surface == "lipid-lowering therapies"
    assert compiled.checkable_predicates[0].min_duration_days == 30
    assert compiled.diagnostics[0].code == "temporal_window.promoted.medication_exposure"


def test_temporal_blood_pressure_medication_event_reroutes_to_class_exposure() -> None:
    criterion = _temporal("medication affecting blood pressure", window_days=28).model_copy(
        update={
            "source_text": (
                "Use of any medication affecting blood pressure within 4 weeks prior "
                "to screening, or planned use during the study period"
            ),
            "mentions": [
                EntityMention(text="medication affecting blood pressure", type="Drug"),
                EntityMention(text="within 4 weeks prior to screening", type="Temporal"),
                EntityMention(text="planned use during the study period", type="Temporal"),
            ],
        }
    )

    result = compile_extracted_criteria([criterion], resolver_policy="cached_only")
    compiled = result.criteria[0]
    predicate = compiled.checkable_predicates[0]

    assert compiled.predicate.status == "resolved"
    assert compiled.predicate.predicate_kind == "medication_exposure"
    assert compiled.expansion.domain == "medication"
    assert compiled.expansion.strategy == "patient_vocabulary_closure"
    assert predicate.predicate_kind == "medication_exposure"
    assert predicate.surface == "medication affecting blood pressure"
    assert predicate.window_days == 28
    assert predicate.target_codes == frozenset(
        {"308136", "313988", "1719286", "310798", "314076", "314077", "979492"}
    )
    assert compiled.diagnostics[0].code == "temporal_window.promoted.medication_exposure"


def test_temporal_reviewed_medication_class_event_reroutes_without_drug_mention() -> None:
    criterion = _temporal("anticoagulation therapy", window_days=180).model_copy(
        update={
            "source_text": "Anticoagulation therapy within 6 months prior to enrollment",
            "mentions": [EntityMention(text="6 months prior to enrollment", type="Temporal")],
        }
    )

    result = compile_extracted_criteria([criterion], resolver_policy="cached_only")
    compiled = result.criteria[0]
    predicate = compiled.checkable_predicates[0]

    assert compiled.predicate.status == "resolved"
    assert compiled.predicate.predicate_kind == "medication_exposure"
    assert compiled.expansion.domain == "medication"
    assert predicate.predicate_kind == "medication_exposure"
    assert predicate.window_days == 180
    assert predicate.target_codes == frozenset({"855332", "854235", "854252", "1659263"})
    assert compiled.diagnostics[0].code == "temporal_window.promoted.medication_exposure"


def test_temporal_pah_background_therapy_without_class_anchor_stays_gap() -> None:
    criterion = _temporal("stable background therapy for PAH", window_days=90).model_copy(
        update={
            "source_text": (
                "Receiving stable background therapy for PAH for >90 days and will "
                "continue receiving throughout trial"
            ),
            "mentions": [EntityMention(text=">90 days", type="Temporal")],
        }
    )

    result = compile_extracted_criteria([criterion], resolver_policy="cached_only")
    compiled = result.criteria[0]

    assert compiled.predicate.status == "unsupported"
    assert compiled.predicate.predicate_kind == "temporal_event"
    assert compiled.checkable_predicates == []
    assert (
        compiled.unresolved_gaps[0].gap_id
        == "gap:criterion:0:temporal:reviewed_composite_unhandled"
    )
    assert compiled.diagnostics[0].code == "temporal_event.reviewed.composite_unhandled"


def test_temporal_coronary_intervention_event_reroutes_to_procedure_history() -> None:
    criterion = _temporal("coronary intervention", window_days=180).model_copy(
        update={
            "source_text": (
                "Performed coronary intervention within 6 months prior to randomization, "
                "or plan to perform coronary intervention during the study."
            ),
            "mentions": [],
        }
    )

    result = compile_extracted_criteria([criterion], resolver_policy="cached_only")
    compiled = result.criteria[0]
    predicate = compiled.checkable_predicates[0]

    assert compiled.predicate.status == "resolved"
    assert compiled.predicate.predicate_kind == "procedure_history"
    assert compiled.expansion.domain == "procedure"
    assert predicate.predicate_kind == "procedure_history"
    assert predicate.surface == "coronary intervention"
    assert predicate.window_days == 180
    assert predicate.target_codes == frozenset({"415070008", "232717009"})
    assert compiled.diagnostics[0].code == "temporal_window.promoted.procedure_history"


def test_free_text_temporal_coronary_intervention_promotes_to_procedure_history() -> None:
    criterion = _free_text(
        "Performed coronary intervention within 6 months prior to randomization, "
        "or plan to perform coronary intervention during the study."
    ).model_copy(
        update={
            "temporal_window": TemporalWindowCriterion(
                event_text="coronary intervention",
                window_days=180,
                direction="within_past",
            ),
            "mentions": [],
        }
    )

    result = compile_extracted_criteria([criterion], resolver_policy="cached_only")
    compiled = result.criteria[0]

    assert compiled.predicate.status == "resolved"
    assert compiled.predicate.predicate_kind == "procedure_history"
    assert compiled.checkable_predicates[0].target_codes == frozenset({"415070008", "232717009"})
    assert compiled.checkable_predicates[0].window_days == 180
    assert compiled.diagnostics[0].code == "temporal_window.promoted.procedure_history"


def test_condition_shaped_trial_exposure_compiles_to_internal_predicate() -> None:
    criterion = _condition(
        "Currently enrolled in or have completed any other investigational product study"
    )

    result = compile_extracted_criteria([criterion], resolver_policy="cached_only")
    compiled = result.criteria[0]

    assert compiled.criterion_kind == "condition_present"
    assert compiled.predicate.status == "resolved"
    assert compiled.predicate.predicate_kind == "trial_exposure"
    assert compiled.checkable_predicates[0].predicate_kind == "trial_exposure"
    assert compiled.resolved_supports[0].domain == "condition"
    assert compiled.diagnostics[0].code == "condition.promoted.trial-exposure"
    assert result.unresolved_gaps == []


def test_demographic_and_temporal_predicates_are_aggregated() -> None:
    criteria = [_age(), _temporal("type 2 diabetes", window_days=90)]

    result = compile_extracted_criteria(criteria)

    assert result.matcher_inputs == criteria
    assert [criterion.predicate.status for criterion in result.criteria] == [
        "resolved",
        "resolved",
    ]
    assert [predicate.predicate_kind for predicate in result.checkable_predicates] == [
        "demographic",
        "temporal_event",
    ]
    assert result.checkable_predicates[1].window_days == 90
