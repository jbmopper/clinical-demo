"""Tests for the LLM match node.

Stub-client pattern mirrors the extractor tests: we never actually
call OpenAI. The stub records the parse() kwargs so we can assert on
the prompt / model / response_format the node sent.
"""

from __future__ import annotations

from typing import cast

import pytest
from pydantic import SecretStr

from clinical_demo.extractor.extractor import (
    ExtractorMissingParsedError,
    ExtractorRefusalError,
)
from clinical_demo.extractor.schema import ExtractedCriterion
from clinical_demo.graph.nodes.llm_match import (
    LLM_MATCHER_VERSION,
    _LLMMatcherOutput,
    llm_match_node,
)
from clinical_demo.graph.state import ScoringState
from clinical_demo.matcher.verdict import (
    MissingEvidence,
    TrialFieldEvidence,
)
from clinical_demo.profile import PatientProfile
from clinical_demo.settings import Settings
from tests.graph._fixtures import (
    LLMMatcherStubClient,
    make_llm_matcher_completion,
)
from tests.matcher._fixtures import (
    AS_OF,
    crit_free_text,
    make_patient,
    make_trial,
)


def _branch_state(*, criterion: ExtractedCriterion | None = None, index: int = 0) -> ScoringState:
    patient = make_patient()
    if criterion is None:
        criterion = crit_free_text()
    return ScoringState(
        patient=patient,
        trial=make_trial(),
        as_of=AS_OF,
        profile=PatientProfile(patient, AS_OF),
        _criterion=criterion,
        _criterion_index=index,
    )


def _stub_settings() -> Settings:
    return Settings(
        openai_api_key=SecretStr("sk-test"),
        extractor_model="gpt-4o-mini-2024-07-18",
        extractor_temperature=0.0,
        extractor_max_output_tokens=4096,
    )


def test_returns_indexed_verdict_with_llm_matcher_version() -> None:
    parsed = _LLMMatcherOutput(
        verdict="pass", reason="ok", rationale="Snapshot satisfies criterion."
    )
    client = LLMMatcherStubClient(make_llm_matcher_completion(parsed=parsed))
    state = _branch_state(index=3)

    update = llm_match_node(state, client=client, settings=_stub_settings())

    assert "indexed_verdicts" in update
    index, verdict = update["indexed_verdicts"][0]
    assert index == 3
    assert verdict.matcher_version == LLM_MATCHER_VERSION
    assert verdict.verdict == "pass"
    assert verdict.reason == "ok"


def test_passes_response_format_and_model_to_client() -> None:
    parsed = _LLMMatcherOutput(verdict="pass", reason="ok", rationale="ok")
    client = LLMMatcherStubClient(make_llm_matcher_completion(parsed=parsed))
    settings = _stub_settings()
    llm_match_node(_branch_state(), client=client, settings=settings)

    assert client.captured is not None
    assert client.captured["model"] == settings.extractor_model
    assert client.captured["response_format"] is _LLMMatcherOutput
    assert client.captured["temperature"] == 0.0
    messages = client.captured["messages"]
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    user = messages[1]["content"]
    assert "CRITERION TEXT" in user
    assert "PATIENT SNAPSHOT" in user


def test_polarity_inversion_on_exclusion() -> None:
    """The model returns the *raw* answer; downstream code XORs
    polarity. An exclusion criterion the patient SATISFIES (raw=pass)
    must yield a final FAIL."""
    parsed = _LLMMatcherOutput(verdict="pass", reason="ok", rationale="Patient meets predicate.")
    client = LLMMatcherStubClient(make_llm_matcher_completion(parsed=parsed))
    state = _branch_state(criterion=crit_free_text(polarity="exclusion"))

    update = llm_match_node(state, client=client, settings=_stub_settings())
    _, verdict = update["indexed_verdicts"][0]
    assert verdict.verdict == "fail"


def test_indeterminate_passes_through_unchanged() -> None:
    """Polarity does NOT flip indeterminate — stay indeterminate
    regardless of inclusion/exclusion."""
    parsed = _LLMMatcherOutput(
        verdict="indeterminate",
        reason="no_data",
        rationale="Snapshot lacks the relevant fact.",
    )
    client = LLMMatcherStubClient(make_llm_matcher_completion(parsed=parsed))
    for polarity in ("inclusion", "exclusion"):
        state = _branch_state(criterion=crit_free_text(polarity=polarity))
        update = llm_match_node(state, client=client, settings=_stub_settings())
        _, verdict = update["indexed_verdicts"][0]
        assert verdict.verdict == "indeterminate", polarity
        assert verdict.reason == "no_data"


def test_evidence_includes_criterion_and_snapshot_rows() -> None:
    parsed = _LLMMatcherOutput(
        verdict="pass",
        reason="ok",
        rationale="ok",
    )
    client = LLMMatcherStubClient(make_llm_matcher_completion(parsed=parsed))
    state = _branch_state()

    update = llm_match_node(state, client=client, settings=_stub_settings())
    _, verdict = update["indexed_verdicts"][0]

    kinds = [type(e).__name__ for e in verdict.evidence]
    assert "TrialFieldEvidence" in kinds
    assert "MissingEvidence" in kinds

    trial_field = next(e for e in verdict.evidence if isinstance(e, TrialFieldEvidence))
    assert trial_field.field == "eligibility_criterion"
    assert "LLM matcher rationale" in (trial_field.note or "")

    missing = next(e for e in verdict.evidence if isinstance(e, MissingEvidence))
    assert "snapshot:" in (missing.note or "")


def test_refusal_raises_extractor_refusal_error() -> None:
    """Refusals must propagate cleanly — they are tracked in the
    Langfuse span as WARNING and the caller decides whether to
    treat the criterion as indeterminate or surface the refusal."""
    completion = make_llm_matcher_completion(
        parsed=None, refusal="I can't help with this.", finish_reason="content_filter"
    )
    client = LLMMatcherStubClient(completion)
    with pytest.raises(ExtractorRefusalError) as excinfo:
        llm_match_node(_branch_state(), client=client, settings=_stub_settings())
    assert "I can't help with this." in str(excinfo.value)


def test_missing_parsed_raises_extractor_missing_parsed_error() -> None:
    completion = make_llm_matcher_completion(parsed=None, finish_reason="length")
    client = LLMMatcherStubClient(completion)
    with pytest.raises(ExtractorMissingParsedError):
        llm_match_node(_branch_state(), client=client, settings=_stub_settings())


def test_hypothetical_mood_short_circuits_without_llm_call() -> None:
    """Same rule as the deterministic matcher: hypothetical mood is
    `indeterminate(unsupported_mood)`, no LLM call. Saves the cost
    line and avoids asking the model to reason about events the
    snapshot can't support."""
    completion = make_llm_matcher_completion(
        parsed=_LLMMatcherOutput(verdict="pass", reason="ok", rationale="x")
    )
    client = LLMMatcherStubClient(completion)

    state = _branch_state(criterion=crit_free_text())
    state["_criterion"] = state["_criterion"].model_copy(update={"mood": "hypothetical"})

    update = llm_match_node(state, client=client, settings=_stub_settings())
    _, verdict = update["indexed_verdicts"][0]
    assert verdict.verdict == "indeterminate"
    assert verdict.reason == "unsupported_mood"
    assert client.call_count == 0


def test_no_client_no_api_key_raises() -> None:
    """If the caller doesn't supply a stub and no key is in settings,
    we fail loudly rather than silently swallowing the error."""
    state = _branch_state()
    settings = Settings(
        openai_api_key=None,
        extractor_model="gpt-4o-mini-2024-07-18",
        extractor_temperature=0.0,
        extractor_max_output_tokens=4096,
    )
    with pytest.raises(Exception) as excinfo:
        llm_match_node(state, client=None, settings=settings)
    assert "OPENAI_API_KEY" in str(excinfo.value)


_ = cast  # silence unused-import lint when refactoring
