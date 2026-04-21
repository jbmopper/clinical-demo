"""Tests for the seed-set selection policy living in scripts/build_eval_seed.py.

The script is structured as importable functions plus a thin `main`,
so we can test the selection logic directly without touching disk.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

from clinical_demo.domain.trial import Trial

# scripts/ is not a package; import by file path.
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))

from build_eval_seed import (
    MAX_PAIRS_PER_PATIENT,
    PAIRS_PER_SLICE_TARGET,
    CohortMemberView,
    rank_patients_for_slice,
    select_pairs,
)


def _member(
    patient_id: str,
    *,
    score: int = 4,
    age: int = 60,
    labels: frozenset[str] = frozenset(),
    has_hba1c: bool = False,
    has_ldl: bool = False,
    has_egfr: bool = False,
    has_systolic_bp: bool = False,
    sex: str = "male",
) -> CohortMemberView:
    return CohortMemberView(
        patient_id=patient_id,
        age=age,
        sex=sex,
        score=score,
        labels=labels,
        has_hba1c=has_hba1c,
        has_ldl=has_ldl,
        has_egfr=has_egfr,
        has_systolic_bp=has_systolic_bp,
    )


def _trial(nct_id: str) -> Trial:
    return Trial(
        nct_id=nct_id,
        title="t",
        overall_status="RECRUITING",
        sponsor_name="s",
        sponsor_class="INDUSTRY",
        eligibility_text="x",
    )


# ---------- rank_patients_for_slice ----------


def test_ranking_prefers_topical_patients() -> None:
    """Patients whose labels match the slice topic outrank others,
    even with lower scores."""
    members = [
        _member("hi-score-non-topical", score=10),
        _member(
            "low-score-topical",
            score=2,
            labels=frozenset({"Type 2 diabetes mellitus"}),
        ),
    ]
    ranked = rank_patients_for_slice(members, "t2dm-industry")
    assert ranked[0].patient_id == "low-score-topical"


def test_ranking_prefers_lab_coverage_within_topical_group() -> None:
    """Among slice-topical patients, those with the slice's required
    lab on file outrank those without."""
    labels = frozenset({"Type 2 diabetes mellitus"})
    members = [
        _member("topical-no-hba1c", score=10, labels=labels, has_hba1c=False),
        _member("topical-with-hba1c", score=4, labels=labels, has_hba1c=True),
    ]
    ranked = rank_patients_for_slice(members, "t2dm-industry")
    assert ranked[0].patient_id == "topical-with-hba1c"


def test_ranking_falls_back_to_score_for_off_topic_slice() -> None:
    """For NSCLC the cohort is intentionally off-topic; ranking should
    just sort by score so the highest-scoring patients still get used."""
    members = [
        _member("low", score=2),
        _member("high", score=8),
        _member("mid", score=5),
    ]
    ranked = rank_patients_for_slice(members, "nsclc")
    assert [m.patient_id for m in ranked] == ["high", "mid", "low"]


# ---------- select_pairs ----------


def test_select_pairs_respects_per_slice_target() -> None:
    """With more than enough patients, each slice yields exactly
    PAIRS_PER_SLICE_TARGET pairs."""
    members = [_member(f"p{i}", score=5) for i in range(20)]
    trials = {
        "t2dm-industry": [_trial("NCT001"), _trial("NCT002")],
        "ckd": [_trial("NCT003"), _trial("NCT004")],
    }
    pairs = list(select_pairs(members, trials, random.Random(0)))
    by_slice: dict[str, int] = {}
    for _, _, s in pairs:
        by_slice[s] = by_slice.get(s, 0) + 1
    assert by_slice == {
        "t2dm-industry": PAIRS_PER_SLICE_TARGET,
        "ckd": PAIRS_PER_SLICE_TARGET,
    }


def test_select_pairs_caps_per_patient_appearances() -> None:
    """No single patient appears more than MAX_PAIRS_PER_PATIENT times
    across the whole manifest, even when they'd otherwise dominate."""
    members = [_member(f"p{i}", score=10 - i) for i in range(20)]
    trials = {f"slice-{j}": [_trial(f"NCT00{j}")] for j in range(5)}
    pairs = list(select_pairs(members, trials, random.Random(0)))
    counts: dict[str, int] = {}
    for m, _, _ in pairs:
        counts[m.patient_id] = counts.get(m.patient_id, 0) + 1
    assert max(counts.values()) <= MAX_PAIRS_PER_PATIENT


def test_select_pairs_distributes_across_trials_in_slice() -> None:
    """Each trial in a slice gets at least one pair when possible."""
    members = [_member(f"p{i}", score=5) for i in range(PAIRS_PER_SLICE_TARGET + 2)]
    trials_in_slice = [_trial(f"NCT00{i}") for i in range(PAIRS_PER_SLICE_TARGET)]
    pairs = list(select_pairs(members, {"t2dm-industry": trials_in_slice}, random.Random(0)))
    selected_trials = {t.nct_id for _, t, _ in pairs}
    assert len(selected_trials) == PAIRS_PER_SLICE_TARGET


def test_select_pairs_handles_slice_with_no_eligible_patients(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Below cohort_score_min, the slice yields no pairs and we log it."""
    members = [_member("p0", score=0)]  # below COHORT_SCORE_MIN (which is 2)
    trials = {"t2dm-industry": [_trial("NCT001")]}
    with caplog.at_level("WARNING"):
        pairs = list(select_pairs(members, trials, random.Random(0)))
    assert pairs == []
    assert any("no eligible patients" in r.message for r in caplog.records)
