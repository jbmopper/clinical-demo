"""Extraction node: trial eligibility text → ExtractedCriteria + PatientProfile.

Two responsibilities:
  1. Resolve the extraction. If the caller pre-supplied one (cache
     hit, replay, eval harness), use it; otherwise call the LLM
     extractor. This is the same `extraction is None ?
     extract_criteria : passthrough` rule the imperative
     `score_pair()` uses, lifted to a node.
  2. Build the `PatientProfile` snapshot once, here, so each
     fan-out match branch shares it instead of re-instantiating.

The node returns a partial state update; LangGraph merges it onto
the channel.
"""

from __future__ import annotations

from typing import Any

from ...extractor.extractor import extract_criteria
from ...profile import PatientProfile
from ...settings import Settings
from ..state import ScoringState


def extract_node(
    state: ScoringState,
    *,
    client: Any | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Resolve extraction (or use supplied), build patient profile.

    `client` and `settings` are kwargs the graph builder threads
    through via a closure / partial — they exist so tests can inject
    a stub OpenAI client without monkey-patching globals."""
    extraction = state.get("extraction")
    if extraction is None:
        extraction = extract_criteria(
            state["trial"].eligibility_text,
            client=client,
            settings=settings,
        )

    profile = PatientProfile(state["patient"], state["as_of"])

    return {"extraction": extraction, "profile": profile}
