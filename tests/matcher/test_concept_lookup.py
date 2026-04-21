"""Concept-lookup tests.

The lookup tables drive the matcher's recall: a missed alias means a
verdict drops to `indeterminate (unmapped_concept)` even when the
patient data would otherwise resolve it. These tests pin the major
condition / lab aliases we rely on; the medication table is
deliberately empty in v0 (see concept_lookup.py docstring)."""

from __future__ import annotations

import pytest

from clinical_demo.matcher.concept_lookup import (
    lookup_condition,
    lookup_lab,
    lookup_medication,
)
from clinical_demo.profile.concept_sets import (
    CHRONIC_KIDNEY_DISEASE,
    EGFR,
    HBA1C,
    HYPERLIPIDEMIA,
    HYPERTENSION,
    LDL_CHOLESTEROL,
    PREDIABETES,
    SYSTOLIC_BP,
    T2DM,
)


@pytest.mark.parametrize(
    "surface,expected",
    [
        ("type 2 diabetes", T2DM),
        ("Type 2 Diabetes", T2DM),
        ("  T2DM  ", T2DM),
        ("type ii diabetes", T2DM),
        ("prediabetes", PREDIABETES),
        ("pre-diabetes", PREDIABETES),
        ("hypertension", HYPERTENSION),
        ("HTN", HYPERTENSION),
        ("hyperlipidemia", HYPERLIPIDEMIA),
        ("dyslipidemia", HYPERLIPIDEMIA),
        ("chronic kidney disease", CHRONIC_KIDNEY_DISEASE),
        ("CKD", CHRONIC_KIDNEY_DISEASE),
    ],
)
def test_lookup_condition_known_aliases(surface: str, expected: object) -> None:
    """Common cardiometabolic aliases must hit; case + whitespace
    are normalized so the LLM's surface form flows through."""
    assert lookup_condition(surface) is expected


def test_lookup_condition_unknown_returns_none() -> None:
    """Anything not in the table must return None — that's how the
    matcher distinguishes 'no evidence' from 'concept not recognized'."""
    assert lookup_condition("Sjogren's syndrome") is None
    assert lookup_condition("") is None


@pytest.mark.parametrize(
    "surface,expected",
    [
        ("hba1c", HBA1C),
        ("HbA1c", HBA1C),
        ("a1c", HBA1C),
        ("glycated hemoglobin", HBA1C),
        ("LDL", LDL_CHOLESTEROL),
        ("ldl-c", LDL_CHOLESTEROL),
        ("low-density lipoprotein cholesterol", LDL_CHOLESTEROL),
        ("eGFR", EGFR),
        ("estimated glomerular filtration rate", EGFR),
        ("systolic blood pressure", SYSTOLIC_BP),
        ("SBP", SYSTOLIC_BP),
    ],
)
def test_lookup_lab_known_aliases(surface: str, expected: object) -> None:
    assert lookup_lab(surface) is expected


def test_lookup_lab_unknown_returns_none() -> None:
    assert lookup_lab("BNP") is None


def test_lookup_medication_v0_returns_none_for_everything() -> None:
    """v0's medication table is intentionally empty; pin that
    behaviour so we notice when it changes."""
    for s in ("metformin", "insulin", "statins", "aspirin"):
        assert lookup_medication(s) is None
