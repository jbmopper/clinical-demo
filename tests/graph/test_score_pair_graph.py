"""End-to-end tests for `score_pair_graph`.

These exercise the full graph wiring: extract → fan-out → mixed
matchers → join → rollup → public envelope. Two stub clients
(one for the extractor, one for the LLM matcher) mean no network.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import SecretStr

from clinical_demo.extractor.schema import (
    ExtractedCriteria,
    ExtractionMetadata,
)
from clinical_demo.graph import score_pair_graph
from clinical_demo.graph.nodes.llm_match import (
    LLM_MATCHER_VERSION,
    _LLMMatcherOutput,
)
from clinical_demo.matcher import MATCHER_VERSION
from clinical_demo.scoring.score_pair import ScorePairResult
from clinical_demo.settings import Settings
from tests.extractor.test_extractor import (
    _make_completion as _make_extractor_completion,
)
from tests.extractor.test_extractor import (
    _StubClient as ExtractorStubClient,
)
from tests.graph._fixtures import (
    LLMMatcherStubClient,
    make_llm_matcher_completion,
)
from tests.matcher._fixtures import (
    AS_OF,
    crit_age,
    crit_condition,
    crit_free_text,
    make_patient,
    make_trial,
)


def _settings() -> Settings:
    return Settings(
        openai_api_key=SecretStr("sk-test"),
        extractor_model="gpt-4o-mini-2024-07-18",
        extractor_temperature=0.0,
        extractor_max_output_tokens=4096,
    )


def _extractor_stub(criteria: list) -> ExtractorStubClient:
    parsed = ExtractedCriteria(
        criteria=criteria,
        metadata=ExtractionMetadata(notes="test"),
    )
    return ExtractorStubClient(_make_extractor_completion(parsed=parsed))


def _llm_matcher_stub(
    *,
    verdict: str = "indeterminate",
    reason: str = "no_data",
    rationale: str = "Snapshot lacks the relevant fact.",
) -> LLMMatcherStubClient:
    parsed = _LLMMatcherOutput(
        verdict=verdict,  # type: ignore[arg-type]
        reason=reason,  # type: ignore[arg-type]
        rationale=rationale,
    )
    return LLMMatcherStubClient(make_llm_matcher_completion(parsed=parsed))


# ---------- result envelope ----------


def test_returns_score_pair_result_envelope() -> None:
    """Same return type as the imperative score_pair — non-negotiable
    so consumers don't branch on which orchestrator produced it."""
    result = score_pair_graph(
        make_patient(),
        make_trial(eligibility_text="Age >= 18."),
        AS_OF,
        extractor_client=_extractor_stub([crit_age(minimum_years=18.0)]),
        llm_matcher_client=_llm_matcher_stub(),
        settings=_settings(),
    )
    assert isinstance(result, ScorePairResult)
    assert result.summary.total_criteria == 1
    assert result.eligibility in ("pass", "fail", "indeterminate")


# ---------- routing ----------


def test_mixed_criteria_run_through_correct_matchers() -> None:
    """Age → deterministic; free_text → LLM. Verdicts carry the
    source matcher_version so we can pin which path each took."""
    result = score_pair_graph(
        make_patient(),
        make_trial(eligibility_text="Age >= 18. Subject must be ambulatory."),
        AS_OF,
        extractor_client=_extractor_stub([crit_age(minimum_years=18.0), crit_free_text()]),
        llm_matcher_client=_llm_matcher_stub(verdict="indeterminate", reason="no_data"),
        settings=_settings(),
    )

    assert result.summary.total_criteria == 2
    by_kind = {v.criterion.kind: v for v in result.verdicts}
    assert by_kind["age"].matcher_version == MATCHER_VERSION
    assert by_kind["free_text"].matcher_version == LLM_MATCHER_VERSION


def test_extraction_order_preserved_across_parallel_fan_in() -> None:
    """Parallel matcher branches fan in via `operator.add`; without
    the rollup's sort, the verdict order would be arrival order
    (nondeterministic). Pin extraction order so eval / replay are
    deterministic."""
    criteria = [
        crit_age(minimum_years=18.0),
        crit_free_text(),
        crit_condition(),
        crit_free_text(polarity="exclusion"),
        crit_age(minimum_years=21.0),
    ]
    result = score_pair_graph(
        make_patient(),
        make_trial(eligibility_text="x"),
        AS_OF,
        extractor_client=_extractor_stub(criteria),
        llm_matcher_client=_llm_matcher_stub(),
        settings=_settings(),
    )
    assert [v.criterion.kind for v in result.verdicts] == [
        "age",
        "free_text",
        "condition_present",
        "free_text",
        "age",
    ]


# ---------- pre-supplied extraction ----------


def test_supplied_extraction_short_circuits_extractor_call() -> None:
    """If the caller passes `extraction=...`, the extract node must
    NOT call the LLM. Pin it: a stub that would error out, never
    invoked, is the proof."""
    parsed = ExtractedCriteria(
        criteria=[crit_age(minimum_years=18.0)],
        metadata=ExtractionMetadata(notes="test"),
    )
    from clinical_demo.extractor import extract_criteria

    extraction = extract_criteria(
        "Age >= 18.",
        client=_extractor_stub([crit_age(minimum_years=18.0)]),
        settings=_settings(),
    )

    _ = parsed

    class _BoomClient:
        @property
        def chat(self) -> object:
            raise AssertionError("extract should not have been called")

    result = score_pair_graph(
        make_patient(),
        make_trial(eligibility_text="Age >= 18."),
        AS_OF,
        extraction=extraction,
        extractor_client=_BoomClient(),
        llm_matcher_client=_llm_matcher_stub(),
        settings=_settings(),
    )
    assert result.summary.total_criteria == 1


# ---------- human checkpoint ----------


def test_public_human_checkpoint_resumes_with_same_thread_id() -> None:
    """The public wrapper keeps the paused checkpoint reachable by
    `thread_id`; the second call resumes instead of starting over."""
    extractor = _extractor_stub([crit_age(minimum_years=18.0)])
    thread_id = f"public-hitl-{uuid4()}"

    first = score_pair_graph(
        make_patient(),
        make_trial(eligibility_text="Age >= 18."),
        AS_OF,
        extractor_client=extractor,
        llm_matcher_client=_llm_matcher_stub(),
        settings=_settings(),
        human_checkpoint=True,
        thread_id=thread_id,
    )

    assert first.eligibility == "pass"
    assert extractor.call_count == 1

    second = score_pair_graph(
        make_patient(),
        make_trial(eligibility_text="Age >= 18."),
        AS_OF,
        extractor_client=extractor,
        llm_matcher_client=_llm_matcher_stub(),
        settings=_settings(),
        human_checkpoint=True,
        thread_id=thread_id,
    )

    assert second.eligibility == "pass"
    assert extractor.call_count == 1


# ---------- rollup edge case ----------


def test_zero_criteria_extraction_runs_clean_to_rollup() -> None:
    """A trial with no extractable criteria has to traverse the
    graph without getting stuck at fan-out — the routing function
    short-circuits to rollup. Eligibility itself is whatever the
    `_rollup` helper returns for an empty list (currently `pass`,
    same as the imperative path); we pin the *graph completion*,
    not the rollup's vacuous-truth policy."""
    result = score_pair_graph(
        make_patient(),
        make_trial(eligibility_text=""),
        AS_OF,
        extractor_client=_extractor_stub([]),
        llm_matcher_client=_llm_matcher_stub(),
        settings=_settings(),
    )
    assert result.summary.total_criteria == 0
    assert result.verdicts == []


def test_any_fail_dominates_rollup() -> None:
    """An exclusion criterion the patient SATISFIES (model raw=pass)
    inverts to FAIL after polarity, and any FAIL drives the rollup
    to FAIL — same conservative rule the imperative path applies."""
    result = score_pair_graph(
        make_patient(),
        make_trial(eligibility_text="x"),
        AS_OF,
        extractor_client=_extractor_stub(
            [crit_age(minimum_years=18.0), crit_free_text(polarity="exclusion")]
        ),
        llm_matcher_client=_llm_matcher_stub(verdict="pass", reason="ok"),
        settings=_settings(),
    )
    assert result.eligibility == "fail"


def test_no_fails_with_indeterminates_yields_indeterminate() -> None:
    result = score_pair_graph(
        make_patient(),
        make_trial(eligibility_text="x"),
        AS_OF,
        extractor_client=_extractor_stub([crit_age(minimum_years=18.0), crit_free_text()]),
        llm_matcher_client=_llm_matcher_stub(verdict="indeterminate", reason="no_data"),
        settings=_settings(),
    )
    assert result.eligibility == "indeterminate"


def test_all_pass_yields_pass() -> None:
    result = score_pair_graph(
        make_patient(),
        make_trial(eligibility_text="x"),
        AS_OF,
        extractor_client=_extractor_stub([crit_age(minimum_years=18.0), crit_free_text()]),
        llm_matcher_client=_llm_matcher_stub(verdict="pass", reason="ok"),
        settings=_settings(),
    )
    assert result.eligibility == "pass"


_ = pytest  # silence unused import if all tests run without a marker
