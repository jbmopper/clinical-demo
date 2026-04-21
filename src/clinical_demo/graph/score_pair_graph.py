"""Graph-based mirror of `clinical_demo.scoring.score_pair()`.

Same signature, same return type. The two implementations live
side-by-side for one cycle so the eval harness can A/B them and so
we can ship the LangGraph wiring without forcing a regression on
every existing caller in one go.

Once the eval harness in 2.3 confirms parity (or surfaces the
intended differences from the LLM matcher node), the imperative
`score_pair()` will be refactored to delegate to this graph.
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
from .graph import build_graph
from .nodes.llm_match import LLM_MATCHER_VERSION, _ClientLike


def score_pair_graph(
    patient: Patient,
    trial: Trial,
    as_of: date,
    *,
    extraction: ExtractionResult | None = None,
    extractor_client: Any | None = None,
    llm_matcher_client: _ClientLike | None = None,
    settings: Settings | None = None,
) -> ScorePairResult:
    """Score one (patient, trial) pair via the LangGraph orchestrator.

    Drop-in alternative to `clinical_demo.scoring.score_pair()`.
    Returns the same `ScorePairResult` envelope so consumers (CLI,
    eval harness, future API) don't branch on which orchestrator
    produced the verdict.

    Parameters
    ----------
    patient, trial, as_of, extraction
        Same semantics as the imperative entry point.
    extractor_client, llm_matcher_client
        Stub-client hooks for tests; production uses None and the
        nodes build their own OpenAI clients from settings.
    settings
        Override the process-wide settings for this call (mainly
        useful for tests pinning a specific model).
    """
    graph = build_graph(
        extractor_client=extractor_client,
        llm_matcher_client=llm_matcher_client,
        settings=settings,
    )

    initial_state: dict[str, Any] = {
        "patient": patient,
        "trial": trial,
        "as_of": as_of,
        "extraction": extraction,
    }

    # Wrap the graph invocation in a parent Langfuse span so the
    # extractor's `generation` and any per-criterion `llm_match`
    # generations nest under it. We tag with the same metadata the
    # imperative `score_pair()` does so the dashboard can union the
    # two orchestrators without splitting the pivot key.
    with traced(
        "score_pair_graph",
        as_type="span",
        input={
            "patient_id": patient.patient_id,
            "nct_id": trial.nct_id,
            "as_of": as_of.isoformat(),
            "eligibility_text_chars": len(trial.eligibility_text or ""),
        },
        metadata={
            "patient_id": patient.patient_id,
            "nct_id": trial.nct_id,
            "matcher_version": MATCHER_VERSION,
            "llm_matcher_version": LLM_MATCHER_VERSION,
            "orchestrator": "langgraph",
        },
    ) as span:
        final_state = graph.invoke(initial_state)

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

        span.update(
            output={
                "eligibility": result.eligibility,
                "total_criteria": result.summary.total_criteria,
                "by_verdict": result.summary.by_verdict,
                "by_reason": result.summary.by_reason,
                "by_polarity": result.summary.by_polarity,
            },
            metadata={
                "patient_id": patient.patient_id,
                "nct_id": trial.nct_id,
                "matcher_version": MATCHER_VERSION,
                "llm_matcher_version": LLM_MATCHER_VERSION,
                "orchestrator": "langgraph",
                "eligibility": result.eligibility,
                "total_criteria": str(result.summary.total_criteria),
                "fail_count": str(result.summary.by_verdict.get("fail", 0)),
                "pass_count": str(result.summary.by_verdict.get("pass", 0)),
                "indeterminate_count": str(result.summary.by_verdict.get("indeterminate", 0)),
            },
        )

    return result


__all__ = ["score_pair_graph"]
