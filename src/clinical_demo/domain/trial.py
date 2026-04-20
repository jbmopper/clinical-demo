"""Trial-side domain model.

A `Trial` is the internal representation of a clinical trial protocol
sufficient to drive eligibility matching. Fields are deliberately minimal
for v0: anything the extractor or matcher will care about must be present;
everything else is left in the source JSON and parsed only on demand.

Pre-structured fields from the source (age range, sex, healthy volunteers)
are kept *as the source provides them* — strings and booleans, not parsed
into ints — because CT.gov uses out-of-band conventions like "N/A" and
"Child" that we don't want to silently lose.

The free-text `eligibility_text` is the input to our criterion extractor.
That blob is the messy half of every trial protocol; structured fields
are the clean half.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# CT.gov enums we care about; not exhaustive, used for documentation.
SponsorClass = Literal[
    "INDUSTRY", "NIH", "FED", "OTHER_GOV", "INDIV", "NETWORK", "OTHER", "UNKNOWN"
]
StudyStatus = Literal[
    "RECRUITING",
    "ACTIVE_NOT_RECRUITING",
    "COMPLETED",
    "TERMINATED",
    "WITHDRAWN",
    "ENROLLING_BY_INVITATION",
    "NOT_YET_RECRUITING",
    "SUSPENDED",
    "UNKNOWN",
]
TrialSex = Literal["ALL", "MALE", "FEMALE"]


class Trial(BaseModel):
    """A single clinical trial protocol, source-agnostic."""

    nct_id: str
    title: str
    phase: list[str] = Field(default_factory=list)
    overall_status: str
    conditions: list[str] = Field(default_factory=list)
    sponsor_name: str
    sponsor_class: str
    intervention_types: list[str] = Field(default_factory=list)
    eligibility_text: str
    minimum_age: str | None = None
    maximum_age: str | None = None
    sex: str = "ALL"
    healthy_volunteers: bool = False
