"""Pydantic schema for the extracted-criteria structured output.

This is the contract between the LLM extractor and the deterministic
matcher. Two design rules drove the shape:

1. **Matcher-ready, not Chia-shaped.** Chia's annotation graph
   (entities + binary relations + n-ary equivalence groups + scopes)
   is a research-grade representation for *humans*. Our matcher, by
   contrast, just needs to know "what kind of criterion is this and
   which slots does it bind?" So each criterion gets a discriminated
   `kind` and a small typed payload, rather than a soup of entities
   and relations the matcher would have to re-resolve. The Chia entity
   vocabulary is preserved as a flat `mentions` list per criterion for
   citation and audit, but it is *not* the load-bearing structure.

2. **OpenAI strict-mode-friendly.** OpenAI's structured-outputs strict
   mode rejects schemas that use `additionalProperties`, default
   values, optional fields without explicit `null`, or open-ended
   `dict` types. Every field in the schemas below is therefore
   required; nullability is expressed via `T | None`; enums use
   `Literal`; lists are typed concretely. Discriminated unions are
   not natively supported under strict mode either, so each criterion
   row carries a `kind` discriminator string plus parallel optional
   payloads (only one of which is non-null per row). The matcher
   dispatches on `kind` at runtime.

References
----------
- Chia annotation manual: cardinality of entity types and the role of
  Negation, Mood, Qualifier, Temporal, Value, Measurement.
- OpenAI Structured Outputs guide on supported JSON-Schema features.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# ---------- enums ----------

EntityType = Literal[
    "Condition",
    "Drug",
    "Measurement",
    "Procedure",
    "Observation",
    "Device",
    "Visit",
    "Person",
    "Value",
    "Temporal",
    "Qualifier",
    "Multiplier",
    "Reference_point",
    "Negation",
    "Mood",
    "Scope",
]
"""Subset of the Chia annotation vocabulary used as audit labels for
entity mentions inside a criterion. Mirrors `data.chia.DOCUMENTED_ENTITY_TYPES`
minus the out-of-scope "Non-query-able"-style markers, which the
extractor handles by emitting a `free_text` criterion instead."""

Polarity = Literal["inclusion", "exclusion"]
"""Whether a satisfied criterion *qualifies* (inclusion) or
*disqualifies* (exclusion) the patient. The text usually makes this
explicit ("Inclusion Criteria:" / "Exclusion Criteria:" headers)."""

ThresholdOperator = Literal["<", "<=", "=", ">=", ">", "in_range", "out_of_range"]
"""Comparison operators the matcher understands. `in_range` /
`out_of_range` use the inclusive `value_low`/`value_high` pair instead
of `value`."""

CriterionKind = Literal[
    "age",
    "sex",
    "condition_present",
    "condition_absent",
    "medication_present",
    "medication_absent",
    "measurement_threshold",
    "temporal_window",
    "free_text",
]
"""Discriminator for the criterion payload. `free_text` is the
catch-all for criteria that don't cleanly fit the structured kinds —
they survive into the matcher as human-review-pending."""

CRITERION_KINDS: tuple[str, ...] = (
    "age",
    "sex",
    "condition_present",
    "condition_absent",
    "medication_present",
    "medication_absent",
    "measurement_threshold",
    "temporal_window",
    "free_text",
)
"""Tuple form of the `CriterionKind` literal, exposed for use in
asserts and parameterized tests."""


# ---------- entity mentions (audit-only) ----------


class EntityMention(BaseModel):
    """A single entity span inside a criterion's source text.

    The matcher does *not* read this field; it exists for two
    purposes: (a) provenance — every criterion can be cited back to
    the snippet of trial text that produced it; (b) downstream
    analysis — measuring how often the LLM picks up Negation/Mood etc.
    is itself an eval signal.
    """

    text: str = Field(description="Surface string from the trial eligibility text.")
    type: EntityType = Field(
        description="Chia-style entity label. Use 'Condition' for diseases, 'Drug' for "
        "medications, 'Measurement' for labs/vitals, 'Value' for numeric values with "
        "their unit, 'Temporal' for time windows, 'Negation' for negated scope words, "
        "'Mood' for hypothetical/historical mood markers, 'Qualifier' for severity/"
        "laterality, 'Reference_point' for anchors like 'Screening' or 'Day 1'."
    )


# ---------- typed criterion payloads ----------


class AgeCriterion(BaseModel):
    """Age boundary in years.

    CT.gov already publishes `minimumAge` / `maximumAge` as structured
    fields, so the extractor often duplicates that information when the
    eligibility text restates it. That is fine — the matcher will
    cross-check; agreement raises confidence, disagreement is itself a
    flag for human review.
    """

    minimum_years: float | None = Field(
        description="Inclusive lower bound in years. Null if the criterion only sets an "
        "upper bound."
    )
    maximum_years: float | None = Field(
        description="Inclusive upper bound in years. Null if the criterion only sets a lower bound."
    )


class SexCriterion(BaseModel):
    """Required biological sex."""

    sex: Literal["MALE", "FEMALE", "ALL"] = Field(
        description="Sex required by the criterion. 'ALL' means the criterion permits "
        "any sex (rare as an explicit criterion, but emitted when the text says e.g. "
        "'Males or females eligible')."
    )


class ConditionCriterion(BaseModel):
    """A clinical condition that the patient must (or must not) have."""

    condition_text: str = Field(
        description="Surface form of the condition as written in the trial, normalized "
        "lightly (lowercased, no leading article). Coded mapping to SNOMED/ICD is the "
        "matcher's job, not the extractor's."
    )


class MedicationCriterion(BaseModel):
    """A medication or therapeutic class the patient must (or must not) be on."""

    medication_text: str = Field(
        description="Surface form of the medication or class. Either a brand or "
        "generic name, or a class (e.g. 'SGLT2 inhibitors', 'beta-blockers'). The "
        "matcher does the RxNorm mapping."
    )


class MeasurementCriterion(BaseModel):
    """A numeric threshold over a lab or vital sign.

    The four payload slots together encode any of: `<value`, `>= value`,
    `between value_low and value_high`, etc. Use `in_range` /
    `out_of_range` only when the criterion specifies *both* a low and a
    high bound (e.g. 'HbA1c 7.0-10.5%'); otherwise use a one-sided
    comparator and leave the unused range slot null.
    """

    measurement_text: str = Field(
        description="Lab or vital name as written in the trial (e.g. 'HbA1c', 'eGFR', "
        "'systolic blood pressure'). Matcher does the LOINC mapping."
    )
    operator: ThresholdOperator = Field(
        description="Comparison operator. Use 'in_range' when both `value_low` and "
        "`value_high` are set; '<', '<=', '=', '>=', '>' for one-sided thresholds with "
        "`value`; 'out_of_range' for criteria like 'patients whose HbA1c is NOT between "
        "7 and 10%'."
    )
    value: float | None = Field(
        description="Single threshold for one-sided comparators. Null when operator is "
        "'in_range' or 'out_of_range'."
    )
    value_low: float | None = Field(
        description="Inclusive lower bound when operator is 'in_range' or "
        "'out_of_range'. Null otherwise."
    )
    value_high: float | None = Field(
        description="Inclusive upper bound when operator is 'in_range' or "
        "'out_of_range'. Null otherwise."
    )
    unit: str | None = Field(
        description="Unit string verbatim from the trial (e.g. 'mg/dL', '%', "
        "'mL/min/1.73 m^2'). Null only if the criterion expresses a count or ratio "
        "with no unit."
    )


class TemporalWindowCriterion(BaseModel):
    """An event-must-have-occurred-within-window criterion.

    Examples: 'AP event within the last 60 months', 'no prior MI in the
    last 24 weeks'. The matcher resolves this against the patient's
    history relative to `as_of` date.
    """

    event_text: str = Field(
        description="What event or condition is being windowed (e.g. 'acute "
        "pancreatitis event', 'myocardial infarction', 'arterial revascularization')."
    )
    window_days: int = Field(
        description="Window length normalized to days. Round month windows at 30 days "
        "and year windows at 365; the matcher tolerates the approximation. Always "
        "non-negative."
    )
    direction: Literal["within_past", "within_future"] = Field(
        description="Whether the window looks backward from `as_of` ('within_past', "
        "the common case for history-of and recency criteria) or forward "
        "('within_future', rare — used for planned events like 'planned surgery within "
        "the next 6 months')."
    )


class FreeTextCriterion(BaseModel):
    """A criterion the extractor could not (or should not) reduce to a
    structured kind.

    Examples: 'Willing to follow diet counseling', 'Investigator
    judgment', complex compound criteria with multiple unresolved
    references. These flow through to the matcher as human-review-
    pending verdicts. Carrying them in the same envelope (rather than
    silently dropping) means the matcher can still produce a complete
    audit trail and the eval can count what fraction of criteria the
    extractor punts on.
    """

    note: str = Field(
        description="Optional clarifying note for the reviewer about why this is "
        "free-text — e.g. 'compound criterion (lab AND clinical judgment)' or "
        "'unresolved abbreviation'. Empty string is fine."
    )


# ---------- top-level extracted criterion ----------


class ExtractedCriterion(BaseModel):
    """One criterion lifted from the trial's eligibility text.

    Exactly one of the typed payload slots (`age`, `sex`, `condition`,
    `medication`, `measurement`, `temporal_window`, `free_text`) is
    non-null per row, selected by `kind`. The matcher dispatches on
    `kind`; the prompt enforces the one-non-null invariant.
    """

    kind: CriterionKind = Field(description="Discriminator: which payload slot is populated.")
    polarity: Polarity = Field(
        description="'inclusion' if the patient must satisfy the criterion to enroll; "
        "'exclusion' if satisfying it disqualifies them. Set from the section header "
        "in the source text."
    )
    source_text: str = Field(
        description="Verbatim excerpt of the trial eligibility text from which this "
        "criterion was derived. Used for citation in the reviewer UI; should be a "
        "complete sentence or bullet, not a single word."
    )
    negated: bool = Field(
        description="True iff the criterion has a Chia-style Negation in scope ('no "
        "history of', 'absence of'). Note this is independent of `polarity`: an "
        "exclusion criterion 'no history of MI' has polarity='exclusion' AND "
        "negated=True, meaning 'patients with no history of MI are *excluded*' would "
        "be unusual; usually you'll see polarity='inclusion' with negated=True."
    )
    mood: Literal["actual", "hypothetical", "historical"] = Field(
        description="Chia-style Mood. 'actual' = current/at-screening; 'hypothetical' "
        "= planned, expected, or speculative ('planned bariatric surgery'); "
        "'historical' = prior/ever ('history of MI'). Distinguishing these matters "
        "for the matcher's temporal logic."
    )
    age: AgeCriterion | None = Field(description="Populated when kind == 'age'.")
    sex: SexCriterion | None = Field(description="Populated when kind == 'sex'.")
    condition: ConditionCriterion | None = Field(
        description="Populated when kind in {'condition_present', 'condition_absent'}."
    )
    medication: MedicationCriterion | None = Field(
        description="Populated when kind in {'medication_present', 'medication_absent'}."
    )
    measurement: MeasurementCriterion | None = Field(
        description="Populated when kind == 'measurement_threshold'."
    )
    temporal_window: TemporalWindowCriterion | None = Field(
        description="Populated when kind == 'temporal_window'."
    )
    free_text: FreeTextCriterion | None = Field(description="Populated when kind == 'free_text'.")
    mentions: list[EntityMention] = Field(
        description="Chia-vocabulary entity spans inside `source_text`. Audit-only — "
        "matchers do not consume this. Empty list is allowed for trivially structured "
        "criteria where every span has already been promoted into the typed payload."
    )


# ---------- top-level extraction envelope ----------


class ExtractionMetadata(BaseModel):
    """Notes the model emits about the extraction itself.

    Distinct from `ExtractorRunMeta` (which is observed externally —
    cost, latency, etc.); this one is *self-reported* by the model and
    is therefore informational, not load-bearing for the matcher.
    """

    notes: str = Field(
        description="Free-text notes about ambiguities, judgement calls, or sections "
        "that were intentionally skipped. Empty string if nothing to flag."
    )


class ExtractedCriteria(BaseModel):
    """Top-level structured output of the criterion extractor."""

    criteria: list[ExtractedCriterion] = Field(
        description="One entry per atomic criterion. Split compound bullets into "
        "separate entries when each part is independently checkable; keep them "
        "together as a single `free_text` criterion when the conjunction is "
        "load-bearing and can't be evaluated piecewise."
    )
    metadata: ExtractionMetadata = Field(
        description="Self-reported metadata about the extraction run."
    )


class ExtractorRunMeta(BaseModel):
    """Observed metadata about an extractor invocation.

    Captured *outside* the LLM call (model name, prompt version, token
    counts, cost). Persisted alongside the extraction so that a
    regenerated batch can be diffed against the prior run and the eval
    can attribute regressions to a specific prompt or model.
    """

    model: str
    prompt_version: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    cost_usd: float | None = None
    latency_ms: float | None = None
