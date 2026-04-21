"""Shared fixtures for graph tests.

Builds on the matcher fixtures (`tests.matcher._fixtures`) for the
domain side and the extractor stubs (`tests.extractor.test_extractor`)
for the OpenAI client side, then adds an LLM-matcher stub client and
helpers for building graph state slices.
"""

from __future__ import annotations

from typing import Any, Literal

from openai.types.chat import (
    ParsedChatCompletion,
    ParsedChatCompletionMessage,
    ParsedChoice,
)
from openai.types.completion_usage import CompletionUsage, PromptTokensDetails

from clinical_demo.graph.nodes.llm_match import (
    _ChatCompletionsParser,
    _ChatGroup,
    _ClientLike,
    _LLMMatcherOutput,
)

FinishReason = Literal["stop", "length", "tool_calls", "content_filter", "function_call"]


def make_llm_matcher_completion(
    *,
    parsed: _LLMMatcherOutput | None,
    refusal: str | None = None,
    finish_reason: FinishReason = "stop",
    prompt_tokens: int = 50,
    completion_tokens: int = 20,
    cached_tokens: int = 0,
    model: str = "gpt-4o-mini-2024-07-18",
) -> ParsedChatCompletion[_LLMMatcherOutput]:
    """Build a `ParsedChatCompletion` for the LLM matcher's stub client.

    Mirrors the extractor's `_make_completion` helper one-for-one so
    the tests look the same shape across modules."""
    message = ParsedChatCompletionMessage[_LLMMatcherOutput](
        role="assistant",
        content=parsed.model_dump_json() if parsed else None,
        refusal=refusal,
        parsed=parsed,
    )
    choice = ParsedChoice[_LLMMatcherOutput](
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
    return ParsedChatCompletion[_LLMMatcherOutput](
        id="cmpl-llm-matcher-test-1",
        choices=[choice],
        created=0,
        model=model,
        object="chat.completion",
        usage=usage,
    )


class _LLMMatcherStubCompletions(_ChatCompletionsParser):
    """Captures parse() args, returns a pre-baked completion."""

    def __init__(self, completion: ParsedChatCompletion[_LLMMatcherOutput]) -> None:
        self._completion = completion
        self.captured: dict[str, Any] | None = None
        self.call_count = 0

    def parse(self, **kwargs: Any) -> ParsedChatCompletion[_LLMMatcherOutput]:
        self.captured = kwargs
        self.call_count += 1
        return self._completion


class _LLMMatcherStubChat(_ChatGroup):
    def __init__(self, completions: _LLMMatcherStubCompletions) -> None:
        self.completions: _ChatCompletionsParser = completions


class LLMMatcherStubClient(_ClientLike):
    """Minimal stub OpenAI client for the LLM matcher node.

    Identical pattern to the extractor's `_StubClient`; gives us a
    typed object that satisfies the node's `_ClientLike` Protocol
    without spinning up anything network-y."""

    def __init__(self, completion: ParsedChatCompletion[_LLMMatcherOutput]) -> None:
        self._completions = _LLMMatcherStubCompletions(completion)
        self.chat: _ChatGroup = _LLMMatcherStubChat(self._completions)

    @property
    def captured(self) -> dict[str, Any] | None:
        return self._completions.captured

    @property
    def call_count(self) -> int:
        return self._completions.call_count


__all__ = [
    "FinishReason",
    "LLMMatcherStubClient",
    "make_llm_matcher_completion",
]
