"""Deterministic per-criterion matcher (v0).

Walks each `ExtractedCriterion` (from the LLM extractor), dispatches
on `kind`, queries the `PatientProfile` (built atop the patient's
typed FHIR record), and emits a `MatchVerdict` per criterion.

What "deterministic" means here
-------------------------------
- No LLM calls. No RAG. No embeddings. No fuzzy matching.
- Every concept the matcher recognizes is in `concept_lookup.py`;
  anything else returns `indeterminate (unmapped_concept)`.
- Every numeric comparison goes through `PatientProfile.meets_threshold`,
  which fails closed on unit mismatches and stale data.
- Polarity is applied at the end: the per-kind matcher computes the
  *raw* answer to "does the patient satisfy the predicate", then
  `_apply_polarity` flips for exclusion criteria and `negated=True`.

What v0 does NOT cover (deliberate)
-----------------------------------
- Compound criteria (AND/OR within one criterion) — the extractor
  splits these where it can; what remains lands as `free_text`.
- Hypothetical mood (planned events) — no patient-side data exists
  on planned events, so we return `indeterminate (unsupported_mood)`.
- Medications — the v0 concept lookup table is empty for meds (see
  D-34 in PLAN.md); they all return `unmapped_concept`.
- Soft thresholds, severity qualifiers, lateralities — out of scope.

The eval harness will measure where the matcher's "ok" verdicts
agree with hand labels and where its "indeterminate" verdicts cluster.
That is exactly the surface we want for v0: a baseline that's
*honest* about what it doesn't handle yet.
"""

from __future__ import annotations

from typing import Any

from ..domain.patient import LabObservation
from ..domain.trial import Trial
from ..evals.seed import parse_age_years
from ..extractor.schema import (
    AgeCriterion,
    ConditionCriterion,
    CriterionKind,
    ExtractedCriterion,
    MeasurementCriterion,
    MedicationCriterion,
    Polarity,
    SexCriterion,
    TemporalWindowCriterion,
)
from ..profile import PatientProfile, ThresholdResult
from ..profile.profile import ConceptSet, ThresholdOp
from .concept_lookup import lookup_condition, lookup_lab, lookup_medication
from .verdict import (
    ConditionEvidence,
    DemographicsEvidence,
    Evidence,
    LabEvidence,
    MatchVerdict,
    MedicationEvidence,
    MissingEvidence,
    TrialFieldEvidence,
    Verdict,
    VerdictReason,
)

MATCHER_VERSION = "matcher-v0.1"
"""Bumped on any change that could shift verdicts on the eval set.
Recorded on every `MatchVerdict` for run attribution."""


# ---------- top-level entry ----------


def match_criterion(
    criterion: ExtractedCriterion,
    profile: PatientProfile,
    trial: Trial,
) -> MatchVerdict:
    """Return a `MatchVerdict` for one extracted criterion.

    The dispatch table is exhaustive over `CriterionKind`. Each
    per-kind handler returns `(raw_verdict, reason, rationale,
    evidence)` — the *raw* answer to the criterion's claim, unflipped
    by polarity. `match_criterion` then applies polarity / negation
    to land on the final eligibility verdict.

    Soft-fails on extractor invariant violations (kind discriminator
    says one thing, payload slot is None) by emitting an
    `indeterminate(extractor_invariant_violation)` verdict instead of
    bubbling up — see `_ExtractorInvariantViolation` and D-66. This
    keeps a single bad criterion from taking down a 30-criterion
    trial's score; the bad criterion stays visible in the verdict
    list so reviewers can see exactly which one the extractor fumbled.
    """
    if criterion.mood == "hypothetical":
        return _build(
            criterion,
            verdict="indeterminate",
            reason="unsupported_mood",
            rationale=(
                "Criterion is hypothetical (planned/expected); v0 has no "
                "patient-side data on planned events."
            ),
            evidence=[],
        )

    try:
        raw, reason, rationale, evidence = _dispatch(criterion, profile, trial)
    except _ExtractorInvariantViolation as exc:
        return _build(
            criterion,
            verdict="indeterminate",
            reason="extractor_invariant_violation",
            rationale=str(exc),
            evidence=[
                MissingEvidence(
                    looked_for=f"non-null {exc.slot_name!r} payload (kind={criterion.kind!r})",
                    note="extractor returned a discriminator/payload mismatch",
                )
            ],
        )
    final = _apply_polarity(raw, criterion.polarity, criterion.negated)
    return _build(
        criterion,
        verdict=final,
        reason=reason,
        rationale=rationale,
        evidence=evidence,
    )


def match_extracted(
    criteria: list[ExtractedCriterion],
    profile: PatientProfile,
    trial: Trial,
) -> list[MatchVerdict]:
    """Convenience: run `match_criterion` over a whole extraction."""
    return [match_criterion(c, profile, trial) for c in criteria]


# ---------- dispatch ----------


def _dispatch(
    criterion: ExtractedCriterion,
    profile: PatientProfile,
    trial: Trial,
) -> tuple[Verdict, VerdictReason, str, list[Evidence]]:
    """Route to the per-kind handler. Each returns (verdict, reason,
    rationale, evidence) with the *raw* polarity-unflipped verdict."""
    kind: CriterionKind = criterion.kind

    if kind == "age":
        return _match_age(_required(criterion.age, "age", kind), profile, trial)
    if kind == "sex":
        return _match_sex(_required(criterion.sex, "sex", kind), profile, trial)
    if kind in ("condition_present", "condition_absent"):
        return _match_condition(_required(criterion.condition, "condition", kind), profile)
    if kind in ("medication_present", "medication_absent"):
        return _match_medication(_required(criterion.medication, "medication", kind), profile)
    if kind == "measurement_threshold":
        return _match_measurement(_required(criterion.measurement, "measurement", kind), profile)
    if kind == "temporal_window":
        return _match_temporal_window(
            _required(criterion.temporal_window, "temporal_window", kind), profile
        )
    if kind == "free_text":
        return (
            "indeterminate",
            "human_review_required",
            "Free-text criterion; deferred to human review.",
            [],
        )
    # Defensive: every CriterionKind member must be handled above.
    return (
        "indeterminate",
        "unsupported_kind",
        f"Matcher v0 does not handle kind={kind!r}.",
        [],
    )


class _ExtractorInvariantViolation(Exception):
    """Raised when an `ExtractedCriterion`'s `kind` discriminator says
    one payload slot should be populated, but that slot is None.

    OpenAI structured outputs enforce field-level shape but cannot
    enforce cross-field invariants like "if `kind` == 'measurement_threshold'
    then `measurement` is non-null." So a model that ignores the
    instruction can produce schema-valid but semantically broken JSON.
    We raise this from `_required(...)` and `match_criterion` catches
    it to emit a soft-fail `indeterminate(extractor_invariant_violation)`
    verdict (D-66) — keeping one bad criterion from killing the trial's
    whole score while staying visible in the verdict list so a reviewer
    sees which specific row the extractor fumbled.

    Carries `slot_name` so the catch site can build a precise
    MissingEvidence row without re-parsing the message string.
    """

    def __init__(self, slot_name: str, kind: str) -> None:
        super().__init__(
            f"ExtractedCriterion claimed kind={kind!r} which requires "
            f"a non-null `{slot_name}` payload, but `{slot_name}` was None."
        )
        self.slot_name = slot_name
        self.kind = kind


def _required(payload: Any, slot_name: str, kind: str) -> Any:
    """Defensive payload accessor.

    The extractor schema's `kind` discriminator should guarantee the
    matching payload slot is non-null in well-formed output, but the
    model can ignore that contract. Hand-built test fixtures can also
    miss it. Raising the typed `_ExtractorInvariantViolation` lets
    `match_criterion` convert this into a per-criterion soft fail
    rather than a 500."""
    if payload is None:
        raise _ExtractorInvariantViolation(slot_name=slot_name, kind=kind)
    return payload


# ---------- per-kind matchers ----------


def _match_age(
    payload: AgeCriterion,
    profile: PatientProfile,
    trial: Trial,
) -> tuple[Verdict, VerdictReason, str, list[Evidence]]:
    """Age criteria are simple range checks against the profile.

    Trial's CT.gov-structured age fields are cited as auxiliary
    evidence when they're present, so the reviewer sees both signals.
    """
    age = profile.age_years
    evidence: list[Evidence] = [
        DemographicsEvidence(field="age_years", value=str(age), note=f"age {age}")
    ]
    trial_min = parse_age_years(trial.minimum_age)
    trial_max = parse_age_years(trial.maximum_age)
    if trial_min is not None:
        evidence.append(
            TrialFieldEvidence(
                field="minimum_age",
                value=trial.minimum_age or "",
                note=f"trial minimum_age={trial.minimum_age!r}",
            )
        )
    if trial_max is not None:
        evidence.append(
            TrialFieldEvidence(
                field="maximum_age",
                value=trial.maximum_age or "",
                note=f"trial maximum_age={trial.maximum_age!r}",
            )
        )

    min_ok = payload.minimum_years is None or age >= payload.minimum_years
    max_ok = payload.maximum_years is None or age <= payload.maximum_years

    if min_ok and max_ok:
        return ("pass", "ok", _age_rationale(age, payload, satisfied=True), evidence)
    return ("fail", "ok", _age_rationale(age, payload, satisfied=False), evidence)


def _age_rationale(age: int, payload: AgeCriterion, *, satisfied: bool) -> str:
    bounds: list[str] = []
    if payload.minimum_years is not None:
        bounds.append(f">= {payload.minimum_years:g}")
    if payload.maximum_years is not None:
        bounds.append(f"<= {payload.maximum_years:g}")
    bound_str = " and ".join(bounds) or "no bound"
    verb = "is in" if satisfied else "is not in"
    return f"Patient age {age} {verb} the criterion's range ({bound_str})."


def _match_sex(
    payload: SexCriterion,
    profile: PatientProfile,
    trial: Trial,
) -> tuple[Verdict, VerdictReason, str, list[Evidence]]:
    """Patient sex is `male|female|other|unknown`; criterion sex is
    `MALE|FEMALE|ALL`. `ALL` always passes; `other`/`unknown` patient
    sex returns indeterminate against a specific MALE/FEMALE
    requirement (we won't guess)."""
    p_sex = profile.sex.lower()
    evidence: list[Evidence] = [
        DemographicsEvidence(field="sex", value=profile.sex, note=f"patient sex={profile.sex}"),
        TrialFieldEvidence(field="sex", value=trial.sex, note=f"trial sex={trial.sex}"),
    ]

    if payload.sex == "ALL":
        return ("pass", "ok", "Criterion accepts all sexes.", evidence)
    if p_sex in ("other", "unknown"):
        return (
            "indeterminate",
            "no_data",
            f"Patient sex is {profile.sex!r}; criterion requires {payload.sex}.",
            evidence,
        )
    expected = payload.sex.lower()
    if p_sex == expected:
        return (
            "pass",
            "ok",
            f"Patient sex {profile.sex!r} matches criterion {payload.sex}.",
            evidence,
        )
    return (
        "fail",
        "ok",
        f"Patient sex {profile.sex!r} does not match criterion {payload.sex}.",
        evidence,
    )


def _match_condition(
    payload: ConditionCriterion,
    profile: PatientProfile,
) -> tuple[Verdict, VerdictReason, str, list[Evidence]]:
    """Condition presence/absence — driven by the surface-form lookup.

    Caller's responsible for polarity flip; this returns the raw
    "is the patient currently coded for this condition" answer."""
    concept_set = lookup_condition(payload.condition_text)
    if concept_set is None:
        return (
            "indeterminate",
            "unmapped_concept",
            f"No ConceptSet mapping for condition {payload.condition_text!r}.",
            [
                MissingEvidence(
                    looked_for=f"ConceptSet mapping for {payload.condition_text!r}",
                    note="condition not in matcher v0 vocabulary",
                )
            ],
        )

    matches = profile.matching_active_conditions(concept_set)
    if matches:
        evidence: list[Evidence] = [
            ConditionEvidence(
                concept=c.concept,
                onset_date=c.onset_date,
                abatement_date=c.abatement_date,
                note=(
                    f"{c.concept.display or c.concept.code} (active "
                    f"as of {profile.as_of.isoformat()})"
                ),
            )
            for c in matches
        ]
        return (
            "pass",
            "ok",
            f"Patient has active condition matching {concept_set.name!r}.",
            evidence,
        )
    return (
        "fail",
        "ok",
        f"Patient has no active condition matching {concept_set.name!r}.",
        [
            MissingEvidence(
                looked_for=f"active condition in {concept_set.name!r} "
                f"({len(concept_set.codes)} codes)",
                note="no matching active conditions on profile",
            )
        ],
    )


def _match_medication(
    payload: MedicationCriterion,
    profile: PatientProfile,
) -> tuple[Verdict, VerdictReason, str, list[Evidence]]:
    """Medication presence/absence. v0's concept-lookup table is
    empty for meds; everything returns `unmapped_concept` until we do
    the RxNorm work."""
    concept_set = lookup_medication(payload.medication_text)
    if concept_set is None:
        return (
            "indeterminate",
            "unmapped_concept",
            f"No ConceptSet mapping for medication {payload.medication_text!r}; "
            f"matcher v0 does not recognize medications.",
            [
                MissingEvidence(
                    looked_for=f"ConceptSet mapping for {payload.medication_text!r}",
                    note="medication not in matcher v0 vocabulary",
                )
            ],
        )
    matches = [m for m in profile.active_medications if m.concept.code in concept_set.codes]
    if matches:
        evidence: list[Evidence] = [
            MedicationEvidence(
                concept=m.concept,
                start_date=m.start_date,
                end_date=m.end_date,
                note=(
                    f"{m.concept.display or m.concept.code} (active "
                    f"as of {profile.as_of.isoformat()})"
                ),
            )
            for m in matches
        ]
        return (
            "pass",
            "ok",
            f"Patient has active medication matching {concept_set.name!r}.",
            evidence,
        )
    return (
        "fail",
        "ok",
        f"Patient has no active medication matching {concept_set.name!r}.",
        [
            MissingEvidence(
                looked_for=f"active medication in {concept_set.name!r}",
                note="no matching active medications on profile",
            )
        ],
    )


def _match_measurement(
    payload: MeasurementCriterion,
    profile: PatientProfile,
) -> tuple[Verdict, VerdictReason, str, list[Evidence]]:
    """Numeric threshold. Delegates to `PatientProfile.meets_threshold`,
    which encapsulates the freshness and unit-mismatch decisions."""
    concept_set = lookup_lab(payload.measurement_text)
    if concept_set is None:
        return (
            "indeterminate",
            "unmapped_concept",
            f"No ConceptSet mapping for measurement {payload.measurement_text!r}.",
            [
                MissingEvidence(
                    looked_for=f"ConceptSet mapping for {payload.measurement_text!r}",
                    note="measurement not in matcher v0 vocabulary",
                )
            ],
        )
    if payload.unit is None:
        return (
            "indeterminate",
            "ambiguous_criterion",
            f"Threshold for {payload.measurement_text!r} has no unit; cannot "
            f"safely compare against patient lab values.",
            [],
        )

    op = payload.operator
    if op in ("in_range", "out_of_range"):
        return _match_range(payload, concept_set, op, profile)
    if op not in ("<", "<=", "=", ">=", ">"):
        return (
            "indeterminate",
            "unsupported_kind",
            f"Operator {op!r} is not supported by matcher v0.",
            [],
        )
    if payload.value is None:
        return (
            "indeterminate",
            "ambiguous_criterion",
            f"One-sided operator {op!r} requires `value`, got None.",
            [],
        )

    obs = _latest_lab_for_concept_set(profile, concept_set)
    if obs is None:
        return _no_lab_data(payload, concept_set)
    loinc_code = obs.concept.code

    profile_op = _to_profile_op(op)
    result = profile.meets_threshold(loinc_code, profile_op, payload.value, payload.unit)
    return _threshold_to_verdict(result, payload, loinc_code, profile)


def _to_profile_op(op: str) -> ThresholdOp:
    """Translate the extractor's clinical-style `=` to the profile's
    Pythonic `==`. Other operators pass through unchanged."""
    if op == "=":
        return "=="
    return op  # type: ignore[return-value]


def _match_range(
    payload: MeasurementCriterion,
    concept_set: ConceptSet,
    op: str,
    profile: PatientProfile,
) -> tuple[Verdict, VerdictReason, str, list[Evidence]]:
    """Range checks decompose into two one-sided threshold checks.

    A patient lab "in_range" must be >= low AND <= high; "out_of_range"
    is the negation of that. Both bounds must be set; freshness/unit
    failures from either probe surface as the indeterminate reason."""
    if payload.value_low is None or payload.value_high is None:
        return (
            "indeterminate",
            "ambiguous_criterion",
            f"Range operator {op!r} requires both value_low and value_high.",
            [],
        )
    if payload.unit is None:
        return (
            "indeterminate",
            "ambiguous_criterion",
            f"Range threshold for {payload.measurement_text!r} has no unit.",
            [],
        )

    obs = _latest_lab_for_concept_set(profile, concept_set)
    if obs is None:
        return _no_lab_data(payload, concept_set)
    loinc_code = obs.concept.code

    low_result = profile.meets_threshold(loinc_code, ">=", payload.value_low, payload.unit)
    high_result = profile.meets_threshold(loinc_code, "<=", payload.value_high, payload.unit)

    # Either probe being indeterminate is the verdict reason — both
    # see the same lab so any data/unit problem propagates.
    for result in (low_result, high_result):
        if result in (
            ThresholdResult.NO_DATA,
            ThresholdResult.STALE_DATA,
            ThresholdResult.UNIT_MISMATCH,
        ):
            return _threshold_to_verdict(result, payload, loinc_code, profile)

    in_range = low_result == ThresholdResult.MEETS and high_result == ThresholdResult.MEETS
    raw = (in_range and op == "in_range") or (not in_range and op == "out_of_range")

    obs = profile.latest_lab(loinc_code)
    evidence: list[Evidence] = []
    if obs is not None:
        evidence.append(
            LabEvidence(
                concept=obs.concept,
                value=obs.value,
                unit=obs.unit,
                effective_date=obs.effective_date,
                note=(f"{obs.value} {obs.unit} on {obs.effective_date.isoformat()}"),
            )
        )
    rationale = (
        f"{payload.measurement_text} {'is' if raw else 'is not'} "
        f"{op} [{payload.value_low}, {payload.value_high}] {payload.unit or ''}".rstrip()
    )
    return ("pass" if raw else "fail", "ok", rationale, evidence)


def _threshold_to_verdict(
    result: ThresholdResult,
    payload: MeasurementCriterion,
    loinc_code: str,
    profile: PatientProfile,
) -> tuple[Verdict, VerdictReason, str, list[Evidence]]:
    """Convert a single `ThresholdResult` into the matcher's verdict
    triple. Centralized so all threshold paths share the same
    indeterminate-reason taxonomy and evidence-building logic."""
    if result == ThresholdResult.MEETS:
        obs = profile.latest_lab(loinc_code)
        evidence: list[Evidence] = []
        if obs is not None:
            evidence.append(
                LabEvidence(
                    concept=obs.concept,
                    value=obs.value,
                    unit=obs.unit,
                    effective_date=obs.effective_date,
                    note=(f"{obs.value} {obs.unit} on {obs.effective_date.isoformat()}"),
                )
            )
        return (
            "pass",
            "ok",
            (
                f"{payload.measurement_text} satisfies "
                f"{payload.operator} {payload.value} {payload.unit or ''}".rstrip()
            ),
            evidence,
        )
    if result == ThresholdResult.DOES_NOT_MEET:
        obs = profile.latest_lab(loinc_code)
        evidence = []
        if obs is not None:
            evidence.append(
                LabEvidence(
                    concept=obs.concept,
                    value=obs.value,
                    unit=obs.unit,
                    effective_date=obs.effective_date,
                    note=(f"{obs.value} {obs.unit} on {obs.effective_date.isoformat()}"),
                )
            )
        return (
            "fail",
            "ok",
            (
                f"{payload.measurement_text} does not satisfy "
                f"{payload.operator} {payload.value} {payload.unit or ''}".rstrip()
            ),
            evidence,
        )
    if result == ThresholdResult.NO_DATA:
        return (
            "indeterminate",
            "no_data",
            f"No lab observation for {payload.measurement_text!r}.",
            [
                MissingEvidence(
                    looked_for=f"latest lab for LOINC {loinc_code} ({payload.measurement_text!r})",
                    note="patient has no observation for this lab",
                )
            ],
        )
    if result == ThresholdResult.STALE_DATA:
        return (
            "indeterminate",
            "stale_data",
            (
                f"Patient's {payload.measurement_text!r} lab exists but is "
                f"older than the freshness window."
            ),
            [],
        )
    return (
        "indeterminate",
        "unit_mismatch",
        (
            f"Cannot compare {payload.measurement_text!r}: lab and threshold "
            f"units do not canonicalize to the same quantity."
        ),
        [],
    )


def _latest_lab_for_concept_set(
    profile: PatientProfile,
    concept_set: ConceptSet,
) -> LabObservation | None:
    """Latest patient observation across every LOINC in a lab ConceptSet.

    The original v0 matcher assumed each lab concept mapped to exactly
    one LOINC. D-69's terminology bridge can return value sets with
    multiple equivalent LOINCs, so choosing `next(iter(codes))` makes
    verdicts depend on frozenset iteration order. Instead, ask the
    patient profile for each code and compare against the most recent
    observation actually present.
    """

    observations = [
        obs
        for code in sorted(concept_set.codes)
        for obs in [profile.latest_lab(code)]
        if obs is not None and obs.concept.system == concept_set.system
    ]
    if not observations:
        return None
    return max(observations, key=lambda obs: (obs.effective_date, obs.concept.code))


def _no_lab_data(
    payload: MeasurementCriterion,
    concept_set: ConceptSet,
) -> tuple[Verdict, VerdictReason, str, list[Evidence]]:
    codes = ", ".join(sorted(concept_set.codes))
    return (
        "indeterminate",
        "no_data",
        f"No lab observation for {payload.measurement_text!r}.",
        [
            MissingEvidence(
                looked_for=(
                    f"latest lab in {concept_set.name!r} ({concept_set.system}; codes={codes})"
                ),
                note="patient has no observation for this lab concept set",
            )
        ],
    )


def _match_temporal_window(
    payload: TemporalWindowCriterion,
    profile: PatientProfile,
) -> tuple[Verdict, VerdictReason, str, list[Evidence]]:
    """Temporal-window criteria check whether an event of the
    specified type occurred within `window_days` of `as_of`.

    v0 routes the event_text through the condition lookup (most
    "history of MI" / "AP within 60 months" style criteria are
    diagnoses). Procedures and visits aren't covered by the lookup
    and land as `unmapped_concept`."""
    if payload.direction == "within_future":
        return (
            "indeterminate",
            "unsupported_mood",
            "Future-window criteria require planned-event data; not in v0.",
            [],
        )

    concept_set = lookup_condition(payload.event_text)
    if concept_set is None:
        return (
            "indeterminate",
            "unmapped_concept",
            f"No ConceptSet mapping for temporal-event {payload.event_text!r}.",
            [
                MissingEvidence(
                    looked_for=f"ConceptSet mapping for {payload.event_text!r}",
                    note="temporal-window event not in matcher v0 vocabulary",
                )
            ],
        )

    cutoff = profile.as_of - _days_timedelta(payload.window_days)
    matches = [
        c
        for c in profile.patient.conditions
        if c.is_clinical
        and c.concept.code in concept_set.codes
        and c.concept.system == concept_set.system
        and c.onset_date is not None
        and cutoff <= c.onset_date <= profile.as_of
    ]
    if matches:
        evidence: list[Evidence] = [
            ConditionEvidence(
                concept=c.concept,
                onset_date=c.onset_date,
                abatement_date=c.abatement_date,
                note=(
                    f"{c.concept.display or c.concept.code} onset "
                    f"{c.onset_date.isoformat() if c.onset_date else 'unknown'} "
                    f"(within {payload.window_days} days of "
                    f"{profile.as_of.isoformat()})"
                ),
            )
            for c in matches
        ]
        return (
            "pass",
            "ok",
            (f"Patient has {concept_set.name!r} event within the last {payload.window_days} days."),
            evidence,
        )
    return (
        "fail",
        "ok",
        (f"Patient has no {concept_set.name!r} event within the last {payload.window_days} days."),
        [
            MissingEvidence(
                looked_for=(
                    f"{concept_set.name!r} event between "
                    f"{cutoff.isoformat()} and {profile.as_of.isoformat()}"
                ),
                note="no matching temporal events found",
            )
        ],
    )


def _days_timedelta(days: int) -> Any:
    """Lazy import to keep `datetime` out of the module signature."""
    from datetime import timedelta

    return timedelta(days=days)


# ---------- polarity / negation glue ----------


def _apply_polarity(raw: Verdict, polarity: Polarity, negated: bool) -> Verdict:
    """Convert the *raw* answer to the criterion's claim into the
    final eligibility verdict.

    Truth table (raw is the answer to "patient satisfies the
    criterion's predicate as written"):

      polarity   | negated | raw=pass    | raw=fail
      -----------|---------|-------------|-------------
      inclusion  | False   | pass        | fail
      inclusion  | True    | fail        | pass
      exclusion  | False   | fail        | pass
      exclusion  | True    | pass        | fail

    `indeterminate` raws stay `indeterminate` regardless — neither
    polarity nor negation can resolve a "we don't know" into a
    decision. Exactly two flips: one for negation, one for exclusion;
    XOR semantics.
    """
    if raw == "indeterminate":
        return raw
    flip = (polarity == "exclusion") ^ negated
    if not flip:
        return raw
    return "fail" if raw == "pass" else "pass"


def _build(
    criterion: ExtractedCriterion,
    *,
    verdict: Verdict,
    reason: VerdictReason,
    rationale: str,
    evidence: list[Evidence],
) -> MatchVerdict:
    """One-stop constructor so every code path stamps the matcher
    version consistently."""
    return MatchVerdict(
        criterion=criterion,
        verdict=verdict,
        reason=reason,
        rationale=rationale,
        evidence=evidence,
        matcher_version=MATCHER_VERSION,
    )


__all__ = [
    "MATCHER_VERSION",
    "match_criterion",
    "match_extracted",
]
