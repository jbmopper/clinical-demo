"""Pydantic types for critic findings and revisions.

Kept in a leaf module (no graph / node imports) so it can be
imported from `state.py` without a circular dep. The actual critic
node and prompt live in `nodes/critic.py`; this file is just the
data carrier.

Why these are Pydantic, not TypedDict
------------------------------------
The graph state itself is `TypedDict` (see state.py docstring), but
the *contents* of state slots are durable, validated domain
objects. A `CriticFinding` is read by the revise node, the rollup
metadata, the trace spans, and the eval harness in 2.3 — every one
of those benefits from validation + serialization, neither of which
TypedDict offers. The `total=False` argument (avoid revalidating
mid-fan-in) doesn't apply here because findings are never built up
incrementally; they're emitted whole by the critic node.

Closed enums (severity, kind, action) are deliberate. The eval
harness will pivot on these — adding a new finding kind should be a
visible code change and a PLAN entry, not a silent string drift.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# ---- finding ----

CriticFindingKind = Literal[
    "low_confidence_indeterminate",
    "extraction_disagreement_with_text",
    "polarity_smell",
]
"""Closed set of finding kinds the critic can emit (v0).

Each kind maps deterministically to a `CriticActionKind` in the
revise node's dispatcher. Adding a new kind is a code change in
both files and a PLAN decision (D-50+).
"""

CriticSeverity = Literal["info", "warning", "blocker"]
"""How serious the finding is.

  - `info`     — recorded but does not trigger a revision (used by
                 the eval harness to track findings the critic
                 noticed but chose not to act on).
  - `warning`  — triggers a revision if the budget allows.
  - `blocker`  — would trigger a human checkpoint when one is
                 enabled. v0 doesn't emit `blocker` from the LLM
                 critic; reserved for the heuristic critic and
                 future human-in-the-loop policies.
"""


class CriticFinding(BaseModel):
    """One issue the critic identified with a single criterion's verdict."""

    criterion_index: int = Field(
        ge=0,
        description=(
            "Index into the extraction's criteria list. Same index "
            "the matcher branches use; lets the revise node target a "
            "single re-run without rebuilding state."
        ),
    )
    kind: CriticFindingKind
    severity: CriticSeverity
    rationale: str = Field(
        max_length=500,
        description=(
            "One sentence explaining the issue. Surfaced in the "
            "revise span's input so the LLM matcher (when called) "
            "sees what to focus on."
        ),
    )

    @property
    def fingerprint(self) -> tuple[int, CriticFindingKind]:
        """Identity for the no-progress detector.

        Two iterations producing the same `(criterion_index, kind)`
        set are treated as 'critic stuck' and we terminate the loop.
        Severity is intentionally excluded — promoting the same
        finding from `info` to `warning` shouldn't reset the
        no-progress counter."""
        return (self.criterion_index, self.kind)


# ---- revision ----

CriticActionKind = Literal[
    "rerun_match_with_focus",
    "rerun_extract_for_criterion",
    "flip_polarity_and_rematch",
]
"""Closed set of revisions the revise node knows how to perform (v0).

Mapped 1:1 from `CriticFindingKind`:
  - low_confidence_indeterminate     → rerun_match_with_focus
  - extraction_disagreement_with_text → rerun_match_with_focus in v0
  - polarity_smell                    → flip_polarity_and_rematch

`rerun_extract_for_criterion` is reserved for the future split where
extraction-disagreement findings trigger a scoped extractor pass.
"""


class CriticRevision(BaseModel):
    """Audit row recording one revision the revise node performed."""

    criterion_index: int = Field(ge=0)
    iteration: int = Field(
        ge=1, description="1-indexed critic iteration that produced this revision."
    )
    finding_kind: CriticFindingKind
    action: CriticActionKind
    rationale: str = Field(
        max_length=500,
        description="One sentence describing what the revise node actually did.",
    )
    verdict_changed: bool = Field(
        description=(
            "Did the new verdict differ from the previous one? Useful "
            "for an eval pivot: 'critic interventions that actually "
            "changed an answer.'"
        ),
    )


__all__ = [
    "CriticActionKind",
    "CriticFinding",
    "CriticFindingKind",
    "CriticRevision",
    "CriticSeverity",
]
