"""Top-level dispatcher tests.

Exercises `match_extracted` over a mixed batch of criterion kinds
and walks through the polarity / negation flips end-to-end so we
catch any miswiring between the per-kind handlers and the polarity
helper.
"""

from __future__ import annotations

from datetime import date

import pytest

from clinical_demo.matcher import match_criterion, match_extracted
from tests.matcher._fixtures import (
    crit_age,
    crit_condition,
    crit_free_text,
    crit_measurement,
    make_condition,
    make_lab,
    make_profile,
    make_trial,
)


def test_match_extracted_runs_each_criterion_independently() -> None:
    """A batch of mixed criteria yields one verdict per input,
    preserving order."""
    profile = make_profile(
        birth=date(1990, 1, 1),
        conditions=[make_condition(code="44054006", display="T2DM")],
        observations=[make_lab(loinc="4548-4", value=8.0, unit="%")],
    )
    crits = [
        crit_age(minimum_years=18.0),
        crit_condition(text="type 2 diabetes"),
        crit_measurement(text="hba1c", operator=">=", value=7.0, unit="%"),
        crit_free_text(),
    ]
    verdicts = match_extracted(crits, profile, make_trial())
    assert len(verdicts) == 4
    assert [v.verdict for v in verdicts] == [
        "pass",
        "pass",
        "pass",
        "indeterminate",
    ]
    assert verdicts[3].reason == "human_review_required"


@pytest.mark.parametrize(
    "polarity,negated,raw_pass_setup,expected",
    [
        # Inclusion + raw pass → pass
        ("inclusion", False, True, "pass"),
        # Inclusion + negated + raw pass → fail (single flip)
        ("inclusion", True, True, "fail"),
        # Exclusion + raw pass → fail (single flip)
        ("exclusion", False, True, "fail"),
        # Exclusion + negated + raw pass → pass (XOR)
        ("exclusion", True, True, "pass"),
    ],
)
def test_polarity_negation_end_to_end_with_condition(
    polarity: str,
    negated: bool,
    raw_pass_setup: bool,
    expected: str,
) -> None:
    """Build a condition criterion that *raw*-passes (patient has
    T2DM) and verify the polarity/negation flip lands on the right
    final verdict — same XOR table as the helper unit test, but
    plumbed through dispatch."""
    profile = make_profile(
        conditions=([make_condition(code="44054006", display="T2DM")] if raw_pass_setup else []),
    )
    v = match_criterion(
        crit_condition(
            text="type 2 diabetes",
            polarity=polarity,
            negated=negated,
        ),
        profile,
        make_trial(),
    )
    assert v.verdict == expected


def test_indeterminate_propagates_through_polarity() -> None:
    """An `indeterminate` raw verdict cannot be flipped by polarity
    — neither the structured fail-closed nor the negation should
    turn 'we don't know' into a decision."""
    profile = make_profile()  # no labs, no conditions
    v = match_criterion(
        crit_measurement(text="hba1c", operator=">=", value=7.0, unit="%", polarity="exclusion"),
        profile,
        make_trial(),
    )
    assert v.verdict == "indeterminate"
    assert v.reason == "no_data"


def test_evidence_is_attached_for_pass_and_fail() -> None:
    """Both pass and fail must come with at least one evidence row;
    the reviewer UI relies on it for click-to-source."""
    profile = make_profile(conditions=[make_condition()])
    pass_v = match_criterion(crit_condition(text="type 2 diabetes"), profile, make_trial())
    fail_v = match_criterion(
        crit_condition(text="type 2 diabetes", polarity="exclusion"),
        profile,
        make_trial(),
    )
    # 'fail' here = patient has T2DM under an exclusion criterion
    assert fail_v.verdict == "fail"
    assert pass_v.evidence
    assert fail_v.evidence


def test_required_payload_invariant_raises_on_corrupted_input() -> None:
    """If a caller hand-builds an `ExtractedCriterion` whose `kind`
    promises one payload but the slot is None, `_required` should
    raise a typed `ValueError` rather than letting `AttributeError`
    leak from the per-kind handler."""
    from clinical_demo.extractor.schema import ExtractedCriterion

    bad = ExtractedCriterion(
        kind="age",
        polarity="inclusion",
        source_text="",
        negated=False,
        mood="actual",
        age=None,  # missing on purpose
        sex=None,
        condition=None,
        medication=None,
        measurement=None,
        temporal_window=None,
        free_text=None,
        mentions=[],
    )
    with pytest.raises(ValueError, match="`age` payload is None"):
        match_criterion(bad, make_profile(), make_trial())
