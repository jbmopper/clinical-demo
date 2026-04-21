"""LLM-driven criterion extractor (v0).

A thin, deliberately un-clever wrapper over OpenAI's structured-outputs
API. v0 contract:

- One provider (OpenAI), one model snapshot (default
  `gpt-4o-mini-2024-07-18`), one prompt revision (`PROMPT_VERSION`).
- No retries, no fallback model, no router. Failures surface as
  typed exceptions; the caller decides what to do.
- Captures cost/latency/token counts as `ExtractorRunMeta` so eval
  attributability works from day one even before Langfuse is wired.

The router, fallback chain, and cost sweep belong to Phase 3 per
PLAN.md; introducing them now would make v0 non-evaluable as a
baseline.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol, cast

from openai import OpenAI
from openai.types.chat import ParsedChatCompletion

from ..observability import traced
from ..settings import Settings, get_settings
from .prompt import PROMPT_VERSION, build_messages
from .schema import ExtractedCriteria, ExtractionMetadata, ExtractorRunMeta


class _ChatCompletionsParser(Protocol):
    """The narrow client surface we actually call.

    Defined as a structural Protocol so test fakes don't have to
    inherit (or fake) the full `OpenAI` client. The real
    `OpenAI` instance trivially satisfies this shape via its
    `chat.completions.parse` method."""

    def parse(self, **kwargs: Any) -> ParsedChatCompletion[ExtractedCriteria]: ...


class _ChatGroup(Protocol):
    completions: _ChatCompletionsParser


class _ClientLike(Protocol):
    chat: _ChatGroup


class ExtractorError(RuntimeError):
    """Base class for extractor failures the caller should handle."""


class ExtractorRefusalError(ExtractorError):
    """The model returned a refusal (safety / content-policy reason).

    The refusal text is preserved on `.refusal_text` so the caller can
    log it; the raw completion object is on `.completion` for
    debugging.
    """

    def __init__(self, refusal_text: str, completion: ParsedChatCompletion[Any]) -> None:
        super().__init__(f"model refused: {refusal_text}")
        self.refusal_text = refusal_text
        # Typed as ParsedChatCompletion[Any] so callers in the LLM
        # matcher node (which use a different parsed payload) can
        # raise this without a cast. The dashboard / log readers
        # only ever read .refusal_text and a few completion-level
        # fields (id, model, finish_reason); the parsed payload's
        # specific shape is irrelevant at this seam.
        self.completion = completion


class ExtractorMissingParsedError(ExtractorError):
    """The completion finished without a parsed payload AND without a refusal.

    This shouldn't happen with strict structured outputs, but is
    handled explicitly so we surface it loudly rather than letting a
    None propagate downstream.
    """


# ---------- price table ----------
#
# Source: openai.com/api/pricing as of v0 implementation.
# Per-million-token prices in USD. Cached input pricing ignored for
# v0; cost-modelling fidelity is a Phase 3 concern.
_PRICES_PER_M_TOKENS_USD: dict[str, tuple[float, float]] = {
    "gpt-4o-mini-2024-07-18": (0.15, 0.60),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o-2024-08-06": (2.50, 10.00),
    "gpt-4o": (2.50, 10.00),
}


def _estimate_cost_usd(
    model: str, input_tokens: int | None, output_tokens: int | None
) -> float | None:
    """Rough cost estimate in USD; None if model isn't in the table.

    Deliberately conservative: not knowing a model's price is worse
    than recording 0.0, because 0.0 is a misleading data point in the
    eval rollup."""
    if input_tokens is None or output_tokens is None:
        return None
    prices = _PRICES_PER_M_TOKENS_USD.get(model)
    if prices is None:
        return None
    in_price, out_price = prices
    return (input_tokens / 1_000_000) * in_price + (output_tokens / 1_000_000) * out_price


# ---------- main entry point ----------


@dataclass(frozen=True)
class ExtractionResult:
    """Bundled output of one extractor call: parsed payload + run metadata."""

    extracted: ExtractedCriteria
    meta: ExtractorRunMeta


def extract_criteria(
    eligibility_text: str,
    *,
    client: _ClientLike | None = None,
    settings: Settings | None = None,
) -> ExtractionResult:
    """Extract structured criteria from a trial's eligibility text.

    Parameters
    ----------
    eligibility_text:
        The free-text inclusion / exclusion section, typically pulled
        from `Trial.eligibility_text`. Empty input returns an empty
        result without an API call.
    client:
        Optional pre-built `OpenAI` client. Tests inject a stub
        client; production callers should pass None and rely on the
        default constructed from settings.
    settings:
        Optional pre-built settings. Tests can supply a `Settings`
        instance with explicit model and key.

    Raises
    ------
    ExtractorRefusalError:
        The model declined to answer. Rare for trial eligibility text,
        but the API can return this for safety reasons; caller decides
        whether to retry with a different prompt.
    ExtractorMissingParsedError:
        Completion finished without a parsed payload and without a
        refusal. Indicates a SDK / API regression; should never happen
        in steady state.
    """
    settings = settings or get_settings()
    if not eligibility_text.strip():
        return ExtractionResult(
            extracted=ExtractedCriteria(criteria=[], metadata=_empty_metadata()),
            meta=ExtractorRunMeta(
                model=settings.extractor_model,
                prompt_version=PROMPT_VERSION,
                input_tokens=0,
                output_tokens=0,
                cached_input_tokens=0,
                cost_usd=0.0,
                latency_ms=0.0,
            ),
        )

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

    messages = build_messages(eligibility_text)
    # One Langfuse `generation` per extractor call. We capture
    # input/output/usage/cost on the happy path and tag the span as
    # errored on the two typed failure modes so dashboards can split
    # refusals from other failures without parsing rationale text.
    with traced(
        "extract_criteria",
        as_type="generation",
        model=settings.extractor_model,
        model_parameters={
            "temperature": settings.extractor_temperature,
            "max_tokens": settings.extractor_max_output_tokens,
        },
        input=eligibility_text,
        metadata={"prompt_version": PROMPT_VERSION},
        version=PROMPT_VERSION,
    ) as span:
        started = time.monotonic()
        try:
            completion = client.chat.completions.parse(
                model=settings.extractor_model,
                messages=messages,
                response_format=ExtractedCriteria,
                temperature=settings.extractor_temperature,
                max_tokens=settings.extractor_max_output_tokens,
            )
        except Exception as exc:
            # Latency on the error path is still a useful signal
            # (timeouts vs immediate 4xx). We surface it via the span
            # before re-raising the original exception unchanged.
            error_latency_ms = (time.monotonic() - started) * 1000.0
            span.update(
                level="ERROR",
                status_message=f"{type(exc).__name__}: {exc}",
                metadata={
                    "prompt_version": PROMPT_VERSION,
                    "latency_ms": str(round(error_latency_ms, 2)),
                },
            )
            raise

        latency_ms = (time.monotonic() - started) * 1000.0
        choice = completion.choices[0]
        usage = completion.usage
        input_tokens = usage.prompt_tokens if usage else None
        output_tokens = usage.completion_tokens if usage else None
        cached_input_tokens: int | None = None
        if usage is not None and usage.prompt_tokens_details is not None:
            cached_input_tokens = usage.prompt_tokens_details.cached_tokens
        cost_usd = _estimate_cost_usd(settings.extractor_model, input_tokens, output_tokens)

        usage_details: dict[str, int] = {}
        if input_tokens is not None:
            usage_details["input"] = input_tokens
        if output_tokens is not None:
            usage_details["output"] = output_tokens
        if cached_input_tokens is not None:
            usage_details["cached_input"] = cached_input_tokens

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

        span.update(
            output=parsed.model_dump(mode="json"),
            usage_details=usage_details or None,
            cost_details={"total": cost_usd} if cost_usd is not None else None,
            metadata={
                "prompt_version": PROMPT_VERSION,
                "criteria_count": str(len(parsed.criteria)),
                "finish_reason": str(choice.finish_reason),
                "latency_ms": str(round(latency_ms, 2)),
            },
        )

    meta = ExtractorRunMeta(
        model=settings.extractor_model,
        prompt_version=PROMPT_VERSION,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached_input_tokens,
        cost_usd=cost_usd,
        latency_ms=latency_ms,
    )
    return ExtractionResult(extracted=parsed, meta=meta)


def _empty_metadata() -> ExtractionMetadata:
    """Build the metadata payload returned when the eligibility text
    is empty — no API call is made, so the model has nothing to
    self-report."""
    return ExtractionMetadata(notes="empty eligibility text; no extraction performed")
