"""Typed criterion compiler intermediate representation.

The first compiler pass is intentionally an identity layer over the
extractor output. It records the inputs and future resolution stages in
typed containers, but does not change the matcher-facing criteria.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from clinical_demo.extractor.schema import (
    CompositeOperator,
    CriterionKind,
    ExtractedCriterion,
    Polarity,
    ThresholdOperator,
)
from clinical_demo.settings import ResolverExecutionPolicy

COMPILER_VERSION = "criterion-compiler-ir-v0.1"

ResolutionStage = Literal[
    "matcher_input",
    "concept_resolution",
    "expansion",
    "compound_logic",
    "unit_normalization",
    "predicate_translation",
]

ResolutionDomain = Literal[
    "demographic",
    "condition",
    "medication",
    "measurement",
    "procedure",
    "temporal",
    "unit",
    "compound",
    "predicate",
    "free_text",
]

ResolutionStatus = Literal[
    "not_attempted",
    "resolved",
    "unresolved",
    "ambiguous",
    "unsupported",
    "skipped",
]

ResolutionGapKind = Literal[
    "unmapped_concept",
    "ambiguous_mapping",
    "missing_unit",
    "unsupported_compound",
    "unsupported_predicate",
    "normal_range_unknown",
    "provenance_required",
    "insufficient_source",
    "interview_required",
    "not_attempted",
]

DiagnosticSeverity = Literal["info", "warning", "error"]
ExpansionStrategy = Literal[
    "none",
    "exact_code",
    "descendants",
    "value_set_oid",
    "reviewed_code_list",
    "patient_vocabulary_closure",
]
PredicateKind = Literal[
    "compound",
    "demographic",
    "condition_presence",
    "medication_exposure",
    "measurement_threshold",
    "procedure_history",
    "temporal_event",
    "trial_exposure",
    "free_text_review",
    "unsupported",
]
CompoundOperator = CompositeOperator | Literal["none"]


class DiagnosticFact(BaseModel):
    """A small typed key/value fact attached to a compiler diagnostic."""

    key: str = Field(description="Stable detail key.")
    value: str = Field(description="Human-readable value.")


class CompilerDiagnostic(BaseModel):
    """Non-blocking compiler note suitable for eval artifacts and UI traces."""

    severity: DiagnosticSeverity = Field(description="Diagnostic severity.")
    code: str = Field(description="Stable machine-readable diagnostic code.")
    message: str = Field(description="Reviewer-facing diagnostic text.")
    stage: ResolutionStage | None = Field(description="Compiler stage that emitted the note.")
    source_criterion_id: str | None = Field(
        description="Source criterion id, when the note applies to one criterion."
    )
    facts: list[DiagnosticFact] = Field(
        default_factory=list,
        description="Typed diagnostic detail facts.",
    )


class ResolutionSupport(BaseModel):
    """Evidence that a compiler stage resolved part of a criterion."""

    support_id: str = Field(description="Stable id within the compilation result.")
    stage: ResolutionStage = Field(description="Stage that produced this support.")
    domain: ResolutionDomain = Field(description="Clinical or compiler domain resolved.")
    source_criterion_id: str = Field(description="Criterion id this support belongs to.")
    surface: str | None = Field(description="Original surface text, if applicable.")
    normalized_surface: str | None = Field(description="Normalized lookup surface, if applicable.")
    target_system: str | None = Field(description="Target coding system or internal namespace.")
    target_id: str | None = Field(description="Resolved code, value-set id, or internal target id.")
    target_label: str | None = Field(description="Resolved display label, if known.")
    resolver_policy: ResolverExecutionPolicy = Field(
        description="Resolver policy in effect when support was produced."
    )


class ResolutionGap(BaseModel):
    """A typed unresolved gap left by a compiler stage."""

    gap_id: str = Field(description="Stable id within the compilation result.")
    stage: ResolutionStage = Field(description="Stage that encountered the gap.")
    domain: ResolutionDomain = Field(description="Clinical or compiler domain with the gap.")
    kind: ResolutionGapKind = Field(description="Machine-readable gap category.")
    source_criterion_id: str = Field(description="Criterion id this gap belongs to.")
    surface: str | None = Field(description="Original surface text, if applicable.")
    message: str = Field(description="Reviewer-facing explanation.")
    resolver_policy: ResolverExecutionPolicy = Field(
        description="Resolver policy in effect when the gap was produced."
    )


class ExpansionPlan(BaseModel):
    """Future concept expansion plan for a criterion surface."""

    status: ResolutionStatus = Field(description="Current expansion status.")
    domain: ResolutionDomain = Field(description="Domain to expand.")
    source_surface: str | None = Field(description="Surface that would be expanded.")
    strategy: ExpansionStrategy = Field(description="Expansion strategy to apply.")
    support_ids: list[str] = Field(
        default_factory=list,
        description="Resolution supports consumed by this expansion.",
    )
    gap_ids: list[str] = Field(
        default_factory=list,
        description="Unresolved gaps blocking expansion.",
    )


class CompoundLogicPlan(BaseModel):
    """Future boolean decomposition plan for compound criteria."""

    status: ResolutionStatus = Field(description="Current compound-logic status.")
    operator: CompoundOperator = Field(description="Boolean operator when known.")
    source_group_ids: list[str] = Field(
        default_factory=list,
        description="Extractor composite group ids attached to this criterion.",
    )
    subcheck_ids: list[str] = Field(
        default_factory=list,
        description="Extractor subcheck ids attached to this criterion.",
    )
    gap_ids: list[str] = Field(
        default_factory=list,
        description="Unresolved gaps blocking compound decomposition.",
    )


class UnitNormalizationPlan(BaseModel):
    """Future unit normalization plan for measurement criteria."""

    status: ResolutionStatus = Field(description="Current unit normalization status.")
    measurement_surface: str | None = Field(
        description="Measurement surface, if this is a lab/vital."
    )
    source_unit: str | None = Field(description="Unit as written by the trial.")
    canonical_unit: str | None = Field(description="Canonical unit after normalization.")
    conventional_unit: str | None = Field(description="Preferred patient-data unit.")
    conversion_factor: float | None = Field(
        description="Multiplier from source unit to conventional unit, if known."
    )
    gap_ids: list[str] = Field(
        default_factory=list,
        description="Unresolved gaps blocking unit normalization.",
    )


class CheckablePredicate(BaseModel):
    """Typed executable predicate target produced by the compiler.

    The current matcher still consumes `ExtractedCriterion`; this model
    is the compiler-side shape that later validation/matcher work can
    promote into the source of execution. Fields are optional by domain
    so one model can carry condition, medication, measurement, temporal,
    demographic, and review predicates without unsafe string parsing.
    """

    predicate_id: str = Field(description="Stable id within the compilation result.")
    predicate_kind: PredicateKind = Field(description="Kind of predicate to execute.")
    source_criterion_id: str = Field(description="Criterion id this predicate belongs to.")
    polarity: Polarity = Field(description="Original criterion polarity.")
    negated: bool = Field(description="Whether the original criterion was negated.")
    surface: str | None = Field(description="Original clinical surface, if applicable.")
    target_system: str | None = Field(description="Coding system URI, when coded.")
    target_codes: frozenset[str] = Field(
        default_factory=frozenset,
        description="Codes or class ids the predicate should check.",
    )
    operator: ThresholdOperator | None = Field(
        description="Numeric threshold operator for measurement predicates."
    )
    value: float | None = Field(description="Single threshold value, when applicable.")
    value_by_sex: dict[str, float] = Field(
        default_factory=dict,
        description="Single threshold values keyed by profile sex, when reference limits are sex-specific.",
    )
    value_low: float | None = Field(description="Inclusive lower bound, when applicable.")
    value_high: float | None = Field(description="Inclusive upper bound, when applicable.")
    unit: str | None = Field(description="Canonical/conventional unit used for comparison.")
    window_days: int | None = Field(description="Temporal window length in days.")
    min_duration_days: int | None = Field(
        default=None,
        description="Minimum observed exposure/event duration in days, when applicable.",
    )
    support_ids: list[str] = Field(
        default_factory=list,
        description="Resolution supports consumed by this predicate.",
    )
    gap_ids: list[str] = Field(
        default_factory=list,
        description="Unresolved gaps blocking or qualifying this predicate.",
    )


class CheckablePredicatePlan(BaseModel):
    """Future executable predicate translation for a compiled criterion."""

    status: ResolutionStatus = Field(description="Current predicate-translation status.")
    predicate_kind: PredicateKind = Field(description="Kind of predicate to build.")
    expression: str | None = Field(description="Stable predicate expression, once available.")
    predicate_ids: list[str] = Field(
        default_factory=list,
        description="Typed checkable predicate ids produced for this criterion.",
    )
    input_refs: list[str] = Field(
        default_factory=list,
        description="Compiled/source ids this predicate consumes.",
    )
    support_ids: list[str] = Field(
        default_factory=list,
        description="Resolution supports consumed by this predicate.",
    )
    gap_ids: list[str] = Field(
        default_factory=list,
        description="Unresolved gaps blocking predicate translation.",
    )


class CompiledCriterion(BaseModel):
    """Compiler wrapper around one matcher-facing criterion.

    `matcher_input` is intentionally the original extractor criterion in
    the no-op compiler. Later compiler stages should add supports,
    gaps, and predicate plans without losing the original matcher input.
    """

    compiled_id: str = Field(description="Stable compiler id for this criterion.")
    source_criterion_id: str = Field(description="Stable id of the extractor criterion.")
    source_index: int = Field(description="Zero-based index in the extracted criteria list.")
    criterion_kind: CriterionKind = Field(description="Original extractor criterion kind.")
    source_text: str = Field(description="Original criterion source text.")
    resolver_policy: ResolverExecutionPolicy = Field(description="Resolver policy for this pass.")
    matcher_input: ExtractedCriterion = Field(
        description="Matcher-facing criterion preserved from the extractor."
    )
    resolved_supports: list[ResolutionSupport] = Field(
        default_factory=list,
        description="Supports resolved for this criterion.",
    )
    unresolved_gaps: list[ResolutionGap] = Field(
        default_factory=list,
        description="Unresolved gaps for this criterion.",
    )
    checkable_predicates: list[CheckablePredicate] = Field(
        default_factory=list,
        description="Typed predicates produced for this criterion.",
    )
    expansion: ExpansionPlan = Field(description="Concept expansion plan.")
    compound_logic: CompoundLogicPlan = Field(description="Compound logic plan.")
    unit_normalization: UnitNormalizationPlan = Field(description="Unit normalization plan.")
    predicate: CheckablePredicatePlan = Field(description="Checkable predicate plan.")
    diagnostics: list[CompilerDiagnostic] = Field(
        default_factory=list,
        description="Criterion-local diagnostics.",
    )


class CriterionCompilationResult(BaseModel):
    """Top-level result from compiling extracted criteria."""

    compiler_version: str = Field(description="Compiler schema/pipeline version.")
    resolver_policy: ResolverExecutionPolicy = Field(description="Resolver policy for the pass.")
    source_criteria_count: int = Field(description="Number of input criteria.")
    criteria: list[CompiledCriterion] = Field(description="Compiled criteria in input order.")
    matcher_inputs: list[ExtractedCriterion] = Field(
        description="No-op matcher inputs in the same order the matcher should consume."
    )
    resolved_supports: list[ResolutionSupport] = Field(
        default_factory=list,
        description="All supports produced by this compilation.",
    )
    unresolved_gaps: list[ResolutionGap] = Field(
        default_factory=list,
        description="All gaps produced by this compilation.",
    )
    checkable_predicates: list[CheckablePredicate] = Field(
        default_factory=list,
        description="All typed predicates produced by this compilation.",
    )
    diagnostics: list[CompilerDiagnostic] = Field(
        default_factory=list,
        description="Compilation-level diagnostics.",
    )
