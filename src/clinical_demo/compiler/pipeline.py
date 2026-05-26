"""Criterion compiler pipeline.

This module gives downstream code a stable typed compilation boundary
without changing current matcher behavior. The result keeps the
extractor criteria as `matcher_inputs` while also producing typed
resolution supports, gaps, expansion plans, unit plans, and checkable
predicates for the new compiler path.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from clinical_demo.criterion_review import is_interview_required_criterion_text
from clinical_demo.extractor.schema import (
    CompositeCriterionGroup,
    CompositeCriterionSubcheck,
    CompositeOperator,
    ConditionCriterion,
    CriterionKind,
    EntityMention,
    ExtractedCriteria,
    ExtractedCriterion,
    MeasurementCriterion,
    MedicationCriterion,
    ThresholdOperator,
)
from clinical_demo.matcher.concept_lookup import lookup_condition_alias
from clinical_demo.profile import ConceptSet
from clinical_demo.settings import get_settings
from clinical_demo.terminology.cache import TerminologyCache
from clinical_demo.terminology.candidates import (
    CandidateSource,
    CandidateSourceKind,
    TerminologyCandidate,
    bucket_for_score,
    gate_candidate_set,
    generate_query_variants,
)
from clinical_demo.terminology.expansion import expand_concept_set
from clinical_demo.terminology.medication_classes import get_reviewed_medication_class_registry
from clinical_demo.terminology.resolver import TerminologyResolver
from clinical_demo.terminology.reviewed_registry import (
    ExpansionPolicy,
    ReviewedMappingEntry,
    ReviewedMappingRegistry,
    load_reviewed_mapping_registry,
)

from .compound_time import (
    TemporalWindowCompilation,
    compile_compound_logic,
    compile_temporal_window,
)
from .measurement import MeasurementResolutionResult, compile_measurement_resolution
from .medication import MedicationCompilationResult, compile_medication_resolution
from .schema import (
    COMPILER_VERSION,
    CheckablePredicate,
    CheckablePredicatePlan,
    CompiledCriterion,
    CompilerDiagnostic,
    CompoundLogicPlan,
    CriterionCompilationResult,
    DiagnosticFact,
    DiagnosticSeverity,
    ExpansionPlan,
    ExpansionStrategy,
    PredicateKind,
    ResolutionDomain,
    ResolutionGap,
    ResolutionGapKind,
    ResolutionStage,
    ResolutionStatus,
    ResolutionSupport,
    ResolverExecutionPolicy,
    UnitNormalizationPlan,
)


@dataclass(frozen=True)
class _CompilerResolutionContext:
    resolver_policy: ResolverExecutionPolicy
    resolver: TerminologyResolver
    reviewed_registry: ReviewedMappingRegistry


@dataclass(frozen=True)
class _BloodPressureThreshold:
    measurement_text: str
    operator: ThresholdOperator
    value: float
    unit: str | None


@dataclass(frozen=True)
class _PlasmaGlucoseThreshold:
    measurement_text: str
    operator: ThresholdOperator
    value: float
    unit: str | None
    source_text: str


_PROMOTABLE_MENTION_TYPES = frozenset(
    {"Condition", "Drug", "Measurement", "Observation", "Procedure"}
)
_SNOMED = "http://snomed.info/sct"
_NEGATION_CUE_RE = re.compile(
    r"\b(?:no|not|without|absence of|free of|negative for|denies|denied)\b",
    re.IGNORECASE,
)
_SYMBOLIC_THRESHOLD_RE = re.compile(
    r"(?P<op>>=|<=|>|<|=)\s*(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>[A-Za-z%][A-Za-z0-9%/*.^{}_-]*)?",
    re.IGNORECASE,
)
_THRESHOLD_OPERATOR_RE = r">=|<=|≥|≤|>|<|="
_NUMERIC_THRESHOLD_RE = r"\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?"
_BLOOD_PRESSURE_UNIT_RE = r"mm\s*\[?\s*hg\s*\]?|mmhg"
_EXPLICIT_BLOOD_PRESSURE_THRESHOLD_RE = re.compile(
    rf"\b(?P<measurement>systolic|diastolic|sbp|dbp)"
    rf"(?:\s+(?:blood\s+pressure|bp))?\s*"
    rf"(?:is|are|was|were|of|controlled\s+to)?\s*"
    rf"(?P<op>{_THRESHOLD_OPERATOR_RE})\s*"
    rf"(?P<value>{_NUMERIC_THRESHOLD_RE})\s*"
    rf"(?P<unit>{_BLOOD_PRESSURE_UNIT_RE})?",
    re.IGNORECASE,
)
_GENERIC_BLOOD_PRESSURE_PAIR_RE = re.compile(
    rf"\b(?:blood\s+pressure|bp)\s*"
    rf"(?:is|are|was|were|of|controlled\s+to)?\s*"
    rf"(?P<op>{_THRESHOLD_OPERATOR_RE})\s*"
    rf"(?P<systolic>{_NUMERIC_THRESHOLD_RE})\s*/\s*"
    rf"(?P<diastolic>{_NUMERIC_THRESHOLD_RE})\s*"
    rf"(?P<unit>{_BLOOD_PRESSURE_UNIT_RE})?",
    re.IGNORECASE,
)
_PLASMA_GLUCOSE_SURFACE_RE = (
    r"fasting\s+plasma\s+glucose|"
    r"(?:2|two)[-\s]*hour\s+plasma\s+glucose|"
    r"random\s+plasma\s+glucose"
)
_WORD_THRESHOLD_OPERATOR_RE = (
    r"greater\s+than\s+or\s+equal\s+to|"
    r"less\s+than\s+or\s+equal\s+to|"
    r"at\s+least|"
    r"at\s+most|"
    r"greater\s+than|"
    r"more\s+than|"
    r"exceeds?|"
    r"less\s+than|"
    r"under"
)
_PLASMA_GLUCOSE_UNIT_RE = r"mg\s*/\s*dL|mmol\s*/\s*L"
_PLASMA_GLUCOSE_THRESHOLD_RE = re.compile(
    rf"\b(?P<measurement>{_PLASMA_GLUCOSE_SURFACE_RE})\b"
    rf"(?:(?!\b(?:{_PLASMA_GLUCOSE_SURFACE_RE})\b).){{0,120}}?"
    rf"(?P<op>{_THRESHOLD_OPERATOR_RE}|{_WORD_THRESHOLD_OPERATOR_RE})\s*"
    rf"(?P<value>{_NUMERIC_THRESHOLD_RE})\s*"
    rf"(?P<unit>{_PLASMA_GLUCOSE_UNIT_RE})?",
    re.IGNORECASE,
)
_AMINOTRANSFERASE_REFERENCE_LIMIT_PAIR_RE = re.compile(
    r"\b(?:aspartate\s+aminotransferase|ast)\b.*"
    r"\b(?:alanine\s+aminotransferase|alt)\b|"
    r"\b(?:alanine\s+aminotransferase|alt)\b.*"
    r"\b(?:aspartate\s+aminotransferase|ast)\b",
    re.IGNORECASE,
)
_REFERENCE_LIMIT_CUE_RE = re.compile(
    r"\b(?:u\.?\s*l\.?\s*n|upper\s+limit\s+of\s+normal|"
    r"l\.?\s*l\.?\s*n|lower\s+limit\s+of\s+normal)\b",
    re.IGNORECASE,
)
_TRIAL_EXPOSURE_RE = re.compile(
    r"\b(?:"
    r"investigational\s+(?:agents?|drugs?|products?)|"
    r"study\s+(?:agents?|drugs?|medications?)|"
    r"clinical\s+trial|"
    r"research\s+study|"
    r"another\s+(?:study|trial)"
    r")\b",
    re.IGNORECASE,
)
_MEDICATION_LIST_CUE_RE = re.compile(
    r"\b(?:"
    r"any\s+of\s+the\s+following|"
    r"following\s+(?:drugs?|medications?|agents?|therap(?:y|ies))|"
    r"(?:treatment|therapy)\s+with\s+any|"
    r"use\s+of\s+any"
    r")\b",
    re.IGNORECASE,
)
_MEDICATION_EXPOSURE_SURFACE_RE = re.compile(
    r"\b(?:"
    r"anticoagulation|"
    r"anticoagulants?|"
    r"drugs?|"
    r"medications?|"
    r"therap(?:y|ies)|"
    r"treatments?"
    r")\b",
    re.IGNORECASE,
)
_TEMPORAL_MEDICATION_PREFIX_RE = re.compile(
    r"^(?:"
    r"use|used|using|received|receiving|previous\s+treatment|ongoing\s+treatment|"
    r"treatment|patients\s+on|participants\s+on|on"
    r")\s+(?:of\s+)?(?:any\s+)?(?:with\s+)?",
    re.IGNORECASE,
)
_TEMPORAL_PROCEDURE_PREFIX_RE = re.compile(
    r"^(?:"
    r"performed|performing|received|receiving|underwent|undergoing|"
    r"planned\s+(?:use|procedure|surgery)|plan(?:ned)?\s+to\s+(?:perform|undergo)|"
    r"have\s+(?:had|a\s+history\s+of)|had|history\s+of"
    r")\s+(?:a\s+|an\s+|any\s+)?",
    re.IGNORECASE,
)
_RELATIVE_WINDOW_RE = re.compile(
    r"\b(?:within|in|during|over)?\s*(?:the\s*)?"
    r"(?:past|last|prior|previous)\s+"
    r"(?:(?P<num>\d+)\s+)?(?P<unit>days?|weeks?|months?|years?)\b",
    re.IGNORECASE,
)
_WITHIN_WINDOW_RE = re.compile(
    r"\bwithin\s+(?P<num>\d+)\s+(?P<unit>days?|weeks?|months?|years?)\b",
    re.IGNORECASE,
)
_MIN_DURATION_RE = re.compile(
    r"\bfor\s+"
    r"(?:(?:at\s+least|a\s+minimum\s+of|minimum\s+of|no\s+less\s+than|"
    r"more\s+than|over)\s+|(?:>=|>|≥)\s*)"
    r"(?P<num>\d+)\s*(?P<unit>days?|weeks?|months?|years?)\b",
    re.IGNORECASE,
)
_STABLE_DURATION_RE = re.compile(
    r"\b(?:stable|background|maintenance|chronic|ongoing|current|receiving|on)\b"
    r".{0,80}?"
    r"\bfor\s+(?P<num>\d+)\s*(?P<unit>days?|weeks?|months?|years?)\b",
    re.IGNORECASE,
)
_PROCEDURE_HISTORY_PREFIX_RE = re.compile(
    r"^(?:personal\s+)?(?:(?:past\s+)?medical\s+)?history\s+(?:of|for)\s+",
    re.IGNORECASE,
)
_PH_ILD_RE = re.compile(
    r"\b(?:"
    r"ph\s*[-/]\s*ild|"
    r"pulmonary\s+hypertension\s+(?:associated\s+with|due\s+to|secondary\s+to)\s+"
    r"(?:interstitial\s+lung\s+disease|ild)"
    r")\b",
    re.IGNORECASE,
)
_CARDIOVASCULAR_PROMOTION_RE = re.compile(
    r"\b(?:"
    r"(?:major\s+adverse\s+)?cardiovascular\s+events?|"
    r"cardiovascular\s*/\s*cerebrovascular\s+events?|"
    r"cardiovascular\s+or\s+cerebrovascular\s+events?|"
    r"cardiovascular\s+conditions?|"
    r"clinically\s+significant\s+cardiovascular\s+diseases?|"
    r"cvd"
    r")\b",
    re.IGNORECASE,
)
_UNSAFE_CARDIOVASCULAR_PROMOTION_RE = re.compile(
    r"\b(?:risk\s+factors?|surgery|procedure|assessment|functional|prevention|screening)\b",
    re.IGNORECASE,
)
_LEFT_SIDED_HEART_DISEASE_RE = re.compile(
    r"\b(?:"
    r"left[-\s]+sided\s+heart\s+disease|"
    r"left[-\s]+sided\s+valvulopathy|"
    r"left\s+ventricular\s+disease\s*/\s*dysfunction"
    r")\b",
    re.IGNORECASE,
)
_NYHA_HEART_FAILURE_RE = re.compile(
    r"\b(?:"
    r"(?:hf|heart\s+failure|congestive\s+heart\s+failure)\b.*"
    r"(?:new\s+york\s+heart\s+association|nyha)\b.*(?:class|grade)\s+[ivx]+"
    r"(?:\s*[-/]\s*[ivx]+)?|"
    r"(?:new\s+york\s+heart\s+association|nyha)\b.*(?:class|grade)\s+[ivx]+"
    r"(?:\s*[-/]\s*[ivx]+)?.*\b(?:hf|heart\s+failure|congestive\s+heart\s+failure)"
    r")\b",
    re.IGNORECASE,
)
_CONTRAINDICATION_RE = re.compile(r"\bcontraindicat(?:ion|ed)\s+to\b", re.IGNORECASE)
_LIFE_EXPECTANCY_RE = re.compile(
    r"\blife\s+expectancy\b.*(?:<|less\s+than|under)\s*\d+\s*" r"(?:days?|weeks?|months?|years?)\b",
    re.IGNORECASE,
)
_STUDY_COMPLIANCE_RE = re.compile(
    r"\b(?:unable|inability|ability)\b.*\b(?:comply|complete\s+the\s+study)\b|"
    r"\bstudy\s+requirements?\b",
    re.IGNORECASE,
)
_REVIEWED_FREE_TEXT_REVIEW_CONDITION_SURFACES = frozenset(
    {
        "history of medical noncompliance",
        "inability to perform activities of daily life",
        "structured exercise program",
        "travel time to xenotransplant center",
    }
)
_QUALIFIED_ARRHYTHMIA_RE = re.compile(
    r"\b(?:ongoing|uncontrolled|severe|ctcae|grade)\b.*\b"
    r"(?:cardiac\s+)?(?:dysrhythmias?|arrhythmias?|atrial\s+fibrillation)\b",
    re.IGNORECASE,
)
_OTHER_MEDICAL_CONDITION_RE = re.compile(
    r"\bother\s+(?:serious\s+)?medical\s+conditions?\b",
    re.IGNORECASE,
)
_DIALYSIS_DEPENDENT_CKD_RE = re.compile(
    r"\b(?:advanced\s+)?(?:chronic\s+kidney\s+disease(?:\s*\(ckd\))?|ckd)\b"
    r".{0,80}?\b(?:requiring|requires|on)\b.{0,40}?\b(?:chronic\s+)?dialysis\b|"
    r"\b(?:chronic\s+)?dialysis\b.{0,80}?"
    r"\b(?:advanced\s+)?(?:chronic\s+kidney\s+disease(?:\s*\(ckd\))?|ckd)\b",
    re.IGNORECASE,
)
_DIALYSIS_DEPENDENT_ESRD_RE = re.compile(
    r"\bend[-\s]+stage\s+renal\s+(?:failure|disease)\b"
    r".{0,80}?\b(?:requiring|requires|on)\b.{0,40}?\b(?:chronic\s+)?dialysis\b|"
    r"\b(?:chronic\s+)?dialysis\b.{0,80}?"
    r"\bend[-\s]+stage\s+renal\s+(?:failure|disease)\b",
    re.IGNORECASE,
)
_CARDIOVASCULAR_EVENT_TERMS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"\b(?:acute\s+coronary\s+syndrome|acs)\b", re.IGNORECASE),
        "acute coronary syndrome",
    ),
    (
        re.compile(r"\b(?:myocardial\s+infarction|heart\s+attack|mi)\b", re.IGNORECASE),
        "myocardial infarction",
    ),
    (re.compile(r"\b(?:cerebrovascular\s+accident|cva|stroke)\b", re.IGNORECASE), "stroke"),
    (
        re.compile(r"\b(?:transient\s+ischa?emic\s+attack|tia)\b", re.IGNORECASE),
        "transient ischemic attack",
    ),
    (
        re.compile(
            r"\b(?:deep\s+vein\s+thrombosis|deep\s+venous\s+thrombosis|dvt)\b", re.IGNORECASE
        ),
        "deep venous thrombosis",
    ),
    (re.compile(r"\bacute\s+pulmonary\s+embolism\b", re.IGNORECASE), "acute pulmonary embolism"),
    (re.compile(r"\b(?:pulmonary\s+embolism|pe)\b", re.IGNORECASE), "pulmonary embolism"),
    (re.compile(r"\bcongestive\s+heart\s+failure\b", re.IGNORECASE), "congestive heart failure"),
    (re.compile(r"\bheart\s+failure\b", re.IGNORECASE), "heart failure"),
    (re.compile(r"\bunstable\s+angina\b", re.IGNORECASE), "unstable angina"),
)


def compile_extracted_criteria(
    extracted: ExtractedCriteria | Sequence[ExtractedCriterion],
    *,
    resolver_policy: ResolverExecutionPolicy = "cached_only",
    compiler_version: str = COMPILER_VERSION,
) -> CriterionCompilationResult:
    """Compile extractor output into the initial compiler IR.

    This first implementation is deliberately behavior-preserving: it
    assigns stable ids and typed plans, but `matcher_inputs` is exactly
    the input criterion list in the original order.
    """

    criteria, composite_groups = _coerce_extracted(extracted)
    groups_by_parent = _groups_by_parent(composite_groups)
    context = _resolution_context(resolver_policy)

    compiled = [
        _compile_criterion(
            criterion,
            index=index,
            resolver_policy=resolver_policy,
            composite_groups=groups_by_parent.get(index, []),
            context=context,
        )
        for index, criterion in enumerate(criteria)
    ]

    return CriterionCompilationResult(
        compiler_version=compiler_version,
        resolver_policy=resolver_policy,
        source_criteria_count=len(criteria),
        criteria=compiled,
        matcher_inputs=list(criteria),
        resolved_supports=[support for item in compiled for support in item.resolved_supports],
        unresolved_gaps=[gap for item in compiled for gap in item.unresolved_gaps],
        checkable_predicates=[
            predicate for item in compiled for predicate in item.checkable_predicates
        ],
        diagnostics=[diagnostic for item in compiled for diagnostic in item.diagnostics],
    )


def source_criterion_id(index: int) -> str:
    """Return the stable id for an extractor criterion index."""

    return f"criterion:{index}"


def compiled_criterion_id(index: int) -> str:
    """Return the stable id for a compiled criterion index."""

    return f"compiled:criterion:{index}"


def _coerce_extracted(
    extracted: ExtractedCriteria | Sequence[ExtractedCriterion],
) -> tuple[list[ExtractedCriterion], list[CompositeCriterionGroup]]:
    if isinstance(extracted, ExtractedCriteria):
        return list(extracted.criteria), list(extracted.composite_groups)
    return list(extracted), []


def _groups_by_parent(
    groups: Sequence[CompositeCriterionGroup],
) -> dict[int, list[CompositeCriterionGroup]]:
    grouped: dict[int, list[CompositeCriterionGroup]] = {}
    for group in groups:
        grouped.setdefault(group.parent_criterion_index, []).append(group)
    return grouped


def _compile_criterion(
    criterion: ExtractedCriterion,
    *,
    index: int,
    resolver_policy: ResolverExecutionPolicy,
    composite_groups: Sequence[CompositeCriterionGroup],
    context: _CompilerResolutionContext,
    source_criterion_id_override: str | None = None,
    allow_condition_phrase_promotion: bool = True,
) -> CompiledCriterion:
    source_id = source_criterion_id_override or source_criterion_id(index)
    domain = _resolution_domain(criterion.kind)
    surface = _criterion_surface(criterion)
    supports: list[ResolutionSupport] = []
    gaps: list[ResolutionGap] = []
    predicates: list[CheckablePredicate] = []
    diagnostics: list[CompilerDiagnostic] = []

    compound = compile_compound_logic(
        composite_groups,
        source_criterion_id=source_id,
        resolver_policy=resolver_policy,
    )
    compound_plan = compound.plan
    supports.extend(compound.supports)
    gaps.extend(compound.gaps)
    diagnostics.extend(compound.diagnostics)

    expansion = _default_expansion_plan(domain=domain, surface=surface)
    unit_normalization = _unit_normalization_plan(criterion)
    predicate = _default_predicate_plan(criterion, source_id)

    if criterion.kind in {"age", "sex"}:
        predicate, predicates, demographic_gaps, demographic_diagnostics = _compile_demographic(
            criterion,
            source_criterion_id=source_id,
            resolver_policy=resolver_policy,
        )
        gaps.extend(demographic_gaps)
        diagnostics.extend(demographic_diagnostics)
    elif criterion.kind in {"condition_present", "condition_absent"}:
        if surface is not None and _looks_like_trial_exposure(surface):
            trial_promotion = _compile_trial_exposure_promotion(
                criterion,
                source_criterion_id=source_id,
                surface=surface,
                resolver_policy=resolver_policy,
                support_domain="condition",
                promotion_label="condition",
            )
            predicate = trial_promotion.predicate
            predicates.extend(trial_promotion.predicates)
            supports.extend(trial_promotion.supports)
            gaps.extend(trial_promotion.gaps)
            diagnostics.extend(trial_promotion.diagnostics)
        elif (
            surface is not None
            and (
                _MEDICATION_EXPOSURE_SURFACE_RE.search(surface) is not None
                or _is_reviewed_medication_class_surface(surface)
            )
            and (
                medication_promotion := _compile_condition_typed_medication_promotion(
                    criterion,
                    index=index,
                    source_criterion_id=source_id,
                    surface=surface,
                    resolver_policy=resolver_policy,
                    context=context,
                    promotion_domain="condition",
                    promotion_label="condition",
                )
            )
            is not None
        ):
            predicate = medication_promotion.predicate
            predicates.extend(medication_promotion.predicates)
            supports.extend(medication_promotion.supports)
            gaps.extend(medication_promotion.gaps)
            diagnostics.extend(medication_promotion.diagnostics)
            if medication_promotion.expansion is not None:
                expansion = medication_promotion.expansion
        elif (
            allow_condition_phrase_promotion
            and surface is not None
            and (
                phrase_promotion := _compile_condition_phrase_promotion(
                    criterion,
                    index=index,
                    source_criterion_id=source_id,
                    surface=surface,
                    resolver_policy=resolver_policy,
                    context=context,
                    promotion_domain="condition",
                    promotion_label="condition",
                )
            )
            is not None
        ):
            predicate = phrase_promotion.predicate
            predicates.extend(phrase_promotion.predicates)
            supports.extend(phrase_promotion.supports)
            gaps.extend(phrase_promotion.gaps)
            diagnostics.extend(phrase_promotion.diagnostics)
            if phrase_promotion.compound_logic is not None:
                compound_plan = phrase_promotion.compound_logic
            if phrase_promotion.expansion is not None:
                expansion = phrase_promotion.expansion
        else:
            procedure = _compile_reviewed_procedure_history(
                criterion,
                source_criterion_id=source_id,
                surface=surface,
                context=context,
            )
            if procedure is not None:
                expansion = procedure.expansion
                predicate = procedure.predicate
                predicates.extend(procedure.predicates)
                supports.extend(procedure.supports)
                gaps.extend(procedure.gaps)
                diagnostics.extend(procedure.diagnostics)
            else:
                condition = _compile_condition_resolution(
                    criterion,
                    source_criterion_id=source_id,
                    context=context,
                )
                expansion = condition.expansion
                predicate = condition.predicate
                predicates.extend(condition.predicates)
                supports.extend(condition.supports)
                gaps.extend(condition.gaps)
                diagnostics.extend(condition.diagnostics)
    elif criterion.kind == "measurement_threshold":
        if (
            aminotransferase_promotion := _compile_aminotransferase_reference_limit_promotion(
                criterion,
                index=index,
                source_criterion_id=source_id,
                resolver_policy=resolver_policy,
                context=context,
            )
        ) is not None:
            predicate = aminotransferase_promotion.predicate
            predicates.extend(aminotransferase_promotion.predicates)
            supports.extend(aminotransferase_promotion.supports)
            gaps.extend(aminotransferase_promotion.gaps)
            diagnostics.extend(aminotransferase_promotion.diagnostics)
            if aminotransferase_promotion.compound_logic is not None:
                compound_plan = aminotransferase_promotion.compound_logic
            if aminotransferase_promotion.unit_normalization is not None:
                unit_normalization = aminotransferase_promotion.unit_normalization
        elif (
            bp_promotion := _compile_blood_pressure_threshold_promotion(
                criterion,
                index=index,
                source_criterion_id=source_id,
                resolver_policy=resolver_policy,
                context=context,
                promotion_domain="measurement",
                promotion_label="measurement",
            )
        ) is not None:
            predicate = bp_promotion.predicate
            predicates.extend(bp_promotion.predicates)
            supports.extend(bp_promotion.supports)
            gaps.extend(bp_promotion.gaps)
            diagnostics.extend(bp_promotion.diagnostics)
            if bp_promotion.compound_logic is not None:
                compound_plan = bp_promotion.compound_logic
            if bp_promotion.unit_normalization is not None:
                unit_normalization = bp_promotion.unit_normalization
        else:
            measurement = compile_measurement_resolution(
                criterion,
                source_id,
                resolver_policy=resolver_policy,
                reviewed_registry=context.reviewed_registry,
            )
            unit_normalization = measurement.unit_normalization
            supports.extend(measurement.resolved_supports)
            gaps.extend(measurement.unresolved_gaps)
            diagnostics.extend(measurement.diagnostics)
            predicate, measurement_predicates = _measurement_predicate(
                criterion,
                source_criterion_id=source_id,
                measurement=measurement,
            )
            predicates.extend(measurement_predicates)
    elif criterion.kind == "temporal_window":
        temporal = compile_temporal_window(
            criterion,
            source_criterion_id=source_id,
            resolver_policy=resolver_policy,
            resolver=context.resolver,
            reviewed_registry=context.reviewed_registry,
        )
        temporal_medication = _compile_temporal_medication_exposure(
            criterion,
            source_criterion_id=source_id,
            temporal=temporal,
            context=context,
        )
        temporal_procedure = (
            None
            if temporal_medication is not None
            else _compile_temporal_procedure_history(
                criterion,
                source_criterion_id=source_id,
                temporal=temporal,
                context=context,
            )
        )
        if temporal_medication is not None:
            expansion = temporal_medication.expansion
            predicate = temporal_medication.predicate
            predicates.extend(temporal_medication.predicates)
            supports.extend(temporal_medication.supports)
            gaps.extend(temporal_medication.gaps)
            diagnostics.extend(temporal_medication.diagnostics)
        elif temporal_procedure is not None:
            expansion = temporal_procedure.expansion
            predicate = temporal_procedure.predicate
            predicates.extend(temporal_procedure.predicates)
            supports.extend(temporal_procedure.supports)
            gaps.extend(temporal_procedure.gaps)
            diagnostics.extend(temporal_procedure.diagnostics)
        else:
            supports.extend(temporal.supports)
            gaps.extend(temporal.gaps)
            diagnostics.extend(temporal.diagnostics)
            predicate, temporal_predicates = _temporal_predicate(
                criterion,
                source_criterion_id=source_id,
                temporal=temporal,
            )
            predicates.extend(temporal_predicates)
    elif criterion.kind in {"medication_present", "medication_absent"}:
        medication = compile_medication_resolution(
            criterion,
            source_criterion_id=source_id,
            resolver_policy=resolver_policy,
            resolver=context.resolver,
        )
        supports.extend(medication.resolved_supports)
        gaps.extend(medication.unresolved_gaps)
        diagnostics.extend(medication.diagnostics)
        predicate, medication_predicates = _medication_predicate(
            criterion,
            source_criterion_id=source_id,
            medication=medication,
        )
        predicates.extend(medication_predicates)
    elif criterion.kind == "free_text":
        free_text_promotion = _compile_free_text_promotion(
            criterion,
            index=index,
            source_criterion_id=source_id,
            resolver_policy=resolver_policy,
            context=context,
        )
        if free_text_promotion is not None:
            supports.extend(free_text_promotion.supports)
            gaps.extend(free_text_promotion.gaps)
            diagnostics.extend(free_text_promotion.diagnostics)
            predicates.extend(free_text_promotion.predicates)
            predicate = free_text_promotion.predicate
            if free_text_promotion.compound_logic is not None:
                compound_plan = free_text_promotion.compound_logic
            if free_text_promotion.expansion is not None:
                expansion = free_text_promotion.expansion
            if free_text_promotion.unit_normalization is not None:
                unit_normalization = free_text_promotion.unit_normalization

    if compound.plan.status == "resolved" and composite_groups:
        compound_supports, compound_gaps, compound_predicates, compound_diagnostics = (
            _compile_compound_subchecks(
                composite_groups,
                index=index,
                resolver_policy=resolver_policy,
                context=context,
            )
        )
        supports = [*compound.supports, *compound_supports]
        gaps = [*compound.gaps, *compound_gaps]
        diagnostics = [*compound.diagnostics, *compound_diagnostics]
        predicates = list(compound_predicates)
        compound_plan = compound.plan
        predicate = _compound_predicate_plan(
            compound.plan.subcheck_ids,
            predicates=compound_predicates,
            gaps=compound_gaps,
            operator=compound.plan.operator,
        )

    return CompiledCriterion(
        compiled_id=f"compiled:{source_id}"
        if source_criterion_id_override
        else compiled_criterion_id(index),
        source_criterion_id=source_id,
        source_index=index,
        criterion_kind=criterion.kind,
        source_text=criterion.source_text,
        resolver_policy=resolver_policy,
        matcher_input=criterion,
        resolved_supports=supports,
        unresolved_gaps=gaps,
        checkable_predicates=predicates,
        expansion=expansion,
        compound_logic=compound_plan,
        unit_normalization=unit_normalization,
        predicate=predicate,
        diagnostics=diagnostics,
    )


def _compile_compound_subchecks(
    groups: Sequence[CompositeCriterionGroup],
    *,
    index: int,
    resolver_policy: ResolverExecutionPolicy,
    context: _CompilerResolutionContext,
) -> tuple[
    list[ResolutionSupport],
    list[ResolutionGap],
    list[CheckablePredicate],
    list[CompilerDiagnostic],
]:
    supports: list[ResolutionSupport] = []
    gaps: list[ResolutionGap] = []
    predicates: list[CheckablePredicate] = []
    diagnostics: list[CompilerDiagnostic] = []

    for group in groups:
        for subcheck in group.subchecks:
            compiled = _compile_criterion(
                subcheck.criterion,
                index=index,
                resolver_policy=resolver_policy,
                composite_groups=[],
                context=context,
                source_criterion_id_override=subcheck.subcheck_id,
            )
            supports.extend(compiled.resolved_supports)
            gaps.extend(compiled.unresolved_gaps)
            predicates.extend(compiled.checkable_predicates)
            diagnostics.extend(compiled.diagnostics)
            if not compiled.checkable_predicates and not compiled.unresolved_gaps:
                gap = _compound_subcheck_gap(
                    subcheck,
                    resolver_policy=resolver_policy,
                )
                gaps.append(gap)
                diagnostics.append(
                    _diagnostic(
                        severity="warning",
                        code="compound_subcheck_not_executable",
                        message="Composite subcheck did not produce an executable predicate.",
                        stage="predicate_translation",
                        source_criterion_id=subcheck.subcheck_id,
                        facts=[
                            ("subcheck_id", subcheck.subcheck_id),
                            ("criterion_kind", subcheck.criterion.kind),
                            ("gap_id", gap.gap_id),
                        ],
                    )
                )

    return supports, gaps, predicates, diagnostics


def _compile_free_text_promotion(
    criterion: ExtractedCriterion,
    *,
    index: int,
    source_criterion_id: str,
    resolver_policy: ResolverExecutionPolicy,
    context: _CompilerResolutionContext,
) -> _FreeTextPromotionCompilation | None:
    if _is_interview_required_free_text(criterion):
        return _interview_required_free_text_review(
            criterion,
            source_criterion_id=source_criterion_id,
            resolver_policy=resolver_policy,
        )

    typed_mentions: list[EntityMention] = [
        mention
        for mention in criterion.mentions
        if mention.type in _PROMOTABLE_MENTION_TYPES and mention.text.strip()
    ]
    if _is_list_like_medication_free_text(criterion, typed_mentions):
        return _compile_free_text_medication_list(
            criterion,
            typed_mentions=typed_mentions,
            index=index,
            source_criterion_id=source_criterion_id,
            resolver_policy=resolver_policy,
            context=context,
        )

    if (
        temporal_procedure := _compile_temporal_procedure_history(
            criterion,
            source_criterion_id=source_criterion_id,
            temporal=None,
            context=context,
        )
    ) is not None:
        return _FreeTextPromotionCompilation(
            predicate=temporal_procedure.predicate,
            predicates=temporal_procedure.predicates,
            supports=temporal_procedure.supports,
            gaps=temporal_procedure.gaps,
            diagnostics=temporal_procedure.diagnostics,
            expansion=temporal_procedure.expansion,
        )

    if (
        bp_promotion := _compile_blood_pressure_threshold_promotion(
            criterion,
            index=index,
            source_criterion_id=source_criterion_id,
            resolver_policy=resolver_policy,
            context=context,
            promotion_domain="free_text",
            promotion_label="free-text",
        )
    ) is not None:
        return bp_promotion

    if (
        plasma_glucose_promotion := _compile_plasma_glucose_threshold_promotion(
            criterion,
            index=index,
            source_criterion_id=source_criterion_id,
            resolver_policy=resolver_policy,
            context=context,
        )
    ) is not None:
        return plasma_glucose_promotion

    phrase_promotion = _compile_condition_phrase_promotion(
        criterion,
        index=index,
        source_criterion_id=source_criterion_id,
        surface=criterion.source_text,
        resolver_policy=resolver_policy,
        context=context,
        promotion_domain="free_text",
        promotion_label="free-text",
    )
    if phrase_promotion is not None:
        return phrase_promotion

    if len(typed_mentions) != 1:
        return None
    if not criterion.negated and _NEGATION_CUE_RE.search(criterion.source_text):
        return None

    mention = typed_mentions[0]
    surface = mention.text.strip()
    if mention.type in {"Drug", "Observation"} and _looks_like_trial_exposure(surface):
        return _compile_trial_exposure_promotion(
            criterion,
            source_criterion_id=source_criterion_id,
            surface=surface,
            resolver_policy=resolver_policy,
            support_domain="free_text",
            promotion_label="free-text",
        )

    surrogate: ExtractedCriterion | None = None
    promotion_kind: str | None = None
    if mention.type == "Condition":
        if (
            medication_promotion := _compile_condition_typed_medication_promotion(
                criterion,
                index=index,
                source_criterion_id=source_criterion_id,
                surface=surface,
                resolver_policy=resolver_policy,
                context=context,
            )
        ) is not None:
            return medication_promotion
        if _looks_unsafe_composite_surface(surface):
            return None
        surrogate = _criterion_like(
            criterion,
            kind="condition_present",
            condition=ConditionCriterion(condition_text=surface),
        )
        promotion_kind = "condition"
    elif mention.type == "Drug":
        surrogate = _criterion_like(
            criterion,
            kind="medication_present",
            medication=MedicationCriterion(medication_text=surface),
        )
        promotion_kind = "medication"
    elif mention.type in {"Measurement", "Observation"}:
        measurement = _free_text_measurement_threshold(criterion.source_text, surface)
        if measurement is None:
            return None
        surrogate = _criterion_like(
            criterion,
            kind="measurement_threshold",
            measurement=measurement,
        )
        promotion_kind = "measurement"
    elif mention.type == "Procedure":
        procedure = _compile_reviewed_procedure_history(
            criterion,
            source_criterion_id=f"{source_criterion_id}:free-text:procedure",
            surface=surface,
            context=context,
        )
        if procedure is None:
            return None
        return _promoted_compilation(
            criterion,
            source_criterion_id=source_criterion_id,
            surface=surface,
            promotion_kind="procedure",
            predicate=procedure.predicate,
            predicates=procedure.predicates,
            supports=procedure.supports,
            gaps=procedure.gaps,
            diagnostics=procedure.diagnostics,
            expansion=procedure.expansion,
        )

    if surrogate is None or promotion_kind is None:
        return None

    sub_id = f"{source_criterion_id}:free-text:{promotion_kind}"
    compiled = _compile_criterion(
        surrogate,
        index=index,
        resolver_policy=resolver_policy,
        composite_groups=[],
        context=context,
        source_criterion_id_override=sub_id,
        allow_condition_phrase_promotion=False,
    )
    output_promotion_kind = (
        "free-text-review"
        if compiled.predicate.predicate_kind == "free_text_review"
        else promotion_kind
    )
    return _promoted_compilation(
        criterion,
        source_criterion_id=source_criterion_id,
        surface=surface,
        promotion_kind=output_promotion_kind,
        predicate=compiled.predicate,
        predicates=compiled.checkable_predicates,
        supports=compiled.resolved_supports,
        gaps=compiled.unresolved_gaps,
        diagnostics=compiled.diagnostics,
        expansion=compiled.expansion,
        unit_normalization=compiled.unit_normalization,
    )


def _is_interview_required_free_text(criterion: ExtractedCriterion) -> bool:
    if criterion.kind != "free_text":
        return False
    if is_interview_required_criterion_text(criterion.source_text):
        return True
    return bool(criterion.free_text and "interview_required" in criterion.free_text.note)


def _interview_required_free_text_review(
    criterion: ExtractedCriterion,
    *,
    source_criterion_id: str,
    resolver_policy: ResolverExecutionPolicy,
) -> _FreeTextPromotionCompilation:
    surface = criterion.source_text
    message = (
        "Criterion asks for trial participation information that is usually collected by "
        "patient/site interview rather than inferred from structured FHIR evidence."
    )
    gap = _gap(
        gap_id=f"{source_criterion_id}:free-text-review:gap:interview-required",
        stage="predicate_translation",
        domain="free_text",
        kind="interview_required",
        source_criterion_id=source_criterion_id,
        surface=surface,
        message=message,
        resolver_policy=resolver_policy,
    )
    predicate = CheckablePredicatePlan(
        status="unsupported",
        predicate_kind="free_text_review",
        expression=None,
        input_refs=[source_criterion_id],
        support_ids=[],
        gap_ids=[gap.gap_id],
    )
    expansion = ExpansionPlan(
        status="skipped",
        domain="free_text",
        source_surface=surface,
        strategy="none",
        support_ids=[],
        gap_ids=[gap.gap_id],
    )
    return _promoted_compilation(
        criterion,
        source_criterion_id=source_criterion_id,
        surface=surface,
        promotion_kind="interview-required",
        predicate=predicate,
        predicates=[],
        supports=[],
        gaps=[gap],
        diagnostics=[
            _diagnostic(
                severity="warning",
                code="free_text.interview_required",
                message=message,
                stage="predicate_translation",
                source_criterion_id=source_criterion_id,
                facts=[("surface", surface), ("gap_id", gap.gap_id)],
            )
        ],
        promotion_domain="free_text",
        promotion_label="free-text",
        expansion=expansion,
    )


def _compile_condition_typed_medication_promotion(
    criterion: ExtractedCriterion,
    *,
    index: int,
    source_criterion_id: str,
    surface: str,
    resolver_policy: ResolverExecutionPolicy,
    context: _CompilerResolutionContext,
    promotion_domain: ResolutionDomain = "free_text",
    promotion_label: str = "free-text",
) -> _FreeTextPromotionCompilation | None:
    surrogate = _criterion_like(
        criterion,
        kind="medication_present",
        medication=MedicationCriterion(medication_text=surface),
    )
    sub_id_component = "free-text" if promotion_label == "free-text" else promotion_label
    sub_id = f"{source_criterion_id}:{sub_id_component}:medication"
    compiled = _compile_criterion(
        surrogate,
        index=index,
        resolver_policy=resolver_policy,
        composite_groups=[],
        context=context,
        source_criterion_id_override=sub_id,
        allow_condition_phrase_promotion=False,
    )
    if not compiled.checkable_predicates:
        return None
    expansion = compiled.expansion
    if any(
        support.domain == "medication" and support.stage == "expansion"
        for support in compiled.resolved_supports
    ):
        expansion = expansion.model_copy(
            update={
                "status": compiled.predicate.status,
                "strategy": "patient_vocabulary_closure",
                "support_ids": list(compiled.predicate.support_ids),
                "gap_ids": list(compiled.predicate.gap_ids),
            }
        )
    return _promoted_compilation(
        criterion,
        source_criterion_id=source_criterion_id,
        surface=surface,
        promotion_kind="medication",
        predicate=compiled.predicate,
        predicates=compiled.checkable_predicates,
        supports=compiled.resolved_supports,
        gaps=compiled.unresolved_gaps,
        diagnostics=compiled.diagnostics,
        expansion=expansion,
        unit_normalization=compiled.unit_normalization,
        promotion_domain=promotion_domain,
        promotion_label=promotion_label,
    )


def _compile_free_text_medication_list(
    criterion: ExtractedCriterion,
    *,
    typed_mentions: Sequence[EntityMention],
    index: int,
    source_criterion_id: str,
    resolver_policy: ResolverExecutionPolicy,
    context: _CompilerResolutionContext,
) -> _FreeTextPromotionCompilation | None:
    surfaces = _unique_surfaces(mention.text for mention in typed_mentions)
    supports: list[ResolutionSupport] = []
    gaps: list[ResolutionGap] = []
    predicates: list[CheckablePredicate] = []
    diagnostics: list[CompilerDiagnostic] = []
    subcheck_ids: list[str] = []

    for sub_index, surface in enumerate(surfaces, start=1):
        sub_id = f"{source_criterion_id}:free-text:medication-list:{sub_index:03d}"
        subcheck_ids.append(sub_id)
        surrogate = _criterion_like(
            criterion,
            kind="medication_present",
            medication=MedicationCriterion(medication_text=surface),
        )
        compiled = _compile_criterion(
            surrogate,
            index=index,
            resolver_policy=resolver_policy,
            composite_groups=[],
            context=context,
            source_criterion_id_override=sub_id,
        )
        supports.extend(compiled.resolved_supports)
        gaps.extend(compiled.unresolved_gaps)
        predicates.extend(compiled.checkable_predicates)
        diagnostics.extend(compiled.diagnostics)

    if not predicates and not gaps:
        return None

    predicate = _compound_predicate_plan(
        subcheck_ids,
        predicates=predicates,
        gaps=gaps,
        operator="any_of",
    )
    compound_logic = CompoundLogicPlan(
        status="resolved" if predicates else "unresolved",
        operator="any_of" if predicates else "none",
        source_group_ids=[],
        subcheck_ids=subcheck_ids if predicates else [],
        gap_ids=[gap.gap_id for gap in gaps],
    )
    return _promoted_compilation(
        criterion,
        source_criterion_id=source_criterion_id,
        surface=", ".join(surfaces),
        promotion_kind="medication-list",
        predicate=predicate,
        predicates=predicates,
        supports=supports,
        gaps=gaps,
        diagnostics=diagnostics,
        compound_logic=compound_logic,
    )


def _compile_aminotransferase_reference_limit_promotion(
    criterion: ExtractedCriterion,
    *,
    index: int,
    source_criterion_id: str,
    resolver_policy: ResolverExecutionPolicy,
    context: _CompilerResolutionContext,
) -> _FreeTextPromotionCompilation | None:
    if ":aminotransferase-reference-limit:" in source_criterion_id:
        return None
    if criterion.kind != "measurement_threshold" or criterion.measurement is None:
        return None
    if _REFERENCE_LIMIT_CUE_RE.search(criterion.source_text) is None:
        return None
    if _AMINOTRANSFERASE_REFERENCE_LIMIT_PAIR_RE.search(criterion.source_text) is None:
        return None
    normalized_surface = _normalize_text(criterion.measurement.measurement_text)
    if normalized_surface not in {
        "aspartate aminotransferase",
        "ast",
        "alanine aminotransferase",
        "alt",
    }:
        return None

    supports: list[ResolutionSupport] = []
    gaps: list[ResolutionGap] = []
    predicates: list[CheckablePredicate] = []
    diagnostics: list[CompilerDiagnostic] = []
    subcheck_ids: list[str] = []

    for sub_index, surface in enumerate(
        ("aspartate aminotransferase", "alanine aminotransferase"),
        start=1,
    ):
        sub_id = f"{source_criterion_id}:aminotransferase-reference-limit:{sub_index:03d}"
        subcheck_ids.append(sub_id)
        surrogate = _criterion_like(
            criterion,
            kind="measurement_threshold",
            measurement=MeasurementCriterion(
                measurement_text=surface,
                operator=criterion.measurement.operator,
                value=criterion.measurement.value,
                value_low=criterion.measurement.value_low,
                value_high=criterion.measurement.value_high,
                unit=criterion.measurement.unit,
            ),
        )
        compiled = _compile_criterion(
            surrogate,
            index=index,
            resolver_policy=resolver_policy,
            composite_groups=[],
            context=context,
            source_criterion_id_override=sub_id,
        )
        supports.extend(compiled.resolved_supports)
        gaps.extend(compiled.unresolved_gaps)
        predicates.extend(compiled.checkable_predicates)
        diagnostics.extend(compiled.diagnostics)

    if not predicates and not gaps:
        return None

    operator = _aminotransferase_compound_operator(criterion)
    predicate = _compound_predicate_plan(
        subcheck_ids,
        predicates=predicates,
        gaps=gaps,
        operator=operator,
    )
    compound_logic = CompoundLogicPlan(
        status="resolved" if predicates and not gaps else "unresolved",
        operator=operator,
        source_group_ids=[],
        subcheck_ids=subcheck_ids,
        gap_ids=[gap.gap_id for gap in gaps],
    )
    return _promoted_compilation(
        criterion,
        source_criterion_id=source_criterion_id,
        surface="aspartate aminotransferase, alanine aminotransferase",
        promotion_kind="aminotransferase-reference-limit",
        predicate=predicate,
        predicates=predicates,
        supports=supports,
        gaps=gaps,
        diagnostics=diagnostics,
        promotion_domain="measurement",
        promotion_label="measurement",
        compound_logic=compound_logic,
    )


def _compile_blood_pressure_threshold_promotion(
    criterion: ExtractedCriterion,
    *,
    index: int,
    source_criterion_id: str,
    resolver_policy: ResolverExecutionPolicy,
    context: _CompilerResolutionContext,
    promotion_domain: ResolutionDomain,
    promotion_label: str,
) -> _FreeTextPromotionCompilation | None:
    if ":blood-pressure:" in source_criterion_id:
        return None
    thresholds = _blood_pressure_thresholds_from_text(criterion.source_text)
    if not thresholds:
        return None
    if criterion.kind == "measurement_threshold" and not _should_promote_bp_measurement(
        criterion,
        thresholds,
    ):
        return None

    supports: list[ResolutionSupport] = []
    gaps: list[ResolutionGap] = []
    predicates: list[CheckablePredicate] = []
    diagnostics: list[CompilerDiagnostic] = []
    subcheck_ids: list[str] = []

    for sub_index, threshold in enumerate(thresholds, start=1):
        sub_id = f"{source_criterion_id}:blood-pressure:{sub_index:03d}"
        subcheck_ids.append(sub_id)
        surrogate = _criterion_like(
            criterion,
            kind="measurement_threshold",
            measurement=MeasurementCriterion(
                measurement_text=threshold.measurement_text,
                operator=threshold.operator,
                value=threshold.value,
                value_low=None,
                value_high=None,
                unit=threshold.unit,
            ),
        )
        compiled = _compile_criterion(
            surrogate,
            index=index,
            resolver_policy=resolver_policy,
            composite_groups=[],
            context=context,
            source_criterion_id_override=sub_id,
        )
        supports.extend(compiled.resolved_supports)
        gaps.extend(compiled.unresolved_gaps)
        predicates.extend(compiled.checkable_predicates)
        diagnostics.extend(compiled.diagnostics)

    if not predicates and not gaps:
        return None

    if len(subcheck_ids) == 1:
        if predicates and not gaps:
            predicate = _resolved_predicate_plan(predicates[0])
        else:
            predicate = CheckablePredicatePlan(
                status="unresolved",
                predicate_kind="measurement_threshold",
                expression=None,
                predicate_ids=[predicate.predicate_id for predicate in predicates],
                input_refs=subcheck_ids,
                support_ids=[
                    support_id for predicate in predicates for support_id in predicate.support_ids
                ],
                gap_ids=[gap.gap_id for gap in gaps],
            )
        compound_logic = None
    else:
        operator = _blood_pressure_compound_operator(thresholds, criterion.source_text)
        predicate = _compound_predicate_plan(
            subcheck_ids,
            predicates=predicates,
            gaps=gaps,
            operator=operator,
        )
        compound_logic = CompoundLogicPlan(
            status="resolved" if predicates and not gaps else "unresolved",
            operator=operator,
            source_group_ids=[],
            subcheck_ids=subcheck_ids,
            gap_ids=[gap.gap_id for gap in gaps],
        )

    surface = ", ".join(
        f"{threshold.measurement_text} {threshold.operator} {threshold.value:g}"
        f"{' ' + threshold.unit if threshold.unit else ''}"
        for threshold in thresholds
    )
    return _promoted_compilation(
        criterion,
        source_criterion_id=source_criterion_id,
        surface=surface,
        promotion_kind="blood-pressure-thresholds",
        predicate=predicate,
        predicates=predicates,
        supports=supports,
        gaps=gaps,
        diagnostics=diagnostics,
        promotion_domain=promotion_domain,
        promotion_label=promotion_label,
        compound_logic=compound_logic,
    )


def _compile_plasma_glucose_threshold_promotion(
    criterion: ExtractedCriterion,
    *,
    index: int,
    source_criterion_id: str,
    resolver_policy: ResolverExecutionPolicy,
    context: _CompilerResolutionContext,
) -> _FreeTextPromotionCompilation | None:
    if criterion.kind != "free_text":
        return None
    if ":plasma-glucose:" in source_criterion_id:
        return None
    thresholds = _plasma_glucose_thresholds_from_text(criterion.source_text)
    if not thresholds:
        return None

    supports: list[ResolutionSupport] = []
    gaps: list[ResolutionGap] = []
    predicates: list[CheckablePredicate] = []
    diagnostics: list[CompilerDiagnostic] = []
    subcheck_ids: list[str] = []
    unit_normalization: UnitNormalizationPlan | None = None

    for sub_index, threshold in enumerate(thresholds, start=1):
        sub_id = f"{source_criterion_id}:plasma-glucose:{sub_index:03d}"
        subcheck_ids.append(sub_id)
        surrogate = _criterion_like(
            criterion,
            kind="measurement_threshold",
            measurement=MeasurementCriterion(
                measurement_text=threshold.measurement_text,
                operator=threshold.operator,
                value=threshold.value,
                value_low=None,
                value_high=None,
                unit=threshold.unit,
            ),
        ).model_copy(update={"source_text": threshold.source_text})
        compiled = _compile_criterion(
            surrogate,
            index=index,
            resolver_policy=resolver_policy,
            composite_groups=[],
            context=context,
            source_criterion_id_override=sub_id,
        )
        supports.extend(compiled.resolved_supports)
        gaps.extend(compiled.unresolved_gaps)
        predicates.extend(compiled.checkable_predicates)
        diagnostics.extend(compiled.diagnostics)
        if len(thresholds) == 1:
            unit_normalization = compiled.unit_normalization

    if not predicates and not gaps:
        return None

    if len(subcheck_ids) == 1:
        if predicates and not gaps:
            predicate = _resolved_predicate_plan(predicates[0])
        else:
            predicate = CheckablePredicatePlan(
                status="unsupported",
                predicate_kind="measurement_threshold",
                expression=None,
                predicate_ids=[predicate.predicate_id for predicate in predicates],
                input_refs=subcheck_ids,
                support_ids=[support.support_id for support in supports],
                gap_ids=[gap.gap_id for gap in gaps],
            )
        compound_logic = None
    else:
        operator = _plasma_glucose_compound_operator(thresholds, criterion.source_text)
        predicate = _compound_predicate_plan(
            subcheck_ids,
            predicates=predicates,
            gaps=gaps,
            operator=operator,
        )
        compound_logic = CompoundLogicPlan(
            status="resolved" if predicates and not gaps else "unresolved",
            operator=operator,
            source_group_ids=[],
            subcheck_ids=subcheck_ids,
            gap_ids=[gap.gap_id for gap in gaps],
        )

    surface = ", ".join(
        f"{threshold.measurement_text} {threshold.operator} {threshold.value:g}"
        f"{' ' + threshold.unit if threshold.unit else ''}"
        for threshold in thresholds
    )
    return _promoted_compilation(
        criterion,
        source_criterion_id=source_criterion_id,
        surface=surface,
        promotion_kind="plasma-glucose-thresholds",
        predicate=predicate,
        predicates=predicates,
        supports=supports,
        gaps=gaps,
        diagnostics=diagnostics,
        promotion_domain="free_text",
        promotion_label="free-text",
        compound_logic=compound_logic,
        unit_normalization=unit_normalization,
    )


def _compile_condition_phrase_promotion(
    criterion: ExtractedCriterion,
    *,
    index: int,
    source_criterion_id: str,
    surface: str,
    resolver_policy: ResolverExecutionPolicy,
    context: _CompilerResolutionContext,
    promotion_domain: ResolutionDomain,
    promotion_label: str,
) -> _FreeTextPromotionCompilation | None:
    normalized = _normalize_text(surface)
    if not normalized:
        return None

    if _DIALYSIS_DEPENDENT_CKD_RE.search(surface) or _DIALYSIS_DEPENDENT_ESRD_RE.search(surface):
        return _compile_dialysis_dependent_renal_phrase(
            criterion,
            index=index,
            source_criterion_id=source_criterion_id,
            surface=surface,
            resolver_policy=resolver_policy,
            context=context,
            promotion_domain=promotion_domain,
            promotion_label=promotion_label,
        )

    if _NYHA_HEART_FAILURE_RE.search(surface):
        return _compile_condition_phrase_with_unsupported_qualifier(
            criterion,
            index=index,
            source_criterion_id=source_criterion_id,
            surface=surface,
            target_surface="heart failure",
            qualifier_surface="NYHA functional class",
            resolver_policy=resolver_policy,
            context=context,
            promotion_kind="nyha-heart-failure",
            promotion_domain=promotion_domain,
            promotion_label=promotion_label,
            reason=(
                "NYHA class requires functional-status evidence that is not encoded "
                "as a condition row in the current patient profile."
            ),
        )

    if _CONTRAINDICATION_RE.search(surface):
        return _unsupported_condition_phrase(
            criterion,
            source_criterion_id=source_criterion_id,
            surface=surface,
            resolver_policy=resolver_policy,
            promotion_domain=promotion_domain,
            promotion_label=promotion_label,
            reason=(
                "Contraindication criteria require procedure- or product-specific "
                "intolerance, allergy, or safety evidence before patient-history execution."
            ),
        )

    if _LIFE_EXPECTANCY_RE.search(surface):
        return _unsupported_condition_phrase(
            criterion,
            source_criterion_id=source_criterion_id,
            surface=surface,
            resolver_policy=resolver_policy,
            promotion_domain=promotion_domain,
            promotion_label=promotion_label,
            reason=(
                "Life-expectancy criteria are prognostic judgments and cannot be "
                "safely represented as an atomic condition mapping."
            ),
        )

    if _STUDY_COMPLIANCE_RE.search(surface):
        return _free_text_review_condition_phrase(
            criterion,
            source_criterion_id=source_criterion_id,
            surface=surface,
            resolver_policy=resolver_policy,
            promotion_domain=promotion_domain,
            promotion_label=promotion_label,
            gap_suffix="study-compliance",
            reason=(
                "Study-compliance criteria require protocol-adherence or investigator "
                "judgment evidence outside the current structured clinical profile."
            ),
        )

    if _QUALIFIED_ARRHYTHMIA_RE.search(surface):
        return _unsupported_condition_phrase(
            criterion,
            source_criterion_id=source_criterion_id,
            surface=surface,
            resolver_policy=resolver_policy,
            promotion_domain=promotion_domain,
            promotion_label=promotion_label,
            reason=(
                "Qualified arrhythmia criteria require active/ongoing status, CTCAE "
                "grade, rhythm subtype, or symptom thresholds before safe execution."
            ),
        )

    if _OTHER_MEDICAL_CONDITION_RE.search(surface):
        return _unsupported_condition_phrase(
            criterion,
            source_criterion_id=source_criterion_id,
            surface=surface,
            resolver_policy=resolver_policy,
            promotion_domain=promotion_domain,
            promotion_label=promotion_label,
            reason=(
                "Generic 'other medical condition' criteria are investigator-judgment "
                "catchalls and should remain explicit compiler gaps."
            ),
        )

    if _LEFT_SIDED_HEART_DISEASE_RE.search(surface):
        return _unsupported_condition_phrase(
            criterion,
            source_criterion_id=source_criterion_id,
            surface=surface,
            resolver_policy=resolver_policy,
            promotion_domain=promotion_domain,
            promotion_label=promotion_label,
            reason=(
                "Left-sided heart disease requires decomposition into left-ventricular "
                "function, valvular disease, or hemodynamic measurements before safe "
                "patient-history execution."
            ),
        )

    if _PH_ILD_RE.search(surface):
        return _compile_condition_phrase_compound(
            criterion,
            index=index,
            source_criterion_id=source_criterion_id,
            surfaces=["PH", "interstitial lung disease"],
            operator="all_of",
            resolver_policy=resolver_policy,
            context=context,
            promotion_kind="ph-ild",
            promotion_domain=promotion_domain,
            promotion_label=promotion_label,
        )

    if _is_promotable_cardiovascular_event_surface(surface):
        return _compile_condition_phrase_alias(
            criterion,
            index=index,
            source_criterion_id=source_criterion_id,
            surface=surface,
            target_surface="cardiovascular disease",
            resolver_policy=resolver_policy,
            context=context,
            promotion_kind="cardiovascular-event",
            promotion_domain=promotion_domain,
            promotion_label=promotion_label,
        )

    event_surfaces = _cardiovascular_event_surfaces(surface)
    if len(event_surfaces) >= 2:
        return _compile_condition_phrase_compound(
            criterion,
            index=index,
            source_criterion_id=source_criterion_id,
            surfaces=event_surfaces,
            operator="any_of",
            resolver_policy=resolver_policy,
            context=context,
            promotion_kind="cardiovascular-event-list",
            promotion_domain=promotion_domain,
            promotion_label=promotion_label,
        )

    return None


def _compile_dialysis_dependent_renal_phrase(
    criterion: ExtractedCriterion,
    *,
    index: int,
    source_criterion_id: str,
    surface: str,
    resolver_policy: ResolverExecutionPolicy,
    context: _CompilerResolutionContext,
    promotion_domain: ResolutionDomain,
    promotion_label: str,
) -> _FreeTextPromotionCompilation:
    renal_surface = (
        "end-stage renal disease"
        if _DIALYSIS_DEPENDENT_ESRD_RE.search(surface)
        else "chronic kidney disease"
    )
    renal_id = f"{source_criterion_id}:renal-dialysis:condition"
    dialysis_id = f"{source_criterion_id}:renal-dialysis:procedure"
    renal_surrogate = _criterion_like(
        criterion,
        kind=_condition_surrogate_kind(criterion),
        condition=ConditionCriterion(condition_text=renal_surface),
    )
    renal = _compile_criterion(
        renal_surrogate,
        index=index,
        resolver_policy=resolver_policy,
        composite_groups=[],
        context=context,
        source_criterion_id_override=renal_id,
        allow_condition_phrase_promotion=False,
    )
    dialysis = _compile_reviewed_procedure_history(
        criterion,
        source_criterion_id=dialysis_id,
        surface="dialysis",
        context=context,
    )
    if dialysis is None:
        dialysis_gap = _gap(
            gap_id=f"{dialysis_id}:procedure:gap:missing-reviewed-dialysis",
            stage="concept_resolution",
            domain="procedure",
            kind="unmapped_concept",
            source_criterion_id=dialysis_id,
            surface="dialysis",
            message=(
                "Dialysis-dependent renal criteria require a reviewed dialysis "
                "procedure mapping before execution."
            ),
            resolver_policy=resolver_policy,
        )
        dialysis_supports: list[ResolutionSupport] = []
        dialysis_gaps = [dialysis_gap]
        dialysis_predicates: list[CheckablePredicate] = []
        dialysis_diagnostics = [
            _diagnostic(
                severity="warning",
                code="renal_dialysis.missing_procedure_mapping",
                message=dialysis_gap.message,
                stage="concept_resolution",
                source_criterion_id=dialysis_id,
                facts=[("gap_id", dialysis_gap.gap_id)],
            )
        ]
    else:
        dialysis_supports = dialysis.supports
        dialysis_gaps = dialysis.gaps
        dialysis_predicates = dialysis.predicates
        dialysis_diagnostics = dialysis.diagnostics

    subcheck_ids = [renal_id, dialysis_id]
    supports = [*renal.resolved_supports, *dialysis_supports]
    gaps = [*renal.unresolved_gaps, *dialysis_gaps]
    predicates = [*renal.checkable_predicates, *dialysis_predicates]
    diagnostics = [*renal.diagnostics, *dialysis_diagnostics]
    predicate = _compound_predicate_plan(
        subcheck_ids,
        predicates=predicates,
        gaps=gaps,
        operator="all_of",
    )
    compound_logic = CompoundLogicPlan(
        status="resolved",
        operator="all_of",
        source_group_ids=[],
        subcheck_ids=subcheck_ids,
        gap_ids=[gap.gap_id for gap in gaps],
    )
    return _promoted_compilation(
        criterion,
        source_criterion_id=source_criterion_id,
        surface=surface,
        promotion_kind="renal-dialysis-dependence",
        predicate=predicate,
        predicates=predicates,
        supports=supports,
        gaps=gaps,
        diagnostics=diagnostics,
        promotion_domain=promotion_domain,
        promotion_label=promotion_label,
        compound_logic=compound_logic,
    )


def _compile_condition_phrase_alias(
    criterion: ExtractedCriterion,
    *,
    index: int,
    source_criterion_id: str,
    surface: str,
    target_surface: str,
    resolver_policy: ResolverExecutionPolicy,
    context: _CompilerResolutionContext,
    promotion_kind: str,
    promotion_domain: ResolutionDomain,
    promotion_label: str,
) -> _FreeTextPromotionCompilation:
    sub_id = f"{source_criterion_id}:condition-phrase:{_slugify(target_surface)}"
    surrogate = _criterion_like(
        criterion,
        kind=_condition_surrogate_kind(criterion),
        condition=ConditionCriterion(condition_text=target_surface),
    )
    compiled = _compile_criterion(
        surrogate,
        index=index,
        resolver_policy=resolver_policy,
        composite_groups=[],
        context=context,
        source_criterion_id_override=sub_id,
        allow_condition_phrase_promotion=False,
    )
    return _promoted_compilation(
        criterion,
        source_criterion_id=source_criterion_id,
        surface=surface,
        promotion_kind=promotion_kind,
        predicate=compiled.predicate,
        predicates=compiled.checkable_predicates,
        supports=compiled.resolved_supports,
        gaps=compiled.unresolved_gaps,
        diagnostics=compiled.diagnostics,
        promotion_domain=promotion_domain,
        promotion_label=promotion_label,
        expansion=compiled.expansion,
    )


def _compile_condition_phrase_with_unsupported_qualifier(
    criterion: ExtractedCriterion,
    *,
    index: int,
    source_criterion_id: str,
    surface: str,
    target_surface: str,
    qualifier_surface: str,
    resolver_policy: ResolverExecutionPolicy,
    context: _CompilerResolutionContext,
    promotion_kind: str,
    promotion_domain: ResolutionDomain,
    promotion_label: str,
    reason: str,
) -> _FreeTextPromotionCompilation:
    sub_id = f"{source_criterion_id}:condition-phrase:{_slugify(target_surface)}"
    qualifier_id = f"{source_criterion_id}:condition-phrase:{_slugify(qualifier_surface)}"
    surrogate = _criterion_like(
        criterion,
        kind=_condition_surrogate_kind(criterion),
        condition=ConditionCriterion(condition_text=target_surface),
    )
    compiled = _compile_criterion(
        surrogate,
        index=index,
        resolver_policy=resolver_policy,
        composite_groups=[],
        context=context,
        source_criterion_id_override=sub_id,
        allow_condition_phrase_promotion=False,
    )
    qualifier_gap = _gap(
        gap_id=f"{qualifier_id}:gap:unsupported",
        stage="predicate_translation",
        domain="condition",
        kind="unsupported_predicate",
        source_criterion_id=qualifier_id,
        surface=surface,
        message=reason,
        resolver_policy=resolver_policy,
    )
    gaps = [*compiled.unresolved_gaps, qualifier_gap]
    diagnostics = [
        *compiled.diagnostics,
        _diagnostic(
            severity="warning",
            code="condition_phrase.unsupported_qualifier",
            message=reason,
            stage="predicate_translation",
            source_criterion_id=qualifier_id,
            facts=[
                ("surface", surface),
                ("target_surface", target_surface),
                ("qualifier_surface", qualifier_surface),
                ("gap_id", qualifier_gap.gap_id),
            ],
        ),
    ]
    support_ids = [support.support_id for support in compiled.resolved_supports]
    predicate = CheckablePredicatePlan(
        status="unsupported",
        predicate_kind="compound",
        expression=None,
        input_refs=[sub_id, qualifier_id],
        support_ids=support_ids,
        gap_ids=[gap.gap_id for gap in gaps],
    )
    compound_logic = CompoundLogicPlan(
        status="unresolved",
        operator="all_of",
        source_group_ids=[],
        subcheck_ids=[sub_id, qualifier_id],
        gap_ids=[gap.gap_id for gap in gaps],
    )
    return _promoted_compilation(
        criterion,
        source_criterion_id=source_criterion_id,
        surface=surface,
        promotion_kind=promotion_kind,
        predicate=predicate,
        predicates=[],
        supports=compiled.resolved_supports,
        gaps=gaps,
        diagnostics=diagnostics,
        promotion_domain=promotion_domain,
        promotion_label=promotion_label,
        compound_logic=compound_logic,
        expansion=compiled.expansion,
    )


def _compile_condition_phrase_compound(
    criterion: ExtractedCriterion,
    *,
    index: int,
    source_criterion_id: str,
    surfaces: Sequence[str],
    operator: str,
    resolver_policy: ResolverExecutionPolicy,
    context: _CompilerResolutionContext,
    promotion_kind: str,
    promotion_domain: ResolutionDomain,
    promotion_label: str,
) -> _FreeTextPromotionCompilation:
    supports: list[ResolutionSupport] = []
    gaps: list[ResolutionGap] = []
    predicates: list[CheckablePredicate] = []
    diagnostics: list[CompilerDiagnostic] = []
    subcheck_ids: list[str] = []

    for sub_index, sub_surface in enumerate(surfaces, start=1):
        sub_id = f"{source_criterion_id}:condition-phrase:{sub_index:03d}"
        subcheck_ids.append(sub_id)
        surrogate = _criterion_like(
            criterion,
            kind=_condition_surrogate_kind(criterion),
            condition=ConditionCriterion(condition_text=sub_surface),
        )
        compiled = _compile_criterion(
            surrogate,
            index=index,
            resolver_policy=resolver_policy,
            composite_groups=[],
            context=context,
            source_criterion_id_override=sub_id,
            allow_condition_phrase_promotion=False,
        )
        supports.extend(compiled.resolved_supports)
        gaps.extend(compiled.unresolved_gaps)
        predicates.extend(compiled.checkable_predicates)
        diagnostics.extend(compiled.diagnostics)

    predicate = _compound_predicate_plan(
        subcheck_ids,
        predicates=predicates,
        gaps=gaps,
        operator=operator,
    )
    compound_logic = CompoundLogicPlan(
        status="resolved",
        operator=operator,  # type: ignore[arg-type]
        source_group_ids=[],
        subcheck_ids=subcheck_ids,
        gap_ids=[gap.gap_id for gap in gaps],
    )
    return _promoted_compilation(
        criterion,
        source_criterion_id=source_criterion_id,
        surface=", ".join(surfaces),
        promotion_kind=promotion_kind,
        predicate=predicate,
        predicates=predicates,
        supports=supports,
        gaps=gaps,
        diagnostics=diagnostics,
        promotion_domain=promotion_domain,
        promotion_label=promotion_label,
        compound_logic=compound_logic,
    )


def _free_text_review_condition_phrase(
    criterion: ExtractedCriterion,
    *,
    source_criterion_id: str,
    surface: str,
    resolver_policy: ResolverExecutionPolicy,
    promotion_domain: ResolutionDomain,
    promotion_label: str,
    gap_suffix: str,
    reason: str,
) -> _FreeTextPromotionCompilation:
    gap = _gap(
        gap_id=f"{source_criterion_id}:free-text-review:gap:{gap_suffix}",
        stage="predicate_translation",
        domain="free_text",
        kind="unsupported_predicate",
        source_criterion_id=source_criterion_id,
        surface=surface,
        message=reason,
        resolver_policy=resolver_policy,
    )
    predicate = CheckablePredicatePlan(
        status="unsupported",
        predicate_kind="free_text_review",
        expression=None,
        input_refs=[source_criterion_id],
        support_ids=[],
        gap_ids=[gap.gap_id],
    )
    expansion = ExpansionPlan(
        status="skipped",
        domain="free_text",
        source_surface=surface,
        strategy="none",
        support_ids=[],
        gap_ids=[gap.gap_id],
    )
    return _promoted_compilation(
        criterion,
        source_criterion_id=source_criterion_id,
        surface=surface,
        promotion_kind="free-text-review",
        predicate=predicate,
        predicates=[],
        supports=[],
        gaps=[gap],
        diagnostics=[
            _diagnostic(
                severity="warning",
                code="condition_phrase.free_text_review",
                message=reason,
                stage="predicate_translation",
                source_criterion_id=source_criterion_id,
                facts=[("surface", surface), ("gap_id", gap.gap_id)],
            )
        ],
        promotion_domain=promotion_domain,
        promotion_label=promotion_label,
        expansion=expansion,
    )


def _unsupported_condition_phrase(
    criterion: ExtractedCriterion,
    *,
    source_criterion_id: str,
    surface: str,
    resolver_policy: ResolverExecutionPolicy,
    promotion_domain: ResolutionDomain,
    promotion_label: str,
    reason: str,
) -> _FreeTextPromotionCompilation:
    gap = _gap(
        gap_id=f"{source_criterion_id}:condition-phrase:gap:unsupported",
        stage="predicate_translation",
        domain="condition",
        kind="unsupported_predicate",
        source_criterion_id=source_criterion_id,
        surface=surface,
        message=reason,
        resolver_policy=resolver_policy,
    )
    predicate = CheckablePredicatePlan(
        status="unsupported",
        predicate_kind="condition_presence",
        expression=None,
        input_refs=[source_criterion_id],
        support_ids=[],
        gap_ids=[gap.gap_id],
    )
    return _promoted_compilation(
        criterion,
        source_criterion_id=source_criterion_id,
        surface=surface,
        promotion_kind="unsupported-condition-phrase",
        predicate=predicate,
        predicates=[],
        supports=[],
        gaps=[gap],
        diagnostics=[
            _diagnostic(
                severity="warning",
                code="condition_phrase.unsupported",
                message=reason,
                stage="predicate_translation",
                source_criterion_id=source_criterion_id,
                facts=[("surface", surface), ("gap_id", gap.gap_id)],
            )
        ],
        promotion_domain=promotion_domain,
        promotion_label=promotion_label,
    )


def _compile_trial_exposure_promotion(
    criterion: ExtractedCriterion,
    *,
    source_criterion_id: str,
    surface: str,
    resolver_policy: ResolverExecutionPolicy,
    support_domain: ResolutionDomain,
    promotion_label: str,
) -> _FreeTextPromotionCompilation:
    support = _support(
        support_id=f"{source_criterion_id}:free-text:support:trial-exposure",
        stage="predicate_translation",
        domain=support_domain,
        source_criterion_id=source_criterion_id,
        surface=surface,
        normalized_surface=_normalize_text(surface),
        target_system="internal.trial_exposure",
        target_id="trial_exposure",
        target_label="Clinical trial or investigational-agent exposure",
        resolver_policy=resolver_policy,
    )
    predicate = _checkable_predicate(
        predicate_id=f"{source_criterion_id}:predicate:trial-exposure",
        predicate_kind="trial_exposure",
        source_criterion_id=source_criterion_id,
        criterion=criterion,
        surface=surface,
        target_system="internal.trial_exposure",
        target_codes=frozenset({"trial_exposure"}),
        window_days=_free_text_window_days(criterion.source_text),
        support_ids=[support.support_id],
    )
    return _promoted_compilation(
        criterion,
        source_criterion_id=source_criterion_id,
        surface=surface,
        promotion_kind="trial-exposure",
        predicate=_resolved_predicate_plan(predicate, support_ids=[support.support_id]),
        predicates=[predicate],
        supports=[support],
        gaps=[],
        diagnostics=[],
        promotion_domain=support_domain,
        promotion_label=promotion_label,
    )


def _promoted_compilation(
    criterion: ExtractedCriterion,
    *,
    source_criterion_id: str,
    surface: str,
    promotion_kind: str,
    predicate: CheckablePredicatePlan,
    predicates: list[CheckablePredicate],
    supports: list[ResolutionSupport],
    gaps: list[ResolutionGap],
    diagnostics: list[CompilerDiagnostic],
    promotion_domain: ResolutionDomain = "free_text",
    promotion_label: str = "free-text",
    compound_logic: CompoundLogicPlan | None = None,
    expansion: ExpansionPlan | None = None,
    unit_normalization: UnitNormalizationPlan | None = None,
) -> _FreeTextPromotionCompilation:
    return _FreeTextPromotionCompilation(
        predicate=predicate,
        predicates=predicates,
        supports=supports,
        gaps=gaps,
        diagnostics=[
            _diagnostic(
                severity="info",
                code=f"{promotion_domain}.promoted.{promotion_kind}",
                message=(
                    f"Promoted correlatable {promotion_label} {promotion_kind} surface "
                    f"{surface!r} to compiler predicate translation."
                ),
                stage="predicate_translation",
                source_criterion_id=source_criterion_id,
                facts=[
                    ("surface", surface),
                    ("source_text", criterion.source_text),
                ],
            ),
            *diagnostics,
        ],
        compound_logic=compound_logic,
        expansion=expansion,
        unit_normalization=unit_normalization,
    )


def _is_list_like_medication_free_text(
    criterion: ExtractedCriterion,
    typed_mentions: Sequence[object],
) -> bool:
    return (
        len(typed_mentions) >= 2
        and all(getattr(mention, "type", None) == "Drug" for mention in typed_mentions)
        and bool(_MEDICATION_LIST_CUE_RE.search(criterion.source_text))
    )


def _unique_surfaces(surfaces: Iterable[object]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for surface in surfaces:
        normalized = " ".join(str(surface).lower().split())
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique.append(str(surface).strip())
    return unique


def _criterion_like(
    criterion: ExtractedCriterion,
    *,
    kind: CriterionKind,
    condition: ConditionCriterion | None = None,
    medication: MedicationCriterion | None = None,
    measurement: MeasurementCriterion | None = None,
) -> ExtractedCriterion:
    return ExtractedCriterion(
        kind=kind,
        polarity=criterion.polarity,
        source_text=criterion.source_text,
        negated=criterion.negated,
        mood=criterion.mood,
        age=None,
        sex=None,
        condition=condition,
        medication=medication,
        measurement=measurement,
        temporal_window=None,
        free_text=None,
        mentions=criterion.mentions,
    )


def _free_text_measurement_threshold(
    source_text: str,
    surface: str,
) -> MeasurementCriterion | None:
    surface_index = source_text.lower().find(surface.lower())
    if surface_index < 0:
        return None
    window = source_text[surface_index : surface_index + max(len(surface) + 48, 64)]
    match = _SYMBOLIC_THRESHOLD_RE.search(window)
    if match is None:
        return None
    return MeasurementCriterion(
        measurement_text=surface,
        operator=match.group("op"),  # type: ignore[arg-type]
        value=float(match.group("value")),
        value_low=None,
        value_high=None,
        unit=match.group("unit"),
    )


def _should_promote_bp_measurement(
    criterion: ExtractedCriterion,
    thresholds: Sequence[_BloodPressureThreshold],
) -> bool:
    if criterion.measurement is None:
        return bool(thresholds)
    normalized_surface = _normalize_text(criterion.measurement.measurement_text)
    if normalized_surface in {"blood pressure", "bp"}:
        return True
    if normalized_surface in {
        "systolic blood pressure",
        "systolic bp",
        "sbp",
        "diastolic blood pressure",
        "diastolic bp",
        "dbp",
    }:
        return len(thresholds) >= 2
    return False


def _blood_pressure_thresholds_from_text(source_text: str) -> list[_BloodPressureThreshold]:
    thresholds: list[_BloodPressureThreshold] = []
    seen: set[tuple[str, str, float, str | None]] = set()

    for match in _GENERIC_BLOOD_PRESSURE_PAIR_RE.finditer(source_text):
        operator = _normalize_threshold_operator(match.group("op"))
        unit = _normalize_blood_pressure_unit(match.group("unit"))
        for measurement_text, value_text in (
            ("systolic blood pressure", match.group("systolic")),
            ("diastolic blood pressure", match.group("diastolic")),
        ):
            _append_blood_pressure_threshold(
                thresholds,
                seen,
                measurement_text=measurement_text,
                operator=operator,
                value_text=value_text,
                unit=unit,
            )

    for match in _EXPLICIT_BLOOD_PRESSURE_THRESHOLD_RE.finditer(source_text):
        measurement_kind = _normalize_text(match.group("measurement"))
        measurement_text = _blood_pressure_measurement_text(measurement_kind)
        _append_blood_pressure_threshold(
            thresholds,
            seen,
            measurement_text=measurement_text,
            operator=_normalize_threshold_operator(match.group("op")),
            value_text=match.group("value"),
            unit=_normalize_blood_pressure_unit(match.group("unit")),
        )

    return thresholds


def _blood_pressure_measurement_text(measurement_kind: str) -> str:
    if measurement_kind in {"systolic", "sbp"}:
        return "systolic blood pressure"
    return "diastolic blood pressure"


def _append_blood_pressure_threshold(
    thresholds: list[_BloodPressureThreshold],
    seen: set[tuple[str, str, float, str | None]],
    *,
    measurement_text: str,
    operator: ThresholdOperator,
    value_text: str,
    unit: str | None,
) -> None:
    value = float(value_text.replace(",", ""))
    key = (measurement_text, operator, value, unit)
    if key in seen:
        return
    seen.add(key)
    thresholds.append(
        _BloodPressureThreshold(
            measurement_text=measurement_text,
            operator=operator,
            value=value,
            unit=unit,
        )
    )


def _normalize_threshold_operator(operator: str) -> ThresholdOperator:
    if operator == "≥":
        return ">="
    if operator == "≤":
        return "<="
    normalized = " ".join(operator.lower().split())
    if normalized in {"greater than or equal to", "at least"}:
        return ">="
    if normalized in {"less than or equal to", "at most"}:
        return "<="
    if normalized in {"greater than", "more than", "exceed", "exceeds"}:
        return ">"
    if normalized in {"less than", "under"}:
        return "<"
    return operator  # type: ignore[return-value]


def _normalize_blood_pressure_unit(unit: str | None) -> str | None:
    if unit is None:
        return None
    normalized = re.sub(r"[\s\[\]]+", "", unit).lower()
    if normalized == "mmhg":
        return "mmHg"
    return unit


def _blood_pressure_compound_operator(
    thresholds: Sequence[_BloodPressureThreshold],
    source_text: str,
) -> CompositeOperator:
    normalized = f" {_normalize_text(source_text)} "
    if " or " in normalized or ";" in source_text:
        return "any_of"
    if any(threshold.operator in {">", ">="} for threshold in thresholds):
        return "any_of"
    if all(threshold.operator in {"<", "<="} for threshold in thresholds):
        return "all_of"
    return "any_of"


def _plasma_glucose_thresholds_from_text(source_text: str) -> list[_PlasmaGlucoseThreshold]:
    thresholds: list[_PlasmaGlucoseThreshold] = []
    seen: set[tuple[str, str, float, str | None]] = set()

    for match in _PLASMA_GLUCOSE_THRESHOLD_RE.finditer(source_text):
        measurement_text = _plasma_glucose_measurement_text(match.group("measurement"))
        operator = _normalize_threshold_operator(match.group("op"))
        value = float(match.group("value").replace(",", ""))
        unit = _normalize_glucose_unit(match.group("unit"))
        key = (measurement_text, operator, value, unit)
        if key in seen:
            continue
        seen.add(key)
        thresholds.append(
            _PlasmaGlucoseThreshold(
                measurement_text=measurement_text,
                operator=operator,
                value=value,
                unit=unit,
                source_text=match.group(0).strip(),
            )
        )

    return thresholds


def _plasma_glucose_measurement_text(surface: str) -> str:
    if re.search(r"\bfasting\s+plasma\s+glucose\b", surface, re.IGNORECASE):
        return "fasting plasma glucose"
    if re.search(r"\b(?:2|two)[-\s]*hour\s+plasma\s+glucose\b", surface, re.IGNORECASE):
        return "2-hour plasma glucose"
    return "random plasma glucose"


def _normalize_glucose_unit(unit: str | None) -> str | None:
    if unit is None:
        return None
    compact = re.sub(r"\s*/\s*", "/", unit.strip())
    normalized = compact.lower()
    if normalized == "mg/dl":
        return "mg/dL"
    if normalized == "mmol/l":
        return "mmol/L"
    return compact


def _plasma_glucose_compound_operator(
    thresholds: Sequence[_PlasmaGlucoseThreshold],
    source_text: str,
) -> CompositeOperator:
    normalized = f" {_normalize_text(source_text)} "
    if " and " in normalized and " or " not in normalized and ";" not in source_text:
        return "all_of"
    if any(threshold.operator in {">", ">="} for threshold in thresholds):
        return "any_of"
    return "all_of"


def _aminotransferase_compound_operator(criterion: ExtractedCriterion) -> CompositeOperator:
    normalized = f" {_normalize_text(criterion.source_text)} "
    if " and/or " in normalized or " or " in normalized:
        return "any_of"
    if criterion.measurement is not None and criterion.measurement.operator in {">", ">="}:
        return "any_of"
    return "all_of"


def _looks_like_trial_exposure(text: str) -> bool:
    return bool(_TRIAL_EXPOSURE_RE.search(text))


def _is_promotable_cardiovascular_event_surface(text: str) -> bool:
    if _UNSAFE_CARDIOVASCULAR_PROMOTION_RE.search(text):
        return False
    normalized = _normalize_text(text)
    if normalized == "cardiovascular disease":
        return False
    return bool(_CARDIOVASCULAR_PROMOTION_RE.search(text))


def _cardiovascular_event_surfaces(text: str) -> list[str]:
    surfaces: list[str] = []
    seen: set[str] = set()
    for pattern, surface in _CARDIOVASCULAR_EVENT_TERMS:
        if pattern.search(text) is None:
            continue
        normalized = _normalize_text(surface)
        if normalized in seen:
            continue
        seen.add(normalized)
        surfaces.append(surface)

    normalized_surfaces = {surface: _normalize_text(surface) for surface in surfaces}
    return [
        surface
        for surface in surfaces
        if not any(
            normalized_surfaces[surface] != other and normalized_surfaces[surface] in other
            for other in normalized_surfaces.values()
        )
    ]


def _condition_surrogate_kind(criterion: ExtractedCriterion) -> CriterionKind:
    if criterion.kind in {"condition_present", "condition_absent"}:
        return criterion.kind
    return "condition_present"


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", _normalize_text(text)).strip("-")
    return slug or "surface"


def _looks_unsafe_composite_surface(text: str) -> bool:
    normalized = f" {_normalize_text(text)} "
    return any(token in normalized for token in (" and ", " or ", ",", ";", "/"))


def _free_text_window_days(source_text: str) -> int | None:
    match = _RELATIVE_WINDOW_RE.search(source_text) or _WITHIN_WINDOW_RE.search(source_text)
    if match is None:
        return None
    return _window_days(match.group("num"), match.group("unit"))


def _minimum_duration_days(source_text: str) -> int | None:
    match = _MIN_DURATION_RE.search(source_text) or _STABLE_DURATION_RE.search(source_text)
    if match is None:
        return None
    return _window_days(match.group("num"), match.group("unit"))


def _window_days(number_text: str | None, unit: str) -> int:
    count = int(number_text) if number_text is not None else 1
    unit = unit.lower()
    if unit.startswith("day"):
        return count
    if unit.startswith("week"):
        return count * 7
    if unit.startswith("month"):
        return count * 30
    return count * 365


def _normalize_text(text: str) -> str:
    return " ".join(text.lower().strip(".,;:()[]{}\"'").split())


def _compound_subcheck_gap(
    subcheck: CompositeCriterionSubcheck,
    *,
    resolver_policy: ResolverExecutionPolicy,
) -> ResolutionGap:
    return _gap(
        gap_id=f"{subcheck.subcheck_id}:compound:gap:not-executable",
        stage="predicate_translation",
        domain=_resolution_domain(subcheck.criterion.kind),
        kind="unsupported_predicate",
        source_criterion_id=subcheck.subcheck_id,
        surface=_criterion_surface(subcheck.criterion) or subcheck.source_text,
        message=(
            "Composite subcheck did not produce an executable compiler predicate; "
            "parent compound rollup must treat this branch as indeterminate."
        ),
        resolver_policy=resolver_policy,
    )


def _compound_predicate_plan(
    subcheck_ids: Sequence[str],
    *,
    predicates: Sequence[CheckablePredicate],
    gaps: Sequence[ResolutionGap],
    operator: str,
) -> CheckablePredicatePlan:
    predicate_ids = [predicate.predicate_id for predicate in predicates]
    gap_ids = [gap.gap_id for gap in gaps]
    status: ResolutionStatus = "resolved" if predicate_ids and not gap_ids else "unresolved"
    return CheckablePredicatePlan(
        status=status,
        predicate_kind="compound",
        expression=(
            f"compound({operator},subchecks={len(subcheck_ids)},"
            f"predicates={len(predicate_ids)},gaps={len(gap_ids)})"
        ),
        predicate_ids=predicate_ids,
        input_refs=list(subcheck_ids),
        support_ids=[
            support_id for predicate in predicates for support_id in predicate.support_ids
        ],
        gap_ids=gap_ids,
    )


def _unit_normalization_plan(criterion: ExtractedCriterion) -> UnitNormalizationPlan:
    if criterion.kind != "measurement_threshold" or criterion.measurement is None:
        return UnitNormalizationPlan(
            status="skipped",
            measurement_surface=None,
            source_unit=None,
            canonical_unit=None,
            conventional_unit=None,
            conversion_factor=None,
            gap_ids=[],
        )

    return UnitNormalizationPlan(
        status="not_attempted",
        measurement_surface=criterion.measurement.measurement_text,
        source_unit=criterion.measurement.unit,
        canonical_unit=None,
        conventional_unit=None,
        conversion_factor=None,
        gap_ids=[],
    )


def _resolution_context(resolver_policy: ResolverExecutionPolicy) -> _CompilerResolutionContext:
    settings = get_settings()
    return _CompilerResolutionContext(
        resolver_policy=resolver_policy,
        resolver=TerminologyResolver(
            TerminologyCache(settings.terminology_cache_dir),
            execution_policy=resolver_policy,
        ),
        reviewed_registry=load_reviewed_mapping_registry(),
    )


def _default_expansion_plan(
    *,
    domain: ResolutionDomain,
    surface: str | None,
) -> ExpansionPlan:
    status: ResolutionStatus = (
        "skipped" if domain not in {"condition", "medication"} else "not_attempted"
    )
    return ExpansionPlan(
        status=status,
        domain=domain,
        source_surface=surface,
        strategy="none",
        support_ids=[],
        gap_ids=[],
    )


def _default_predicate_plan(
    criterion: ExtractedCriterion,
    source_criterion_id: str,
) -> CheckablePredicatePlan:
    return CheckablePredicatePlan(
        status="not_attempted",
        predicate_kind=_predicate_kind(criterion.kind),
        expression=None,
        input_refs=[source_criterion_id],
        support_ids=[],
        gap_ids=[],
    )


@dataclass(frozen=True)
class _ConditionCompilation:
    expansion: ExpansionPlan
    predicate: CheckablePredicatePlan
    predicates: list[CheckablePredicate]
    supports: list[ResolutionSupport]
    gaps: list[ResolutionGap]
    diagnostics: list[CompilerDiagnostic]


def _compile_temporal_medication_exposure(
    criterion: ExtractedCriterion,
    *,
    source_criterion_id: str,
    temporal: TemporalWindowCompilation,
    context: _CompilerResolutionContext,
) -> _ConditionCompilation | None:
    """Reroute Drug-shaped temporal windows to medication exposure predicates."""

    if criterion.temporal_window is None or criterion.temporal_window.direction != "within_past":
        return None
    if temporal.predicate.status == "resolved":
        return None

    surfaces = _temporal_medication_surfaces(criterion)
    if not surfaces:
        return None

    for surface in surfaces:
        surrogate = _criterion_like(
            criterion,
            kind="medication_present",
            medication=MedicationCriterion(medication_text=surface),
        )
        medication = compile_medication_resolution(
            surrogate,
            source_criterion_id=source_criterion_id,
            resolver_policy=context.resolver_policy,
            resolver=context.resolver,
            reviewed_registry=context.reviewed_registry,
        )
        predicate, predicates = _medication_predicate(
            surrogate,
            source_criterion_id=source_criterion_id,
            medication=medication,
        )
        compiled = _temporal_medication_output(
            criterion,
            source_criterion_id=source_criterion_id,
            surface=surface,
            medication=medication,
            predicate=predicate,
            predicates=predicates,
        )
        if predicates:
            return compiled

    return None


def _temporal_medication_surfaces(criterion: ExtractedCriterion) -> list[str]:
    if criterion.temporal_window is None:
        return []

    drug_mentions = [mention.text for mention in criterion.mentions if mention.type == "Drug"]
    event_text_is_reviewed_class = _is_reviewed_medication_class_surface(
        criterion.temporal_window.event_text
    )
    if not drug_mentions and not event_text_is_reviewed_class:
        return []

    surfaces: list[str] = []
    seen: set[str] = set()

    def add(surface: str | None) -> None:
        if surface is None:
            return
        stripped = " ".join(surface.strip().split())
        normalized = _normalize_text(stripped)
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        surfaces.append(stripped)

    add(criterion.temporal_window.event_text)
    for mention_surface in drug_mentions:
        add(mention_surface)
    for surface in tuple(surfaces):
        add(_TEMPORAL_MEDICATION_PREFIX_RE.sub("", surface))
    return surfaces


def _is_reviewed_medication_class_surface(surface: str) -> bool:
    return get_reviewed_medication_class_registry().lookup(surface) is not None


def _temporal_medication_output(
    criterion: ExtractedCriterion,
    *,
    source_criterion_id: str,
    surface: str,
    medication: MedicationCompilationResult,
    predicate: CheckablePredicatePlan,
    predicates: list[CheckablePredicate],
) -> _ConditionCompilation:
    support_ids = list(predicate.support_ids)
    gap_ids = [gap.gap_id for gap in medication.unresolved_gaps]
    expansion_strategy: ExpansionStrategy = (
        "patient_vocabulary_closure" if medication.medication_class.status == "resolved" else "none"
    )
    expansion = ExpansionPlan(
        status=predicate.status,
        domain="medication",
        source_surface=surface,
        strategy=expansion_strategy,
        support_ids=support_ids,
        gap_ids=gap_ids,
    )
    diagnostics = [
        _diagnostic(
            severity="info" if predicates else "warning",
            code=(
                "temporal_window.promoted.medication_exposure"
                if predicates
                else "temporal_window.medication_exposure_not_executable"
            ),
            message=(
                f"Rerouted temporal-window event surface {surface!r} through "
                "medication exposure compilation."
            ),
            stage="predicate_translation",
            source_criterion_id=source_criterion_id,
            facts=[
                ("surface", surface),
                ("source_text", criterion.source_text),
                ("predicate_status", predicate.status),
            ],
        ),
        *medication.diagnostics,
    ]
    return _ConditionCompilation(
        expansion=expansion,
        predicate=predicate,
        predicates=predicates,
        supports=medication.resolved_supports,
        gaps=medication.unresolved_gaps,
        diagnostics=diagnostics,
    )


def _compile_temporal_procedure_history(
    criterion: ExtractedCriterion,
    *,
    source_criterion_id: str,
    temporal: TemporalWindowCompilation | None,
    context: _CompilerResolutionContext,
) -> _ConditionCompilation | None:
    """Reroute reviewed procedure-shaped temporal windows to Procedure checks."""

    if criterion.temporal_window is None or criterion.temporal_window.direction != "within_past":
        return None
    if temporal is not None and temporal.predicate.status == "resolved":
        return None

    surfaces = _temporal_procedure_surfaces(criterion)
    if not surfaces:
        return None

    for surface in surfaces:
        procedure = _compile_reviewed_procedure_history(
            criterion,
            source_criterion_id=source_criterion_id,
            surface=surface,
            context=context,
            window_days=criterion.temporal_window.window_days,
        )
        if procedure is not None and procedure.predicates:
            return _temporal_procedure_output(
                criterion,
                source_criterion_id=source_criterion_id,
                surface=surface,
                procedure=procedure,
            )

    return None


def _temporal_procedure_surfaces(criterion: ExtractedCriterion) -> list[str]:
    if criterion.temporal_window is None:
        return []

    procedure_mentions = [
        mention.text for mention in criterion.mentions if mention.type == "Procedure"
    ]
    surfaces: list[str] = []
    seen: set[str] = set()

    def add(surface: str | None) -> None:
        if surface is None:
            return
        stripped = " ".join(surface.strip().split())
        normalized = _normalize_text(stripped)
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        surfaces.append(stripped)

    add(criterion.temporal_window.event_text)
    for mention_surface in procedure_mentions:
        add(mention_surface)
    for surface in tuple(surfaces):
        add(_TEMPORAL_PROCEDURE_PREFIX_RE.sub("", surface))
    return surfaces


def _temporal_procedure_output(
    criterion: ExtractedCriterion,
    *,
    source_criterion_id: str,
    surface: str,
    procedure: _ConditionCompilation,
) -> _ConditionCompilation:
    diagnostics = [
        _diagnostic(
            severity="info",
            code="temporal_window.promoted.procedure_history",
            message=(
                f"Rerouted temporal-window event surface {surface!r} through "
                "procedure-history compilation."
            ),
            stage="predicate_translation",
            source_criterion_id=source_criterion_id,
            facts=[
                ("surface", surface),
                ("source_text", criterion.source_text),
                ("window_days", str(criterion.temporal_window.window_days))
                if criterion.temporal_window is not None
                else ("window_days", "none"),
                ("predicate_status", procedure.predicate.status),
            ],
        ),
        *procedure.diagnostics,
    ]
    return _ConditionCompilation(
        expansion=procedure.expansion,
        predicate=procedure.predicate,
        predicates=procedure.predicates,
        supports=procedure.supports,
        gaps=procedure.gaps,
        diagnostics=diagnostics,
    )


@dataclass(frozen=True)
class _FreeTextPromotionCompilation:
    predicate: CheckablePredicatePlan
    predicates: list[CheckablePredicate]
    supports: list[ResolutionSupport]
    gaps: list[ResolutionGap]
    diagnostics: list[CompilerDiagnostic]
    compound_logic: CompoundLogicPlan | None = None
    expansion: ExpansionPlan | None = None
    unit_normalization: UnitNormalizationPlan | None = None


def _compile_reviewed_procedure_history(
    criterion: ExtractedCriterion,
    *,
    source_criterion_id: str,
    surface: str | None,
    context: _CompilerResolutionContext,
    window_days: int | None = None,
) -> _ConditionCompilation | None:
    if surface is None:
        return None
    reviewed = _reviewed_procedure_entry(surface, context=context)
    if reviewed is None:
        return None

    entry, lookup_surface = reviewed
    support_ids: list[str] = []
    supports: list[ResolutionSupport] = []
    gaps: list[ResolutionGap] = []
    predicates: list[CheckablePredicate] = []
    diagnostics: list[CompilerDiagnostic] = []
    concept_set = _procedure_concept_set_from_reviewed_entry(entry)
    status: ResolutionStatus = "resolved"

    if entry.status != "mapped":
        status = "ambiguous" if entry.status == "ambiguous" else "unsupported"
        gap = _gap(
            gap_id=f"{source_criterion_id}:procedure:gap:reviewed-{entry.status}",
            stage="concept_resolution",
            domain="procedure",
            kind="ambiguous_mapping" if entry.status == "ambiguous" else "unsupported_predicate",
            source_criterion_id=source_criterion_id,
            surface=surface,
            message=(
                f"Reviewed procedure surface {lookup_surface!r} is classified as "
                f"{entry.status}: {entry.reason}"
            ),
            resolver_policy=context.resolver_policy,
        )
        gaps.append(gap)
        diagnostics.append(
            _diagnostic(
                severity="warning",
                code=f"procedure.reviewed.{entry.status}",
                message=gap.message,
                stage="concept_resolution",
                source_criterion_id=source_criterion_id,
                facts=[
                    ("surface", surface),
                    ("lookup_surface", lookup_surface),
                    ("reviewed_status", entry.status),
                    ("gap_id", gap.gap_id),
                ],
            )
        )
    elif concept_set is None:
        status = "unresolved"
        gap = _gap(
            gap_id=f"{source_criterion_id}:procedure:gap:missing-reviewed-codes",
            stage="concept_resolution",
            domain="procedure",
            kind="insufficient_source",
            source_criterion_id=source_criterion_id,
            surface=surface,
            message=(
                f"Reviewed procedure surface {lookup_surface!r} is mapped but has no "
                "candidate code list for predicate execution."
            ),
            resolver_policy=context.resolver_policy,
        )
        gaps.append(gap)
        diagnostics.append(
            _diagnostic(
                severity="warning",
                code="procedure.reviewed.missing_codes",
                message=gap.message,
                stage="concept_resolution",
                source_criterion_id=source_criterion_id,
                facts=[("lookup_surface", lookup_surface), ("gap_id", gap.gap_id)],
            )
        )
    else:
        support = _support(
            support_id=f"{source_criterion_id}:procedure:support:reviewed-code-list",
            stage="concept_resolution",
            domain="procedure",
            source_criterion_id=source_criterion_id,
            surface=surface,
            normalized_surface=lookup_surface,
            target_system=concept_set.system,
            target_id=_concept_set_target_id(concept_set),
            target_label=concept_set.name,
            resolver_policy=context.resolver_policy,
        )
        support_ids.append(support.support_id)
        supports.append(support)
        diagnostics.append(
            _diagnostic(
                severity="info",
                code="procedure.reviewed.mapped",
                message=(
                    f"Mapped procedure-history surface {surface!r} through reviewed "
                    f"procedure decision {lookup_surface!r}."
                ),
                stage="concept_resolution",
                source_criterion_id=source_criterion_id,
                facts=[
                    ("lookup_surface", lookup_surface),
                    ("code_count", str(len(concept_set.codes))),
                ],
            )
        )
        predicate = _checkable_predicate(
            predicate_id=f"{source_criterion_id}:predicate:procedure",
            predicate_kind="procedure_history",
            source_criterion_id=source_criterion_id,
            criterion=criterion,
            surface=surface,
            target_system=concept_set.system,
            target_codes=concept_set.codes,
            negated=_criterion_is_absence(criterion),
            window_days=window_days
            if window_days is not None
            else _free_text_window_days(criterion.source_text),
            support_ids=support_ids,
            gap_ids=[],
        )
        predicates.append(predicate)

    gap_ids = [gap.gap_id for gap in gaps]
    expansion = ExpansionPlan(
        status=status,
        domain="procedure",
        source_surface=surface,
        strategy=entry.expansion_policy,
        support_ids=support_ids,
        gap_ids=gap_ids,
    )
    predicate_plan = (
        _resolved_predicate_plan(predicates[0], support_ids=support_ids)
        if predicates and not gaps
        else CheckablePredicatePlan(
            status=status,
            predicate_kind="procedure_history",
            expression=None,
            input_refs=[source_criterion_id],
            support_ids=support_ids,
            gap_ids=gap_ids,
        )
    )
    return _ConditionCompilation(
        expansion=expansion,
        predicate=predicate_plan,
        predicates=predicates,
        supports=supports,
        gaps=gaps,
        diagnostics=diagnostics,
    )


def _reviewed_procedure_entry(
    surface: str,
    *,
    context: _CompilerResolutionContext,
) -> tuple[ReviewedMappingEntry, str] | None:
    for lookup_surface in _procedure_lookup_variants(surface):
        entry = context.reviewed_registry.lookup("procedure", lookup_surface)
        if entry is not None:
            return entry, lookup_surface
    return None


def _procedure_lookup_variants(surface: str) -> list[str]:
    variants: list[str] = []
    seen: set[str] = set()

    def add(candidate: str) -> None:
        normalized = " ".join(candidate.lower().split())
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        variants.append(candidate)

    add(surface)
    stripped = _PROCEDURE_HISTORY_PREFIX_RE.sub("", surface).strip()
    add(stripped)
    for variant in generate_query_variants(surface):
        add(variant.variant)
    for variant in generate_query_variants(stripped):
        add(variant.variant)
    return variants


def _procedure_concept_set_from_reviewed_entry(
    entry: ReviewedMappingEntry,
) -> ConceptSet | None:
    if entry.status != "mapped":
        return None
    candidate = next((candidate for candidate in entry.candidates if candidate.codes), None)
    if candidate is None:
        return None
    return ConceptSet(
        name=candidate.name or entry.concept_set or entry.surface,
        system=candidate.system or _SNOMED,
        codes=candidate.codes,
    )


def _compile_condition_resolution(
    criterion: ExtractedCriterion,
    *,
    source_criterion_id: str,
    context: _CompilerResolutionContext,
) -> _ConditionCompilation:
    surface = criterion.condition.condition_text if criterion.condition is not None else None
    if surface is None:
        gap = _gap(
            gap_id=f"{source_criterion_id}:condition:gap:missing-source",
            stage="concept_resolution",
            domain="condition",
            kind="insufficient_source",
            source_criterion_id=source_criterion_id,
            surface=None,
            message="Condition compiler received a criterion without a condition payload.",
            resolver_policy=context.resolver_policy,
        )
        return _condition_output(
            criterion,
            source_criterion_id=source_criterion_id,
            surface=None,
            expansion=_default_expansion_plan(domain="condition", surface=None).model_copy(
                update={"status": "unresolved", "gap_ids": [gap.gap_id]}
            ),
            supports=[],
            gaps=[gap],
            diagnostics=[],
            concept_set=None,
            support_ids=[],
        )

    reviewed_nonmapped = _reviewed_nonmapped_condition_entry(surface, context=context)
    if reviewed_nonmapped is not None:
        reviewed_entry, lookup_surface = reviewed_nonmapped
        if _is_reviewed_free_text_review_condition(reviewed_entry, lookup_surface):
            return _reviewed_free_text_review_condition_output(
                criterion,
                source_criterion_id=source_criterion_id,
                surface=surface,
                lookup_surface=lookup_surface,
                reviewed_entry=reviewed_entry,
                context=context,
            )
        gap_kind = _reviewed_nonmapped_gap_kind(reviewed_entry)
        status: ResolutionStatus = "ambiguous" if gap_kind == "ambiguous_mapping" else "unsupported"
        gap = _gap(
            gap_id=f"{source_criterion_id}:condition:gap:reviewed-{reviewed_entry.status}",
            stage="concept_resolution",
            domain="condition",
            kind=gap_kind,
            source_criterion_id=source_criterion_id,
            surface=surface,
            message=(
                f"Reviewed condition surface {lookup_surface!r} is classified as "
                f"{reviewed_entry.status}: {reviewed_entry.reason}"
            ),
            resolver_policy=context.resolver_policy,
        )
        diagnostic = _diagnostic(
            severity="warning",
            code=f"condition.reviewed.{reviewed_entry.status}",
            message=gap.message,
            stage="concept_resolution",
            source_criterion_id=source_criterion_id,
            facts=[
                ("surface", surface),
                ("lookup_surface", lookup_surface),
                ("reviewed_status", reviewed_entry.status),
                ("gap_id", gap.gap_id),
            ],
        )
        return _condition_output(
            criterion,
            source_criterion_id=source_criterion_id,
            surface=surface,
            expansion=ExpansionPlan(
                status=status,
                domain="condition",
                source_surface=surface,
                strategy=reviewed_entry.expansion_policy,
                support_ids=[],
                gap_ids=[gap.gap_id],
            ),
            supports=[],
            gaps=[gap],
            diagnostics=[diagnostic],
            concept_set=None,
            support_ids=[],
        )

    candidates, concept_sets = _condition_candidates(surface, context=context)
    decision = gate_candidate_set(candidates)
    if decision.verdict != "auto_map" or decision.selected is None:
        kind: ResolutionGapKind = (
            "unmapped_concept" if decision.verdict == "no_candidates" else "ambiguous_mapping"
        )
        gap = _gap(
            gap_id=f"{source_criterion_id}:condition:gap:{decision.verdict}",
            stage="concept_resolution",
            domain="condition",
            kind=kind,
            source_criterion_id=source_criterion_id,
            surface=surface,
            message=decision.reason,
            resolver_policy=context.resolver_policy,
        )
        diagnostic = _diagnostic(
            severity="warning",
            code=f"condition.{decision.verdict}",
            message=decision.reason,
            stage="concept_resolution",
            source_criterion_id=source_criterion_id,
            facts=[
                ("candidate_count", str(len(decision.ranked_candidates))),
                ("gap_id", gap.gap_id),
            ],
        )
        return _condition_output(
            criterion,
            source_criterion_id=source_criterion_id,
            surface=surface,
            expansion=_default_expansion_plan(domain="condition", surface=surface).model_copy(
                update={"status": "unresolved", "gap_ids": [gap.gap_id]}
            ),
            supports=[],
            gaps=[gap],
            diagnostics=[diagnostic],
            concept_set=None,
            support_ids=[],
        )

    concept_set = concept_sets[decision.selected.target_key]
    policy = _condition_expansion_policy(decision.selected, surface, context)
    concept_support = _support(
        support_id=f"{source_criterion_id}:condition:support:concept-set",
        stage="concept_resolution",
        domain="condition",
        source_criterion_id=source_criterion_id,
        surface=surface,
        normalized_surface=decision.selected.matched_variant,
        target_system=concept_set.system,
        target_id=_concept_set_target_id(concept_set),
        target_label=concept_set.name,
        resolver_policy=context.resolver_policy,
    )
    supports = [concept_support]
    gaps: list[ResolutionGap] = []
    diagnostics = [
        _diagnostic(
            severity="info",
            code="condition.mapped",
            message=(
                f"Mapped condition surface {surface!r} through "
                f"{decision.selected.source.kind} candidate {decision.selected.matched_variant!r}."
            ),
            stage="concept_resolution",
            source_criterion_id=source_criterion_id,
            facts=[
                ("matched_variant", decision.selected.matched_variant),
                ("candidate_source", decision.selected.source.kind),
            ],
        )
    ]

    expansion_result = expand_concept_set(concept_set, policy=policy)
    expanded_concept_set = expansion_result.expanded_concept_set
    expansion_support_ids = [concept_support.support_id]
    if expansion_result.status == "resolved" and expanded_concept_set is not None:
        expansion_support = _support(
            support_id=f"{source_criterion_id}:condition:support:expansion",
            stage="expansion",
            domain="condition",
            source_criterion_id=source_criterion_id,
            surface=surface,
            normalized_surface=decision.selected.matched_variant,
            target_system=expanded_concept_set.system,
            target_id=",".join(sorted(expanded_concept_set.codes)),
            target_label=expanded_concept_set.name,
            resolver_policy=context.resolver_policy,
        )
        supports.append(expansion_support)
        expansion_support_ids.append(expansion_support.support_id)
    else:
        gap = _gap(
            gap_id=f"{source_criterion_id}:condition:gap:expansion",
            stage="expansion",
            domain="condition",
            kind="unsupported_predicate"
            if expansion_result.status == "unsupported"
            else "insufficient_source",
            source_criterion_id=source_criterion_id,
            surface=surface,
            message=expansion_result.reason,
            resolver_policy=context.resolver_policy,
        )
        gaps.append(gap)
        diagnostics.append(
            _diagnostic(
                severity="warning",
                code=f"condition.expansion.{expansion_result.status}",
                message=expansion_result.reason,
                stage="expansion",
                source_criterion_id=source_criterion_id,
                facts=[("gap_id", gap.gap_id), ("policy", policy)],
            )
        )

    expansion = ExpansionPlan(
        status=expansion_result.status,
        domain="condition",
        source_surface=surface,
        strategy=policy,
        support_ids=expansion_support_ids,
        gap_ids=[gap.gap_id for gap in gaps],
    )
    return _condition_output(
        criterion,
        source_criterion_id=source_criterion_id,
        surface=surface,
        expansion=expansion,
        supports=supports,
        gaps=gaps,
        diagnostics=diagnostics,
        concept_set=expanded_concept_set if expanded_concept_set is not None else concept_set,
        support_ids=expansion_support_ids,
    )


def _condition_candidates(
    surface: str,
    *,
    context: _CompilerResolutionContext,
) -> tuple[list[TerminologyCandidate], dict[tuple[str, str], ConceptSet]]:
    candidates: list[TerminologyCandidate] = []
    concept_sets: dict[tuple[str, str], ConceptSet] = {}

    exact_reviewed_entry = context.reviewed_registry.lookup("condition", surface)
    if (
        exact_reviewed_entry is not None
        and exact_reviewed_entry.status == "mapped"
        and context.resolver_policy != "disabled"
    ):
        concept_set = context.resolver.resolve_condition(surface)
        if concept_set is not None:
            candidate = TerminologyCandidate(
                source=CandidateSource(kind="reviewed_registry", name="reviewed_registry"),
                matched_surface=surface,
                matched_variant=surface,
                code=_concept_set_target_id(concept_set),
                system=concept_set.system,
                name=concept_set.name,
                score=1.0,
                confidence_bucket=bucket_for_score(1.0),
            )
            return [candidate], {candidate.target_key: concept_set}

    lookup_variants: list[tuple[str, int]] = []
    seen_lookup_variants: set[str] = set()

    def add_lookup_variant(candidate: str, transform_count: int) -> None:
        normalized = " ".join(candidate.lower().split())
        if not normalized or normalized in seen_lookup_variants:
            return
        seen_lookup_variants.add(normalized)
        lookup_variants.append((candidate, transform_count))

    add_lookup_variant(surface, 0)
    for variant in generate_query_variants(surface):
        add_lookup_variant(variant.variant, len(variant.transforms))

    for lookup_surface, transform_count in lookup_variants:
        reviewed_entry = context.reviewed_registry.lookup("condition", lookup_surface)
        concept_set = (
            context.resolver.resolve_condition(lookup_surface)
            if context.resolver_policy != "disabled"
            else None
        )
        source_kind: CandidateSourceKind = (
            "reviewed_registry" if reviewed_entry is not None else "surface_cache"
        )
        if concept_set is None and reviewed_entry is None and context.resolver_policy != "disabled":
            concept_set = lookup_condition_alias(lookup_surface)
            source_kind = "local_alias"
        if concept_set is None:
            continue

        score = _candidate_score(source_kind, transform_count)
        candidate = TerminologyCandidate(
            source=CandidateSource(kind=source_kind, name=source_kind),
            matched_surface=surface,
            matched_variant=lookup_surface,
            code=_concept_set_target_id(concept_set),
            system=concept_set.system,
            name=concept_set.name,
            score=score,
            confidence_bucket=bucket_for_score(score),
        )
        candidates.append(candidate)
        concept_sets[candidate.target_key] = concept_set

    return candidates, concept_sets


def _is_reviewed_free_text_review_condition(
    entry: ReviewedMappingEntry,
    lookup_surface: str,
) -> bool:
    return (
        entry.status == "out_of_scope"
        and _normalize_text(lookup_surface) in _REVIEWED_FREE_TEXT_REVIEW_CONDITION_SURFACES
    )


def _reviewed_free_text_review_condition_output(
    criterion: ExtractedCriterion,
    *,
    source_criterion_id: str,
    surface: str,
    lookup_surface: str,
    reviewed_entry: ReviewedMappingEntry,
    context: _CompilerResolutionContext,
) -> _ConditionCompilation:
    message = (
        f"Reviewed condition surface {lookup_surface!r} is a human-review/free-text "
        f"criterion rather than a condition ConceptSet: {reviewed_entry.reason}"
    )
    gap = _gap(
        gap_id=f"{source_criterion_id}:free-text-review:gap:reviewed-{reviewed_entry.status}",
        stage="predicate_translation",
        domain="free_text",
        kind="unsupported_predicate",
        source_criterion_id=source_criterion_id,
        surface=surface,
        message=message,
        resolver_policy=context.resolver_policy,
    )
    diagnostic = _diagnostic(
        severity="warning",
        code=f"condition.reviewed.{reviewed_entry.status}.free_text_review",
        message=message,
        stage="predicate_translation",
        source_criterion_id=source_criterion_id,
        facts=[
            ("surface", surface),
            ("lookup_surface", lookup_surface),
            ("reviewed_status", reviewed_entry.status),
            ("gap_id", gap.gap_id),
        ],
    )
    return _ConditionCompilation(
        expansion=ExpansionPlan(
            status="skipped",
            domain="free_text",
            source_surface=surface,
            strategy="none",
            support_ids=[],
            gap_ids=[gap.gap_id],
        ),
        predicate=CheckablePredicatePlan(
            status="unsupported",
            predicate_kind="free_text_review",
            expression=None,
            input_refs=[source_criterion_id],
            support_ids=[],
            gap_ids=[gap.gap_id],
        ),
        predicates=[],
        supports=[],
        gaps=[gap],
        diagnostics=[diagnostic],
    )


def _reviewed_nonmapped_condition_entry(
    surface: str,
    *,
    context: _CompilerResolutionContext,
) -> tuple[ReviewedMappingEntry, str] | None:
    lookup_variants: list[str] = []
    seen_lookup_variants: set[str] = set()

    def add_lookup_variant(candidate: str) -> None:
        normalized = " ".join(candidate.lower().split())
        if not normalized or normalized in seen_lookup_variants:
            return
        seen_lookup_variants.add(normalized)
        lookup_variants.append(candidate)

    add_lookup_variant(surface)
    for variant in generate_query_variants(surface):
        add_lookup_variant(variant.variant)

    for lookup_surface in lookup_variants:
        entry = context.reviewed_registry.lookup("condition", lookup_surface)
        if entry is not None and entry.status != "mapped":
            return entry, lookup_surface
    return None


def _reviewed_nonmapped_gap_kind(entry: ReviewedMappingEntry) -> ResolutionGapKind:
    if entry.status == "ambiguous":
        return "ambiguous_mapping"
    return "unsupported_predicate"


def _candidate_score(source_kind: CandidateSourceKind, transform_count: int) -> float:
    base = {
        "reviewed_registry": 1.0,
        "surface_cache": 0.96,
        "local_alias": 0.93,
    }.get(source_kind, 0.70)
    return max(0.0, base - min(transform_count, 6) * 0.005)


def _condition_expansion_policy(
    candidate: TerminologyCandidate,
    original_surface: str,
    context: _CompilerResolutionContext,
) -> ExpansionPolicy:
    entry = context.reviewed_registry.lookup("condition", candidate.matched_variant)
    if entry is None:
        entry = context.reviewed_registry.lookup("condition", original_surface)
    return entry.expansion_policy if entry is not None else "exact_code"


def _condition_output(
    criterion: ExtractedCriterion,
    *,
    source_criterion_id: str,
    surface: str | None,
    expansion: ExpansionPlan,
    supports: list[ResolutionSupport],
    gaps: list[ResolutionGap],
    diagnostics: list[CompilerDiagnostic],
    concept_set: ConceptSet | None,
    support_ids: list[str],
) -> _ConditionCompilation:
    gap_ids = [gap.gap_id for gap in gaps]
    predicates: list[CheckablePredicate] = []
    predicate_ids: list[str] = []
    expression = None
    status: ResolutionStatus = "resolved" if concept_set is not None and not gaps else "unresolved"
    if concept_set is not None and not gaps:
        predicate = _checkable_predicate(
            predicate_id=f"{source_criterion_id}:predicate:condition",
            predicate_kind="condition_presence",
            source_criterion_id=source_criterion_id,
            criterion=criterion,
            surface=surface,
            target_system=concept_set.system,
            target_codes=concept_set.codes,
            negated=_criterion_is_absence(criterion),
            support_ids=support_ids,
            gap_ids=[],
        )
        predicates.append(predicate)
        predicate_ids.append(predicate.predicate_id)
        expression = _coded_expression(predicate)

    return _ConditionCompilation(
        expansion=expansion,
        predicate=CheckablePredicatePlan(
            status=status,
            predicate_kind="condition_presence",
            expression=expression,
            predicate_ids=predicate_ids,
            input_refs=[source_criterion_id],
            support_ids=support_ids,
            gap_ids=gap_ids,
        ),
        predicates=predicates,
        supports=supports,
        gaps=gaps,
        diagnostics=diagnostics,
    )


def _compile_demographic(
    criterion: ExtractedCriterion,
    *,
    source_criterion_id: str,
    resolver_policy: ResolverExecutionPolicy,
) -> tuple[
    CheckablePredicatePlan,
    list[CheckablePredicate],
    list[ResolutionGap],
    list[CompilerDiagnostic],
]:
    predicate_id = f"{source_criterion_id}:predicate:demographic"
    if criterion.age is not None:
        operator: ThresholdOperator | None = None
        value = None
        value_low = criterion.age.minimum_years
        value_high = criterion.age.maximum_years
        if value_low is not None and value_high is not None:
            operator = "in_range"
        elif value_low is not None:
            operator = ">="
            value = value_low
            value_low = None
        elif value_high is not None:
            operator = "<="
            value = value_high
            value_high = None
        else:
            return _unsupported_demographic(
                criterion,
                source_criterion_id=source_criterion_id,
                resolver_policy=resolver_policy,
                message="Age criterion has neither minimum nor maximum age.",
            )
        predicate = _checkable_predicate(
            predicate_id=predicate_id,
            predicate_kind="demographic",
            source_criterion_id=source_criterion_id,
            criterion=criterion,
            surface=criterion.source_text,
            target_system="demographic.age",
            target_codes=frozenset(),
            operator=operator,
            value=value,
            value_low=value_low,
            value_high=value_high,
            unit="years",
        )
        return _resolved_predicate_plan(predicate), [predicate], [], []

    if criterion.sex is not None:
        predicate = _checkable_predicate(
            predicate_id=predicate_id,
            predicate_kind="demographic",
            source_criterion_id=source_criterion_id,
            criterion=criterion,
            surface=criterion.source_text,
            target_system="demographic.sex",
            target_codes=frozenset({criterion.sex.sex}),
        )
        return _resolved_predicate_plan(predicate), [predicate], [], []

    return _unsupported_demographic(
        criterion,
        source_criterion_id=source_criterion_id,
        resolver_policy=resolver_policy,
        message="Demographic compiler received age/sex kind without a payload.",
    )


def _unsupported_demographic(
    criterion: ExtractedCriterion,
    *,
    source_criterion_id: str,
    resolver_policy: ResolverExecutionPolicy,
    message: str,
) -> tuple[
    CheckablePredicatePlan,
    list[CheckablePredicate],
    list[ResolutionGap],
    list[CompilerDiagnostic],
]:
    gap = _gap(
        gap_id=f"{source_criterion_id}:demographic:gap:insufficient-source",
        stage="predicate_translation",
        domain="demographic",
        kind="insufficient_source",
        source_criterion_id=source_criterion_id,
        surface=criterion.source_text,
        message=message,
        resolver_policy=resolver_policy,
    )
    return (
        CheckablePredicatePlan(
            status="unresolved",
            predicate_kind="demographic",
            expression=None,
            input_refs=[source_criterion_id],
            support_ids=[],
            gap_ids=[gap.gap_id],
        ),
        [],
        [gap],
        [],
    )


def _measurement_predicate(
    criterion: ExtractedCriterion,
    *,
    source_criterion_id: str,
    measurement: MeasurementResolutionResult,
) -> tuple[CheckablePredicatePlan, list[CheckablePredicate]]:
    support_ids = [support.support_id for support in measurement.resolved_supports]
    gap_ids = [gap.gap_id for gap in measurement.unresolved_gaps]
    if (
        measurement.concept_set is None
        or measurement.unit_normalization.status != "resolved"
        or measurement.unresolved_gaps
    ):
        return (
            CheckablePredicatePlan(
                status="unsupported"
                if _has_unsupported_measurement_gap(measurement)
                else "unresolved",
                predicate_kind="measurement_threshold",
                expression=None,
                input_refs=[source_criterion_id],
                support_ids=support_ids,
                gap_ids=gap_ids,
            ),
            [],
        )

    predicate = _checkable_predicate(
        predicate_id=f"{source_criterion_id}:predicate:measurement",
        predicate_kind="measurement_threshold",
        source_criterion_id=source_criterion_id,
        criterion=criterion,
        surface=measurement.measurement_surface,
        target_system=measurement.concept_set.system,
        target_codes=frozenset(measurement.loinc_codes),
        operator=measurement.normalized_operator,
        value=measurement.normalized_value,
        value_by_sex=measurement.normalized_value_by_sex,
        value_low=measurement.normalized_value_low,
        value_high=measurement.normalized_value_high,
        unit=measurement.unit_normalization.conventional_unit,
        support_ids=support_ids,
        gap_ids=[],
    )
    return _resolved_predicate_plan(predicate, support_ids=support_ids), [predicate]


def _has_unsupported_measurement_gap(measurement: MeasurementResolutionResult) -> bool:
    return any(
        gap.kind in {"normal_range_unknown", "provenance_required", "unsupported_predicate"}
        for gap in measurement.unresolved_gaps
    )


def _temporal_predicate(
    criterion: ExtractedCriterion,
    *,
    source_criterion_id: str,
    temporal: TemporalWindowCompilation,
) -> tuple[CheckablePredicatePlan, list[CheckablePredicate]]:
    if temporal.predicate.status != "resolved" or temporal.event_concept_set is None:
        return temporal.predicate, []

    predicate = _checkable_predicate(
        predicate_id=f"{source_criterion_id}:predicate:temporal",
        predicate_kind="temporal_event",
        source_criterion_id=source_criterion_id,
        criterion=criterion,
        surface=temporal.event_surface,
        target_system=temporal.event_concept_set.system,
        target_codes=temporal.event_concept_set.codes,
        window_days=temporal.window_days,
        support_ids=temporal.predicate.support_ids,
        gap_ids=[],
    )
    return temporal.predicate.model_copy(update={"predicate_ids": [predicate.predicate_id]}), [
        predicate
    ]


def _medication_predicate(
    criterion: ExtractedCriterion,
    *,
    source_criterion_id: str,
    medication: MedicationCompilationResult,
) -> tuple[CheckablePredicatePlan, list[CheckablePredicate]]:
    if medication.predicate.status != "resolved" or medication.concept_set is None:
        return medication.predicate, []

    predicate = _checkable_predicate(
        predicate_id=f"{source_criterion_id}:predicate:medication",
        predicate_kind="medication_exposure",
        source_criterion_id=source_criterion_id,
        criterion=criterion,
        surface=medication.surface,
        target_system=medication.concept_set.system,
        target_codes=medication.concept_set.codes,
        negated=_criterion_is_absence(criterion),
        window_days=_free_text_window_days(criterion.source_text),
        min_duration_days=_minimum_duration_days(criterion.source_text),
        support_ids=medication.predicate.support_ids,
        gap_ids=[],
    )
    return medication.predicate.model_copy(
        update={
            "expression": _coded_expression(predicate),
            "predicate_ids": [predicate.predicate_id],
        }
    ), [predicate]


def _resolved_predicate_plan(
    predicate: CheckablePredicate,
    *,
    support_ids: list[str] | None = None,
) -> CheckablePredicatePlan:
    return CheckablePredicatePlan(
        status="resolved",
        predicate_kind=predicate.predicate_kind,
        expression=_coded_expression(predicate),
        predicate_ids=[predicate.predicate_id],
        input_refs=[predicate.source_criterion_id],
        support_ids=support_ids or predicate.support_ids,
        gap_ids=[],
    )


def _checkable_predicate(
    *,
    predicate_id: str,
    predicate_kind: PredicateKind,
    source_criterion_id: str,
    criterion: ExtractedCriterion,
    surface: str | None,
    target_system: str | None,
    target_codes: frozenset[str],
    negated: bool | None = None,
    operator: ThresholdOperator | None = None,
    value: float | None = None,
    value_by_sex: dict[str, float] | None = None,
    value_low: float | None = None,
    value_high: float | None = None,
    unit: str | None = None,
    window_days: int | None = None,
    min_duration_days: int | None = None,
    support_ids: list[str] | None = None,
    gap_ids: list[str] | None = None,
) -> CheckablePredicate:
    return CheckablePredicate(
        predicate_id=predicate_id,
        predicate_kind=predicate_kind,
        source_criterion_id=source_criterion_id,
        polarity=criterion.polarity,
        negated=criterion.negated if negated is None else negated,
        surface=surface,
        target_system=target_system,
        target_codes=target_codes,
        operator=operator,
        value=value,
        value_by_sex=value_by_sex or {},
        value_low=value_low,
        value_high=value_high,
        unit=unit,
        window_days=window_days,
        min_duration_days=min_duration_days,
        support_ids=support_ids or [],
        gap_ids=gap_ids or [],
    )


def _coded_expression(predicate: CheckablePredicate) -> str:
    code_count = len(predicate.target_codes)
    parts = [f"codes={code_count}"]
    if predicate.window_days is not None:
        parts.append(f"window={predicate.window_days}d")
    if predicate.min_duration_days is not None:
        parts.append(f"min_duration={predicate.min_duration_days}d")
    return f"{predicate.predicate_kind}({predicate.predicate_id},{','.join(parts)})"


def _criterion_is_absence(criterion: ExtractedCriterion) -> bool:
    return criterion.negated or criterion.kind in {"condition_absent", "medication_absent"}


def _concept_set_target_id(concept_set: ConceptSet) -> str:
    return f"{concept_set.name}|{','.join(sorted(concept_set.codes))}"


def _support(
    *,
    support_id: str,
    stage: ResolutionStage,
    domain: ResolutionDomain,
    source_criterion_id: str,
    surface: str | None,
    normalized_surface: str | None,
    target_system: str | None,
    target_id: str | None,
    target_label: str | None,
    resolver_policy: ResolverExecutionPolicy,
) -> ResolutionSupport:
    return ResolutionSupport(
        support_id=support_id,
        stage=stage,
        domain=domain,
        source_criterion_id=source_criterion_id,
        surface=surface,
        normalized_surface=normalized_surface,
        target_system=target_system,
        target_id=target_id,
        target_label=target_label,
        resolver_policy=resolver_policy,
    )


def _gap(
    *,
    gap_id: str,
    stage: ResolutionStage,
    domain: ResolutionDomain,
    kind: ResolutionGapKind,
    source_criterion_id: str,
    surface: str | None,
    message: str,
    resolver_policy: ResolverExecutionPolicy,
) -> ResolutionGap:
    return ResolutionGap(
        gap_id=gap_id,
        stage=stage,
        domain=domain,
        kind=kind,
        source_criterion_id=source_criterion_id,
        surface=surface,
        message=message,
        resolver_policy=resolver_policy,
    )


def _diagnostic(
    *,
    severity: DiagnosticSeverity,
    code: str,
    message: str,
    stage: ResolutionStage,
    source_criterion_id: str,
    facts: list[tuple[str, str]],
) -> CompilerDiagnostic:
    return CompilerDiagnostic(
        severity=severity,
        code=code,
        message=message,
        stage=stage,
        source_criterion_id=source_criterion_id,
        facts=[DiagnosticFact(key=key, value=value) for key, value in facts],
    )


def _resolution_domain(kind: CriterionKind) -> ResolutionDomain:
    if kind == "age" or kind == "sex":
        return "demographic"
    if kind in {"condition_present", "condition_absent"}:
        return "condition"
    if kind in {"medication_present", "medication_absent"}:
        return "medication"
    if kind == "measurement_threshold":
        return "measurement"
    if kind == "temporal_window":
        return "temporal"
    return "free_text"


def _predicate_kind(kind: CriterionKind) -> PredicateKind:
    if kind == "age" or kind == "sex":
        return "demographic"
    if kind in {"condition_present", "condition_absent"}:
        return "condition_presence"
    if kind in {"medication_present", "medication_absent"}:
        return "medication_exposure"
    if kind == "measurement_threshold":
        return "measurement_threshold"
    if kind == "temporal_window":
        return "temporal_event"
    if kind == "free_text":
        return "free_text_review"
    return "unsupported"


def _criterion_surface(criterion: ExtractedCriterion) -> str | None:
    if criterion.condition is not None:
        return criterion.condition.condition_text
    if criterion.medication is not None:
        return criterion.medication.medication_text
    if criterion.measurement is not None:
        return criterion.measurement.measurement_text
    if criterion.temporal_window is not None:
        return criterion.temporal_window.event_text
    if criterion.free_text is not None:
        return criterion.source_text
    if criterion.age is not None or criterion.sex is not None:
        return criterion.source_text
    return None
