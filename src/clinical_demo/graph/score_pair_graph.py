"""Graph-based mirror of `clinical_demo.scoring.score_pair()`.

Same signature, same return type when the critic loop is disabled
(default). The two implementations live side-by-side for one cycle
so the eval harness can A/B them and so we can ship the LangGraph
wiring without forcing a regression on every existing caller in
one go.

Once the eval harness in 2.3 confirms parity (or surfaces the
intended differences from the LLM matcher node and critic loop),
the imperative `score_pair()` will be refactored to delegate to
this graph.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from ..domain.patient import Patient
from ..domain.trial import Trial
from ..extractor.extractor import ExtractionResult
from ..matcher import MATCHER_VERSION
from ..observability import traced
from ..scoring.score_pair import ScorePairResult
from ..settings import Settings
from .graph import DEFAULT_MAX_CRITIC_ITERATIONS, build_graph
from .nodes.critic import LLM_CRITIC_VERSION
from .nodes.critic import _ClientLike as _CriticClient
from .nodes.llm_match import LLM_MATCHER_VERSION, _ClientLike


def score_pair_graph(
    patient: Patient,
    trial: Trial,
    as_of: date,
    *,
    extraction: ExtractionResult | None = None,
    extractor_client: Any | None = None,
    llm_matcher_client: _ClientLike | None = None,
    critic_client: _CriticClient | None = None,
    settings: Settings | None = None,
    critic_enabled: bool = False,
    max_critic_iterations: int = DEFAULT_MAX_CRITIC_ITERATIONS,
    human_checkpoint: bool = False,
    thread_id: str | None = None,
    recursion_limit: int | None = None,
) -> ScorePairResult:
    """Score one (patient, trial) pair via the LangGraph orchestrator.

    Drop-in alternative to `clinical_demo.scoring.score_pair()` when
    `critic_enabled=False` (the default). Returns the same
    `ScorePairResult` envelope so consumers (CLI, eval harness,
    future API) don't branch on which orchestrator produced the
    verdict. With the critic enabled, the envelope is the same; the
    additional audit data (revisions, iteration count) lives in the
    Langfuse trace, not in the response shape, deliberately —
    Phase 2.3 may surface a subset on a richer envelope, but I
    want to ship the loop without churning every consumer first.

    Parameters
    ----------
    patient, trial, as_of, extraction
        Same semantics as the imperative entry point.
    extractor_client, llm_matcher_client, critic_client
        Stub-client hooks for tests; production uses None and the
        nodes build their own OpenAI clients from settings. The
        critic gets its own kwarg in case we point it at a
        different model in the future.
    settings
        Override the process-wide settings for this call (mainly
        useful for tests pinning a specific model).
    critic_enabled : bool
        Opt-in to the critic + revise loop. When False (default),
        behaviour is identical to 2.1: rollup → finalize → END.
    max_critic_iterations : int
        Soft budget for the critic loop. Ignored when
        `critic_enabled=False`. Default 2.
    human_checkpoint : bool
        When True, compile with a checkpointer + interrupt before
        finalize. Caller must also pass `thread_id`. v0 returns
        the partial result on first invoke; resuming with
        `Command(resume=...)` and the same thread_id completes the
        run. Phase 2.8 builds a UI on this seam.
    thread_id : optional
        Required when `human_checkpoint=True`. Used by LangGraph's
        checkpointer to identify the conversation.
    recursion_limit : optional
        Hard backstop above the explicit critic budget. Plumbed
        through to LangGraph's runtime config. Default None means
        LangGraph's own default (currently 25), which is plenty for
        our 2-iteration soft budget but cheap insurance.
    """
    graph = build_graph(
        extractor_client=extractor_client,
        llm_matcher_client=llm_matcher_client,
        critic_client=critic_client,
        settings=settings,
        critic_enabled=critic_enabled,
        max_critic_iterations=max_critic_iterations,
        human_checkpoint=human_checkpoint,
    )

    initial_state: dict[str, Any] = {
        "patient": patient,
        "trial": trial,
        "as_of": as_of,
        "extraction": extraction,
    }

    config: dict[str, Any] = {}
    if recursion_limit is not None:
        config["recursion_limit"] = recursion_limit
    if human_checkpoint:
        if thread_id is None:
            raise ValueError(
                "human_checkpoint=True requires thread_id (LangGraph's "
                "checkpointer needs a stable conversation id)."
            )
        config["configurable"] = {"thread_id": thread_id}

    # Wrap the graph invocation in a parent Langfuse span so the
    # extractor's `generation` and any per-criterion `llm_match`
    # generations nest under it. We tag with the same metadata the
    # imperative `score_pair()` does so the dashboard can union the
    # two orchestrators without splitting the pivot key.
    parent_metadata: dict[str, str] = {
        "patient_id": patient.patient_id,
        "nct_id": trial.nct_id,
        "matcher_version": MATCHER_VERSION,
        "llm_matcher_version": LLM_MATCHER_VERSION,
        "orchestrator": "langgraph",
        "critic_enabled": str(critic_enabled).lower(),
    }
    if critic_enabled:
        parent_metadata["llm_critic_version"] = LLM_CRITIC_VERSION
        parent_metadata["max_critic_iterations"] = str(max_critic_iterations)

    with traced(
        "score_pair_graph",
        as_type="span",
        input={
            "patient_id": patient.patient_id,
            "nct_id": trial.nct_id,
            "as_of": as_of.isoformat(),
            "eligibility_text_chars": len(trial.eligibility_text or ""),
            "critic_enabled": critic_enabled,
        },
        metadata=parent_metadata,
    ) as span:
        final_state = graph.invoke(initial_state, config=config or None)

        result = ScorePairResult(
            patient_id=patient.patient_id,
            nct_id=trial.nct_id,
            as_of=as_of,
            extraction=final_state["extraction"].extracted,
            extraction_meta=final_state["extraction"].meta,
            verdicts=final_state["final_verdicts"],
            summary=final_state["summary"],
            eligibility=final_state["eligibility"],
        )

        critic_iterations = final_state.get("critic_iterations", 0)
        revisions = final_state.get("critic_revisions", []) or []

        output_metadata: dict[str, str] = {
            **parent_metadata,
            "eligibility": result.eligibility,
            "total_criteria": str(result.summary.total_criteria),
            "fail_count": str(result.summary.by_verdict.get("fail", 0)),
            "pass_count": str(result.summary.by_verdict.get("pass", 0)),
            "indeterminate_count": str(result.summary.by_verdict.get("indeterminate", 0)),
        }
        if critic_enabled:
            output_metadata["critic_iterations"] = str(critic_iterations)
            output_metadata["revisions_total"] = str(len(revisions))
            output_metadata["revisions_changed_verdict"] = str(
                sum(1 for r in revisions if r.verdict_changed)
            )

        span.update(
            output={
                "eligibility": result.eligibility,
                "total_criteria": result.summary.total_criteria,
                "by_verdict": result.summary.by_verdict,
                "by_reason": result.summary.by_reason,
                "by_polarity": result.summary.by_polarity,
                "critic_iterations": critic_iterations,
                "revisions_total": len(revisions),
            },
            metadata=output_metadata,
        )

    return result


__all__ = ["score_pair_graph"]
