"""Critic node (v0): LLM critique of the current rollup → CriticFinding[].

Same design discipline as the LLM matcher (one model, one prompt
revision, stub-friendly Protocol client, Langfuse generation). What
differs is the *role*: the critic doesn't decide eligibility — it
identifies process problems that the revise node will turn into
targeted re-runs. See `prompts/critic.py` for the full role
description.

Why structured-output Pydantic, not free-text + parse
-----------------------------------------------------
Structured output gives us schema-validated `criterion_index`
bounds-checking for free, lets the eval harness pivot on `kind`
without string-matching, and makes the no-progress detector
trivial (`fingerprint` is well-defined). The cost is a slightly
heavier prompt; the benefit is no JSON-parse failure mode and no
"the model invented a new finding kind" failure mode.

Defensive contract
------------------
Even if the model returns garbage or an out-of-range index,
`_filter_findings` drops invalid entries with a WARNING-tagged
span update rather than raising — the critic is best-effort and
must not break the scoring path it's reviewing. Worst case: zero
findings emitted, the loop terminates gracefully on the next
router check.
"""

from __future__ import annotations

import time
from typing import Any, Protocol, cast

from openai import OpenAI
from openai.types.chat import ParsedChatCompletion
from pydantic import BaseModel, Field

from ...extractor.extractor import (
    ExtractorError,
    ExtractorMissingParsedError,
    ExtractorRefusalError,
    _estimate_cost_usd,
)
from ...matcher import MatchVerdict
from ...observability import traced
from ...settings import Settings, get_settings
from ..critic_types import (
    CriticFinding,
    CriticFindingKind,
    CriticSeverity,
)
from ..prompts.critic import (
    LLM_CRITIC_PROMPT_VERSION,
    LLM_CRITIC_SYSTEM_PROMPT,
)
from ..state import ScoringState

LLM_CRITIC_VERSION = "llm-critic-v0.1"


# ---------- structured-output schema ----------


class _LLMCriticFinding(BaseModel):
    criterion_index: int = Field(ge=0)
    kind: CriticFindingKind
    severity: CriticSeverity
    rationale: str = Field(max_length=500)


class _LLMCriticOutput(BaseModel):
    """Strict-mode structured output the OpenAI parse() call enforces.

    Mirrors `CriticFinding` exactly so the model and downstream code
    speak the same shape. Findings list is bounded to keep the
    prompt-economic loop tight — a critic emitting 30 findings on
    one trial is almost certainly hallucinating."""

    findings: list[_LLMCriticFinding] = Field(max_length=10)


# ---------- client surface (Protocol) ----------


class _ChatCompletionsParser(Protocol):
    def parse(self, **kwargs: Any) -> ParsedChatCompletion[_LLMCriticOutput]: ...


class _ChatGroup(Protocol):
    completions: _ChatCompletionsParser


class _ClientLike(Protocol):
    chat: _ChatGroup


# ---------- node ----------


def critic_node(
    state: ScoringState,
    *,
    client: _ClientLike | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Critique the current rollup. Emits findings (possibly empty).

    Returns a partial state update with:
      - `critic_findings`: the new findings list (None → list, even
        if empty, so the router can distinguish "ran, nothing"
        from "didn't run").
      - `critic_iterations`: bumped by 1.
    """
    settings = settings or get_settings()

    verdicts = state.get("final_verdicts", [])
    trial = state["trial"]
    iteration = state.get("critic_iterations", 0) + 1
    prev_findings = state.get("critic_findings") or []
    prev_fingerprints = {f.fingerprint for f in prev_findings}

    if not verdicts:
        # Nothing to critique. Bump the counter so the router still
        # sees forward progress (and terminates), but skip the LLM
        # call. Saves a cost line on empty-criteria trials.
        return {
            "critic_findings": [],
            "critic_iterations": iteration,
            "_critic_prev_fingerprints": prev_fingerprints,
        }

    if client is None:
        if settings.openai_api_key is None:
            raise ExtractorError(
                "OPENAI_API_KEY is not set; cannot construct an OpenAI client. "
                "Pass `client=` for tests or set the env var for production."
            )
        client = cast(
            _ClientLike,
            OpenAI(api_key=settings.openai_api_key.get_secret_value()),
        )

    user_message = _build_user_message(verdicts, trial.eligibility_text or "")
    messages = [
        {"role": "system", "content": LLM_CRITIC_SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]

    with traced(
        "critic",
        as_type="generation",
        model=settings.extractor_model,
        model_parameters={
            "temperature": settings.extractor_temperature,
            "max_tokens": 1024,
        },
        input=user_message,
        metadata={
            "prompt_version": LLM_CRITIC_PROMPT_VERSION,
            "iteration": str(iteration),
            "verdict_count": str(len(verdicts)),
        },
        version=LLM_CRITIC_VERSION,
    ) as span:
        started = time.monotonic()
        try:
            completion = client.chat.completions.parse(
                model=settings.extractor_model,
                messages=messages,
                response_format=_LLMCriticOutput,
                temperature=settings.extractor_temperature,
                max_tokens=1024,
            )
        except Exception as exc:
            span.update(
                level="ERROR",
                status_message=f"{type(exc).__name__}: {exc}",
            )
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
                status_message=(f"missing parsed payload; finish_reason={choice.finish_reason!r}"),
                usage_details=usage_details or None,
            )
            raise ExtractorMissingParsedError(
                f"completion had neither parsed payload nor refusal; "
                f"finish_reason={choice.finish_reason!r}"
            )

        # Defensive: drop findings with out-of-range indices instead
        # of letting them propagate. The model SHOULD respect the
        # schema's `ge=0` and the prompt's "must refer to a real
        # index" rule, but a flaky model is not allowed to break the
        # scoring path. We log dropped findings to the span so a
        # reviewer can see *why* the critic seemed quiet.
        valid, dropped = _filter_findings(parsed.findings, len(verdicts))

        span.update(
            output=parsed.model_dump(mode="json"),
            usage_details=usage_details or None,
            cost_details={"total": cost_usd} if cost_usd is not None else None,
            metadata={
                "prompt_version": LLM_CRITIC_PROMPT_VERSION,
                "iteration": str(iteration),
                "findings_count": str(len(valid)),
                "findings_dropped_invalid": str(dropped),
                "latency_ms": str(round(latency_ms, 2)),
            },
        )

    findings = [
        CriticFinding(
            criterion_index=f.criterion_index,
            kind=f.kind,
            severity=f.severity,
            rationale=f.rationale,
        )
        for f in valid
    ]

    return {
        "critic_findings": findings,
        "critic_iterations": iteration,
        "_critic_prev_fingerprints": prev_fingerprints,
    }


# ---------- helpers ----------


def _build_user_message(verdicts: list[MatchVerdict], eligibility_text: str) -> str:
    """Render the per-call prompt body.

    System prompt is fixed (cache hit); only this user message
    varies. Format chosen to be glanceable in the trace UI: each
    verdict numbered by index, with the criterion's source text and
    the matcher's rationale on consecutive lines.
    """
    eligibility = (eligibility_text or "").strip() or "<empty>"
    if len(eligibility) > 4000:
        eligibility = eligibility[:4000] + "… [truncated]"

    lines = [
        "TRIAL ELIGIBILITY TEXT (verbatim):",
        eligibility,
        "",
        f"VERDICTS ({len(verdicts)} total):",
    ]
    for i, v in enumerate(verdicts):
        c = v.criterion
        source = (c.source_text or "").replace("\n", " ").strip()
        if len(source) > 200:
            source = source[:200] + "…"
        lines.append(
            f"  [{i}] kind={c.kind} polarity={c.polarity} negated={c.negated} mood={c.mood}"
        )
        lines.append(f"      source_text: {source!r}")
        lines.append(f"      verdict={v.verdict} reason={v.reason} matcher={v.matcher_version}")
        rationale = (v.rationale or "").replace("\n", " ").strip()
        lines.append(f"      rationale: {rationale}")
    lines.append("")
    lines.append(
        "Identify findings per the system prompt. One finding per real "
        "issue, no padding. Empty findings list is fine."
    )
    return "\n".join(lines)


def _filter_findings(
    findings: list[_LLMCriticFinding], n_verdicts: int
) -> tuple[list[_LLMCriticFinding], int]:
    """Drop findings with out-of-range `criterion_index`.

    Returns (kept, dropped_count). Used by `critic_node` to log the
    drop count to the span so a reviewer can see why the critic
    seemed quiet — silent suppression would be the wrong default.
    """
    kept: list[_LLMCriticFinding] = []
    dropped = 0
    for f in findings:
        if 0 <= f.criterion_index < n_verdicts:
            kept.append(f)
        else:
            dropped += 1
    return kept, dropped
