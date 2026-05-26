"""Patient-side FHIR evidence calibration set helpers.

Chia gives us trial-text extraction gold labels. This module is the
patient-side analogue for matcher adjudication: small reviewed rows that
say whether the patient source records support presence, absence, a
measurement comparison, or insufficient evidence for one trial criterion.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from clinical_demo.evals.layer_three import (
    JudgeTarget,
    LayerThreeJudgment,
    LayerThreeReport,
    LayerThreeSourceContext,
    LayerThreeSourceRecord,
    select_judge_targets,
)
from clinical_demo.evals.run import RunResult
from clinical_demo.extractor.composite import (
    CompositeCriterionSubcheck,
    build_composite_criterion_groups,
)
from clinical_demo.extractor.schema import ExtractedCriterion
from clinical_demo.matcher import DEFAULT_MATCHER_ASSUMPTION_MODE, MatcherAssumptionMode, Verdict
from clinical_demo.matcher.concept_lookup import lookup_condition, lookup_lab, lookup_medication
from clinical_demo.matcher.verdict import MatchVerdict
from clinical_demo.privacy import (
    AnonymizationContext,
    PrivacyPolicy,
    anonymization_context,
    anonymize_text,
    sanitize_for_public_export,
)
from clinical_demo.retrieval import (
    RetrievalSourceRow,
    RetrievedPatientEvidence,
    retrieve_structured_patient_evidence,
)

PatientEvidenceLabel = Literal[
    "supports_present",
    "supports_absent",
    "supports_measurement_comparison",
    "insufficient_evidence",
]
PatientEvidenceScope = Literal["cardiometabolic_core", "all"]
EvidenceRetrievalState = Literal[
    "structured_and_note_retrieved",
    "structured_retrieved",
    "note_retrieved",
    "no_patient_evidence_retrieved",
]
FreeTextReviewHint = Literal[
    "not_needed",
    "criterion_is_free_text",
    "note_evidence_retrieved",
    "unmapped_or_no_structured_evidence",
]
MappingState = Literal[
    "all_mapped",
    "some_unmapped",
    "all_unmapped",
    "no_mappable_slots",
]
CompositeOperator = Literal["any_of", "all_of"]

CARDIOMETABOLIC_SLICES = frozenset(
    {
        "ckd",
        "hyperlipidemia",
        "hypertension-academic",
        "hypertension-industry",
        "t2dm-academic",
        "t2dm-industry",
    }
)


class PatientEvidenceHumanLabel(BaseModel):
    """Human label for one patient-side evidence calibration row."""

    pair_id: str
    criterion_index: int
    label: PatientEvidenceLabel | None = None
    cited_source_row_ids: list[str] = Field(default_factory=list)
    expected_matcher_verdict: Verdict | None = None
    matcher_assumption_mode: MatcherAssumptionMode = DEFAULT_MATCHER_ASSUMPTION_MODE
    reviewer: str | None = None
    rationale: str = ""


PATIENT_EVIDENCE_LABELS_ARTIFACT_SAFETY = {
    "public_export": "synthetic",
    "contains_real_patient_data": False,
    "source_data": "Synthetic Synthea patients and public ClinicalTrials.gov trial metadata.",
}


class PatientEvidenceSourceRow(LayerThreeSourceRecord):
    """A source row with a stable local id for reviewer citation."""

    row_id: str


class PatientEvidenceConceptMapping(BaseModel):
    """Mapping status for one criterion surface the matcher can code."""

    slot: Literal["condition", "medication", "measurement", "temporal_event"]
    surface: str
    mapped: bool
    concept_set_name: str | None = None
    system: str | None = None
    codes: list[str] = Field(default_factory=list)


class PatientEvidenceCompositeLineItem(BaseModel):
    """Reviewer-facing subcheck inside a compound criterion."""

    item_id: str
    operator: CompositeOperator
    source_text: str


class PatientEvidenceCompositeSubcheck(BaseModel):
    """One stable subcheck inside a composite criterion group."""

    subcheck_id: str
    operator: CompositeOperator
    criterion_kind: str
    source_text: str
    criterion: dict
    retrieved_source_row_ids: list[str] = Field(default_factory=list)
    retrieved_source_row_counts: dict[str, int] = Field(default_factory=dict)
    retrieval_reasons: dict[str, list[str]] = Field(default_factory=dict)


class PatientEvidenceCompositeGroup(BaseModel):
    """Boolean group of subchecks under one parent criterion."""

    group_id: str
    operator: CompositeOperator
    parent_source_text: str
    subchecks: list[PatientEvidenceCompositeSubcheck] = Field(default_factory=list)


class PatientEvidenceCalibrationRow(BaseModel):
    """Reviewer-facing candidate row for patient-side evidence labeling."""

    pair_id: str
    patient_id: str
    nct_id: str
    eval_slice: str = ""
    criterion_index: int
    candidate_bucket: str
    criterion_kind: str
    criterion_source_text: str
    polarity: str
    negated: bool
    mood: str
    matcher_verdict: str
    matcher_reason: str
    matcher_rationale: str
    matcher_assumption_mode: MatcherAssumptionMode = DEFAULT_MATCHER_ASSUMPTION_MODE
    matcher_evidence: list[dict] = Field(default_factory=list)
    judge_label: str | None = None
    judge_error_categories: list[str] = Field(default_factory=list)
    judge_rationale: str | None = None
    source_rows: list[PatientEvidenceSourceRow] = Field(default_factory=list)
    source_row_counts: dict[str, int] = Field(default_factory=dict)
    retrieved_source_row_ids: list[str] = Field(default_factory=list)
    retrieved_source_row_counts: dict[str, int] = Field(default_factory=dict)
    retrieved_structured_source_row_ids: list[str] = Field(default_factory=list)
    retrieved_note_source_row_ids: list[str] = Field(default_factory=list)
    retrieval_reasons: dict[str, list[str]] = Field(default_factory=dict)
    concept_mappings: list[PatientEvidenceConceptMapping] = Field(default_factory=list)
    composite_line_items: list[PatientEvidenceCompositeLineItem] = Field(default_factory=list)
    composite_groups: list[PatientEvidenceCompositeGroup] = Field(default_factory=list)
    mapping_state: MappingState = "no_mappable_slots"
    unmapped_surfaces: list[str] = Field(default_factory=list)
    evidence_retrieval_state: EvidenceRetrievalState = "no_patient_evidence_retrieved"
    free_text_review_hint: FreeTextReviewHint = "not_needed"
    open_world_label_guidance: str = ""
    closed_world_label_guidance: str = ""
    existing_label: PatientEvidenceHumanLabel | None = None


class PatientEvidenceLabelCompleteness(BaseModel):
    """Completeness counts for the human patient-evidence label file."""

    total_labels: int
    filled_labels: int
    usable_labels: int
    missing_expected_verdict: list[str] = Field(default_factory=list)


class PatientEvidenceRunMetrics(BaseModel):
    """One run's agreement against the patient-evidence labels."""

    run_id: str
    notes: str = ""
    llm_use_level: str = ""
    matcher_assumption_mode: str = ""
    total_label_targets: int
    comparable_targets: int
    skipped_assumption_mismatch_targets: int = 0
    missing_result_targets: int = 0
    correct_verdicts: int = 0
    verdict_accuracy: float | None = None
    abstentions: int = 0
    abstention_rate: float | None = None
    citation_targets: int = 0
    citation_matches: int = 0
    citation_agreement: float | None = None
    retrieved_patient_row_counts: dict[str, int] = Field(default_factory=dict)
    cited_patient_row_counts: dict[str, int] = Field(default_factory=dict)
    eligibility_counts: dict[str, int] = Field(default_factory=dict)
    adjudicator_calls: int = 0
    adjudicator_cost_usd: float = 0.0
    adjudicator_input_tokens: int = 0
    adjudicator_output_tokens: int = 0


class PatientEvidenceModeDelta(BaseModel):
    """Case-level rollup movement from the baseline run to a comparison run."""

    baseline_run_id: str
    comparison_run_id: str
    changed_cases: int
    movements: dict[str, int] = Field(default_factory=dict)


class PatientEvidenceReport(BaseModel):
    """Calibrated patient-evidence report across one or more eval runs."""

    label_path: str
    label_completeness: PatientEvidenceLabelCompleteness
    runs: list[PatientEvidenceRunMetrics]
    mode_deltas: list[PatientEvidenceModeDelta] = Field(default_factory=list)


def load_patient_evidence_labels(path: Path | str) -> list[PatientEvidenceHumanLabel]:
    """Load patient-side evidence labels.

    The committed calibration file uses a public-export metadata envelope, while
    older temporary/test files may still be a bare list.
    """

    raw = json.loads(Path(path).read_text())
    if isinstance(raw, dict):
        raw = raw.get("labels", [])
    if not isinstance(raw, list):
        raise ValueError(
            f"patient-evidence label file must be a JSON list or labels envelope: {path}"
        )
    return [PatientEvidenceHumanLabel.model_validate(item) for item in raw]


def load_patient_evidence_labels_if_exists(
    path: Path | str,
) -> list[PatientEvidenceHumanLabel]:
    """Load labels when present; otherwise return an empty list."""

    label_path = Path(path)
    if not label_path.exists():
        return []
    return load_patient_evidence_labels(label_path)


def save_patient_evidence_labels(
    path: Path | str,
    labels: list[PatientEvidenceHumanLabel],
) -> None:
    """Persist patient-side evidence labels as reviewer-editable JSON."""

    label_path = Path(path)
    label_path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(labels, key=lambda label: (label.pair_id, label.criterion_index))
    payload = {
        "artifact_safety": PATIENT_EVIDENCE_LABELS_ARTIFACT_SAFETY,
        "labels": [label.model_dump(mode="json") for label in ordered],
    }
    label_path.write_text(json.dumps(payload, indent=2) + "\n")


def save_patient_evidence_rows(
    path: Path | str,
    rows: list[PatientEvidenceCalibrationRow],
) -> None:
    """Persist reviewer rows as stable, manually editable JSON."""

    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(rows, key=lambda row: (row.pair_id, row.criterion_index))
    with anonymization_context() as context:
        export_rows = [_public_export_row(row, context=context) for row in ordered]
    output_path.write_text(
        json.dumps([row.model_dump(mode="json") for row in export_rows], indent=2) + "\n"
    )


def load_patient_evidence_rows(path: Path | str) -> list[PatientEvidenceCalibrationRow]:
    """Load a persisted patient-side evidence candidate packet."""

    raw = json.loads(Path(path).read_text())
    return [PatientEvidenceCalibrationRow.model_validate(item) for item in raw]


def _public_export_row(
    row: PatientEvidenceCalibrationRow,
    *,
    context: AnonymizationContext,
) -> PatientEvidenceCalibrationRow:
    policy = PrivacyPolicy.public_export()
    return row.model_copy(
        update={
            "patient_id": context.placeholder_for("PATIENT_ID", row.patient_id),
            "matcher_rationale": anonymize_text(
                row.matcher_rationale,
                context=context,
                policy=policy,
            ).text,
            "matcher_evidence": sanitize_for_public_export(
                row.matcher_evidence,
                context=context,
            ),
            "judge_rationale": anonymize_text(
                row.judge_rationale,
                context=context,
                policy=policy,
            ).text
            if row.judge_rationale
            else None,
            "source_rows": [
                _public_export_source_row(source_row, context=context)
                for source_row in row.source_rows
            ],
            "existing_label": _public_export_label(row.existing_label, context=context),
        }
    )


def _public_export_source_row(
    row: PatientEvidenceSourceRow,
    *,
    context: AnonymizationContext,
) -> PatientEvidenceSourceRow:
    if row.source != "patient":
        return row
    policy = PrivacyPolicy.public_export()
    return row.model_copy(
        update={
            "label": anonymize_text(row.label, context=context, policy=policy).text,
            "value": anonymize_text(row.value, context=context, policy=policy).text,
            "date": anonymize_text(row.date, context=context, policy=policy).text
            if row.date
            else None,
            "status": anonymize_text(row.status, context=context, policy=policy).text
            if row.status
            else None,
        }
    )


def _public_export_label(
    label: PatientEvidenceHumanLabel | None,
    *,
    context: AnonymizationContext,
) -> PatientEvidenceHumanLabel | None:
    if label is None:
        return None
    policy = PrivacyPolicy.public_export()
    return label.model_copy(
        update={
            "reviewer": anonymize_text(label.reviewer, context=context, policy=policy).text
            if label.reviewer
            else None,
            "rationale": anonymize_text(label.rationale, context=context, policy=policy).text,
        }
    )


def merge_patient_evidence_labels(
    existing: list[PatientEvidenceHumanLabel],
    updates: list[PatientEvidenceHumanLabel],
) -> list[PatientEvidenceHumanLabel]:
    """Merge reviewer updates into existing labels by target key."""

    merged = {(label.pair_id, label.criterion_index): label for label in existing}
    for label in updates:
        merged[(label.pair_id, label.criterion_index)] = label
    return list(merged.values())


def patient_evidence_label_completeness(
    labels: list[PatientEvidenceHumanLabel],
) -> PatientEvidenceLabelCompleteness:
    """Summarize which labels are ready for calibrated reporting."""

    filled = []
    missing_expected = []
    for label in labels:
        has_content = (
            label.label is not None
            or label.expected_matcher_verdict is not None
            or bool(label.cited_source_row_ids)
            or bool(label.rationale)
        )
        if has_content:
            filled.append(label)
        if label.label is not None and label.expected_matcher_verdict is None:
            missing_expected.append(_label_key(label))
    usable = [label for label in labels if label.expected_matcher_verdict is not None]
    return PatientEvidenceLabelCompleteness(
        total_labels=len(labels),
        filled_labels=len(filled),
        usable_labels=len(usable),
        missing_expected_verdict=missing_expected,
    )


def build_patient_evidence_report(
    runs: list[RunResult],
    labels: list[PatientEvidenceHumanLabel],
    *,
    label_path: Path | str,
) -> PatientEvidenceReport:
    """Compare eval runs against filled patient-evidence labels.

    A label becomes comparable once `expected_matcher_verdict` is set.
    Citation agreement is computed only for decisive expected verdicts
    with at least one human-cited patient source row.
    """

    completeness = patient_evidence_label_completeness(labels)
    metrics = [_run_metrics(run, labels) for run in runs]
    return PatientEvidenceReport(
        label_path=str(label_path),
        label_completeness=completeness,
        runs=metrics,
        mode_deltas=_mode_deltas(runs),
    )


def render_patient_evidence_report(report: PatientEvidenceReport) -> str:
    """Render a concise Markdown report for CLI / baseline snapshots."""

    c = report.label_completeness
    lines = [
        "# Patient Evidence Calibration Report",
        "",
        "## Labels",
        "",
        f"- path: `{report.label_path}`",
        f"- filled: {c.filled_labels}/{c.total_labels}",
        f"- usable for verdict metrics: {c.usable_labels}/{c.total_labels}",
    ]
    if c.missing_expected_verdict:
        lines.append(
            f"- missing expected matcher verdict: {len(c.missing_expected_verdict)} label(s)"
        )
    lines.extend(["", "## Runs", ""])
    lines.append(
        "| Run | LLM use | Comparable | Accuracy | Abstention | Citation agreement | "
        "Mode skipped | Retrieved rows | Decisive citations | Eligibility | Adjudicator |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for item in report.runs:
        eligibility = " / ".join(
            f"{key}={value}" for key, value in sorted(item.eligibility_counts.items())
        )
        retrieved_counts = _counts_cell(item.retrieved_patient_row_counts)
        cited_counts = _counts_cell(item.cited_patient_row_counts)
        adjudicator = f"{item.adjudicator_calls} calls / ${item.adjudicator_cost_usd:.4f}"
        lines.append(
            f"| `{item.run_id}` | `{item.llm_use_level or 'unknown'}` | "
            f"{item.comparable_targets}/{item.total_label_targets} | "
            f"{_pct(item.verdict_accuracy)} | {_pct(item.abstention_rate)} | "
            f"{_citation_cell(item)} | {item.skipped_assumption_mismatch_targets} | "
            f"{retrieved_counts} | {cited_counts} | "
            f"{eligibility or '(none)'} | {adjudicator} |"
        )
    if report.mode_deltas:
        lines.extend(["", "## Case Rollup Movement", ""])
        lines.append("| Baseline | Comparison | Changed cases | Movements |")
        lines.append("|---|---|---:|---|")
        for delta in report.mode_deltas:
            movements = " / ".join(
                f"{key}={value}" for key, value in sorted(delta.movements.items())
            )
            lines.append(
                f"| `{delta.baseline_run_id}` | `{delta.comparison_run_id}` | "
                f"{delta.changed_cases} | {movements or '(none)'} |"
            )
    lines.append("")
    return "\n".join(lines)


def patient_evidence_source_rows(
    context: LayerThreeSourceContext,
) -> list[PatientEvidenceSourceRow]:
    """Convert a source context into stable row ids for patient-evidence review."""

    return _indexed_source_rows(context)


def load_layer_three_report(path: Path | str) -> LayerThreeReport:
    """Load a persisted Layer-3 judge report."""

    return LayerThreeReport.model_validate_json(Path(path).read_text())


def select_patient_evidence_targets(
    run: RunResult,
    *,
    judge_report: LayerThreeReport | None = None,
    limit: int = 60,
    scope: PatientEvidenceScope = "cardiometabolic_core",
    preserve_keys: set[tuple[str, int]] | None = None,
) -> list[JudgeTarget]:
    """Select a deterministic, evidence-focused calibration packet.

    In the core scope, oncology / NSCLC rows are filtered out unless
    they are deliberately hand-crafted later. Judge-incorrect rows are
    included first only when they are still patient-evidence targets;
    wrong age/sex rows belong in Layer 1/Layer 3 diagnostics, not this
    patient-side evidence packet. The remaining slots round-robin
    across patient-evidence buckets so the packet does not become 50
    copies of the same `unmapped_concept` failure.
    """

    if limit < 1:
        raise ValueError("limit must be positive")

    targets = select_judge_targets(run)
    judgments = _judgments_by_key(judge_report)
    preserved = preserve_keys or set()
    selected: list[JudgeTarget] = []
    selected_keys: set[tuple[str, int]] = set()

    for target in targets:
        if not _target_in_scope(target, scope):
            continue
        key = (target.pair_id, target.criterion_index)
        if key not in preserved:
            continue
        selected.append(target)
        selected_keys.add(key)
        if len(selected) >= limit:
            return selected

    for target in targets:
        if not _target_in_scope(target, scope):
            continue
        key = (target.pair_id, target.criterion_index)
        if key in selected_keys:
            continue
        judgment = judgments.get(key)
        bucket = patient_evidence_bucket(target, judgment)
        if judgment is None or judgment.judge_label != "incorrect" or bucket is None:
            continue
        selected.append(target)
        selected_keys.add(key)
        if len(selected) >= limit:
            return selected

    buckets: dict[str, list[JudgeTarget]] = {}
    for target in targets:
        if not _target_in_scope(target, scope):
            continue
        key = (target.pair_id, target.criterion_index)
        if key in selected_keys:
            continue
        judgment = judgments.get(key)
        if judgment is not None and judgment.judge_label == "correct":
            continue
        bucket = patient_evidence_bucket(target, judgment)
        if bucket is None:
            continue
        buckets.setdefault(bucket, []).append(target)

    bucket_names = sorted(buckets)
    while len(selected) < limit and bucket_names:
        next_bucket_names: list[str] = []
        for bucket in bucket_names:
            bucket_targets = buckets[bucket]
            if bucket_targets:
                target = bucket_targets.pop(0)
                selected.append(target)
                if len(selected) >= limit:
                    break
            if bucket_targets:
                next_bucket_names.append(bucket)
        bucket_names = next_bucket_names

    return selected


def build_patient_evidence_rows(
    targets: list[JudgeTarget],
    *,
    source_contexts: dict[str, LayerThreeSourceContext],
    judge_report: LayerThreeReport | None = None,
    existing_labels: list[PatientEvidenceHumanLabel] | None = None,
) -> list[PatientEvidenceCalibrationRow]:
    """Convert selected targets into reviewer-facing evidence rows."""

    judgments = _judgments_by_key(judge_report)
    labels = {(label.pair_id, label.criterion_index): label for label in existing_labels or []}
    rows: list[PatientEvidenceCalibrationRow] = []
    for target in targets:
        criterion = target.verdict.criterion
        key = (target.pair_id, target.criterion_index)
        judgment = judgments.get(key)
        source_context = source_contexts[target.pair_id]
        source_rows = _indexed_source_rows(source_context)
        retrieval_rows = [
            RetrievalSourceRow.model_validate(row.model_dump()) for row in source_rows
        ]
        retrieved = retrieve_structured_patient_evidence(
            criterion,
            retrieval_rows,
            limit=12,
        )
        concept_mappings = _criterion_concept_mappings(target.verdict)
        composite_groups = _composite_groups(
            criterion,
            criterion_index=target.criterion_index,
            source_rows=retrieval_rows,
        )
        retrieved_ids = [item.row.row_id for item in retrieved]
        retrieved_structured_ids = [
            item.row.row_id for item in retrieved if item.row.kind != "note"
        ]
        retrieved_note_ids = [item.row.row_id for item in retrieved if item.row.kind == "note"]
        rows.append(
            PatientEvidenceCalibrationRow(
                pair_id=target.pair_id,
                patient_id=target.patient_id,
                nct_id=target.nct_id,
                eval_slice=target.slice,
                criterion_index=target.criterion_index,
                candidate_bucket=patient_evidence_bucket(target, judgment) or "other",
                criterion_kind=criterion.kind,
                criterion_source_text=criterion.source_text,
                polarity=criterion.polarity,
                negated=criterion.negated,
                mood=criterion.mood,
                matcher_verdict=target.verdict.verdict,
                matcher_reason=target.verdict.reason,
                matcher_rationale=target.verdict.rationale,
                matcher_assumption_mode=DEFAULT_MATCHER_ASSUMPTION_MODE,
                matcher_evidence=[e.model_dump(mode="json") for e in target.verdict.evidence],
                judge_label=judgment.judge_label if judgment else None,
                judge_error_categories=list(judgment.error_categories) if judgment else [],
                judge_rationale=judgment.rationale if judgment else None,
                source_rows=source_rows,
                source_row_counts=_source_row_counts(source_rows),
                retrieved_source_row_ids=retrieved_ids,
                retrieved_source_row_counts=_retrieved_row_counts(retrieved),
                retrieved_structured_source_row_ids=retrieved_structured_ids,
                retrieved_note_source_row_ids=retrieved_note_ids,
                retrieval_reasons={item.row.row_id: item.reasons for item in retrieved},
                concept_mappings=concept_mappings,
                composite_line_items=_composite_line_items_from_groups(composite_groups),
                composite_groups=composite_groups,
                mapping_state=_mapping_state(concept_mappings),
                unmapped_surfaces=[
                    mapping.surface for mapping in concept_mappings if not mapping.mapped
                ],
                evidence_retrieval_state=_evidence_retrieval_state(
                    retrieved_structured_ids=retrieved_structured_ids,
                    retrieved_note_ids=retrieved_note_ids,
                ),
                free_text_review_hint=_free_text_review_hint(
                    target.verdict,
                    retrieved_structured_ids=retrieved_structured_ids,
                    retrieved_note_ids=retrieved_note_ids,
                    concept_mappings=concept_mappings,
                ),
                open_world_label_guidance=_open_world_label_guidance(
                    target.verdict,
                    retrieved_structured_ids=retrieved_structured_ids,
                    retrieved_note_ids=retrieved_note_ids,
                    concept_mappings=concept_mappings,
                ),
                closed_world_label_guidance=_closed_world_label_guidance(target.verdict),
                existing_label=labels.get(key),
            )
        )
    return rows


def patient_evidence_bucket(
    target: JudgeTarget,
    judgment: LayerThreeJudgment | None = None,
) -> str | None:
    """Classify a target for patient-side evidence review."""

    if judgment is not None and judgment.judge_label == "incorrect":
        if not _is_patient_evidence_target(target):
            return None
        categories = ",".join(judgment.error_categories) or "uncategorized"
        return f"judge_incorrect:{categories}"

    kind = target.verdict.criterion.kind
    reason = target.verdict.reason
    text = target.verdict.criterion.source_text.lower()

    if reason in {"unit_mismatch", "ambiguous_criterion"} or kind == "measurement_threshold":
        return "measurement_or_unit"
    if kind in {"condition_present", "condition_absent"}:
        return kind
    if kind in {"medication_present", "medication_absent"}:
        return kind
    if reason == "no_data":
        return "no_data"
    if reason == "unmapped_concept":
        return "unmapped_concept"
    if reason == "extractor_invariant_violation":
        return "extractor_invariant_violation"
    if reason == "human_review_required" and _looks_patient_evidence_relevant(text):
        return "free_text_patient_evidence"
    return None


def _target_in_scope(target: JudgeTarget, scope: PatientEvidenceScope) -> bool:
    if scope == "all":
        return True
    return target.slice in CARDIOMETABOLIC_SLICES


def _is_patient_evidence_target(target: JudgeTarget) -> bool:
    kind = target.verdict.criterion.kind
    reason = target.verdict.reason
    text = target.verdict.criterion.source_text.lower()
    return (
        kind
        in {
            "condition_present",
            "condition_absent",
            "medication_present",
            "medication_absent",
            "measurement_threshold",
        }
        or reason
        in {
            "ambiguous_criterion",
            "extractor_invariant_violation",
            "no_data",
            "unit_mismatch",
            "unmapped_concept",
        }
        or (reason == "human_review_required" and _looks_patient_evidence_relevant(text))
    )


def summarize_patient_evidence_rows(
    rows: list[PatientEvidenceCalibrationRow],
) -> dict[str, int]:
    """Return bucket counts for CLI progress output."""

    return dict(sorted(Counter(row.candidate_bucket for row in rows).items()))


def _criterion_concept_mappings(
    verdict: MatchVerdict,
) -> list[PatientEvidenceConceptMapping]:
    criterion = verdict.criterion
    mappings: list[PatientEvidenceConceptMapping] = []
    if criterion.condition is not None:
        mappings.append(
            _concept_mapping(
                slot="condition",
                surface=criterion.condition.condition_text,
                concept_set=lookup_condition(criterion.condition.condition_text),
            )
        )
    if criterion.medication is not None:
        mappings.append(
            _concept_mapping(
                slot="medication",
                surface=criterion.medication.medication_text,
                concept_set=lookup_medication(criterion.medication.medication_text),
            )
        )
    if criterion.measurement is not None:
        mappings.append(
            _concept_mapping(
                slot="measurement",
                surface=criterion.measurement.measurement_text,
                concept_set=lookup_lab(criterion.measurement.measurement_text),
            )
        )
    if criterion.temporal_window is not None:
        mappings.append(
            _concept_mapping(
                slot="temporal_event",
                surface=criterion.temporal_window.event_text,
                concept_set=lookup_condition(criterion.temporal_window.event_text),
            )
        )
    if criterion.kind == "free_text":
        mappings.extend(_free_text_mention_concept_mappings(criterion))
    return mappings


def _free_text_mention_concept_mappings(
    criterion: ExtractedCriterion,
) -> list[PatientEvidenceConceptMapping]:
    mappings: list[PatientEvidenceConceptMapping] = []
    seen: set[tuple[str, str]] = set()
    for mention in criterion.mentions:
        surface = mention.text.strip()
        if not surface:
            continue
        if mention.type == "Condition":
            slot = "condition"
            concept_set = lookup_condition(surface)
        elif mention.type == "Drug":
            slot = "medication"
            concept_set = lookup_medication(surface)
        elif mention.type in {"Measurement", "Observation"}:
            slot = "measurement"
            concept_set = lookup_lab(surface)
        else:
            continue
        key = (slot, " ".join(surface.lower().split()))
        if key in seen:
            continue
        seen.add(key)
        mappings.append(
            _concept_mapping(
                slot=slot,  # type: ignore[arg-type]
                surface=surface,
                concept_set=concept_set,
            )
        )
    return mappings


def _concept_mapping(
    *,
    slot: Literal["condition", "medication", "measurement", "temporal_event"],
    surface: str,
    concept_set: object | None,
) -> PatientEvidenceConceptMapping:
    if concept_set is None:
        return PatientEvidenceConceptMapping(
            slot=slot,
            surface=surface,
            mapped=False,
        )
    return PatientEvidenceConceptMapping(
        slot=slot,
        surface=surface,
        mapped=True,
        concept_set_name=getattr(concept_set, "name", None),
        system=getattr(concept_set, "system", None),
        codes=sorted(getattr(concept_set, "codes", [])),
    )


def _composite_groups(
    criterion: ExtractedCriterion,
    *,
    criterion_index: int,
    source_rows: list[RetrievalSourceRow],
) -> list[PatientEvidenceCompositeGroup]:
    """Add patient-evidence retrieval metadata to shared composite groups."""

    groups = build_composite_criterion_groups(criterion, criterion_index=criterion_index)
    return [
        PatientEvidenceCompositeGroup(
            group_id=group.group_id,
            operator=group.operator,
            parent_source_text=group.parent_source_text,
            subchecks=[
                _composite_subcheck(
                    subcheck=subcheck,
                    source_rows=source_rows,
                )
                for subcheck in group.subchecks
            ],
        )
        for group in groups
    ]


def _composite_subcheck(
    *,
    subcheck: CompositeCriterionSubcheck,
    source_rows: list[RetrievalSourceRow],
) -> PatientEvidenceCompositeSubcheck:
    retrieved = retrieve_structured_patient_evidence(subcheck.criterion, source_rows, limit=5)
    return PatientEvidenceCompositeSubcheck(
        subcheck_id=subcheck.subcheck_id,
        operator=subcheck.operator,
        criterion_kind=subcheck.criterion.kind,
        source_text=subcheck.source_text,
        criterion=subcheck.criterion.model_dump(mode="json"),
        retrieved_source_row_ids=[item.row.row_id for item in retrieved],
        retrieved_source_row_counts=_retrieved_row_counts(retrieved),
        retrieval_reasons={item.row.row_id: item.reasons for item in retrieved},
    )


def _composite_line_items_from_groups(
    groups: list[PatientEvidenceCompositeGroup],
) -> list[PatientEvidenceCompositeLineItem]:
    return [
        PatientEvidenceCompositeLineItem(
            item_id=subcheck.subcheck_id,
            operator=group.operator,
            source_text=subcheck.source_text,
        )
        for group in groups
        for subcheck in group.subchecks
    ]


def _clean_composite_part(part: str) -> str:
    return part.strip().strip("-; )")


def _mapping_state(mappings: list[PatientEvidenceConceptMapping]) -> MappingState:
    if not mappings:
        return "no_mappable_slots"
    mapped = sum(1 for mapping in mappings if mapping.mapped)
    if mapped == len(mappings):
        return "all_mapped"
    if mapped == 0:
        return "all_unmapped"
    return "some_unmapped"


def _source_row_counts(rows: list[PatientEvidenceSourceRow]) -> dict[str, int]:
    counts: Counter[str] = Counter(f"{row.source}:{row.kind}" for row in rows)
    return dict(sorted(counts.items()))


def _retrieved_row_counts(retrieved: list[RetrievedPatientEvidence]) -> dict[str, int]:
    counts: Counter[str] = Counter(item.row.kind for item in retrieved)
    return dict(sorted(counts.items()))


def _evidence_retrieval_state(
    *,
    retrieved_structured_ids: list[str],
    retrieved_note_ids: list[str],
) -> EvidenceRetrievalState:
    if retrieved_structured_ids and retrieved_note_ids:
        return "structured_and_note_retrieved"
    if retrieved_structured_ids:
        return "structured_retrieved"
    if retrieved_note_ids:
        return "note_retrieved"
    return "no_patient_evidence_retrieved"


def _free_text_review_hint(
    verdict: MatchVerdict,
    *,
    retrieved_structured_ids: list[str],
    retrieved_note_ids: list[str],
    concept_mappings: list[PatientEvidenceConceptMapping],
) -> FreeTextReviewHint:
    if verdict.criterion.kind == "free_text":
        return "criterion_is_free_text"
    if retrieved_note_ids:
        return "note_evidence_retrieved"
    if (
        verdict.reason in {"human_review_required", "unmapped_concept", "no_data"}
        and not retrieved_structured_ids
    ) or any(not mapping.mapped for mapping in concept_mappings):
        return "unmapped_or_no_structured_evidence"
    return "not_needed"


def _open_world_label_guidance(
    verdict: MatchVerdict,
    *,
    retrieved_structured_ids: list[str],
    retrieved_note_ids: list[str],
    concept_mappings: list[PatientEvidenceConceptMapping],
) -> str:
    if retrieved_note_ids:
        return (
            "Open-world: review note rows as patient evidence; decisive labels need cited "
            "note or structured row ids."
        )
    if retrieved_structured_ids:
        return (
            "Open-world: cite retrieved structured patient rows when they support or "
            "contradict the criterion."
        )
    if any(not mapping.mapped for mapping in concept_mappings):
        return (
            "Open-world: unmapped surfaces may indicate a matcher mapping gap; absence of "
            "retrieved rows is not proof of absence."
        )
    if verdict.reason == "no_data":
        return "Open-world: no patient row is insufficient evidence, not proof of absence."
    return "Open-world: label only what the cited patient rows actually support."


def _closed_world_label_guidance(verdict: MatchVerdict) -> str:
    if verdict.criterion.kind in {
        "condition_present",
        "condition_absent",
        "medication_present",
        "medication_absent",
        "temporal_window",
    }:
        return (
            "Closed-world: if the patient file is treated as complete for this data type, "
            "absence of matching rows can support absence; otherwise keep open-world."
        )
    if verdict.criterion.kind == "measurement_threshold":
        return (
            "Closed-world: missing measurements still need care; a complete file can prove "
            "no recorded value, not that an unmeasured threshold is clinically false."
        )
    return "Closed-world: no special absence inference for this criterion type."


def _run_metrics(
    run: RunResult,
    labels: list[PatientEvidenceHumanLabel],
) -> PatientEvidenceRunMetrics:
    verdicts = _verdicts_by_key(run)
    run_assumption = _run_assumption(run)
    comparable_all = [label for label in labels if label.expected_matcher_verdict is not None]
    comparable = [
        label for label in comparable_all if str(label.matcher_assumption_mode) == run_assumption
    ]
    skipped_assumption_mismatch = len(comparable_all) - len(comparable)
    correct = 0
    abstentions = 0
    missing = 0
    citation_targets = 0
    citation_matches = 0
    for label in comparable:
        verdict = verdicts.get((label.pair_id, label.criterion_index))
        if verdict is None:
            missing += 1
            continue
        if verdict.verdict == label.expected_matcher_verdict:
            correct += 1
        if verdict.verdict == "indeterminate":
            abstentions += 1
        if label.expected_matcher_verdict != "indeterminate" and label.cited_source_row_ids:
            citation_targets += 1
            if set(label.cited_source_row_ids).issubset(_cited_patient_row_ids(verdict)):
                citation_matches += 1

    total_cost = 0.0
    calls = 0
    input_tokens = 0
    output_tokens = 0
    llm_use_level = ""
    assumption = ""
    eligibility: Counter[str] = Counter()
    retrieved_counts: Counter[str] = Counter()
    cited_counts: Counter[str] = Counter()
    for record in run.cases:
        if record.result is None:
            continue
        llm_use_level = llm_use_level or record.result.llm_use_level
        assumption = assumption or record.result.matcher_assumption_mode
        eligibility[record.result.eligibility] += 1
        for verdict in record.result.verdicts:
            for evidence in verdict.evidence:
                if evidence.kind != "retrieved_patient_row":
                    continue
                retrieved_counts[evidence.row_kind] += 1
                if verdict.verdict in {"pass", "fail"}:
                    cited_counts[evidence.row_kind] += 1
        calls += record.result.summary.adjudicator_calls
        total_cost += record.result.summary.adjudicator_cost_usd or 0.0
        input_tokens += record.result.summary.adjudicator_input_tokens or 0
        output_tokens += record.result.summary.adjudicator_output_tokens or 0

    comparable_count = len(comparable)
    return PatientEvidenceRunMetrics(
        run_id=run.run_id,
        notes=run.notes,
        llm_use_level=llm_use_level,
        matcher_assumption_mode=assumption or run_assumption,
        total_label_targets=len(labels),
        comparable_targets=comparable_count,
        skipped_assumption_mismatch_targets=skipped_assumption_mismatch,
        missing_result_targets=missing,
        correct_verdicts=correct,
        verdict_accuracy=_ratio(correct, comparable_count - missing),
        abstentions=abstentions,
        abstention_rate=_ratio(abstentions, comparable_count - missing),
        citation_targets=citation_targets,
        citation_matches=citation_matches,
        citation_agreement=_ratio(citation_matches, citation_targets),
        retrieved_patient_row_counts=dict(sorted(retrieved_counts.items())),
        cited_patient_row_counts=dict(sorted(cited_counts.items())),
        eligibility_counts=dict(sorted(eligibility.items())),
        adjudicator_calls=calls,
        adjudicator_cost_usd=total_cost,
        adjudicator_input_tokens=input_tokens,
        adjudicator_output_tokens=output_tokens,
    )


def _mode_deltas(runs: list[RunResult]) -> list[PatientEvidenceModeDelta]:
    if len(runs) < 2:
        return []
    baseline = runs[0]
    baseline_rollups = _rollups_by_pair(baseline)
    deltas = []
    for run in runs[1:]:
        movements: Counter[str] = Counter()
        changed = 0
        for pair_id, baseline_rollup in baseline_rollups.items():
            comparison_rollup = _rollups_by_pair(run).get(pair_id)
            if comparison_rollup is None or comparison_rollup == baseline_rollup:
                continue
            changed += 1
            movements[f"{baseline_rollup}->{comparison_rollup}"] += 1
        deltas.append(
            PatientEvidenceModeDelta(
                baseline_run_id=baseline.run_id,
                comparison_run_id=run.run_id,
                changed_cases=changed,
                movements=dict(sorted(movements.items())),
            )
        )
    return deltas


def _verdicts_by_key(run: RunResult) -> dict[tuple[str, int], MatchVerdict]:
    out = {}
    for record in run.cases:
        if record.result is None:
            continue
        for index, verdict in enumerate(record.result.verdicts):
            out[(record.case.pair_id, index)] = verdict
    return out


def _run_assumption(run: RunResult) -> str:
    for record in run.cases:
        if record.result is not None:
            return str(record.result.matcher_assumption_mode)
    return ""


def _rollups_by_pair(run: RunResult) -> dict[str, str]:
    return {
        record.case.pair_id: record.result.eligibility
        for record in run.cases
        if record.result is not None
    }


def _cited_patient_row_ids(verdict: MatchVerdict) -> set[str]:
    return {
        evidence.row_id for evidence in verdict.evidence if evidence.kind == "retrieved_patient_row"
    }


def _label_key(label: PatientEvidenceHumanLabel) -> str:
    return f"{label.pair_id}[{label.criterion_index}]"


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return numerator / denominator


def _pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.1f}%"


def _citation_cell(item: PatientEvidenceRunMetrics) -> str:
    if item.citation_targets == 0:
        return "n/a"
    return f"{_pct(item.citation_agreement)} ({item.citation_matches}/{item.citation_targets})"


def _counts_cell(counts: dict[str, int]) -> str:
    if not counts:
        return "(none)"
    return " / ".join(f"{key}={value}" for key, value in sorted(counts.items()))


def _looks_patient_evidence_relevant(text: str) -> bool:
    needles = (
        "diagnos",
        "history",
        "use of",
        "tobacco",
        "nicotine",
        "marijuana",
        "alcohol",
        "drug",
        "medication",
        "therapy",
        "treatment",
        "laboratory",
        "lab",
        "hba1c",
        "glucose",
        "egfr",
        "blood pressure",
        "ldl",
    )
    return any(needle in text for needle in needles)


def _judgments_by_key(
    report: LayerThreeReport | None,
) -> dict[tuple[str, int], LayerThreeJudgment]:
    if report is None:
        return {}
    return {(j.pair_id, j.criterion_index): j for j in report.judgments}


def _indexed_source_rows(
    context: LayerThreeSourceContext,
) -> list[PatientEvidenceSourceRow]:
    rows: list[PatientEvidenceSourceRow] = []
    for index, record in enumerate(context.patient):
        rows.append(_source_row_with_id(record, row_id=f"patient:{index:03d}"))
    for index, record in enumerate(context.trial):
        if record.kind == "trial_field" and record.label == "Eligibility text":
            continue
        rows.append(_source_row_with_id(record, row_id=f"trial:{index:03d}"))
    return rows


def _source_row_with_id(
    record: LayerThreeSourceRecord,
    *,
    row_id: str,
) -> PatientEvidenceSourceRow:
    return PatientEvidenceSourceRow(
        row_id=row_id,
        source=record.source,
        kind=record.kind,
        label=record.label,
        value=record.value,
        date=record.date,
        code=record.code,
        system=record.system,
        status=record.status,
    )


__all__ = [
    "CARDIOMETABOLIC_SLICES",
    "EvidenceRetrievalState",
    "FreeTextReviewHint",
    "MappingState",
    "PatientEvidenceCalibrationRow",
    "PatientEvidenceConceptMapping",
    "PatientEvidenceHumanLabel",
    "PatientEvidenceLabel",
    "PatientEvidenceLabelCompleteness",
    "PatientEvidenceModeDelta",
    "PatientEvidenceReport",
    "PatientEvidenceRunMetrics",
    "PatientEvidenceScope",
    "PatientEvidenceSourceRow",
    "build_patient_evidence_report",
    "build_patient_evidence_rows",
    "load_layer_three_report",
    "load_patient_evidence_labels",
    "load_patient_evidence_labels_if_exists",
    "load_patient_evidence_rows",
    "merge_patient_evidence_labels",
    "patient_evidence_bucket",
    "patient_evidence_label_completeness",
    "patient_evidence_source_rows",
    "render_patient_evidence_report",
    "save_patient_evidence_labels",
    "save_patient_evidence_rows",
    "select_patient_evidence_targets",
    "summarize_patient_evidence_rows",
]
