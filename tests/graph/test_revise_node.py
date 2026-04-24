"""Unit tests for `revise_node`.

Covers:
  - Action dispatch table is correct (one finding kind → one action).
  - `flip_polarity_and_rematch` actually flips and re-runs the
    deterministic matcher for non-free-text criteria.
  - `rerun_match_with_focus` is a no-op for non-free-text (matcher
    is replay-stable; we don't waste an LLM call).
  - `rerun_match_with_focus` for free-text routes through the
    LLM matcher node and emits the new verdict.
  - Empty / non-actionable findings produce empty update.
  - Out-of-range index emits an audit row but no verdict change.
  - Audit row's `verdict_changed` flag is correct.
"""

from __future__ import annotations

from clinical_demo.graph.critic_types import CriticFinding
from clinical_demo.graph.nodes.llm_match import _LLMMatcherOutput, llm_match_node
from clinical_demo.graph.nodes.revise import _action_for, _flip_polarity, revise_node
from clinical_demo.graph.state import ScoringState
from clinical_demo.matcher import MATCHER_VERSION, MatchVerdict
from clinical_demo.matcher.verdict import TrialFieldEvidence
from clinical_demo.settings import Settings

from ._fixtures import (
    LLMMatcherStubClient,
    make_llm_matcher_completion,
    state_with_verdicts,
)

# ---- dispatch table ----


def test_action_table_covers_all_finding_kinds() -> None:
    """Every kind in `CriticFindingKind` must have an action.

    If you add a new finding kind, this test will start failing
    (KeyError) until you wire it into the dispatch — that's the
    point."""
    from typing import get_args

    from clinical_demo.graph.critic_types import CriticFindingKind

    for kind in get_args(CriticFindingKind):
        action = _action_for(kind)
        assert action in {
            "rerun_match_with_focus",
            "rerun_extract_for_criterion",
            "flip_polarity_and_rematch",
        }


def test_flip_polarity_inverts_and_preserves_other_fields() -> None:
    from ..matcher._fixtures import crit_age

    inclusion = crit_age(minimum_years=18.0, polarity="inclusion")
    flipped = _flip_polarity(inclusion)
    assert flipped.polarity == "exclusion"
    assert flipped.kind == inclusion.kind
    assert flipped.source_text == inclusion.source_text
    assert flipped.age == inclusion.age

    back = _flip_polarity(flipped)
    assert back.polarity == "inclusion"


# ---- non-actionable findings ----


def test_no_findings_returns_empty_update() -> None:
    state = state_with_verdicts([_age_verdict()], critic_findings_in=[])
    assert revise_node(state) == {}


def test_only_info_findings_returns_empty_update() -> None:
    state = state_with_verdicts(
        [_age_verdict()],
        critic_findings_in=[
            CriticFinding(
                criterion_index=0,
                kind="polarity_smell",
                severity="info",
                rationale="just FYI",
            )
        ],
    )
    assert revise_node(state) == {}


# ---- flip_polarity_and_rematch (deterministic path) ----


def test_polarity_flip_on_age_inverts_verdict() -> None:
    """Patient is 50, age criterion is 'inclusion: >=18' (passes).
    Flipping polarity → 'exclusion: >=18' should now fail."""
    age_v = _age_verdict()
    state = state_with_verdicts(
        [age_v],
        critic_findings_in=[
            CriticFinding(
                criterion_index=0,
                kind="polarity_smell",
                severity="warning",
                rationale="extractor mis-tagged section",
            )
        ],
        critic_iterations_in=1,
    )

    result = revise_node(state)

    assert "indexed_verdicts" in result
    assert len(result["indexed_verdicts"]) == 1
    new_idx, new_v = result["indexed_verdicts"][0]
    assert new_idx == 0
    assert new_v.criterion.polarity == "exclusion"
    assert new_v.verdict != age_v.verdict  # changed

    revisions = result["critic_revisions"]
    assert len(revisions) == 1
    assert revisions[0].action == "flip_polarity_and_rematch"
    assert revisions[0].verdict_changed is True


# ---- rerun_match_with_focus (no-op for deterministic) ----


def test_rerun_with_focus_on_deterministic_criterion_is_noop_revision() -> None:
    """Re-running the deterministic matcher on a coded criterion
    would produce the same answer; revise should record a no-op."""
    age_v = _age_verdict()
    state = state_with_verdicts(
        [age_v],
        critic_findings_in=[
            CriticFinding(
                criterion_index=0,
                kind="low_confidence_indeterminate",
                severity="warning",
                rationale="rationale was thin",
            )
        ],
        critic_iterations_in=1,
    )

    result = revise_node(state)

    new_idx, new_v = result["indexed_verdicts"][0]
    assert new_idx == 0
    assert new_v is age_v  # unchanged
    assert result["critic_revisions"][0].verdict_changed is False
    assert "deterministic" in result["critic_revisions"][0].rationale.lower()


# ---- rerun_match_with_focus (free-text path) ----


def test_rerun_with_focus_on_free_text_calls_llm_matcher() -> None:
    """Free-text criterion → revise routes through the LLM matcher
    and emits the new verdict."""
    from ..matcher._fixtures import crit_free_text

    free_v = MatchVerdict(
        criterion=crit_free_text(),
        verdict="indeterminate",
        reason="no_data",
        rationale="snapshot didn't mention it",
        evidence=[],
        matcher_version=MATCHER_VERSION,
    )
    state = state_with_verdicts(
        [free_v],
        critic_findings_in=[
            CriticFinding(
                criterion_index=0,
                kind="low_confidence_indeterminate",
                severity="warning",
                rationale="might find signal with focus",
            )
        ],
        critic_iterations_in=1,
    )

    client = LLMMatcherStubClient(
        make_llm_matcher_completion(
            parsed=_LLMMatcherOutput(
                verdict="pass",
                reason="ok",
                rationale="re-run with focus found the signal",
            )
        )
    )

    result = revise_node(state, client=client, settings=_settings())

    assert client.call_count == 1
    new_v = result["indexed_verdicts"][0][1]
    assert new_v.verdict == "pass"
    assert result["critic_revisions"][0].verdict_changed is True


def test_rerun_with_focus_prompt_differs_from_original_free_text_match() -> None:
    """The focused re-run must pass reviewer context to the LLM matcher."""
    from ..matcher._fixtures import crit_free_text

    criterion = crit_free_text()
    free_v = MatchVerdict(
        criterion=criterion,
        verdict="indeterminate",
        reason="no_data",
        rationale="snapshot didn't mention it",
        evidence=[],
        matcher_version=MATCHER_VERSION,
    )
    finding_rationale = "prior rationale missed a borderline active-condition signal"
    state = state_with_verdicts(
        [free_v],
        critic_findings_in=[
            CriticFinding(
                criterion_index=0,
                kind="low_confidence_indeterminate",
                severity="warning",
                rationale=finding_rationale,
            )
        ],
        critic_iterations_in=1,
    )

    original_client = LLMMatcherStubClient(
        make_llm_matcher_completion(
            parsed=_LLMMatcherOutput(
                verdict="indeterminate",
                reason="no_data",
                rationale="original pass found no signal",
            )
        )
    )
    original_branch: ScoringState = {
        "patient": state["patient"],
        "trial": state["trial"],
        "as_of": state["as_of"],
        "extraction": state.get("extraction"),
        "profile": state["profile"],
        "_criterion": criterion,
        "_criterion_index": 0,
    }
    llm_match_node(original_branch, client=original_client, settings=_settings())

    focused_client = LLMMatcherStubClient(
        make_llm_matcher_completion(
            parsed=_LLMMatcherOutput(
                verdict="pass",
                reason="ok",
                rationale="focused pass considered the reviewer note",
            )
        )
    )
    revise_node(state, client=focused_client, settings=_settings())

    original_captured = original_client.captured
    focused_captured = focused_client.captured
    assert original_captured is not None
    assert focused_captured is not None

    original_user = original_captured["messages"][1]["content"]
    focused_user = focused_captured["messages"][1]["content"]

    assert original_user != focused_user
    assert "REVIEWER NOTE" not in original_user
    assert focused_user.startswith("REVIEWER NOTE:\n")
    assert finding_rationale in focused_user
    assert "CRITERION TEXT" in focused_user


# ---- defensive: out-of-range index ----


def test_out_of_range_index_logs_no_op_revision() -> None:
    state = state_with_verdicts(
        [_age_verdict()],
        critic_findings_in=[
            CriticFinding(
                criterion_index=99,
                kind="polarity_smell",
                severity="warning",
                rationale="should never happen",
            )
        ],
        critic_iterations_in=1,
    )

    result = revise_node(state)

    assert "indexed_verdicts" not in result
    assert len(result["critic_revisions"]) == 1
    assert result["critic_revisions"][0].verdict_changed is False


# ---- helpers ----


def _age_verdict() -> MatchVerdict:
    from ..matcher._fixtures import crit_age

    return MatchVerdict(
        criterion=crit_age(minimum_years=18.0, polarity="inclusion"),
        verdict="pass",
        reason="ok",
        rationale="patient age >= 18",
        evidence=[
            TrialFieldEvidence(
                kind="trial_field",
                field="minimum_years",
                value="18.0",
                note="age lower bound",
            )
        ],
        matcher_version=MATCHER_VERSION,
    )


def _settings() -> Settings:
    from pydantic import SecretStr

    return Settings(
        openai_api_key=SecretStr("sk-test"),
        extractor_model="gpt-4o-mini-2024-07-18",
        extractor_temperature=0.0,
    )
