"""Layer-3 eval: LLM-as-judge over matcher verdict quality.

Layer 1 checks deterministic structured fields against seed labels.
Layer 2 checks extraction mentions against Chia. Layer 3 is different:
it asks a rubric-bound judge to assess whether a matcher verdict is
supported by the criterion, rationale, and cited evidence. It is not
clinical ground truth by itself; it becomes useful once calibrated
against a small human-labeled set.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from openai import OpenAI
from openai.types.chat import ParsedChatCompletion
from pydantic import BaseModel, Field

from clinical_demo.evals.run import RunResult
from clinical_demo.extractor.extractor import (
    ExtractorError,
    ExtractorMissingParsedError,
    ExtractorRefusalError,
    _estimate_cost_usd,
)
from clinical_demo.matcher import MatchVerdict
from clinical_demo.observability import traced
from clinical_demo.settings import Settings, get_settings

LLM_JUDGE_VERSION = "llm-judge-v0.1"
LLM_JUDGE_PROMPT_VERSION = "llm-judge-rubric-v0.2"

JudgeLabel = Literal["correct", "incorrect", "unjudgeable"]
JudgeConfidence = Literal["low", "medium", "high"]
JudgeErrorCategory = Literal[
    "wrong_verdict",
    "unsupported_evidence",
    "missing_evidence",
    "polarity_or_negation_error",
    "rationale_mismatch",
    "unscorable",
]

LLM_JUDGE_SYSTEM_PROMPT = """\
You are an evaluation judge for a clinical-trial eligibility matcher.

You are given exactly one matcher verdict for one extracted eligibility
criterion. Your job is NOT to decide patient eligibility from scratch.
Your job is to grade whether the matcher verdict is supported by the
criterion text, matcher rationale, and cited evidence.

Return a structured grade:

  - label: "correct" if the matcher verdict follows from the criterion,
           polarity/negation fields, rationale, and cited evidence.
           "incorrect" if the verdict is contradicted by those inputs
           or applies polarity/negation incorrectly.
           "unjudgeable" only if the judge prompt itself lacks enough
           criterion/verdict/rationale detail to grade the matcher.
  - confidence: low / medium / high.
  - error_categories: zero or more closed categories explaining an
           incorrect or unjudgeable grade.
  - rationale: one short sentence, citing the decisive reason.

Rubric:

  - Be conservative. Use "unjudgeable" instead of guessing.
  - Do not use outside medical knowledge beyond simple reading of the
    criterion and evidence.
  - A justified indeterminate verdict is CORRECT. If the matcher says
    indeterminate because evidence is missing, stale, unmapped, or
    insufficient, and the rationale/evidence support that explanation,
    label it "correct", not "unjudgeable".
  - Use "missing_evidence" only when the matcher claims a pass/fail (or
    a specific factual basis) without citing the needed evidence. Do not
    use it merely because the matcher correctly reports no data.
  - Penalize rationales that claim evidence not present in the evidence list.
  - Penalize verdicts that invert inclusion/exclusion or negation
    incorrectly.
  - Output strict JSON matching the schema. No prose outside JSON.
"""


class _LLMJudgeOutput(BaseModel):
    """Strict structured output emitted by the judge model."""

    label: JudgeLabel
    confidence: JudgeConfidence
    error_categories: list[JudgeErrorCategory] = Field(default_factory=list, max_length=5)
    rationale: str = Field(max_length=500)


class JudgeTarget(BaseModel):
    """One matcher verdict selected for Layer-3 judging."""

    pair_id: str
    patient_id: str
    nct_id: str
    criterion_index: int
    verdict: MatchVerdict


class LayerThreeJudgment(BaseModel):
    """One LLM judge grade for one matcher verdict."""

    pair_id: str
    patient_id: str
    nct_id: str
    criterion_index: int
    matcher_verdict: str
    judge_label: JudgeLabel
    confidence: JudgeConfidence
    error_categories: list[JudgeErrorCategory] = Field(default_factory=list)
    rationale: str
    judge_version: str = LLM_JUDGE_VERSION
    prompt_version: str = LLM_JUDGE_PROMPT_VERSION
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    latency_ms: float | None = None


class LayerThreeHumanLabel(BaseModel):
    """Human calibration label for one judged verdict."""

    pair_id: str
    criterion_index: int
    label: JudgeLabel | None = None
    reviewer: str | None = None
    rationale: str = ""


class LayerThreeCalibrationRow(BaseModel):
    """UI-ready row for human Layer-3 calibration."""

    pair_id: str
    patient_id: str
    nct_id: str
    criterion_index: int
    bucket: str
    criterion_kind: str
    criterion_source_text: str
    polarity: str
    negated: bool
    mood: str
    matcher_verdict: str
    matcher_reason: str
    matcher_rationale: str
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    existing_label: LayerThreeHumanLabel | None = None


class LayerThreeAgreement(BaseModel):
    """Inter-rater agreement between LLM judge and human labels."""

    compared: int
    agreed: int
    agreement_rate: float | None
    cohen_kappa: float | None
    missing_judgments: int = 0
    missing_human_labels: int = 0


class LayerThreeReport(BaseModel):
    """Aggregate Layer-3 judge report."""

    judgments: list[LayerThreeJudgment]
    total_judgments: int
    label_counts: dict[str, int]
    confidence_counts: dict[str, int]
    error_category_counts: dict[str, int]
    agreement: LayerThreeAgreement | None = None
    total_cost_usd: float | None = None


class _ChatCompletionsParser(Protocol):
    def parse(self, **kwargs: Any) -> ParsedChatCompletion[_LLMJudgeOutput]: ...


class _ChatGroup(Protocol):
    completions: _ChatCompletionsParser


class _ClientLike(Protocol):
    chat: _ChatGroup


def select_judge_targets(
    run: RunResult,
    *,
    limit: int | None = None,
    only_free_text: bool = False,
) -> list[JudgeTarget]:
    """Flatten a persisted run into Layer-3 judge targets."""

    targets: list[JudgeTarget] = []
    for record in run.cases:
        if record.result is None:
            continue
        for index, verdict in enumerate(record.result.verdicts):
            if only_free_text and verdict.criterion.kind != "free_text":
                continue
            targets.append(
                JudgeTarget(
                    pair_id=record.case.pair_id,
                    patient_id=record.case.patient_id,
                    nct_id=record.case.nct_id,
                    criterion_index=index,
                    verdict=verdict,
                )
            )
            if limit is not None and len(targets) >= limit:
                return targets
    return targets


def select_stratified_judge_targets(
    run: RunResult,
    *,
    limit: int = 50,
) -> list[JudgeTarget]:
    """Select a reason/verdict-stratified calibration sample.

    This is deterministic so a reviewer can refresh the page without
    the target set jumping around. Buckets are round-robined to avoid
    the first large trial dominating the calibration packet.
    """

    if limit < 1:
        raise ValueError("limit must be positive")

    buckets: dict[str, list[JudgeTarget]] = {}
    for target in select_judge_targets(run):
        buckets.setdefault(_target_bucket(target), []).append(target)

    selected: list[JudgeTarget] = []
    bucket_names = sorted(buckets)
    while len(selected) < limit and bucket_names:
        next_bucket_names: list[str] = []
        for bucket in bucket_names:
            targets = buckets[bucket]
            if targets:
                selected.append(targets.pop(0))
                if len(selected) >= limit:
                    break
            if targets:
                next_bucket_names.append(bucket)
        bucket_names = next_bucket_names
    return selected


def build_calibration_rows(
    targets: list[JudgeTarget],
    *,
    existing_labels: list[LayerThreeHumanLabel] | None = None,
) -> list[LayerThreeCalibrationRow]:
    """Convert targets into UI rows and attach any existing label."""

    labels = {(label.pair_id, label.criterion_index): label for label in existing_labels or []}
    rows: list[LayerThreeCalibrationRow] = []
    for target in targets:
        criterion = target.verdict.criterion
        rows.append(
            LayerThreeCalibrationRow(
                pair_id=target.pair_id,
                patient_id=target.patient_id,
                nct_id=target.nct_id,
                criterion_index=target.criterion_index,
                bucket=_target_bucket(target),
                criterion_kind=criterion.kind,
                criterion_source_text=criterion.source_text,
                polarity=criterion.polarity,
                negated=criterion.negated,
                mood=criterion.mood,
                matcher_verdict=target.verdict.verdict,
                matcher_reason=target.verdict.reason,
                matcher_rationale=target.verdict.rationale,
                evidence=[e.model_dump(mode="json") for e in target.verdict.evidence],
                existing_label=labels.get((target.pair_id, target.criterion_index)),
            )
        )
    return rows


def judge_target(
    target: JudgeTarget,
    *,
    client: _ClientLike | None = None,
    settings: Settings | None = None,
) -> LayerThreeJudgment:
    """Run the Layer-3 LLM judge for one target."""

    settings = settings or get_settings()
    if client is None:
        if settings.openai_api_key is None:
            raise ExtractorError(
                "OPENAI_API_KEY is not set; cannot construct an OpenAI client. "
                "Pass `client=` for tests or set the env var for production."
            )
        client = cast(_ClientLike, OpenAI(api_key=settings.openai_api_key.get_secret_value()))

    user_message = build_judge_user_message(target)
    messages = [
        {"role": "system", "content": LLM_JUDGE_SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]

    with traced(
        "layer3_judge",
        as_type="generation",
        model=settings.extractor_model,
        model_parameters={
            "temperature": settings.extractor_temperature,
            "max_tokens": settings.judge_max_output_tokens,
        },
        input=user_message,
        metadata={
            "prompt_version": LLM_JUDGE_PROMPT_VERSION,
            "pair_id": target.pair_id,
            "criterion_index": str(target.criterion_index),
        },
        version=LLM_JUDGE_VERSION,
    ) as span:
        started = time.monotonic()
        try:
            completion = client.chat.completions.parse(
                model=settings.extractor_model,
                messages=messages,
                response_format=_LLMJudgeOutput,
                temperature=settings.extractor_temperature,
                max_tokens=settings.judge_max_output_tokens,
            )
        except Exception as exc:
            span.update(level="ERROR", status_message=f"{type(exc).__name__}: {exc}")
            raise

        latency_ms = (time.monotonic() - started) * 1000.0
        choice = completion.choices[0]
        usage = completion.usage
        input_tokens = usage.prompt_tokens if usage else None
        output_tokens = usage.completion_tokens if usage else None
        cost_usd = _estimate_cost_usd(settings.extractor_model, input_tokens, output_tokens)

        usage_details: dict[str, int] = {}
        if input_tokens is not None:
            usage_details["input"] = input_tokens
        if output_tokens is not None:
            usage_details["output"] = output_tokens

        if choice.message.refusal:
            span.update(
                level="WARNING",
                status_message=f"refusal: {choice.message.refusal}",
                output={"refusal": choice.message.refusal},
                usage_details=usage_details or None,
                cost_details={"total": cost_usd} if cost_usd is not None else None,
            )
            raise ExtractorRefusalError(choice.message.refusal, completion)

        parsed = choice.message.parsed
        if parsed is None:
            span.update(
                level="ERROR",
                status_message=f"missing parsed payload; finish_reason={choice.finish_reason!r}",
                usage_details=usage_details or None,
            )
            raise ExtractorMissingParsedError(
                "judge completion had neither parsed payload nor refusal; "
                f"finish_reason={choice.finish_reason!r}"
            )

        span.update(
            output=parsed.model_dump(mode="json"),
            usage_details=usage_details or None,
            cost_details={"total": cost_usd} if cost_usd is not None else None,
            metadata={
                "prompt_version": LLM_JUDGE_PROMPT_VERSION,
                "label": parsed.label,
                "confidence": parsed.confidence,
                "latency_ms": str(round(latency_ms, 2)),
            },
        )

    return LayerThreeJudgment(
        pair_id=target.pair_id,
        patient_id=target.patient_id,
        nct_id=target.nct_id,
        criterion_index=target.criterion_index,
        matcher_verdict=target.verdict.verdict,
        judge_label=parsed.label,
        confidence=parsed.confidence,
        error_categories=parsed.error_categories,
        rationale=parsed.rationale,
        model=settings.extractor_model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        latency_ms=latency_ms,
    )


def build_judge_user_message(target: JudgeTarget) -> str:
    """Render the per-target judge prompt body."""

    payload = {
        "pair_id": target.pair_id,
        "patient_id": target.patient_id,
        "nct_id": target.nct_id,
        "criterion_index": target.criterion_index,
        "criterion": target.verdict.criterion.model_dump(mode="json"),
        "matcher_output": {
            "verdict": target.verdict.verdict,
            "reason": target.verdict.reason,
            "rationale": target.verdict.rationale,
            "evidence": [e.model_dump(mode="json") for e in target.verdict.evidence],
            "matcher_version": target.verdict.matcher_version,
        },
    }
    return (
        "Grade this single matcher verdict using the Layer-3 rubric. "
        "Return only the structured JSON grade.\n\n"
        f"{json.dumps(payload, indent=2, sort_keys=True)}"
    )


def build_layer_three_report(
    judgments: list[LayerThreeJudgment],
    *,
    human_labels: list[LayerThreeHumanLabel] | None = None,
) -> LayerThreeReport:
    label_counts: Counter[str] = Counter(j.judge_label for j in judgments)
    confidence_counts: Counter[str] = Counter(j.confidence for j in judgments)
    error_counts: Counter[str] = Counter()
    total_cost = 0.0
    has_cost = False
    for judgment in judgments:
        error_counts.update(judgment.error_categories)
        if judgment.cost_usd is not None:
            total_cost += judgment.cost_usd
            has_cost = True

    return LayerThreeReport(
        judgments=judgments,
        total_judgments=len(judgments),
        label_counts=dict(sorted(label_counts.items())),
        confidence_counts=dict(sorted(confidence_counts.items())),
        error_category_counts=dict(sorted(error_counts.items())),
        agreement=(
            compute_agreement(judgments, human_labels) if human_labels is not None else None
        ),
        total_cost_usd=total_cost if has_cost else None,
    )


def compute_agreement(
    judgments: list[LayerThreeJudgment],
    human_labels: list[LayerThreeHumanLabel],
) -> LayerThreeAgreement:
    judge_by_key = {(j.pair_id, j.criterion_index): j.judge_label for j in judgments}
    human_by_key = {
        (h.pair_id, h.criterion_index): h.label for h in human_labels if h.label is not None
    }
    shared = sorted(set(judge_by_key) & set(human_by_key))
    if not shared:
        return LayerThreeAgreement(
            compared=0,
            agreed=0,
            agreement_rate=None,
            cohen_kappa=None,
            missing_judgments=len(human_by_key),
            missing_human_labels=len(judge_by_key),
        )

    judge_labels = [judge_by_key[key] for key in shared]
    human = [human_by_key[key] for key in shared]
    agreed = sum(1 for left, right in zip(judge_labels, human, strict=True) if left == right)
    return LayerThreeAgreement(
        compared=len(shared),
        agreed=agreed,
        agreement_rate=agreed / len(shared),
        cohen_kappa=_cohen_kappa(judge_labels, human),
        missing_judgments=len(set(human_by_key) - set(judge_by_key)),
        missing_human_labels=len(set(judge_by_key) - set(human_by_key)),
    )


def load_human_labels(path: Path | str) -> list[LayerThreeHumanLabel]:
    """Load a JSON list of LayerThreeHumanLabel records."""

    raw = json.loads(Path(path).read_text())
    return [LayerThreeHumanLabel.model_validate(item) for item in raw]


def load_human_labels_if_exists(path: Path | str) -> list[LayerThreeHumanLabel]:
    """Load human labels if present; otherwise return an empty list."""

    label_path = Path(path)
    if not label_path.exists():
        return []
    return load_human_labels(label_path)


def save_human_labels(path: Path | str, labels: list[LayerThreeHumanLabel]) -> None:
    """Persist human labels as stable, reviewer-editable JSON."""

    label_path = Path(path)
    label_path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(labels, key=lambda label: (label.pair_id, label.criterion_index))
    label_path.write_text(
        json.dumps([label.model_dump(mode="json") for label in ordered], indent=2) + "\n"
    )


def merge_human_labels(
    existing: list[LayerThreeHumanLabel],
    updates: list[LayerThreeHumanLabel],
) -> list[LayerThreeHumanLabel]:
    """Merge updates into existing labels by target key."""

    merged = {(label.pair_id, label.criterion_index): label for label in existing}
    for label in updates:
        merged[(label.pair_id, label.criterion_index)] = label
    return list(merged.values())


def _target_bucket(target: JudgeTarget) -> str:
    if target.verdict.reason in {"unmapped_concept", "human_review_required"}:
        return target.verdict.reason
    if target.verdict.verdict in {"pass", "fail"}:
        return target.verdict.verdict
    return target.verdict.reason


def _cohen_kappa(left: list[JudgeLabel], right: list[JudgeLabel]) -> float | None:
    if len(left) != len(right) or not left:
        return None
    labels: tuple[JudgeLabel, ...] = ("correct", "incorrect", "unjudgeable")
    observed = sum(1 for a, b in zip(left, right, strict=True) if a == b) / len(left)
    left_counts = Counter(left)
    right_counts = Counter(right)
    expected = sum(
        (left_counts[label] / len(left)) * (right_counts[label] / len(right)) for label in labels
    )
    if expected == 1:
        return 1.0 if observed == 1 else None
    return (observed - expected) / (1 - expected)


__all__ = [
    "LLM_JUDGE_PROMPT_VERSION",
    "LLM_JUDGE_SYSTEM_PROMPT",
    "LLM_JUDGE_VERSION",
    "JudgeTarget",
    "LayerThreeAgreement",
    "LayerThreeCalibrationRow",
    "LayerThreeHumanLabel",
    "LayerThreeJudgment",
    "LayerThreeReport",
    "build_calibration_rows",
    "build_judge_user_message",
    "build_layer_three_report",
    "compute_agreement",
    "judge_target",
    "load_human_labels",
    "load_human_labels_if_exists",
    "merge_human_labels",
    "save_human_labels",
    "select_judge_targets",
    "select_stratified_judge_targets",
]
