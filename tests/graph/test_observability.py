"""Pin the Langfuse span tree for the graph orchestrator.

Three spans should land on a typical mixed-criteria run:

  1. score_pair_graph (parent SPAN)
  2. extract_criteria (GENERATION) — extractor's own
  3. llm_match (GENERATION) — one per free_text criterion

The recording client doesn't model parent/child OTel context (just
flat order), but ordering + names are enough to pin the contract.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr

from clinical_demo.extractor.schema import (
    ExtractedCriteria,
    ExtractionMetadata,
)
from clinical_demo.graph import score_pair_graph
from clinical_demo.graph.nodes.llm_match import (
    LLM_MATCHER_VERSION,
    _LLMMatcherOutput,
)
from clinical_demo.matcher import MATCHER_VERSION
from clinical_demo.observability import langfuse_client
from clinical_demo.settings import Settings
from tests.extractor.test_extractor import (
    _make_completion as _make_extractor_completion,
)
from tests.extractor.test_extractor import (
    _StubClient as ExtractorStubClient,
)
from tests.graph._fixtures import (
    LLMMatcherStubClient,
    make_llm_matcher_completion,
)
from tests.matcher._fixtures import (
    AS_OF,
    crit_age,
    crit_free_text,
    make_patient,
    make_trial,
)

# ---------- recording client ----------


class _RecordingSpan:
    def __init__(self, name: str, kwargs: dict[str, Any]) -> None:
        self.name = name
        self.start_kwargs = kwargs
        self.updates: list[dict[str, Any]] = []

    def update(self, **kwargs: Any) -> None:
        self.updates.append(kwargs)

    def set_status(self, status: str, **_kwargs: Any) -> None:
        self.updates.append({"status": status})

    def end(self, **_kwargs: Any) -> None:
        return None

    def __enter__(self) -> _RecordingSpan:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class _RecordingClient:
    def __init__(self) -> None:
        self.spans: list[_RecordingSpan] = []

    def start_as_current_observation(self, **kwargs: Any) -> _RecordingSpan:
        span = _RecordingSpan(kwargs.get("name", "<unnamed>"), kwargs)
        self.spans.append(span)
        return span

    def flush(self) -> None:
        return None


@pytest.fixture
def recording_client(monkeypatch: pytest.MonkeyPatch) -> _RecordingClient:
    client = _RecordingClient()
    langfuse_client.get_client.cache_clear()
    monkeypatch.setattr(langfuse_client, "get_client", lambda: client)
    return client


# ---------- helpers ----------


def _settings() -> Settings:
    return Settings(
        openai_api_key=SecretStr("sk-test"),
        extractor_model="gpt-4o-mini-2024-07-18",
        extractor_temperature=0.0,
        extractor_max_output_tokens=4096,
    )


def _extractor_stub(criteria: list) -> ExtractorStubClient:
    return ExtractorStubClient(
        _make_extractor_completion(
            parsed=ExtractedCriteria(
                criteria=criteria,
                metadata=ExtractionMetadata(notes="test"),
            )
        )
    )


def _llm_matcher_stub() -> LLMMatcherStubClient:
    return LLMMatcherStubClient(
        make_llm_matcher_completion(
            parsed=_LLMMatcherOutput(
                verdict="indeterminate",
                reason="no_data",
                rationale="Snapshot lacks the relevant fact.",
            )
        )
    )


# ---------- tests ----------


def test_graph_emits_parent_span_with_orchestrator_tag(
    recording_client: _RecordingClient,
) -> None:
    score_pair_graph(
        make_patient(),
        make_trial(eligibility_text="Age >= 18."),
        AS_OF,
        extractor_client=_extractor_stub([crit_age(minimum_years=18.0)]),
        llm_matcher_client=_llm_matcher_stub(),
        settings=_settings(),
    )

    parents = [s for s in recording_client.spans if s.name == "score_pair_graph"]
    assert len(parents) == 1
    parent = parents[0]
    assert parent.start_kwargs["as_type"] == "span"
    assert parent.start_kwargs["metadata"]["orchestrator"] == "langgraph"
    assert parent.start_kwargs["metadata"]["matcher_version"] == MATCHER_VERSION
    assert parent.start_kwargs["metadata"]["llm_matcher_version"] == LLM_MATCHER_VERSION


def test_graph_emits_extractor_generation_inside_parent(
    recording_client: _RecordingClient,
) -> None:
    """Two spans on a no-free-text run: parent + extractor generation."""
    score_pair_graph(
        make_patient(),
        make_trial(eligibility_text="Age >= 18."),
        AS_OF,
        extractor_client=_extractor_stub([crit_age(minimum_years=18.0)]),
        llm_matcher_client=_llm_matcher_stub(),
        settings=_settings(),
    )

    names = [s.name for s in recording_client.spans]
    assert names == ["score_pair_graph", "extract_criteria"]


def test_graph_emits_one_llm_match_generation_per_free_text(
    recording_client: _RecordingClient,
) -> None:
    """Mixed run: parent + extractor + 2 llm_match generations
    (one per free_text criterion). Exact-count assertion catches a
    regression where the LLM matcher would silently fire on a
    non-free-text criterion."""
    score_pair_graph(
        make_patient(),
        make_trial(eligibility_text="x"),
        AS_OF,
        extractor_client=_extractor_stub(
            [crit_age(minimum_years=18.0), crit_free_text(), crit_free_text()]
        ),
        llm_matcher_client=_llm_matcher_stub(),
        settings=_settings(),
    )

    by_name: dict[str, int] = {}
    for s in recording_client.spans:
        by_name[s.name] = by_name.get(s.name, 0) + 1
    assert by_name == {
        "score_pair_graph": 1,
        "extract_criteria": 1,
        "llm_match": 2,
    }


def test_llm_match_span_has_criterion_index_metadata(
    recording_client: _RecordingClient,
) -> None:
    """Per-criterion indexing must be visible in the trace, otherwise
    a dashboard pivot can't link a span back to a specific verdict
    when multiple free_text criteria are in flight."""
    score_pair_graph(
        make_patient(),
        make_trial(eligibility_text="x"),
        AS_OF,
        extractor_client=_extractor_stub(
            [crit_free_text(), crit_age(minimum_years=18.0), crit_free_text()]
        ),
        llm_matcher_client=_llm_matcher_stub(),
        settings=_settings(),
    )

    llm_spans = [s for s in recording_client.spans if s.name == "llm_match"]
    indices = sorted(s.start_kwargs["metadata"]["criterion_index"] for s in llm_spans)
    # The two free_text criteria are at indices 0 and 2 in the
    # extraction. The deterministic age criterion at index 1 doesn't
    # produce an llm_match span.
    assert indices == ["0", "2"]


def test_parent_metadata_includes_eligibility_and_counts(
    recording_client: _RecordingClient,
) -> None:
    score_pair_graph(
        make_patient(),
        make_trial(eligibility_text="x"),
        AS_OF,
        extractor_client=_extractor_stub([crit_age(minimum_years=18.0)]),
        llm_matcher_client=_llm_matcher_stub(),
        settings=_settings(),
    )
    parent = next(s for s in recording_client.spans if s.name == "score_pair_graph")
    update = parent.updates[-1]
    for key in (
        "eligibility",
        "total_criteria",
        "fail_count",
        "pass_count",
        "indeterminate_count",
        "orchestrator",
    ):
        assert key in update["metadata"], key
    assert update["metadata"]["orchestrator"] == "langgraph"
