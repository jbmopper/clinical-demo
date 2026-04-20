"""Pull a curated set of trials from ClinicalTrials.gov.

Writes one raw `protocolSection`-bearing JSON per trial to
`data/curated/trials/<NCT_ID>.json` (gitignored), plus a manifest at
`data/curated/trials_manifest.json` summarizing the curated set.

Curation strategy (PLAN.md Phase 1, task 1.4):
- ~25 cardiometabolic trials split across diabetes, hypertension,
  hyperlipidemia, CKD; mix of industry and academic sponsors.
- ~5 lung-cancer trials (the stretch generalization probe).
- All trials: interventional, currently RECRUITING, Phase 2 or 3 — to
  bias toward criteria that are detailed and clinically interesting.

Run:  uv run python scripts/curate_trials.py
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import NamedTuple

from clinical_demo.data.clinicaltrials import ClinicalTrialsClient, trial_from_raw

logger = logging.getLogger(__name__)


class Slice(NamedTuple):
    label: str
    condition: str
    sponsor_class: str | None
    target_count: int


# Cardiometabolic + lung cancer slices. Industry-heavy because industry
# trials carry richer, longer eligibility text (good substrate for the
# extractor and matcher).
SLICES: list[Slice] = [
    Slice("t2dm-industry", "type 2 diabetes", "INDUSTRY", 7),
    Slice("t2dm-academic", "type 2 diabetes", "OTHER", 3),
    Slice("hypertension-industry", "hypertension", "INDUSTRY", 4),
    Slice("hypertension-academic", "hypertension", "OTHER", 1),
    Slice("hyperlipidemia", "hyperlipidemia", None, 5),
    Slice("ckd", "chronic kidney disease", None, 5),
    Slice("nsclc", "non-small cell lung cancer", None, 5),
]


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    out_dir = Path("data/curated/trials")
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest: list[dict[str, object]] = []
    seen_ids: set[str] = set()

    with ClinicalTrialsClient() as client:
        for sl in SLICES:
            logger.info("---- slice: %s (target %d) ----", sl.label, sl.target_count)
            kept = 0
            # Pull more than we need so we can skip duplicates and trials
            # with empty eligibility text without missing the target.
            for raw in client.iter_raw_studies(
                condition=sl.condition,
                phases=["PHASE2", "PHASE3"],
                sponsor_class=sl.sponsor_class,
                overall_status="RECRUITING",
                page_size=20,
                max_results=sl.target_count * 3,
            ):
                ps = raw.get("protocolSection", raw)
                trial = trial_from_raw(raw)
                if not trial.nct_id or trial.nct_id in seen_ids:
                    continue
                # Skip trivially-short criteria; they are uninteresting for
                # the extractor and matcher.
                if len(trial.eligibility_text.strip()) < 200:
                    continue
                _write_trial(out_dir, trial.nct_id, ps)
                manifest.append(
                    {
                        "nct_id": trial.nct_id,
                        "slice": sl.label,
                        "title": trial.title,
                        "phase": trial.phase,
                        "sponsor_name": trial.sponsor_name,
                        "sponsor_class": trial.sponsor_class,
                        "conditions": trial.conditions,
                        "eligibility_chars": len(trial.eligibility_text),
                    }
                )
                seen_ids.add(trial.nct_id)
                logger.info(
                    "  + %s  %s  (%d chars)",
                    trial.nct_id,
                    trial.title[:60],
                    len(trial.eligibility_text),
                )
                kept += 1
                if kept >= sl.target_count:
                    break
            if kept < sl.target_count:
                logger.warning(
                    "  ! slice %s: only got %d / %d trials", sl.label, kept, sl.target_count
                )

    manifest_path = Path("data/curated/trials_manifest.json")
    with manifest_path.open("w") as f:
        json.dump({"trials": manifest}, f, indent=2)
    logger.info(
        "\nwrote %d trials to %s and manifest to %s",
        len(manifest),
        out_dir,
        manifest_path,
    )


def _write_trial(out_dir: Path, nct_id: str, protocol_section: dict[str, object]) -> None:
    path = out_dir / f"{nct_id}.json"
    with path.open("w") as f:
        json.dump({"protocolSection": protocol_section}, f, indent=2)


if __name__ == "__main__":
    main()
