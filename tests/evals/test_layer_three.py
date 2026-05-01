"""Tests for Layer-3 LLM-as-judge scaffolding."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from openai.types.chat import ParsedChatCompletion, ParsedChatCompletionMessage, ParsedChoice
from openai.types.completion_usage import CompletionUsage
from pydantic import SecretStr

from clinical_demo.evals.layer_three import (
    LLM_JUDGE_SYSTEM_PROMPT,
    LayerThreeHumanLabel,
    _LLMJudgeOutput,
    build_calibration_rows,
    build_judge_user_message,
    build_layer_three_report,
    judge_target,
    merge_human_labels,
    select_judge_targets,
    select_stratified_judge_targets,
)
from clinical_demo.evals.report_layer_three import render_layer_three
from clinical_demo.evals.run import CaseRecord, EvalCase, RunResult
from clinical_demo.matcher import MATCHER_VERSION, MatchVerdict, Verdict, VerdictReason
from clinical_demo.settings import Settings
from tests.matcher._fixtures import crit_age, crit_free_text

from ._fixtures import AS_OF, make_score_pair_result


def _case(pair_id: str = "p1__T1") -> EvalCase:
    return EvalCase(
        pair_id=pair_id,
        patient_id="p1",
        nct_id="T1",
        as_of=AS_OF,
        slice="test",
    )


def _run(records: list[CaseRecord]) -> RunResult:
    return RunResult(
        started_at=datetime(2025, 1, 1, 0, 0, 0),
        finished_at=datetime(2025, 1, 1, 0, 0, 1),
        dataset_path="seed.json",
        notes="layer-3 test",
        cases=records,
    )


def _verdict(
    kind: str = "age",
    *,
    verdict: Verdict = "pass",
    reason: VerdictReason = "ok",
) -> MatchVerdict:
    criterion = crit_free_text() if kind == "free_text" else crit_age()
    return MatchVerdict(
        criterion=criterion,
        verdict=verdict,
        reason=reason,
        rationale="test rationale",
        evidence=[],
        matcher_version=MATCHER_VERSION,
    )


def test_select_judge_targets_skips_failed_cases_and_filters_free_text() -> None:
    result = make_score_pair_result(
        verdicts=[
            _verdict("age"),
            _verdict("free_text"),
        ]
    )
    run = _run(
        [
            CaseRecord(case=_case("ok"), result=result),
            CaseRecord(case=_case("failed"), result=None, error="boom"),
        ]
    )

    targets = select_judge_targets(run, only_free_text=True)

    assert len(targets) == 1
    assert targets[0].pair_id == "ok"
    assert targets[0].criterion_index == 1
    assert targets[0].verdict.criterion.kind == "free_text"


def test_stratified_targets_round_robin_verdict_and_reason_buckets() -> None:
    result = make_score_pair_result(
        verdicts=[
            _verdict(verdict="pass"),
            _verdict(verdict="pass"),
            _verdict(verdict="fail"),
            _verdict(verdict="indeterminate", reason="unmapped_concept"),
            _verdict("free_text", verdict="indeterminate", reason="human_review_required"),
        ]
    )

    targets = select_stratified_judge_targets(
        _run([CaseRecord(case=_case("ok"), result=result)]),
        limit=4,
    )

    buckets = [
        target.verdict.reason
        if target.verdict.verdict == "indeterminate"
        else target.verdict.verdict
        for target in targets
    ]
    assert buckets == ["fail", "human_review_required", "pass", "unmapped_concept"]


def test_build_calibration_rows_attaches_existing_labels() -> None:
    target = select_judge_targets(
        _run([CaseRecord(case=_case("ok"), result=make_score_pair_result(verdicts=[_verdict()]))])
    )[0]

    rows = build_calibration_rows(
        [target],
        existing_labels=[
            LayerThreeHumanLabel(
                pair_id="ok",
                criterion_index=0,
                label="correct",
                rationale="looks right",
            )
        ],
    )

    assert rows[0].bucket == "pass"
    assert rows[0].criterion_source_text
    assert rows[0].existing_label is not None
    assert rows[0].existing_label.label == "correct"


def test_merge_human_labels_preserves_unmentioned_existing_labels() -> None:
    existing = [
        LayerThreeHumanLabel(pair_id="a", criterion_index=0, label="correct"),
        LayerThreeHumanLabel(pair_id="b", criterion_index=1, label="incorrect"),
    ]
    updates = [LayerThreeHumanLabel(pair_id="a", criterion_index=0, label="unjudgeable")]

    merged = merge_human_labels(existing, updates)

    by_key = {(label.pair_id, label.criterion_index): label.label for label in merged}
    assert by_key == {("a", 0): "unjudgeable", ("b", 1): "incorrect"}


def test_judge_target_uses_structured_response_and_wraps_metadata() -> None:
    target = select_judge_targets(
        _run([CaseRecord(case=_case(), result=make_score_pair_result(verdicts=[_verdict()]))])
    )[0]
    client = _StubClient(
        _completion(
            _LLMJudgeOutput(
                label="correct",
                confidence="high",
                error_categories=[],
                rationale="The verdict is supported by the age criterion.",
            )
        )
    )

    judgment = judge_target(target, client=client, settings=_settings())

    assert client.call_count == 1
    assert client.captured["response_format"].__name__ == "_LLMJudgeOutput"
    assert judgment.pair_id == target.pair_id
    assert judgment.criterion_index == 0
    assert judgment.judge_label == "correct"
    assert judgment.input_tokens == 70
    assert judgment.output_tokens == 15


def test_build_layer_three_report_computes_agreement_and_kappa() -> None:
    target = select_judge_targets(
        _run([CaseRecord(case=_case(), result=make_score_pair_result(verdicts=[_verdict()]))])
    )[0]
    judgment = judge_target(
        target,
        client=_StubClient(
            _completion(
                _LLMJudgeOutput(
                    label="incorrect",
                    confidence="medium",
                    error_categories=["wrong_verdict"],
                    rationale="The verdict is contradicted by the evidence.",
                )
            )
        ),
        settings=_settings(),
    )

    report = build_layer_three_report(
        [judgment],
        human_labels=[
            LayerThreeHumanLabel(pair_id=judgment.pair_id, criterion_index=0, label="incorrect")
        ],
    )

    assert report.label_counts == {"incorrect": 1}
    assert report.error_category_counts == {"wrong_verdict": 1}
    assert report.agreement is not None
    assert report.agreement.agreement_rate == 1.0
    assert report.agreement.cohen_kappa == 1.0
    rendered = render_layer_three(report)
    assert "Layer-3 LLM-as-judge report" in rendered
    assert "kappa=1.000" in rendered


def test_build_judge_user_message_contains_criterion_and_matcher_output() -> None:
    target = select_judge_targets(
        _run([CaseRecord(case=_case(), result=make_score_pair_result(verdicts=[_verdict()]))])
    )[0]

    message = build_judge_user_message(target)

    assert "criterion" in message
    assert "matcher_output" in message
    assert "test rationale" in message


def test_judge_prompt_treats_justified_indeterminate_as_correct() -> None:
    assert "A justified indeterminate verdict is CORRECT" in LLM_JUDGE_SYSTEM_PROMPT
    assert 'label it "correct", not "unjudgeable"' in LLM_JUDGE_SYSTEM_PROMPT


def _settings() -> Settings:
    return Settings(
        openai_api_key=SecretStr("sk-test"),
        extractor_model="gpt-4o-mini-2024-07-18",
        extractor_temperature=0.0,
    )


def _completion(parsed: _LLMJudgeOutput) -> ParsedChatCompletion[_LLMJudgeOutput]:
    message = ParsedChatCompletionMessage[_LLMJudgeOutput](
        role="assistant",
        content=parsed.model_dump_json(),
        refusal=None,
        parsed=parsed,
    )
    choice = ParsedChoice[_LLMJudgeOutput](
        finish_reason="stop",
        index=0,
        logprobs=None,
        message=message,
    )
    usage = CompletionUsage(completion_tokens=15, prompt_tokens=70, total_tokens=85)
    return ParsedChatCompletion[_LLMJudgeOutput](
        id="cmpl-layer-three-test",
        choices=[choice],
        created=0,
        model="gpt-4o-mini-2024-07-18",
        object="chat.completion",
        usage=usage,
    )


class _StubClient:
    def __init__(self, completion: ParsedChatCompletion[_LLMJudgeOutput]) -> None:
        self._completion = completion
        self.captured: dict[str, Any] = {}
        self.call_count = 0

        class _Completions:
            def parse(inner_self, **kwargs: Any) -> ParsedChatCompletion[_LLMJudgeOutput]:
                self.captured = kwargs
                self.call_count += 1
                return self._completion

        class _Chat:
            completions = _Completions()

        self.chat: Any = _Chat()
