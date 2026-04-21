"""Unit tests for `critic_node`.

Covers:
  - Empty verdicts → no LLM call, returns empty findings + bumped iteration.
  - Happy path: stub returns findings, node forwards them.
  - Out-of-range criterion_index is dropped (defensive filter).
  - Refusal / missing-parsed propagate as the same exception types
    the extractor uses.
  - Hypothetical and other matcher-only concerns are NOT the
    critic's problem (no per-mood short-circuit; behaviour parity
    with the LLM matcher node tests).
  - The previous-fingerprints snapshot is written so the router
    can detect "no progress."
"""

from __future__ import annotations

import pytest

from clinical_demo.extractor.extractor import (
    ExtractorMissingParsedError,
    ExtractorRefusalError,
)
from clinical_demo.graph.critic_types import CriticFinding
from clinical_demo.graph.nodes.critic import LLM_CRITIC_VERSION, critic_node
from clinical_demo.matcher import MATCHER_VERSION, MatchVerdict
from clinical_demo.matcher.verdict import TrialFieldEvidence
from clinical_demo.settings import Settings

from ._fixtures import (
    CriticStubClient,
    critic_findings,
    make_critic_completion,
    state_with_verdicts,
)


def _settings() -> Settings:
    """Minimal settings double; the critic node only reads three keys."""
    from pydantic import SecretStr

    return Settings(
        openai_api_key=SecretStr("sk-test"),
        extractor_model="gpt-4o-mini-2024-07-18",
        extractor_temperature=0.0,
    )


# ---- empty verdicts ----


def test_empty_verdicts_skips_llm_and_bumps_iteration() -> None:
    state = state_with_verdicts([])
    client = CriticStubClient(make_critic_completion(parsed=critic_findings()))

    result = critic_node(state, client=client, settings=_settings())

    assert client.call_count == 0
    assert result["critic_findings"] == []
    assert result["critic_iterations"] == 1
    assert result["_critic_prev_fingerprints"] == set()


# ---- happy path ----


def test_emits_findings_from_parsed_completion(crit_age_verdict: MatchVerdict) -> None:
    state = state_with_verdicts([crit_age_verdict])
    client = CriticStubClient(
        make_critic_completion(
            parsed=critic_findings(
                (0, "polarity_smell", "warning", "rationale wording inconsistent")
            )
        )
    )

    result = critic_node(state, client=client, settings=_settings())

    assert client.call_count == 1
    findings: list[CriticFinding] = result["critic_findings"]
    assert len(findings) == 1
    assert findings[0].criterion_index == 0
    assert findings[0].kind == "polarity_smell"
    assert findings[0].severity == "warning"
    assert result["critic_iterations"] == 1


def test_uses_pinned_model_and_response_format(crit_age_verdict: MatchVerdict) -> None:
    state = state_with_verdicts([crit_age_verdict])
    client = CriticStubClient(make_critic_completion(parsed=critic_findings()))

    critic_node(state, client=client, settings=_settings())

    captured = client.captured[0]
    assert captured["model"] == "gpt-4o-mini-2024-07-18"
    assert captured["temperature"] == 0.0
    assert captured["response_format"].__name__ == "_LLMCriticOutput"


def test_user_message_includes_eligibility_text_and_indexed_verdicts(
    crit_age_verdict: MatchVerdict,
) -> None:
    state = state_with_verdicts([crit_age_verdict])
    client = CriticStubClient(make_critic_completion(parsed=critic_findings()))

    critic_node(state, client=client, settings=_settings())

    user_msg = client.captured[0]["messages"][1]["content"]
    assert "TRIAL ELIGIBILITY TEXT" in user_msg
    assert "[0]" in user_msg
    assert MATCHER_VERSION in user_msg
    assert LLM_CRITIC_VERSION  # constant exposed


# ---- defensive filter ----


def test_out_of_range_criterion_index_is_dropped(crit_age_verdict: MatchVerdict) -> None:
    state = state_with_verdicts([crit_age_verdict])  # only index 0 valid
    client = CriticStubClient(
        make_critic_completion(
            parsed=critic_findings(
                (0, "polarity_smell", "warning", "valid"),
                (5, "low_confidence_indeterminate", "warning", "out of range"),
            )
        )
    )

    result = critic_node(state, client=client, settings=_settings())

    assert len(result["critic_findings"]) == 1
    assert result["critic_findings"][0].criterion_index == 0


# ---- error paths ----


def test_refusal_raises_extractor_refusal_error(crit_age_verdict: MatchVerdict) -> None:
    state = state_with_verdicts([crit_age_verdict])
    client = CriticStubClient(make_critic_completion(parsed=None, refusal="cannot help with that"))

    with pytest.raises(ExtractorRefusalError):
        critic_node(state, client=client, settings=_settings())


def test_missing_parsed_raises_extractor_missing_parsed_error(
    crit_age_verdict: MatchVerdict,
) -> None:
    state = state_with_verdicts([crit_age_verdict])
    client = CriticStubClient(make_critic_completion(parsed=None, finish_reason="length"))

    with pytest.raises(ExtractorMissingParsedError):
        critic_node(state, client=client, settings=_settings())


# ---- previous-fingerprints snapshot ----


def test_writes_previous_fingerprints_for_router(
    crit_age_verdict: MatchVerdict,
) -> None:
    """The router needs the previous iteration's fingerprints to
    detect 'no progress.' The critic node snapshots them."""
    prev = [
        CriticFinding(
            criterion_index=0,
            kind="polarity_smell",
            severity="warning",
            rationale="prev",
        )
    ]
    state = state_with_verdicts(
        [crit_age_verdict],
        critic_findings_in=prev,
        critic_iterations_in=1,
    )
    client = CriticStubClient(make_critic_completion(parsed=critic_findings()))

    result = critic_node(state, client=client, settings=_settings())

    assert result["_critic_prev_fingerprints"] == {(0, "polarity_smell")}
    assert result["critic_iterations"] == 2  # bumped from 1


# ---- shared fixture ----


@pytest.fixture()
def crit_age_verdict() -> MatchVerdict:
    """A passing age verdict to feed the critic. The critic doesn't
    care about the specifics; it only needs ONE verdict so the
    'empty verdicts' short-circuit isn't taken."""
    from ..matcher._fixtures import crit_age

    return MatchVerdict(
        criterion=crit_age(minimum_years=18.0),
        verdict="pass",
        reason="ok",
        rationale="patient age 50 ≥ 18",
        evidence=[
            TrialFieldEvidence(
                kind="trial_field",
                field="minimum_years",
                value="18.0",
                note="age criterion lower bound",
            )
        ],
        matcher_version=MATCHER_VERSION,
    )
