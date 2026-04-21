"""Build the eval seed-set from the curated cohort and curated trials.

This script picks ~50 (patient, trial) pairs stratified across our
seven trial slices, runs the mechanical pre-labeler against each, and
persists the result to `data/curated/eval_seed.json` along with full
provenance.

What it does *not* do: label criteria that live in the trial's
free-text eligibility blob (clinical-judgement criteria, hard
thresholds, exclusions on prior therapies, etc.). Those are owed to
a human reviewer; the manifest tracks how many remain per pair.

Run from repo root:

    uv run python scripts/build_eval_seed.py

Inputs:
    data/curated/cohort_manifest.json  (from curate_cohort.py)
    data/curated/trials_manifest.json  (from curate_trials.py)
    data/curated/trials/*.json         (raw CT.gov payloads)
    data/synthea/                      (FHIR patient bundles)

Output:
    data/curated/eval_seed.json
"""

from __future__ import annotations

import json
import logging
import random
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from clinical_demo.data.clinicaltrials import trial_from_raw
from clinical_demo.data.synthea import iter_bundles
from clinical_demo.domain.patient import Patient
from clinical_demo.domain.trial import Trial
from clinical_demo.evals.seed import (
    EvalPair,
    EvalSeed,
    SelectionPolicy,
    estimate_free_text_criteria,
    mechanical_verdicts,
)

logger = logging.getLogger("build_eval_seed")

# The set of slice -> condition labels in our cohort manifest. Patients
# carrying a label in a slice's set are 'topical' for that slice. For
# NSCLC, the cohort intentionally has no matching patients; we still
# include pairs there so the matcher's behavior on out-of-domain trials
# is exercised.
SLICE_TOPIC_LABELS: dict[str, set[str]] = {
    "t2dm-industry": {"Type 2 diabetes mellitus"},
    "t2dm-academic": {"Type 2 diabetes mellitus"},
    "hypertension-industry": {"Essential hypertension"},
    "hypertension-academic": {"Essential hypertension"},
    "hyperlipidemia": {"Hyperlipidemia"},
    "ckd": set(),
    "nsclc": set(),
}

# Slices whose hard thresholds typically reference labs we track. For
# these, prefer patients with the relevant lab actually on file.
SLICE_REQUIRED_LAB: dict[str, str] = {
    "t2dm-industry": "has_hba1c",
    "t2dm-academic": "has_hba1c",
    "hypertension-industry": "has_systolic_bp",
    "hypertension-academic": "has_systolic_bp",
    "hyperlipidemia": "has_ldl",
    "ckd": "has_egfr",
}

PAIRS_PER_SLICE_TARGET = 7
COHORT_SCORE_MIN = 2  # default: any cohort member; raise to demand richer profiles
MAX_PAIRS_PER_PATIENT = 2  # spread coverage; otherwise the top-ranked patient dominates
RNG_SEED = 20260101  # deterministic selection across runs


@dataclass(frozen=True)
class CohortMemberView:
    """Slim view over a cohort manifest member; lets us rank without
    re-deriving labs/conditions from FHIR."""

    patient_id: str
    age: int
    sex: str
    score: int
    labels: frozenset[str]
    has_hba1c: bool
    has_ldl: bool
    has_egfr: bool
    has_systolic_bp: bool

    def lab_flag(self, name: str) -> bool:
        return bool(getattr(self, name))


def load_cohort(manifest_path: Path) -> tuple[list[CohortMemberView], date, Path]:
    raw = json.loads(manifest_path.read_text())
    members = [
        CohortMemberView(
            patient_id=m["patient_id"],
            age=m["age"],
            sex=m["sex"],
            score=m["score"],
            labels=frozenset(m["cardiometabolic_labels"]),
            has_hba1c=m["has_hba1c"],
            has_ldl=m["has_ldl"],
            has_egfr=m["has_egfr"],
            has_systolic_bp=m["has_systolic_bp"],
        )
        for m in raw["members"]
    ]
    as_of = date.fromisoformat(raw["as_of"])
    synthea_dir = Path(raw["synthea_dir"])
    return members, as_of, synthea_dir


def load_trials(manifest_path: Path) -> dict[str, list[Trial]]:
    """Group curated trials by slice, returning Trial domain objects."""
    raw = json.loads(manifest_path.read_text())
    trials_dir = manifest_path.parent / "trials"
    by_slice: dict[str, list[Trial]] = defaultdict(list)
    for entry in raw["trials"]:
        nct_id = entry["nct_id"]
        slice_name = entry["slice"]
        payload = json.loads((trials_dir / f"{nct_id}.json").read_text())
        by_slice[slice_name].append(trial_from_raw(payload))
    return dict(by_slice)


def load_patients(synthea_dir: Path, wanted_ids: set[str]) -> dict[str, Patient]:
    """Load only the FHIR patients whose ids we need.

    `iter_bundles` already yields `Patient` objects (skipping non-
    patient bundles like hospital/practitioner files). We iterate
    once and keep only the ones we recognize, short-circuiting once
    we've found everything.
    """
    found: dict[str, Patient] = {}
    for patient in iter_bundles(synthea_dir):
        if patient.patient_id in wanted_ids:
            found[patient.patient_id] = patient
        if len(found) == len(wanted_ids):
            break
    missing = wanted_ids - found.keys()
    if missing:
        logger.warning(
            "could not locate %d/%d wanted patients in %s",
            len(missing),
            len(wanted_ids),
            synthea_dir,
        )
    return found


def rank_patients_for_slice(
    members: list[CohortMemberView], slice_name: str
) -> list[CohortMemberView]:
    """Order cohort members by suitability for a slice.

    Suitability key, descending: (slice-topical, has-required-lab,
    score, age). The age tiebreaker prefers older patients for
    cardiometabolic slices (more chronic-condition exposure) but is a
    soft signal — every member becomes eligible once the topical /
    lab filters are exhausted.
    """
    topic_labels = SLICE_TOPIC_LABELS.get(slice_name, set())
    lab_field = SLICE_REQUIRED_LAB.get(slice_name)

    def sort_key(m: CohortMemberView) -> tuple[int, int, int, int, str]:
        topical = 1 if (topic_labels & m.labels) else 0
        has_lab = 1 if (lab_field and m.lab_flag(lab_field)) else 0
        return (-topical, -has_lab, -m.score, -m.age, m.patient_id)

    return sorted(
        [m for m in members if m.score >= COHORT_SCORE_MIN],
        key=sort_key,
    )


def select_pairs(
    members: list[CohortMemberView],
    trials_by_slice: dict[str, list[Trial]],
    rng: random.Random,
) -> Iterator[tuple[CohortMemberView, Trial, str]]:
    """Yield (member, trial, slice_name) triples per the policy.

    Per slice: take the top-ranked patients (slice-topical first,
    then has-required-lab, then cohort score) and round-robin them
    across the slice's trials. A patient is capped at
    `MAX_PAIRS_PER_PATIENT` total appearances across the whole
    manifest, so the eval set exercises a diverse set of profiles
    instead of being dominated by the single highest-scoring patient.
    """
    used_count: dict[str, int] = defaultdict(int)
    for slice_name, trials in trials_by_slice.items():
        if not trials:
            continue
        ranked = rank_patients_for_slice(members, slice_name)
        if not ranked:
            logger.warning("no eligible patients for slice %s", slice_name)
            continue
        # Shuffle the trial order deterministically so the manifest
        # is stable across runs but trials don't get a fixed pole
        # position from CT.gov sort order.
        trial_pool = trials.copy()
        rng.shuffle(trial_pool)
        chosen: list[CohortMemberView] = []
        for m in ranked:
            if used_count[m.patient_id] >= MAX_PAIRS_PER_PATIENT:
                continue
            chosen.append(m)
            if len(chosen) >= PAIRS_PER_SLICE_TARGET:
                break
        if len(chosen) < PAIRS_PER_SLICE_TARGET:
            logger.info(
                "slice %s: only %d distinct patients available within per-patient cap "
                "(target was %d); proceeding with what we have",
                slice_name,
                len(chosen),
                PAIRS_PER_SLICE_TARGET,
            )
        for i, patient in enumerate(chosen):
            trial = trial_pool[i % len(trial_pool)]
            used_count[patient.patient_id] += 1
            yield patient, trial, slice_name


def build_pair(
    member: CohortMemberView,
    trial: Trial,
    slice_name: str,
    patient: Patient,
    as_of: date,
) -> EvalPair:
    verdicts = mechanical_verdicts(patient, trial, as_of)
    free_count = estimate_free_text_criteria(trial.eligibility_text)
    return EvalPair(
        pair_id=f"{member.patient_id[:8]}__{trial.nct_id}",
        patient_id=member.patient_id,
        nct_id=trial.nct_id,
        slice=slice_name,
        structured_verdicts=verdicts,
        free_text_criteria_count=free_count,
        free_text_review_status="pending",
        notes=f"slice={slice_name}; cohort_score={member.score}",
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    cohort_manifest = Path("data/curated/cohort_manifest.json")
    trials_manifest = Path("data/curated/trials_manifest.json")
    output_path = Path("data/curated/eval_seed.json")

    members, as_of, synthea_dir = load_cohort(cohort_manifest)
    trials_by_slice = load_trials(trials_manifest)
    logger.info(
        "loaded %d cohort members, %d slices (%d trials total) as_of %s",
        len(members),
        len(trials_by_slice),
        sum(len(v) for v in trials_by_slice.values()),
        as_of,
    )

    rng = random.Random(RNG_SEED)
    selected = list(select_pairs(members, trials_by_slice, rng))
    logger.info("selected %d (patient, trial) pairs", len(selected))

    wanted_ids = {m.patient_id for m, _, _ in selected}
    patients = load_patients(synthea_dir, wanted_ids)

    pairs: list[EvalPair] = []
    for member, trial, slice_name in selected:
        patient = patients.get(member.patient_id)
        if patient is None:
            logger.warning(
                "skipping pair %s/%s: patient bundle not loaded",
                member.patient_id,
                trial.nct_id,
            )
            continue
        pairs.append(build_pair(member, trial, slice_name, patient, as_of))

    seed = EvalSeed(
        cohort_manifest_path=str(cohort_manifest),
        trials_manifest_path=str(trials_manifest),
        as_of=as_of,
        generated_at=datetime.now(),
        selection_policy=SelectionPolicy(
            target_pairs=len(selected),
            pairs_per_slice_target=PAIRS_PER_SLICE_TARGET,
            max_pairs_per_patient=MAX_PAIRS_PER_PATIENT,
            cohort_score_min=COHORT_SCORE_MIN,
            require_lab_coverage_for_threshold_trials=True,
            description=(
                "Per slice, prefer cohort members whose labels match the "
                "slice's topic and who have the slice's primary lab on "
                "file. Aim for ~7 pairs per slice; round-robin across "
                "trials in the slice; cap each patient at "
                "max_pairs_per_patient appearances total so the seed "
                "set exercises a diverse set of profiles instead of "
                "being dominated by the highest-scoring patient. "
                "Mechanical pre-labels structured fields only (age "
                "bounds, sex, healthy_volunteers); free-text criteria "
                "are counted, not labeled — they require human review."
            ),
        ),
        pairs=pairs,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(seed.model_dump_json(indent=2))

    _print_summary(seed)


def _print_summary(seed: EvalSeed) -> None:
    by_slice: dict[str, int] = defaultdict(int)
    by_verdict: dict[str, int] = defaultdict(int)
    free_total = 0
    for p in seed.pairs:
        by_slice[p.slice] += 1
        free_total += p.free_text_criteria_count
        for v in p.structured_verdicts:
            by_verdict[v.verdict] += 1
    logger.info("---")
    logger.info("eval seed summary")
    logger.info("  pairs by slice:")
    for s in sorted(by_slice):
        logger.info("    %-22s  %d", s, by_slice[s])
    logger.info("  mechanical verdicts:")
    for v in ("pass", "fail", "indeterminate"):
        logger.info("    %-22s  %d", v, by_verdict.get(v, 0))
    logger.info("  free-text criteria pending human review: %d", free_total)
    logger.info(
        "  pairs marked complete: %d / %d",
        sum(1 for p in seed.pairs if p.free_text_review_status == "complete"),
        len(seed.pairs),
    )


if __name__ == "__main__":
    main()
