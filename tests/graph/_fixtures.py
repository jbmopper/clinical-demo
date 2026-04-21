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

from clinical_demo.graph.nodes.critic import _LLMCriticFinding, _LLMCriticOutput
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


# ---- critic stubs ----


def make_critic_completion(
    *,
    parsed: _LLMCriticOutput | None,
    refusal: str | None = None,
    finish_reason: FinishReason = "stop",
    prompt_tokens: int = 80,
    completion_tokens: int = 20,
    cached_tokens: int = 0,
    model: str = "gpt-4o-mini-2024-07-18",
) -> ParsedChatCompletion[_LLMCriticOutput]:
    """Build a `ParsedChatCompletion` for the critic node's stub client.

    Same shape as `make_llm_matcher_completion`; lives here so the
    critic tests don't have to wire OpenAI types themselves."""
    message = ParsedChatCompletionMessage[_LLMCriticOutput](
        role="assistant",
        content=parsed.model_dump_json() if parsed else None,
        refusal=refusal,
        parsed=parsed,
    )
    choice = ParsedChoice[_LLMCriticOutput](
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
    return ParsedChatCompletion[_LLMCriticOutput](
        id="cmpl-critic-test-1",
        choices=[choice],
        created=0,
        model=model,
        object="chat.completion",
        usage=usage,
    )


class CriticStubClient:
    """Stub OpenAI client for the critic node.

    Returns the same completion on every call. Use
    `SequentialCriticStubClient` for tests that exercise the
    multi-iteration loop where each call must yield a different
    completion."""

    def __init__(self, completion: ParsedChatCompletion[_LLMCriticOutput]) -> None:
        self._completion = completion
        self.captured: list[dict[str, Any]] = []
        self.call_count = 0

        class _Completions:
            def parse(inner_self, **kwargs: Any) -> ParsedChatCompletion[_LLMCriticOutput]:
                self.captured.append(kwargs)
                self.call_count += 1
                return self._completion

        class _Chat:
            completions = _Completions()

        self.chat: Any = _Chat()


class SequentialCriticStubClient:
    """Stub critic client that returns a different completion per call.

    The list is consumed in order; once exhausted, the LAST entry is
    re-served on subsequent calls (so a "critic eventually goes
    quiet" pattern is just `[finding_completion, empty_completion]`).
    """

    def __init__(self, completions: list[ParsedChatCompletion[_LLMCriticOutput]]) -> None:
        if not completions:
            raise ValueError("SequentialCriticStubClient needs at least one completion")
        self._completions = completions
        self.captured: list[dict[str, Any]] = []
        self.call_count = 0

        class _Completions:
            def parse(inner_self, **kwargs: Any) -> ParsedChatCompletion[_LLMCriticOutput]:
                self.captured.append(kwargs)
                idx = min(self.call_count, len(self._completions) - 1)
                self.call_count += 1
                return self._completions[idx]

        class _Chat:
            completions = _Completions()

        self.chat: Any = _Chat()


def critic_findings(*pairs: tuple[int, str, str, str]) -> _LLMCriticOutput:
    """Convenience: build a critic output from `(index, kind, severity, rationale)`.

    Lets a test write `critic_findings((0, "polarity_smell", "warning", "x"))`
    instead of constructing the nested Pydantic types by hand. The
    `kind` and `severity` strings are forwarded as `Any` because the
    test surface uses bare `str` (avoids importing the Literal aliases
    in every test); Pydantic validates at construction time."""
    findings: list[_LLMCriticFinding] = []
    for idx, kind, sev, rationale in pairs:
        findings.append(
            _LLMCriticFinding.model_validate(
                {
                    "criterion_index": idx,
                    "kind": kind,
                    "severity": sev,
                    "rationale": rationale,
                }
            )
        )
    return _LLMCriticOutput(findings=findings)


# ---- state helpers ----


def state_with_verdicts(
    verdicts: list[Any],
    *,
    critic_findings_in: list[Any] | None = None,
    critic_iterations_in: int = 0,
) -> Any:
    """Build a `ScoringState` that looks like rollup just finished.

    Used by critic / revise / route tests that don't care about the
    extract / match phase. We construct minimal patient/trial/profile
    so the critic node's "must have a trial" preconditions pass; the
    actual values don't matter because the critic only reads the
    eligibility text and the verdicts list.
    """
    from datetime import date

    from clinical_demo.graph.state import ScoringState
    from clinical_demo.scoring.score_pair import ScoringSummary

    from ..matcher._fixtures import (
        make_patient,
        make_profile,
        make_trial,
    )

    state: ScoringState = {
        "patient": make_patient(),
        "trial": make_trial(eligibility_text="age >= 18"),
        "as_of": date(2024, 1, 1),
        "extraction": None,
        "profile": make_profile(),
        "indexed_verdicts": [(i, v) for i, v in enumerate(verdicts)],
        "final_verdicts": list(verdicts),
        "summary": ScoringSummary(
            total_criteria=len(verdicts),
            by_verdict={},
            by_reason={},
            by_polarity={},
        ),
        "eligibility": "indeterminate",
        "critic_iterations": critic_iterations_in,
        "critic_findings": critic_findings_in,
        "critic_revisions": [],
    }
    return state


__all__ = [
    "CriticStubClient",
    "FinishReason",
    "LLMMatcherStubClient",
    "SequentialCriticStubClient",
    "critic_findings",
    "make_critic_completion",
    "make_llm_matcher_completion",
    "state_with_verdicts",
]
