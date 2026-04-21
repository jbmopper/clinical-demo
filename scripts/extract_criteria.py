"""Smoke-script: run the criterion extractor against curated trials.

Iterates the curated trials manifest, loads each raw CT.gov payload,
runs `extract_criteria(...)`, and persists the result alongside its
run metadata to `data/curated/extractions/<NCT_ID>.json`.

By default, processes the first 5 trials (small, cheap sample to
inspect prompt+schema quality before paying to extract all 30). Use
`--limit 0` for the full set, `--force` to overwrite existing outputs.

`--dry-run` renders the prompt for the first trial and exits without
calling the API; useful for prompt iteration without spending tokens.

Run:
    uv run python scripts/extract_criteria.py
    uv run python scripts/extract_criteria.py --limit 0 --force
    uv run python scripts/extract_criteria.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections import Counter
from pathlib import Path

from clinical_demo.data.clinicaltrials import trial_from_raw
from clinical_demo.domain.trial import Trial
from clinical_demo.extractor import (
    ExtractorError,
    ExtractorRefusalError,
    build_messages,
    extract_criteria,
)
from clinical_demo.scoring import StoredExtraction

logger = logging.getLogger(__name__)

CURATED_TRIALS_DIR = Path("data/curated/trials")
TRIALS_MANIFEST = Path("data/curated/trials_manifest.json")
EXTRACTIONS_DIR = Path("data/curated/extractions")


def _load_trial(nct_id: str) -> Trial:
    """Load one curated trial JSON and translate to the domain model.

    Raises FileNotFoundError if the trial wasn't curated yet — caller
    decides whether to skip or fail."""
    raw_path = CURATED_TRIALS_DIR / f"{nct_id}.json"
    raw = json.loads(raw_path.read_text())
    return trial_from_raw(raw)


def _select_trials(limit: int) -> list[str]:
    """Read the curated manifest and return up to `limit` NCT IDs.

    `limit=0` means "no cap, process everything"."""
    manifest = json.loads(TRIALS_MANIFEST.read_text())
    trials = manifest["trials"]
    nct_ids = [t["nct_id"] for t in trials]
    return nct_ids if limit == 0 else nct_ids[:limit]


def _output_path(nct_id: str) -> Path:
    return EXTRACTIONS_DIR / f"{nct_id}.json"


def _run_one(nct_id: str) -> StoredExtraction:
    """Extract one trial. Caller handles errors / I/O."""
    trial = _load_trial(nct_id)
    if not trial.eligibility_text.strip():
        logger.warning("trial %s has empty eligibility_text; skipping API call", nct_id)
    result = extract_criteria(trial.eligibility_text)
    return StoredExtraction(
        nct_id=nct_id,
        extraction=result.extracted,
        meta=result.meta,
    )


def _print_summary(stored: list[StoredExtraction]) -> None:
    """Single-screen rollup: extractions count, kind histogram, cost,
    per-trial averages. Designed to fit in a terminal so the human
    can sanity-check the run before drilling into individual files."""
    if not stored:
        print("no extractions to summarize")
        return

    total_criteria = sum(len(s.extraction.criteria) for s in stored)
    kind_hist: Counter[str] = Counter()
    polarity_hist: Counter[str] = Counter()
    mood_hist: Counter[str] = Counter()
    negated = 0
    for s in stored:
        for c in s.extraction.criteria:
            kind_hist[c.kind] += 1
            polarity_hist[c.polarity] += 1
            mood_hist[c.mood] += 1
            if c.negated:
                negated += 1

    total_cost = sum(s.meta.cost_usd or 0.0 for s in stored)
    total_in_tokens = sum(s.meta.input_tokens or 0 for s in stored)
    total_out_tokens = sum(s.meta.output_tokens or 0 for s in stored)
    total_latency = sum(s.meta.latency_ms or 0.0 for s in stored)

    print()
    print("=" * 60)
    print(f"  Extracted {len(stored)} trial(s)")
    print(f"  Total criteria:  {total_criteria}")
    print(f"  Mean / trial:    {total_criteria / len(stored):.1f}")
    print()
    print("  By kind:")
    for kind, count in sorted(kind_hist.items(), key=lambda x: -x[1]):
        print(f"    {kind:30s} {count:4d}")
    print()
    print("  By polarity:  " + ", ".join(f"{k}={v}" for k, v in polarity_hist.items()))
    print("  By mood:      " + ", ".join(f"{k}={v}" for k, v in mood_hist.items()))
    print(f"  Negated:      {negated}")
    print()
    print(f"  Tokens:       {total_in_tokens} in / {total_out_tokens} out")
    print(f"  Cost:         ${total_cost:.4f}")
    print(
        f"  Latency:      {total_latency / 1000.0:.1f}s total "
        f"({total_latency / len(stored):.0f}ms / trial avg)"
    )
    print("=" * 60)
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Max trials to process; 0 = all curated trials. Default 5.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-extract trials even if an output file already exists.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Render the prompt for the first selected trial and exit. "
        "No API call is made. Useful for prompt iteration.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging.")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    EXTRACTIONS_DIR.mkdir(parents=True, exist_ok=True)
    selected = _select_trials(args.limit)

    if args.dry_run:
        if not selected:
            print("no trials selected; nothing to render", file=sys.stderr)
            return 1
        nct_id = selected[0]
        trial = _load_trial(nct_id)
        msgs = build_messages(trial.eligibility_text)
        print(f"--- prompt for {nct_id} ({len(msgs)} messages) ---")
        for i, m in enumerate(msgs):
            print(f"\n[{i}] role={m['role']} ({len(m['content'])} chars)")
            preview = m["content"]
            if len(preview) > 600:
                preview = preview[:600] + " …"
            print(preview)
        return 0

    stored: list[StoredExtraction] = []
    started = time.monotonic()
    for nct_id in selected:
        out_path = _output_path(nct_id)
        if out_path.exists() and not args.force:
            logger.info("skipping %s (output exists; use --force to overwrite)", nct_id)
            stored.append(StoredExtraction.model_validate_json(out_path.read_text()))
            continue
        try:
            logger.info("extracting %s …", nct_id)
            result = _run_one(nct_id)
        except ExtractorRefusalError as e:
            logger.error("refusal on %s: %s", nct_id, e.refusal_text)
            continue
        except ExtractorError as e:
            logger.error("extractor error on %s: %s", nct_id, e)
            continue
        out_path.write_text(result.model_dump_json(indent=2))
        logger.info(
            "wrote %s (%d criteria, $%.4f)",
            out_path,
            len(result.extraction.criteria),
            result.meta.cost_usd or 0.0,
        )
        stored.append(result)

    wall_seconds = time.monotonic() - started
    logger.info("processed %d trial(s) in %.1fs wall", len(stored), wall_seconds)
    _print_summary(stored)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
