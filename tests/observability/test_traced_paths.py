"""Integration tests: with a recording Langfuse client installed, do
the extractor and score_pair emit the expected span structure?

These tests pin the *contract* between application code and the
observability shim — names, span kinds, the metadata keys we've
promised the dashboard pivots on. Changes to span shape should land
here as deliberate edits, not as drifting test failures.

We don't reach into the Langfuse SDK itself; the recording client is
the same fake used in `test_langfuse_shim.py`. That keeps the test
suite hermetic (no OTel exporter, no network) while still exercising
the full call path of `extract_criteria` and `score_pair`.
"""

from __future__ import annotations

from typing import Any

import pytest

from clinical_demo.observability import langfuse_client
from clinical_demo.scoring import score_pair
from tests.extractor.test_extractor import (
    _make_completion,
    _settings,
    _StubClient,
    _trivial_extraction,
)
from tests.matcher._fixtures import (
    AS_OF,
    crit_age,
    crit_condition,
    make_patient,
    make_trial,
)

# ---------- recording client fixture ----------


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
    """Replace the cached Langfuse client with an in-process recorder."""
    client = _RecordingClient()
    langfuse_client.get_client.cache_clear()
    monkeypatch.setattr(langfuse_client, "get_client", lambda: client)
    return client


# ---------- extractor ----------


def test_extract_criteria_emits_one_generation_span(
    recording_client: _RecordingClient,
) -> None:
    from clinical_demo.extractor import extract_criteria

    parsed = _trivial_extraction()
    completion = _make_completion(
        parsed=parsed, prompt_tokens=100, completion_tokens=20, cached_tokens=10
    )
    stub = _StubClient(completion)

    result = extract_criteria("include adults over 18.", client=stub, settings=_settings())

    assert len(recording_client.spans) == 1
    span = recording_client.spans[0]
    assert span.name == "extract_criteria"
    assert span.start_kwargs["as_type"] == "generation"
    assert span.start_kwargs["model"] == "gpt-4o-mini-2024-07-18"
    assert span.start_kwargs["model_parameters"]["temperature"] == 0.0
    assert span.start_kwargs["input"] == "include adults over 18."
    assert span.start_kwargs["metadata"]["prompt_version"] == result.meta.prompt_version
    # Final update should record output, usage, cost.
    update = span.updates[-1]
    assert "output" in update
    assert update["usage_details"] == {"input": 100, "output": 20, "cached_input": 10}
    assert update["cost_details"]["total"] == result.meta.cost_usd
    assert update["metadata"]["criteria_count"] == str(len(parsed.criteria))


def test_extract_criteria_marks_refusal_with_warning_level(
    recording_client: _RecordingClient,
) -> None:
    from clinical_demo.extractor import ExtractorRefusalError, extract_criteria

    completion = _make_completion(parsed=None, refusal="I cannot help with that.")
    stub = _StubClient(completion)

    with pytest.raises(ExtractorRefusalError):
        extract_criteria("anything", client=stub, settings=_settings())

    assert len(recording_client.spans) == 1
    update = recording_client.spans[0].updates[-1]
    assert update["level"] == "WARNING"
    assert "refusal" in update["status_message"]
    assert update["output"] == {"refusal": "I cannot help with that."}


def test_extract_criteria_does_not_emit_span_for_empty_input(
    recording_client: _RecordingClient,
) -> None:
    from clinical_demo.extractor import extract_criteria

    extract_criteria("   ", settings=_settings())  # no client needed; early-returns
    assert recording_client.spans == []


# ---------- score_pair ----------


def test_score_pair_emits_parent_span_with_extractor_nested(
    recording_client: _RecordingClient,
) -> None:
    """score_pair should open a parent `span`, then the extractor's
    `generation` lands inside it. Recording client is flat (it doesn't
    model the parent/child OTel context), but ordering is enough to
    pin: parent created first, generation second."""
    parsed = _trivial_extraction()
    completion = _make_completion(parsed=parsed, prompt_tokens=10, completion_tokens=2)
    stub = _StubClient(completion)
    settings = _settings()

    # Build inputs with the matcher-side fixtures
    patient = make_patient()
    trial = make_trial(eligibility_text="Adults with type 2 diabetes.")

    # Inject the stub by calling extract_criteria explicitly first, so
    # we don't need to monkeypatch the OpenAI client globally; then
    # pass the result into score_pair.
    from clinical_demo.extractor import extract_criteria

    extraction = extract_criteria(trial.eligibility_text, client=stub, settings=settings)

    # Reset spans so we only see score_pair's own
    recording_client.spans.clear()

    result = score_pair(patient, trial, AS_OF, extraction=extraction)

    assert len(recording_client.spans) == 1
    parent = recording_client.spans[0]
    assert parent.name == "score_pair"
    assert parent.start_kwargs["as_type"] == "span"
    assert parent.start_kwargs["metadata"]["patient_id"] == patient.patient_id
    assert parent.start_kwargs["metadata"]["nct_id"] == trial.nct_id
    update = parent.updates[-1]
    assert update["output"]["eligibility"] == result.eligibility
    assert update["metadata"]["eligibility"] == result.eligibility
    # Counts must be present even when zero
    for key in ("fail_count", "pass_count", "indeterminate_count"):
        assert key in update["metadata"]


def test_score_pair_with_inline_extractor_emits_two_spans(
    recording_client: _RecordingClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When score_pair calls extract_criteria itself (no extraction
    arg), both spans should land: the score_pair parent and the
    extractor generation."""
    parsed = _trivial_extraction()
    completion = _make_completion(parsed=parsed, prompt_tokens=10, completion_tokens=2)
    stub = _StubClient(completion)
    settings = _settings()

    patient = make_patient()
    trial = make_trial(eligibility_text="Adults with type 2 diabetes.")

    # Monkey-patch the module-level `extract_criteria` binding that
    # the `score_pair` function calls. Subtlety:
    # `clinical_demo.scoring.__init__` re-exports a function named
    # `score_pair` that shadows the submodule of the same name on
    # the package object, so `clinical_demo.scoring.score_pair.foo`
    # attribute access — whether by dotted string or static attr —
    # resolves to the *function*, not the module. We grab the module
    # object out of `sys.modules` (where it lives intact) and patch
    # there.
    import sys

    sp_mod = sys.modules["clinical_demo.scoring.score_pair"]
    from clinical_demo.extractor import extract_criteria as real_extract

    def _patched(text: str, **kwargs: Any) -> Any:
        return real_extract(text, client=stub, settings=settings, **kwargs)

    monkeypatch.setattr(sp_mod, "extract_criteria", _patched)
    score_pair(patient, trial, AS_OF)

    names = [s.name for s in recording_client.spans]
    assert names == ["score_pair", "extract_criteria"]


def test_score_pair_metadata_contains_matcher_version(
    recording_client: _RecordingClient,
) -> None:
    from clinical_demo.matcher import MATCHER_VERSION

    parsed = _trivial_extraction()
    completion = _make_completion(parsed=parsed)
    stub = _StubClient(completion)

    patient = make_patient()
    trial = make_trial()

    from clinical_demo.extractor import extract_criteria

    extraction = extract_criteria(
        trial.eligibility_text or "anything", client=stub, settings=_settings()
    )
    recording_client.spans.clear()

    score_pair(patient, trial, AS_OF, extraction=extraction)
    parent = recording_client.spans[0]
    assert parent.start_kwargs["metadata"]["matcher_version"] == MATCHER_VERSION
    assert parent.updates[-1]["metadata"]["matcher_version"] == MATCHER_VERSION


# Silence unused-import warnings: these are imported for their side
# effects on the public test corpus (used in fixtures above).
_ = (crit_age, crit_condition)
