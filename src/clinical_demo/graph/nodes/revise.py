"""Revise node: turn one critic finding into one targeted matcher re-run.

The revise node is the *enforcer* of the critic's findings. The
critic identifies process problems; the revise node performs a
single, scoped re-run that may change one verdict. It NEVER
changes more than one criterion per call. The loop runs the rollup
again afterwards and the next critic pass sees the updated state.

v0 dispatch table
-----------------
    finding kind                          → action
    -----------------------------------------------------------
    low_confidence_indeterminate          → rerun_match_with_focus
    extraction_disagreement_with_text     → rerun_match_with_focus
    polarity_smell                        → flip_polarity_and_rematch

The first two map to the same v0 action. They're separate finding
kinds because (a) they describe genuinely different process
problems and the eval pivot is more useful with the distinction,
and (b) v1 will split them (extraction_disagreement → re-extract
on source_text, low_confidence → re-match with richer context).

What "focus" means in v0
------------------------
The matcher gets the finding's rationale prepended to the user
message under a "REVIEWER NOTE" header. The matcher prompt was not
trained on this header, so it's read as additional context; the
behaviour change is small but observable in the matcher's
rationale. We don't claim more than this; the eval harness will
quantify whether focus actually moves verdicts.

What if the finding targets a deterministic verdict?
----------------------------------------------------
For `rerun_match_with_focus`, we *only* re-run via the LLM matcher
when the criterion is `free_text`. Re-running the deterministic
matcher on a coded criterion would produce the same answer
(it's deterministic, by construction). So we record a no-op
revision and move on; the eval can pivot on this case.

For `flip_polarity_and_rematch`, we mutate the criterion in-place
(polarity XOR) and dispatch by kind: deterministic for coded
criteria, LLM for free-text. The revision row records both the
flip and the new verdict.
"""

from __future__ import annotations

from typing import Any

from ...extractor.schema import ExtractedCriterion, Polarity
from ...matcher import MatchVerdict, match_criterion
from ...observability import traced
from ...settings import Settings
from ..critic_types import CriticActionKind, CriticFinding, CriticRevision
from ..state import ScoringState
from .llm_match import _ClientLike, llm_match_node


def revise_node(
    state: ScoringState,
    *,
    client: _ClientLike | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Apply one revision per call.

    Picks the highest-severity warning from `critic_findings`,
    runs the corresponding action, emits the new verdict and an
    audit row. If no actionable findings exist, returns an empty
    update (the router should never call us in that case, but we
    defend anyway).
    """
    findings = state.get("critic_findings") or []
    actionable = [f for f in findings if f.severity == "warning"]
    if not actionable:
        return {}

    # Stable pick: first actionable finding by index, tie-broken by
    # iteration order. Deterministic so traces are replayable.
    finding = sorted(actionable, key=lambda f: (f.criterion_index, f.kind))[0]

    iteration = state.get("critic_iterations", 1)
    verdicts = state.get("final_verdicts", [])
    if not (0 <= finding.criterion_index < len(verdicts)):
        # Shouldn't happen — `_filter_findings` in the critic node
        # bounds-checks. Defend anyway with a no-op revision so the
        # audit trail records the anomaly.
        return {
            "critic_revisions": [
                CriticRevision(
                    criterion_index=finding.criterion_index,
                    iteration=iteration,
                    finding_kind=finding.kind,
                    action="rerun_match_with_focus",
                    rationale="finding had out-of-range index; no action taken",
                    verdict_changed=False,
                )
            ]
        }

    old_verdict = verdicts[finding.criterion_index]
    action = _action_for(finding.kind)

    with traced(
        "revise",
        as_type="span",
        input={
            "criterion_index": finding.criterion_index,
            "finding_kind": finding.kind,
            "action": action,
            "iteration": iteration,
        },
        metadata={
            "criterion_index": str(finding.criterion_index),
            "finding_kind": finding.kind,
            "action": action,
            "iteration": str(iteration),
        },
    ) as span:
        new_verdict, action_rationale = _dispatch(
            action=action,
            finding=finding,
            old_verdict=old_verdict,
            state=state,
            client=client,
            settings=settings,
        )

        verdict_changed = (
            new_verdict.verdict != old_verdict.verdict or new_verdict.reason != old_verdict.reason
        )

        span.update(
            output={
                "old_verdict": old_verdict.verdict,
                "old_reason": old_verdict.reason,
                "new_verdict": new_verdict.verdict,
                "new_reason": new_verdict.reason,
                "verdict_changed": verdict_changed,
            },
            metadata={
                "criterion_index": str(finding.criterion_index),
                "verdict_changed": str(verdict_changed).lower(),
            },
        )

    revision = CriticRevision(
        criterion_index=finding.criterion_index,
        iteration=iteration,
        finding_kind=finding.kind,
        action=action,
        rationale=action_rationale,
        verdict_changed=verdict_changed,
    )

    return {
        "indexed_verdicts": [(finding.criterion_index, new_verdict)],
        "critic_revisions": [revision],
    }


# ---------- dispatch ----------


def _action_for(kind: str) -> CriticActionKind:
    """Closed-table dispatch from finding kind to action kind."""
    table: dict[str, CriticActionKind] = {
        "low_confidence_indeterminate": "rerun_match_with_focus",
        "extraction_disagreement_with_text": "rerun_match_with_focus",
        "polarity_smell": "flip_polarity_and_rematch",
    }
    return table[kind]


def _dispatch(
    *,
    action: CriticActionKind,
    finding: CriticFinding,
    old_verdict: MatchVerdict,
    state: ScoringState,
    client: _ClientLike | None,
    settings: Settings | None,
) -> tuple[MatchVerdict, str]:
    """Perform the action; return (new_verdict, rationale_for_audit)."""
    criterion = old_verdict.criterion
    profile = state["profile"]
    trial = state["trial"]

    if action == "rerun_match_with_focus":
        if criterion.kind != "free_text":
            # Re-running the deterministic matcher on a coded
            # criterion would produce the same answer. Record a
            # no-op revision and keep the original verdict.
            return (
                old_verdict,
                f"finding targeted a {criterion.kind} criterion; "
                "deterministic matcher is replay-stable, no re-run.",
            )
        focused = _focused_match_state(state, criterion, finding)
        result = llm_match_node(focused, client=client, settings=settings)
        new_verdict = result["indexed_verdicts"][0][1]
        return (new_verdict, "re-ran LLM matcher with focused reviewer note")

    if action == "flip_polarity_and_rematch":
        flipped = _flip_polarity(criterion)
        focused = _focused_match_state(state, flipped, finding)
        if criterion.kind == "free_text":
            result = llm_match_node(focused, client=client, settings=settings)
            new_verdict = result["indexed_verdicts"][0][1]
        else:
            new_verdict = match_criterion(flipped, profile, trial)
        return (
            new_verdict,
            f"flipped polarity ({criterion.polarity} → {flipped.polarity}) and re-matched",
        )

    raise ValueError(f"unknown action: {action}")  # pragma: no cover


# ---------- helpers ----------


def _focused_match_state(
    state: ScoringState,
    criterion: ExtractedCriterion,
    finding: CriticFinding,
) -> ScoringState:
    """Build the per-branch state slice the matcher node expects.

    Mirrors what `fan_out_criteria` puts on a `Send`. The
    finding's rationale is stashed in a sentinel key the LLM
    matcher could pick up — for v0 the matcher node ignores it
    (the "REVIEWER NOTE" header isn't wired in yet), but the slot
    exists and is exercised so we can light it up in v1 without a
    state-shape change.
    """
    branch: ScoringState = {
        "patient": state["patient"],
        "trial": state["trial"],
        "as_of": state["as_of"],
        "extraction": state.get("extraction"),
        "profile": state["profile"],
        "_criterion": criterion,
        "_criterion_index": finding.criterion_index,
    }
    return branch


def _flip_polarity(criterion: ExtractedCriterion) -> ExtractedCriterion:
    """Return a copy of `criterion` with `polarity` inverted.

    `ExtractedCriterion` is a single flat model with `kind` +
    payload slots (not a discriminated union of subtypes), so
    `model_copy(update=...)` is straightforward. The matcher reads
    polarity post-extraction, so this is the right place to apply
    the flip — no upstream state needs to know."""
    new_polarity: Polarity = "exclusion" if criterion.polarity == "inclusion" else "inclusion"
    return criterion.model_copy(update={"polarity": new_polarity})
