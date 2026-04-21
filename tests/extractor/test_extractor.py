"""End-to-end extractor tests using a stub OpenAI client.

We deliberately never hit the real API in CI: the extractor's
correctness is in (a) request construction, (b) response handling,
and (c) metadata bookkeeping — all of which can be exercised with a
prefab `ParsedChatCompletion`. The semantic quality of the model's
output is the eval harness's job, not these unit tests'.

The stub captures the keyword arguments passed to `parse(...)` so we
can assert on model choice, message ordering, and `response_format`
shape without coupling tests to any specific token count.
"""

from __future__ import annotations

from typing import Any, Literal

import pytest
from openai.types.chat import (
    ParsedChatCompletion,
    ParsedChatCompletionMessage,
    ParsedChoice,
)
from openai.types.completion_usage import CompletionUsage, PromptTokensDetails
from pydantic import SecretStr

from clinical_demo.extractor import (
    PROMPT_VERSION,
    ExtractedCriteria,
    ExtractedCriterion,
    ExtractionMetadata,
    ExtractorError,
    ExtractorMissingParsedError,
    ExtractorRefusalError,
    FreeTextCriterion,
    extract_criteria,
)
from clinical_demo.extractor.extractor import _estimate_cost_usd
from clinical_demo.settings import Settings

# ---------- stub helpers ----------


FinishReason = Literal["stop", "length", "tool_calls", "content_filter", "function_call"]


def _make_completion(
    *,
    parsed: ExtractedCriteria | None,
    refusal: str | None = None,
    finish_reason: FinishReason = "stop",
    prompt_tokens: int = 1234,
    completion_tokens: int = 567,
    cached_tokens: int = 0,
    model: str = "gpt-4o-mini-2024-07-18",
) -> ParsedChatCompletion[ExtractedCriteria]:
    """Construct a `ParsedChatCompletion` with the bare minimum
    fields populated for our extractor's read path."""
    message = ParsedChatCompletionMessage[ExtractedCriteria](
        role="assistant",
        content=parsed.model_dump_json() if parsed else None,
        refusal=refusal,
        parsed=parsed,
    )
    choice = ParsedChoice[ExtractedCriteria](
        finish_reason=finish_reason,
        index=0,
        logprobs=None,
        message=message,
    )
    usage = CompletionUsage(
        completion_tokens=completion_tokens,
        prompt_tokens=prompt_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        prompt_tokens_details=PromptTokensDetails(cached_tokens=cached_tokens),
    )
    return ParsedChatCompletion[ExtractedCriteria](
        id="cmpl-test-1",
        choices=[choice],
        created=0,
        model=model,
        object="chat.completion",
        usage=usage,
    )


from clinical_demo.extractor.extractor import (  # noqa: E402
    _ChatCompletionsParser,
    _ChatGroup,
    _ClientLike,
)


class _StubCompletions(_ChatCompletionsParser):
    """Captures parse() args, returns a pre-baked completion."""

    def __init__(self, completion: ParsedChatCompletion[ExtractedCriteria]) -> None:
        self._completion = completion
        self.captured: dict[str, Any] | None = None

    def parse(self, **kwargs: Any) -> ParsedChatCompletion[ExtractedCriteria]:
        self.captured = kwargs
        return self._completion


class _StubChat(_ChatGroup):
    def __init__(self, completions: _StubCompletions) -> None:
        self.completions: _ChatCompletionsParser = completions


class _StubClient(_ClientLike):
    def __init__(self, completion: ParsedChatCompletion[ExtractedCriteria]) -> None:
        self._completions = _StubCompletions(completion)
        self.chat: _ChatGroup = _StubChat(self._completions)

    @property
    def captured(self) -> dict[str, Any] | None:
        return self._completions.captured


def _settings(model: str = "gpt-4o-mini-2024-07-18") -> Settings:
    return Settings(
        openai_api_key=SecretStr("sk-test-not-used"),
        extractor_model=model,
        extractor_temperature=0.0,
        extractor_max_output_tokens=4096,
    )


def _trivial_extraction() -> ExtractedCriteria:
    """A minimal but valid extraction: one free-text criterion."""
    return ExtractedCriteria(
        criteria=[
            ExtractedCriterion(
                kind="free_text",
                polarity="inclusion",
                source_text="Some bullet text",
                negated=False,
                mood="actual",
                age=None,
                sex=None,
                condition=None,
                medication=None,
                measurement=None,
                temporal_window=None,
                free_text=FreeTextCriterion(note=""),
                mentions=[],
            )
        ],
        metadata=ExtractionMetadata(notes=""),
    )


# ---------- empty-input fast path ----------


def test_empty_input_short_circuits_without_calling_client():
    """Whitespace-only input must not consume API credits; the result
    should still be well-formed so callers don't need a special
    case."""
    settings = _settings()

    class ExplodingClient:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"client should not be touched (accessed {name!r})")

    result = extract_criteria(
        "   \n\t  ",
        client=ExplodingClient(),
        settings=settings,
    )
    assert result.extracted.criteria == []
    assert result.meta.input_tokens == 0
    assert result.meta.output_tokens == 0
    assert result.meta.cost_usd == 0.0
    assert result.meta.model == settings.extractor_model
    assert result.meta.prompt_version == PROMPT_VERSION


# ---------- happy path ----------


def test_happy_path_passes_correct_args_to_client():
    """The model name, temperature, max_tokens, and response_format
    should all flow through from settings to the API call."""
    settings = _settings()
    client = _StubClient(_make_completion(parsed=_trivial_extraction()))
    result = extract_criteria(
        "Inclusion Criteria:\n* Adults 18+",
        client=client,
        settings=settings,
    )
    captured = client.captured
    assert captured is not None
    assert captured["model"] == settings.extractor_model
    assert captured["temperature"] == settings.extractor_temperature
    assert captured["max_tokens"] == settings.extractor_max_output_tokens
    assert captured["response_format"] is ExtractedCriteria
    msgs = captured["messages"]
    assert msgs[0]["role"] == "system"
    assert msgs[-1]["role"] == "user"
    assert "Adults 18+" in msgs[-1]["content"]
    assert result.extracted.criteria[0].source_text == "Some bullet text"
    assert result.meta.input_tokens == 1234
    assert result.meta.output_tokens == 567
    assert result.meta.cached_input_tokens == 0
    assert result.meta.cost_usd is not None
    assert result.meta.cost_usd > 0
    assert result.meta.latency_ms is not None


def test_cached_input_tokens_propagated():
    """When the API reports cached prompt tokens, we surface them so
    the cost analysis can credit the cache."""
    settings = _settings()
    client = _StubClient(_make_completion(parsed=_trivial_extraction(), cached_tokens=900))
    result = extract_criteria(
        "Inclusion Criteria:\n* X",
        client=client,
        settings=settings,
    )
    assert result.meta.cached_input_tokens == 900


# ---------- failure modes ----------


def test_refusal_is_raised_as_typed_exception():
    """A refusal should raise `ExtractorRefusalError`, not silently
    return an empty extraction. The caller can decide whether to log
    and skip or escalate."""
    settings = _settings()
    client = _StubClient(_make_completion(parsed=None, refusal="I won't help with that"))
    with pytest.raises(ExtractorRefusalError) as excinfo:
        extract_criteria(
            "anything",
            client=client,
            settings=settings,
        )
    assert "I won't help" in excinfo.value.refusal_text


def test_missing_parsed_without_refusal_raises():
    """Defensive: shouldn't happen with strict mode, but if it does
    we want a loud error."""
    settings = _settings()
    client = _StubClient(_make_completion(parsed=None, refusal=None))
    with pytest.raises(ExtractorMissingParsedError):
        extract_criteria(
            "anything",
            client=client,
            settings=settings,
        )


def test_missing_api_key_raises_when_no_client_provided():
    """Production callers must either set OPENAI_API_KEY or pass a
    client; we don't silently use anonymous credentials."""
    settings = Settings(
        openai_api_key=None,
        extractor_model="gpt-4o-mini",
        extractor_temperature=0.0,
        extractor_max_output_tokens=128,
    )
    with pytest.raises(ExtractorError, match="OPENAI_API_KEY"):
        extract_criteria("anything", client=None, settings=settings)


# ---------- cost estimator ----------


def test_cost_estimator_known_model():
    """Spot-check the math for gpt-4o-mini at known prices.
    1M input @ $0.15 + 1M output @ $0.60 = $0.75."""
    cost = _estimate_cost_usd("gpt-4o-mini-2024-07-18", 1_000_000, 1_000_000)
    assert cost is not None
    assert cost == pytest.approx(0.75)


def test_cost_estimator_unknown_model_returns_none():
    """Unknown model should return None so the eval rollup can flag
    the gap rather than silently record $0."""
    assert _estimate_cost_usd("future-model-7", 100, 100) is None


def test_cost_estimator_handles_missing_token_counts():
    """If the API doesn't report tokens, cost can't be inferred."""
    assert _estimate_cost_usd("gpt-4o-mini", None, 100) is None
    assert _estimate_cost_usd("gpt-4o-mini", 100, None) is None
