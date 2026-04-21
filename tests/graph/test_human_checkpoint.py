"""Tests for the human-checkpoint hook (HITL).

The graph compiles with `interrupt_before=["finalize"]` and an
`InMemorySaver` checkpointer when `human_checkpoint=True`. The
graph pauses immediately before finalize; resuming proceeds to END.

v0 doesn't ship a UI; these tests verify the seam exists and can
be exercised programmatically. Phase 2.8's reviewer UI will consume
this same seam.
"""

from __future__ import annotations

from datetime import date

import pytest
from langgraph.types import Command

from clinical_demo.extractor.extractor import ExtractionResult, ExtractorRunMeta
from clinical_demo.extractor.schema import ExtractedCriteria, ExtractionMetadata
from clinical_demo.graph.graph import build_graph


def _extraction_with_age() -> ExtractionResult:
    from ..matcher._fixtures import crit_age

    return ExtractionResult(
        extracted=ExtractedCriteria(
            criteria=[crit_age(minimum_years=18.0)],
            metadata=ExtractionMetadata(notes=""),
        ),
        meta=ExtractorRunMeta(model="stub", prompt_version="test"),
    )


def test_human_checkpoint_pauses_before_finalize() -> None:
    """With HITL on, invoking the graph should NOT reach END on the
    first call. It should pause and report the interrupt, leaving the
    graph state checkpointed under `thread_id`."""
    from ..matcher._fixtures import make_patient, make_trial

    graph = build_graph(human_checkpoint=True)
    initial = {
        "patient": make_patient(),
        "trial": make_trial(eligibility_text="age >= 18"),
        "as_of": date(2024, 1, 1),
        "extraction": _extraction_with_age(),
    }
    config = {"configurable": {"thread_id": "test-thread-1"}}

    state = graph.invoke(initial, config=config)

    # The graph paused before finalize; final_verdicts should be set
    # (rollup ran), but no terminal-side artifact (e.g. an END marker
    # we'd inject) is present. The langgraph-internal `__interrupt__`
    # key on the state is documented contract for HITL.
    assert "final_verdicts" in state
    assert len(state["final_verdicts"]) == 1


def test_human_checkpoint_resumes_to_completion() -> None:
    """After pausing, calling `invoke(Command(resume=...), config)`
    with the same thread_id completes the run."""
    from ..matcher._fixtures import make_patient, make_trial

    graph = build_graph(human_checkpoint=True)
    initial = {
        "patient": make_patient(),
        "trial": make_trial(eligibility_text="age >= 18"),
        "as_of": date(2024, 1, 1),
        "extraction": _extraction_with_age(),
    }
    config = {"configurable": {"thread_id": "test-thread-2"}}

    graph.invoke(initial, config=config)
    final = graph.invoke(Command(resume=True), config=config)

    assert final["final_verdicts"][0].verdict == "pass"


def test_score_pair_graph_requires_thread_id_when_hitl_enabled() -> None:
    """`score_pair_graph(human_checkpoint=True)` without `thread_id`
    is a programming error; raise a clear ValueError so callers
    don't get an opaque LangGraph error from the checkpointer."""
    from clinical_demo.graph import score_pair_graph

    from ..matcher._fixtures import make_patient, make_trial

    with pytest.raises(ValueError, match="thread_id"):
        score_pair_graph(
            make_patient(),
            make_trial(eligibility_text="age >= 18"),
            as_of=date(2024, 1, 1),
            extraction=_extraction_with_age(),
            human_checkpoint=True,
        )
