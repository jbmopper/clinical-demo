"""Smoke test so the test suite is not empty before we have real code."""

from clinical_demo import __version__


def test_version_is_set() -> None:
    assert __version__
