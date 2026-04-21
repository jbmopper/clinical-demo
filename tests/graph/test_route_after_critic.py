"""Unit tests for the post-critic router.

Covers the four termination paths in `route_after_critic`:
  1. No actionable findings → finalize.
  2. Iteration budget exhausted → finalize.
  3. No-progress (current fingerprints == previous) → finalize.
  4. Otherwise → revise.

Pure function on state; no LLM, no graph compilation.
"""

from __future__ import annotations

from clinical_demo.graph.critic_types import CriticFinding
from clinical_demo.graph.nodes.route import route_after_critic

from ._fixtures import state_with_verdicts


def _finding(
    *, idx: int = 0, kind: str = "polarity_smell", severity: str = "warning"
) -> CriticFinding:
    return CriticFinding(
        criterion_index=idx,
        kind=kind,  # type: ignore[arg-type]
        severity=severity,  # type: ignore[arg-type]
        rationale="t",
    )


# ---- termination: no actionable findings ----


def test_empty_findings_routes_to_finalize() -> None:
    state = state_with_verdicts([], critic_findings_in=[])
    assert route_after_critic(state, max_iterations=2) == "finalize"


def test_only_info_findings_routes_to_finalize() -> None:
    state = state_with_verdicts([], critic_findings_in=[_finding(severity="info")])
    assert route_after_critic(state, max_iterations=2) == "finalize"


# ---- termination: budget exhausted ----


def test_at_budget_routes_to_finalize_even_with_warnings() -> None:
    state = state_with_verdicts(
        [],
        critic_findings_in=[_finding(severity="warning")],
        critic_iterations_in=2,
    )
    assert route_after_critic(state, max_iterations=2) == "finalize"


def test_below_budget_with_warnings_routes_to_revise() -> None:
    state = state_with_verdicts(
        [],
        critic_findings_in=[_finding(severity="warning")],
        critic_iterations_in=1,
    )
    assert route_after_critic(state, max_iterations=2) == "revise"


# ---- termination: no progress ----


def test_repeated_fingerprints_routes_to_finalize() -> None:
    """Critic emitted the same finding set as last iteration → stuck."""
    state = state_with_verdicts(
        [],
        critic_findings_in=[_finding(idx=0, kind="polarity_smell")],
        critic_iterations_in=1,
    )
    state["_critic_prev_fingerprints"] = {(0, "polarity_smell")}

    assert route_after_critic(state, max_iterations=5) == "finalize"


def test_different_fingerprints_routes_to_revise() -> None:
    state = state_with_verdicts(
        [],
        critic_findings_in=[_finding(idx=0, kind="polarity_smell")],
        critic_iterations_in=1,
    )
    state["_critic_prev_fingerprints"] = {(1, "low_confidence_indeterminate")}

    assert route_after_critic(state, max_iterations=5) == "revise"


def test_severity_change_does_not_count_as_progress() -> None:
    """Promoting a finding from info to warning shouldn't be enough
    to count as progress — fingerprints exclude severity."""
    state = state_with_verdicts(
        [],
        critic_findings_in=[_finding(idx=0, kind="polarity_smell", severity="warning")],
        critic_iterations_in=1,
    )
    state["_critic_prev_fingerprints"] = {(0, "polarity_smell")}

    assert route_after_critic(state, max_iterations=5) == "finalize"
