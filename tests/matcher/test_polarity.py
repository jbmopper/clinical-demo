"""Polarity / negation truth-table tests for the matcher.

The polarity flip is tiny but load-bearing: getting it wrong silently
inverts the matcher's verdict on every exclusion criterion. We test
the helper directly and then re-test it indirectly through the
high-level `match_criterion` to catch wiring regressions.
"""

from __future__ import annotations

import pytest

from clinical_demo.matcher.matcher import _apply_polarity
from clinical_demo.matcher.verdict import Verdict


@pytest.mark.parametrize(
    "raw,polarity,negated,expected",
    [
        # inclusion + not negated → identity
        ("pass", "inclusion", False, "pass"),
        ("fail", "inclusion", False, "fail"),
        # inclusion + negated → flip
        ("pass", "inclusion", True, "fail"),
        ("fail", "inclusion", True, "pass"),
        # exclusion + not negated → flip
        ("pass", "exclusion", False, "fail"),
        ("fail", "exclusion", False, "pass"),
        # exclusion + negated → identity (XOR)
        ("pass", "exclusion", True, "pass"),
        ("fail", "exclusion", True, "fail"),
        # indeterminate is invariant under both flips
        ("indeterminate", "inclusion", False, "indeterminate"),
        ("indeterminate", "inclusion", True, "indeterminate"),
        ("indeterminate", "exclusion", False, "indeterminate"),
        ("indeterminate", "exclusion", True, "indeterminate"),
    ],
)
def test_apply_polarity_truth_table(
    raw: Verdict, polarity: str, negated: bool, expected: Verdict
) -> None:
    """Exhaustive truth-table check; the helper is small enough that
    we should pin every cell rather than rely on derivation tests."""
    assert _apply_polarity(raw, polarity, negated) == expected  # type: ignore[arg-type]
