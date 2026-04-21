"""Tests for the deterministic match node.

The node is intentionally a thin shim — its correctness is the
matcher's correctness, which has its own 79-test suite. These tests
pin the *shim*: index plumbing, return shape, no leakage of branch-
internal state into the result.
"""

from __future__ import annotations

from clinical_demo.extractor.schema import ExtractedCriterion
from clinical_demo.graph.nodes.deterministic import deterministic_match_node
from clinical_demo.graph.state import ScoringState
from clinical_demo.matcher import MATCHER_VERSION
from clinical_demo.profile import PatientProfile
from tests.matcher._fixtures import (
    AS_OF,
    crit_age,
    make_patient,
    make_trial,
)


def _branch_state(criterion: ExtractedCriterion, index: int) -> ScoringState:
    """Build the per-branch state slice the fan-out would emit."""
    patient = make_patient()
    return ScoringState(
        patient=patient,
        trial=make_trial(),
        as_of=AS_OF,
        profile=PatientProfile(patient, AS_OF),
        _criterion=criterion,
        _criterion_index=index,
    )


def test_emits_indexed_verdict_with_correct_index() -> None:
    state = _branch_state(crit_age(minimum_years=18.0), index=7)
    update = deterministic_match_node(state)
    assert "indexed_verdicts" in update
    assert len(update["indexed_verdicts"]) == 1
    index, verdict = update["indexed_verdicts"][0]
    assert index == 7
    assert verdict.criterion.kind == "age"


def test_verdict_carries_matcher_version() -> None:
    state = _branch_state(crit_age(minimum_years=18.0), index=0)
    update = deterministic_match_node(state)
    _, verdict = update["indexed_verdicts"][0]
    assert verdict.matcher_version == MATCHER_VERSION


def test_emits_no_other_state_keys() -> None:
    """The shim returns only the reducer slot. Pin it so a refactor
    that returns extra keys (which would clobber the parent state)
    trips a test."""
    state = _branch_state(crit_age(), index=0)
    update = deterministic_match_node(state)
    assert set(update.keys()) == {"indexed_verdicts"}
