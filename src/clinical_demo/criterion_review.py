"""Small curated classifiers for criteria that need offline review."""

from __future__ import annotations

import re

_CLINICAL_TRIAL_PARTICIPATION_RE = re.compile(
    r"\b(?:participat(?:e|es|ed|ing)|enroll(?:ed|ment)?|taking part)\b"
    r".{0,80}\b(?:clinical trial|clinical study|another study)\b",
    re.IGNORECASE,
)


def is_interview_required_criterion_text(text: str) -> bool:
    """Return true when a criterion asks for information usually outside FHIR."""

    normalized = " ".join(text.split())
    return bool(_CLINICAL_TRIAL_PARTICIPATION_RE.search(normalized))


__all__ = ["is_interview_required_criterion_text"]
