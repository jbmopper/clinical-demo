"""Build the working patient cohort from Synthea and persist a manifest.

Reads bundles from `data/raw/synthea/fhir/`, applies the cardiometabolic
curation policy in `clinical_demo.data.cohort`, and writes
`data/curated/cohort_manifest.json`.

The manifest is the durable contract between this script and downstream
work (eval seed pairs, cohort UI, etc.). Patient records themselves are
not duplicated — downstream code reads the original bundle by patient id.

Run:  uv run python scripts/curate_cohort.py
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import date
from pathlib import Path

from clinical_demo.data.cohort import ALL_CARDIOMETABOLIC, CohortMember, curate
from clinical_demo.data.synthea import iter_bundles

logger = logging.getLogger(__name__)

SYNTHEA_DIR = Path("data/raw/synthea/fhir")
OUT_PATH = Path("data/curated/cohort_manifest.json")
TARGET_SIZE = 150
# Reference date used as the "today" for active-condition and age
# evaluation. Holding this constant in the manifest means downstream
# code can reproduce the cohort exactly without depending on the
# system clock.
AS_OF = date(2025, 1, 1)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if not SYNTHEA_DIR.exists():
        raise SystemExit(f"missing {SYNTHEA_DIR}; see README.md for the Synthea download step")

    logger.info("loading bundles from %s ...", SYNTHEA_DIR)
    patients = list(iter_bundles(SYNTHEA_DIR))
    logger.info("  %d patients loaded", len(patients))

    members = curate(patients, as_of=AS_OF, target_size=TARGET_SIZE)
    logger.info("  %d patients selected (target %d)", len(members), TARGET_SIZE)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("w") as f:
        json.dump(
            {
                "as_of": AS_OF.isoformat(),
                "synthea_dir": str(SYNTHEA_DIR),
                "target_size": TARGET_SIZE,
                "policy": {
                    "core_cardiometabolic": _label_map("core"),
                    "prediabetes": _label_map("prediabetes"),
                    "score_formula": "2 * core_count + prediabetes_count",
                    "age_range": [18, 95],
                },
                "members": [asdict(m) for m in members],
            },
            f,
            indent=2,
        )
    logger.info("wrote manifest to %s", OUT_PATH)
    _print_distribution(members)


def _label_map(bucket: str) -> dict[str, str]:
    """Re-derive the curated SNOMED → label mapping from the cohort module
    so the manifest carries a self-describing copy of the policy."""
    from clinical_demo.data.cohort import CORE_CARDIOMETABOLIC, PREDIABETES

    if bucket == "core":
        return dict(CORE_CARDIOMETABOLIC)
    if bucket == "prediabetes":
        return dict(PREDIABETES)
    raise ValueError(bucket)


def _print_distribution(members: list[CohortMember]) -> None:
    """Quick distributional summary for a sanity check at the CLI."""
    from collections import Counter

    if not members:
        logger.info("(empty cohort)")
        return

    score_counts = Counter(m.score for m in members)
    age_buckets: Counter[str] = Counter()
    label_counts: Counter[str] = Counter()
    sex_counts: Counter[str] = Counter()
    for m in members:
        bucket_low = (m.age // 10) * 10
        age_buckets[f"{bucket_low}-{bucket_low + 9}"] += 1
        for label in m.cardiometabolic_labels:
            label_counts[label] += 1
        sex_counts[m.sex] += 1

    logger.info("\n--- score distribution ---")
    for score in sorted(score_counts, reverse=True):
        logger.info("  score=%d: %d", score, score_counts[score])
    logger.info("\n--- age buckets ---")
    for bucket in sorted(age_buckets):
        logger.info("  %s: %d", bucket, age_buckets[bucket])
    logger.info("\n--- sex ---")
    for sex, n in sex_counts.most_common():
        logger.info("  %s: %d", sex, n)
    logger.info("\n--- condition prevalence in cohort ---")
    for lbl, n in label_counts.most_common():
        logger.info("  %3d  %s", n, lbl)
    logger.info("\n--- key labs availability ---")
    has_hba1c = sum(1 for m in members if m.has_hba1c)
    has_ldl = sum(1 for m in members if m.has_ldl)
    has_egfr = sum(1 for m in members if m.has_egfr)
    has_sbp = sum(1 for m in members if m.has_systolic_bp)
    n = len(members)
    logger.info("  HbA1c:    %d / %d", has_hba1c, n)
    logger.info("  LDL:      %d / %d", has_ldl, n)
    logger.info("  eGFR:     %d / %d", has_egfr, n)
    logger.info("  systolic: %d / %d", has_sbp, n)
    logger.info("\nALL_CARDIOMETABOLIC codes: %d", len(ALL_CARDIOMETABOLIC))


if __name__ == "__main__":
    main()
