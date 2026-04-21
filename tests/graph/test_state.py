"""Tests for the ScoringState reducers."""

from __future__ import annotations

import operator
from typing import Annotated, get_type_hints

from clinical_demo.graph.critic_types import CriticRevision
from clinical_demo.graph.state import ScoringState, merge_indexed_verdicts


def test_indexed_verdicts_uses_replace_by_index_reducer() -> None:
    """The reducer is the sole concurrency contract between the
    matcher branches AND the critic revise loop. Pin its identity
    so a refactor that swaps it for `operator.add` trips a test —
    that swap would silently break revise (old + new tuples both
    landing, the rollup's stable sort arbitrarily picking one)."""
    hints = get_type_hints(ScoringState, include_extras=True)
    annotation = hints["indexed_verdicts"]
    metadata = getattr(annotation, "__metadata__", ())
    assert metadata == (merge_indexed_verdicts,)


def test_merge_indexed_verdicts_concat_when_no_overlap() -> None:
    # The reducer's contract is structural: it operates on
    # `(int, value)` tuples regardless of value type. Using plain
    # strings here keeps the test focused on the merge semantics
    # without a fixture wall to build real MatchVerdict objects.
    from typing import cast

    from clinical_demo.matcher import MatchVerdict

    a = cast(list[tuple[int, MatchVerdict]], [(0, "v0"), (2, "v2")])
    b = cast(list[tuple[int, MatchVerdict]], [(1, "v1")])
    merged = merge_indexed_verdicts(a, b)
    expected = cast(list[tuple[int, MatchVerdict]], [(0, "v0"), (2, "v2"), (1, "v1")])
    assert merged == expected


def test_merge_indexed_verdicts_replaces_overlapping_index() -> None:
    """The right-hand side wins for any index it touches.

    This is the property the critic revise loop relies on: when
    revise produces a new verdict for index N, it must SUPERSEDE
    the old one rather than coexist with it."""
    from typing import cast

    from clinical_demo.matcher import MatchVerdict

    a = cast(
        list[tuple[int, MatchVerdict]],
        [(0, "old0"), (1, "old1"), (2, "old2")],
    )
    b = cast(list[tuple[int, MatchVerdict]], [(1, "new1")])
    merged = merge_indexed_verdicts(a, b)
    by_index = {idx: v for idx, v in merged}
    expected_by_index = cast(dict[int, MatchVerdict], {0: "old0", 1: "new1", 2: "old2"})
    assert by_index == expected_by_index
    assert len(merged) == 3


def test_merge_indexed_verdicts_handles_empty_inputs() -> None:
    from typing import cast

    from clinical_demo.matcher import MatchVerdict

    a = cast(list[tuple[int, MatchVerdict]], [(0, "x")])
    expected = cast(list[tuple[int, MatchVerdict]], [(0, "x")])
    assert merge_indexed_verdicts(a, []) == expected
    assert merge_indexed_verdicts([], a) == expected
    assert merge_indexed_verdicts([], []) == []


def test_critic_revisions_uses_add_reducer() -> None:
    """`critic_revisions` is append-only across iterations (each
    iteration's revisions are added to the cumulative audit trail).
    `operator.add` is the right reducer; pin it."""
    hints = get_type_hints(ScoringState, include_extras=True)
    annotation = hints["critic_revisions"]
    metadata = getattr(annotation, "__metadata__", ())
    assert metadata == (operator.add,)


def test_critic_revision_fingerprint_excludes_severity() -> None:
    """Two revisions for the same `(index, kind)` pair should be
    indistinguishable for no-progress detection regardless of
    severity. The CriticFinding's `.fingerprint` is what the router
    actually uses; revision rows are the audit trail."""
    from clinical_demo.graph.critic_types import CriticFinding

    a = CriticFinding(criterion_index=0, kind="polarity_smell", severity="warning", rationale="r")
    b = CriticFinding(criterion_index=0, kind="polarity_smell", severity="info", rationale="r")
    assert a.fingerprint == b.fingerprint


def test_state_is_typeddict_total_false() -> None:
    assert ScoringState.__total__ is False


def test_critic_state_keys_present() -> None:
    """Defensive: a refactor that drops `critic_iterations` or
    `_critic_prev_fingerprints` would make the router silently
    treat every iteration as iteration 0. Make that visible."""
    keys = set(ScoringState.__annotations__.keys())
    for key in (
        "critic_iterations",
        "critic_findings",
        "critic_revisions",
        "_critic_prev_fingerprints",
    ):
        assert key in keys, key


def test_critic_revision_records_required_audit_fields() -> None:
    """One row, all the dashboard pivots: criterion index, iteration,
    finding kind that triggered it, action taken, did-it-change."""
    rev = CriticRevision(
        criterion_index=0,
        iteration=1,
        finding_kind="polarity_smell",
        action="flip_polarity_and_rematch",
        rationale="flipped per finding",
        verdict_changed=True,
    )
    dumped = rev.model_dump()
    for k in (
        "criterion_index",
        "iteration",
        "finding_kind",
        "action",
        "rationale",
        "verdict_changed",
    ):
        assert k in dumped


_ = Annotated
