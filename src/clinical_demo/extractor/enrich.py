"""Post-extraction enrichment from CT.gov structured fields.

The criterion extractor reads only `Trial.eligibility_text`, but
trials publish age/sex bounds in CT.gov structured fields
(`minimumAge`, `maximumAge`, `sex`) that the eligibility prose
sometimes restates and sometimes does not. When those bounds live
*only* in the structured fields, the extractor produces no `age` /
`sex` criterion -- and the layer-1 eval reports the cell as
`missing` even though the matcher could have scored it trivially
against the patient profile.

This module fills that gap *deterministically*, after extraction,
**without** touching the prompt:

- Scan the extracted criteria. If one of `kind="age"` / `kind="sex"`
  is already present, leave it alone (the LLM is closer to the
  eligibility text and may have nuanced the bounds, e.g. "18-65
  except <70 with ECOG=0"; we don't override).
- Otherwise, parse the corresponding structured field and inject
  a synthetic `ExtractedCriterion` with a sentinel `source_text`
  (`"[ct.gov structured field: ...]"`) so reviewers and the
  citation UI can tell the difference between LLM-extracted and
  CT.gov-injected criteria at a glance.

Why a post-processor and not a prompt change:

1. Determinism. The trial designer asserts "minimum age = 18" as
   structured data; an LLM re-interpreting that string is silly
   and lossy. Post-process keeps it canonical.
2. Cache reuse. Bumping `PROMPT_VERSION` invalidates the D-66
   extractor cache (30 cached extractions per current curated
   set). The Rule-13 patch just did this; doing it again for a
   purely deterministic mechanic would be wasteful.
3. Layered concerns. The extractor does free-text -> structured;
   this layer enforces invariants from a different (canonical)
   source. Wiring them through one prompt would conflate them.
4. Provenance. Anyone reading the enriched extraction can tell
   which criteria came from the LLM vs. from the CT.gov
   structured fields by inspecting `source_text` -- impossible
   if the LLM had absorbed the structured fields into its
   typical-looking output.

Sex="ALL" is a no-op: it does not constrain the matcher and an
injected `kind="sex"` row with `sex="ALL"` would just clutter the
verdict list. Sex="MALE"/"FEMALE" inject normally.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .schema import (
    AgeCriterion,
    ExtractedCriteria,
    ExtractedCriterion,
    SexCriterion,
)

if TYPE_CHECKING:
    from ..domain.trial import Trial

log = logging.getLogger(__name__)

# Sentinel marker used in `source_text` for criteria injected by
# this module. Tested as a stable string so the citation UI and
# downstream tooling can detect provenance without a separate flag.
INJECTED_SOURCE_PREFIX = "[ct.gov structured field"


def enrich_with_structured_fields(
    extracted: ExtractedCriteria,
    trial: Trial,
) -> ExtractedCriteria:
    """Inject synthetic age/sex criteria from CT.gov structured fields.

    No-op when the LLM already extracted criteria of those kinds
    (the LLM saw the same trial's eligibility text and may have
    encoded nuance the structured field can't); no-op when the
    structured field is missing, "N/A", "ALL", or otherwise
    uninformative for the matcher.

    Returns a *new* `ExtractedCriteria` (does not mutate input).
    The returned criteria list preserves original order with any
    injected rows appended at the end -- keeps citation indices
    on the extractor-extracted criteria stable across enrichment.
    """
    new_rows: list[ExtractedCriterion] = []
    has_age = any(c.kind == "age" for c in extracted.criteria)
    has_sex = any(c.kind == "sex" for c in extracted.criteria)

    if not has_age:
        age_row = _build_age_row(trial)
        if age_row is not None:
            new_rows.append(age_row)

    if not has_sex:
        sex_row = _build_sex_row(trial)
        if sex_row is not None:
            new_rows.append(sex_row)

    if not new_rows:
        return extracted

    return ExtractedCriteria(
        criteria=[*extracted.criteria, *new_rows],
        metadata=extracted.metadata,
    )


def _build_age_row(trial: Trial) -> ExtractedCriterion | None:
    """Synthesize a `kind='age'` row from `trial.minimum_age` /
    `trial.maximum_age`, or return None if neither parses to a
    usable bound."""
    min_years = _parse_ctgov_age_string(trial.minimum_age)
    max_years = _parse_ctgov_age_string(trial.maximum_age)
    if min_years is None and max_years is None:
        return None

    parts: list[str] = []
    if trial.minimum_age:
        parts.append(f"minimumAge={trial.minimum_age}")
    if trial.maximum_age:
        parts.append(f"maximumAge={trial.maximum_age}")
    source_text = f"{INJECTED_SOURCE_PREFIX}: {', '.join(parts)}]"

    return ExtractedCriterion(
        kind="age",
        polarity="inclusion",
        source_text=source_text,
        negated=False,
        mood="actual",
        age=AgeCriterion(minimum_years=min_years, maximum_years=max_years),
        sex=None,
        condition=None,
        medication=None,
        measurement=None,
        temporal_window=None,
        free_text=None,
        mentions=[],
    )


def _build_sex_row(trial: Trial) -> ExtractedCriterion | None:
    """Synthesize a `kind='sex'` row from `trial.sex`. Skips
    `ALL` (vacuous; the matcher's ALL branch would always pass)
    and unrecognized values."""
    sex = (trial.sex or "").strip().upper()
    if sex not in ("MALE", "FEMALE"):
        return None

    source_text = f"{INJECTED_SOURCE_PREFIX}: sex={trial.sex}]"
    return ExtractedCriterion(
        kind="sex",
        polarity="inclusion",
        source_text=source_text,
        negated=False,
        mood="actual",
        age=None,
        sex=SexCriterion(sex=sex),  # type: ignore[arg-type]
        condition=None,
        medication=None,
        measurement=None,
        temporal_window=None,
        free_text=None,
        mentions=[],
    )


# CT.gov age strings are ASCII-numeric followed by a unit word:
# "18 Years", "6 Months", "30 Days", "2 Weeks". "N/A" and empty
# mean "no bound." Categorical labels like "Child" / "Adult" /
# "Senior" are intentionally rejected -- they don't carry a
# precise numeric bound and silently mapping (e.g.) "Adult" to
# 18 would be the same kind of wrong-default the soft-fail
# discipline (D-65/D-66) exists to avoid.
_UNIT_TO_YEARS: dict[str, float] = {
    "year": 1.0,
    "month": 1.0 / 12.0,
    "week": 1.0 / 52.0,
    "day": 1.0 / 365.0,
}


def _parse_ctgov_age_string(value: str | None) -> float | None:
    """Convert a CT.gov age string ('18 Years', '6 Months') to years.

    Returns None for anything we can't parse with confidence:
    empty / None / 'N/A' / categorical labels. The caller treats
    None as "no bound for this side" (same shape as the seed
    labeler's missing-cell logic), so a parse failure here just
    means we won't synthesize a bound on that side -- never that
    we silently invent one.
    """
    if not value:
        return None
    s = value.strip()
    if not s or s.upper() == "N/A":
        return None

    parts = s.split()
    if len(parts) != 2:
        log.debug("CT.gov age string %r is not 'N Unit'; skipping", value)
        return None

    try:
        n = float(parts[0])
    except ValueError:
        log.debug("CT.gov age string %r has non-numeric leading token; skipping", value)
        return None

    # Strip a trailing 's' so "Years"/"Year", "Months"/"Month", etc. all hit.
    unit = parts[1].lower().rstrip("s")
    factor = _UNIT_TO_YEARS.get(unit)
    if factor is None:
        log.debug("CT.gov age string %r has unrecognized unit %r; skipping", value, parts[1])
        return None

    return n * factor


__all__ = [
    "INJECTED_SOURCE_PREFIX",
    "enrich_with_structured_fields",
]
