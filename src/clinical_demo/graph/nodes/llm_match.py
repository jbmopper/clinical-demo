"""LLM matcher node (v0): structured-output OpenAI call → MatchVerdict.

Mirrors the extractor's design discipline:
  - One provider, one model snapshot, one prompt revision (pinned via
    `LLM_MATCHER_PROMPT_VERSION` and `LLM_MATCHER_VERSION`).
  - Structural-typed `_ClientLike` Protocol for stub-friendly tests
    (no real API call in CI).
  - Langfuse `generation` span per call, with model/version/cost/usage.
  - Returns the same `MatchVerdict` envelope the deterministic matcher
    produces, so the rest of the graph doesn't care which path was
    taken. Polarity / negation are applied here (XOR), the same way
    `match_criterion` does.

v0 routing
----------
This node only fires for `kind == "free_text"` per `route.route_by_kind`.
The matcher dispatcher in `clinical_demo.matcher.matcher` already
returns `indeterminate (human_review_required)` for free-text
criteria; the LLM node *replaces* that fallback with a real
attempt at a verdict. The patient snapshot we pass to the model is a
small typed bundle (no narrative text), to keep the prompt-injection
surface tight.
"""

from __future__ import annotations

import time
from typing import Any, Literal, Protocol, cast

from openai import OpenAI
from openai.types.chat import ParsedChatCompletion
from pydantic import BaseModel, Field

from ...extractor.extractor import (
    ExtractorError,
    ExtractorMissingParsedError,
    ExtractorRefusalError,
    _estimate_cost_usd,
)
from ...extractor.schema import ExtractedCriterion
from ...matcher import MATCHER_VERSION, MatchVerdict
from ...matcher.matcher import _apply_polarity
from ...matcher.verdict import (
    Evidence,
    MissingEvidence,
    TrialFieldEvidence,
    Verdict,
    VerdictReason,
)
from ...observability import traced
from ...profile import PatientProfile
from ...settings import Settings, get_settings
from ..prompts.llm_matcher import (
    LLM_MATCHER_PROMPT_VERSION,
    LLM_MATCHER_SYSTEM_PROMPT,
)
from ..state import ScoringState

LLM_MATCHER_VERSION = "llm-matcher-v0.1"

# Closed subset of the matcher reasons that the LLM is allowed to
# emit. The deterministic-only reasons (unit_mismatch, stale_data,
# etc.) intentionally don't appear — the LLM has no business
# returning them on a free-text criterion.
_LLMMatcherReason = Literal[
    "ok",
    "no_data",
    "human_review_required",
    "ambiguous_criterion",
]


# ---------- patient snapshot ----------


class _PatientSnapshot(BaseModel):
    """Small typed view of the patient passed to the LLM.

    Deliberately excludes narrative text fields. Each list element is
    a *coded* concept code + display string so the prompt never
    becomes a vector for free-text injection from upstream FHIR
    notes."""

    age_years: int | None
    sex: str | None
    active_conditions: list[str]
    current_medications: list[str]


def _build_snapshot(profile: PatientProfile) -> _PatientSnapshot:
    """Build the snapshot from a `PatientProfile`.

    Uses the profile's already-computed as-of view so the LLM sees
    the same window the deterministic matcher would. We fall through
    `Condition.concept` / `Medication.concept` to surface the
    `display` string when present, otherwise the bare code."""
    return _PatientSnapshot(
        age_years=profile.age_years,
        sex=profile.sex,
        active_conditions=[c.concept.display or c.concept.code for c in profile.active_conditions],
        current_medications=[
            m.concept.display or m.concept.code for m in profile.active_medications
        ],
    )


# ---------- structured-output schema ----------


class _LLMMatcherOutput(BaseModel):
    """Strict-mode structured output the OpenAI parse() call enforces."""

    verdict: Verdict
    reason: _LLMMatcherReason
    rationale: str = Field(max_length=400)


# ---------- client surface (Protocol) ----------


class _ChatCompletionsParser(Protocol):
    def parse(self, **kwargs: Any) -> ParsedChatCompletion[_LLMMatcherOutput]: ...


class _ChatGroup(Protocol):
    completions: _ChatCompletionsParser


class _ClientLike(Protocol):
    chat: _ChatGroup


# ---------- node ----------


def llm_match_node(
    state: ScoringState,
    *,
    client: _ClientLike | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Run one free-text criterion through the LLM matcher.

    `client` and `settings` are kwargs the graph builder threads
    through via a closure; tests inject a stub client.

    Returns `{"indexed_verdicts": [(index, MatchVerdict)]}` so the
    reducer concatenates with the deterministic branches.
    """
    settings = settings or get_settings()
    criterion = state["_criterion"]
    index = state["_criterion_index"]
    profile = state["profile"]

    # Hypothetical-mood short-circuit: same rule the deterministic
    # matcher applies. The LLM has nothing useful to add about
    # planned/expected events when the snapshot has no future data.
    if criterion.mood == "hypothetical":
        return {
            "indexed_verdicts": [
                (
                    index,
                    _build_verdict(
                        criterion,
                        verdict="indeterminate",
                        reason="unsupported_mood",
                        rationale=(
                            "Criterion is hypothetical (planned/expected); "
                            "v0 has no patient-side data on planned events."
                        ),
                        evidence=[
                            MissingEvidence(
                                kind="missing",
                                looked_for="patient-side data on hypothetical/planned events",
                                note="hypothetical mood not supported in v0",
                            )
                        ],
                    ),
                )
            ]
        }

    snapshot = _build_snapshot(profile)

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

    user_message = _build_user_message(criterion, snapshot)
    messages = [
        {"role": "system", "content": LLM_MATCHER_SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]

    with traced(
        "llm_match",
        as_type="generation",
        model=settings.extractor_model,
        model_parameters={
            "temperature": settings.extractor_temperature,
            "max_tokens": 512,
        },
        input=user_message,
        metadata={
            "prompt_version": LLM_MATCHER_PROMPT_VERSION,
            "criterion_kind": criterion.kind,
            "criterion_index": str(index),
        },
        version=LLM_MATCHER_VERSION,
    ) as span:
        started = time.monotonic()
        try:
            completion = client.chat.completions.parse(
                model=settings.extractor_model,
                messages=messages,
                response_format=_LLMMatcherOutput,
                temperature=settings.extractor_temperature,
                max_tokens=512,
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

        span.update(
            output=parsed.model_dump(mode="json"),
            usage_details=usage_details or None,
            cost_details={"total": cost_usd} if cost_usd is not None else None,
            metadata={
                "prompt_version": LLM_MATCHER_PROMPT_VERSION,
                "raw_verdict": parsed.verdict,
                "reason": parsed.reason,
                "latency_ms": str(round(latency_ms, 2)),
            },
        )

    # The model returns the *raw* answer to the criterion's predicate;
    # downstream code (here, mirroring `match_criterion`) applies
    # polarity / negation as XOR.
    final_verdict = _apply_polarity(parsed.verdict, criterion.polarity, criterion.negated)
    evidence = _build_evidence(criterion, snapshot, parsed.rationale)

    return {
        "indexed_verdicts": [
            (
                index,
                _build_verdict(
                    criterion,
                    verdict=final_verdict,
                    reason=parsed.reason,
                    rationale=parsed.rationale,
                    evidence=evidence,
                ),
            )
        ]
    }


# ---------- helpers ----------


def _build_user_message(criterion: ExtractedCriterion, snapshot: _PatientSnapshot) -> str:
    """Render the per-call prompt body.

    Format chosen for prompt-cache friendliness: the system prompt
    is identical across calls (cached), and only this user message
    varies. We use simple labelled fields rather than JSON so a
    refusal-eval reading the trace can scan it at a glance."""
    polarity_note = (
        "(this criterion is an exclusion; downstream code will invert)"
        if criterion.polarity == "exclusion"
        else "(this criterion is an inclusion)"
    )
    negation_note = (
        " (criterion is negated; downstream code will invert)" if criterion.negated else ""
    )

    return (
        f"CRITERION TEXT (verbatim): {criterion.source_text!r}\n"
        f"CRITERION POLARITY: {criterion.polarity} {polarity_note}{negation_note}\n"
        "\n"
        "PATIENT SNAPSHOT:\n"
        f"  age_years: {snapshot.age_years}\n"
        f"  sex: {snapshot.sex}\n"
        f"  active_conditions: {snapshot.active_conditions or '<none recorded>'}\n"
        f"  current_medications: {snapshot.current_medications or '<none recorded>'}\n"
        "\n"
        "Decide pass / fail / indeterminate for the predicate of the "
        "criterion against this snapshot, per the rules in the system "
        "prompt. Return strict JSON."
    )


def _build_verdict(
    criterion: ExtractedCriterion,
    *,
    verdict: Verdict,
    reason: VerdictReason,
    rationale: str,
    evidence: list[Evidence],
) -> MatchVerdict:
    """Construct a MatchVerdict stamped with the LLM matcher version.

    Note we use `LLM_MATCHER_VERSION` (not `MATCHER_VERSION`) so
    eval consumers can pivot on which matcher produced the verdict.
    `MATCHER_VERSION` is referenced here only to cross-link in the
    rationale where useful."""
    _ = MATCHER_VERSION  # kept imported for future cross-linking; quiet unused-warning
    return MatchVerdict(
        criterion=criterion,
        verdict=verdict,
        reason=reason,
        rationale=rationale,
        evidence=evidence,
        matcher_version=LLM_MATCHER_VERSION,
    )


def _build_evidence(
    criterion: ExtractedCriterion,
    snapshot: _PatientSnapshot,
    rationale: str,
) -> list[Evidence]:
    """Cite the criterion text and the snapshot the LLM saw.

    v0 produces two evidence rows: one `TrialFieldEvidence` pointing
    at the criterion source text, and one `MissingEvidence`-shaped
    audit row enumerating the snapshot the model decided against
    (so an auditor can reproduce the call without rerunning it).
    `looked_for` describes what the snapshot was meant to cover;
    the `note` carries the snapshot itself."""
    snapshot_note = (
        f"snapshot: age={snapshot.age_years}, sex={snapshot.sex}, "
        f"conditions={snapshot.active_conditions or '<none>'}, "
        f"medications={snapshot.current_medications or '<none>'}"
    )
    return [
        TrialFieldEvidence(
            kind="trial_field",
            field="eligibility_criterion",
            value=criterion.source_text or "",
            note=f"LLM matcher rationale: {rationale}",
        ),
        MissingEvidence(
            kind="missing",
            looked_for="patient snapshot consulted by LLM matcher",
            note=snapshot_note,
        ),
    ]
