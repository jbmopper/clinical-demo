"""End-to-end tests for the critic loop via `score_pair_graph`.

The key invariants:
  - With critic disabled (default), the graph behaviour is bit-for-bit
    identical to the 2.1 path (no critic_iterations bump, no
    revisions, same envelope).
  - With critic enabled but no findings, behaviour is identical to
    disabled (envelope-wise) plus a single critic span.
  - With critic enabled and one finding, the loop runs once: critic
    → revise → rollup → critic (now empty) → finalize. Two critic
    calls, one revision in the audit trail.
  - Iteration budget is honoured: a noisy critic that always returns
    findings stops at `max_critic_iterations`.
  - No-progress detection: same finding set twice in a row terminates
    even if the budget allows more.
"""

from __future__ import annotations

from datetime import date

from pydantic import SecretStr

from clinical_demo.extractor.extractor import (
    ExtractionResult,
    ExtractorRunMeta,
)
from clinical_demo.extractor.schema import ExtractedCriteria, ExtractionMetadata
from clinical_demo.graph import score_pair_graph
from clinical_demo.graph.nodes.llm_match import _LLMMatcherOutput
from clinical_demo.settings import Settings

from ._fixtures import (
    LLMMatcherStubClient,
    SequentialCriticStubClient,
    critic_findings,
    make_critic_completion,
    make_llm_matcher_completion,
)

# ---- shared fixtures ----


def _settings() -> Settings:
    return Settings(
        openai_api_key=SecretStr("sk-test"),
        extractor_model="gpt-4o-mini-2024-07-18",
        extractor_temperature=0.0,
    )


def _patient_trial_extraction(*, polarity: str = "inclusion") -> tuple:
    """Patient + trial + a one-criterion extraction (age >=18, inclusion).

    Patient is 50, so the verdict is `pass`. Flipping polarity to
    exclusion in a revise step makes it `fail`.
    """
    from ..matcher._fixtures import (
        crit_age,
        make_patient,
        make_trial,
    )

    patient = make_patient()
    trial = make_trial(eligibility_text="age >= 18")
    extraction = ExtractionResult(
        extracted=ExtractedCriteria(
            criteria=[crit_age(minimum_years=18.0, polarity=polarity)],
            metadata=ExtractionMetadata(notes=""),
        ),
        meta=ExtractorRunMeta(
            model="stub",
            prompt_version="test",
            input_tokens=0,
            output_tokens=0,
            cost_usd=0.0,
            latency_ms=0.0,
        ),
    )
    return patient, trial, extraction


# ---- critic disabled: 2.1 parity ----


def test_critic_disabled_matches_2_1_envelope_shape() -> None:
    patient, trial, extraction = _patient_trial_extraction()

    result = score_pair_graph(patient, trial, as_of=date(2024, 1, 1), extraction=extraction)

    assert result.eligibility == "pass"
    assert len(result.verdicts) == 1
    assert result.summary.total_criteria == 1


# ---- critic enabled, no findings: terminates after 1 critic call ----


def test_critic_enabled_no_findings_terminates_immediately() -> None:
    patient, trial, extraction = _patient_trial_extraction()
    critic = SequentialCriticStubClient([make_critic_completion(parsed=critic_findings())])

    result = score_pair_graph(
        patient,
        trial,
        as_of=date(2024, 1, 1),
        extraction=extraction,
        critic_enabled=True,
        critic_client=critic,
        settings=_settings(),
    )

    assert result.eligibility == "pass"
    assert critic.call_count == 1


# ---- critic enabled, one finding: loop runs once ----


def test_critic_enabled_one_finding_revises_then_terminates() -> None:
    """Critic emits one polarity_smell finding, revise flips polarity
    (pass → fail), critic runs again with no findings, loop ends."""
    patient, trial, extraction = _patient_trial_extraction()

    critic = SequentialCriticStubClient(
        [
            make_critic_completion(
                parsed=critic_findings((0, "polarity_smell", "warning", "extractor mis-tagged"))
            ),
            make_critic_completion(parsed=critic_findings()),
        ]
    )

    result = score_pair_graph(
        patient,
        trial,
        as_of=date(2024, 1, 1),
        extraction=extraction,
        critic_enabled=True,
        critic_client=critic,
        settings=_settings(),
    )

    assert result.eligibility == "fail"
    assert critic.call_count == 2


# ---- iteration budget honoured ----


def test_iteration_budget_caps_loop() -> None:
    """A critic that ALWAYS returns a (different) finding stops at
    `max_critic_iterations` thanks to the budget check."""
    patient, trial, extraction = _patient_trial_extraction()

    completions = [
        make_critic_completion(
            parsed=critic_findings((0, "polarity_smell", "warning", f"iteration {i}"))
        )
        for i in range(10)
    ]
    critic = SequentialCriticStubClient(completions)

    score_pair_graph(
        patient,
        trial,
        as_of=date(2024, 1, 1),
        extraction=extraction,
        critic_enabled=True,
        critic_client=critic,
        max_critic_iterations=2,
        settings=_settings(),
    )

    # Budget=2 means at most 2 critic invocations. The router
    # terminates as soon as `iteration >= max`, *after* critic ran.
    assert critic.call_count == 2


# ---- no-progress detection ----


def test_no_progress_terminates_loop() -> None:
    """Critic returns the SAME finding fingerprint twice in a row.
    Loop terminates after the second critic call, even if budget
    would allow a third."""
    patient, trial, extraction = _patient_trial_extraction()

    same_finding = make_critic_completion(
        parsed=critic_findings((0, "polarity_smell", "warning", "stuck on same issue"))
    )
    critic = SequentialCriticStubClient([same_finding, same_finding, same_finding])

    score_pair_graph(
        patient,
        trial,
        as_of=date(2024, 1, 1),
        extraction=extraction,
        critic_enabled=True,
        critic_client=critic,
        max_critic_iterations=5,  # generous budget
        settings=_settings(),
    )

    # Iteration 1: emit finding, no prev → revise.
    # Iteration 2: emit same finding, prev matches → finalize.
    # Iteration 3: never reached.
    assert critic.call_count == 2


# ---- LLM matcher used in revise (free-text path) ----


def test_revise_uses_llm_matcher_for_free_text_finding() -> None:
    """When revise needs to re-run a free-text criterion, it routes
    through the LLM matcher node (verifying the client wiring)."""
    from ..matcher._fixtures import (
        crit_free_text,
        make_patient,
        make_trial,
    )

    patient = make_patient()
    trial = make_trial(eligibility_text="must have prior disease X (free text)")
    extraction = ExtractionResult(
        extracted=ExtractedCriteria(
            criteria=[crit_free_text(polarity="inclusion")],
            metadata=ExtractionMetadata(notes=""),
        ),
        meta=ExtractorRunMeta(
            model="stub",
            prompt_version="test",
        ),
    )

    matcher = LLMMatcherStubClient(
        make_llm_matcher_completion(
            parsed=_LLMMatcherOutput(
                verdict="indeterminate",
                reason="no_data",
                rationale="snapshot doesn't mention disease X",
            )
        )
    )
    critic = SequentialCriticStubClient(
        [
            make_critic_completion(
                parsed=critic_findings(
                    (
                        0,
                        "low_confidence_indeterminate",
                        "warning",
                        "rationale hints at borderline signal",
                    )
                )
            ),
            make_critic_completion(parsed=critic_findings()),
        ]
    )

    score_pair_graph(
        patient,
        trial,
        as_of=date(2024, 1, 1),
        extraction=extraction,
        critic_enabled=True,
        llm_matcher_client=matcher,
        critic_client=critic,
        settings=_settings(),
    )

    # Matcher called once for initial fan-out, once for revise re-run.
    assert matcher.call_count == 2
    assert critic.call_count == 2
