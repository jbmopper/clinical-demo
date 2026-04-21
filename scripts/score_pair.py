"""Score one (patient, trial) pair end-to-end and print the verdicts.

Loads the patient from the curated cohort, the trial from the
curated trials, runs the extractor (or re-uses a cached extraction),
runs the deterministic matcher, and prints a per-criterion verdict
table plus the rollup. This is the demo loop the partner sees in
the 20-minute presentation: one CLI command, in, with citations.

Examples
--------
    # cheapest sane invocation: cached extraction, pretty output
    uv run python scripts/score_pair.py \\
        --patient-id 9ef4db86-c427-ddfe-a607-737f08ffb0c1 \\
        --nct-id NCT06000462

    # use a different evaluation as-of date
    uv run python scripts/score_pair.py \\
        --patient-id <id> --nct-id <nct> --as-of 2024-06-01

    # never call the LLM — fail loudly if no cached extraction exists
    uv run python scripts/score_pair.py --patient-id <id> \\
        --nct-id <nct> --no-llm

    # re-extract even if a cached envelope is present
    uv run python scripts/score_pair.py --patient-id <id> \\
        --nct-id <nct> --force-extract

    # machine-readable
    uv run python scripts/score_pair.py --patient-id <id> \\
        --nct-id <nct> --json > out.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

from clinical_demo.data.clinicaltrials import trial_from_raw
from clinical_demo.data.synthea import iter_bundles
from clinical_demo.domain.patient import Patient
from clinical_demo.domain.trial import Trial
from clinical_demo.extractor import ExtractorError, ExtractorRefusalError
from clinical_demo.scoring import (
    ScorePairResult,
    cache_path_for,
    load_cached_extraction,
    score_pair,
)

logger = logging.getLogger(__name__)

CURATED_TRIALS_DIR = Path("data/curated/trials")
COHORT_MANIFEST = Path("data/curated/cohort_manifest.json")
EXTRACTIONS_DIR = Path("data/curated/extractions")


def _load_trial(nct_id: str) -> Trial:
    raw_path = CURATED_TRIALS_DIR / f"{nct_id}.json"
    raw = json.loads(raw_path.read_text())
    return trial_from_raw(raw)


def _load_patient(patient_id: str) -> Patient:
    """Locate one patient by id in the curated cohort.

    Reads the cohort manifest only to confirm the patient is in the
    curated set (and to discover the synthea_dir); the actual Patient
    is parsed from the source FHIR bundle via `iter_bundles`. We
    short-circuit as soon as the patient is found.
    """
    if not COHORT_MANIFEST.exists():
        raise FileNotFoundError(
            f"Cohort manifest not found at {COHORT_MANIFEST}; run "
            f"`uv run python scripts/curate_cohort.py` first."
        )
    cohort = json.loads(COHORT_MANIFEST.read_text())
    member_ids = {m["patient_id"] for m in cohort["members"]}
    if patient_id not in member_ids:
        raise ValueError(
            f"patient_id {patient_id!r} is not in the curated cohort "
            f"({len(member_ids)} members). Pick one from "
            f"{COHORT_MANIFEST}."
        )
    synthea_dir = Path(cohort["synthea_dir"])
    for patient in iter_bundles(synthea_dir):
        if patient.patient_id == patient_id:
            return patient
    raise FileNotFoundError(
        f"patient_id {patient_id!r} listed in the cohort manifest but no "
        f"matching FHIR bundle was found in {synthea_dir}."
    )


def _resolve_extraction(
    *, nct_id: str, force_extract: bool, no_llm: bool
) -> tuple[bool, Path] | None:
    """Decide where the extraction will come from for this run.

    Returns (cached, path) if we should reuse an existing envelope,
    or None if the caller should fall back to a live LLM call.
    Raises FileNotFoundError under `--no-llm` if nothing is cached.
    """
    cache_file = cache_path_for(nct_id, EXTRACTIONS_DIR)
    if cache_file.exists() and not force_extract:
        return (True, cache_file)
    if no_llm:
        raise FileNotFoundError(
            f"--no-llm requires a cached extraction at {cache_file}; "
            f"run `scripts/extract_criteria.py` first or drop --no-llm."
        )
    return None


def _format_pretty(result: ScorePairResult) -> str:
    """One-screen human-readable rendering of a ScorePairResult.

    Goal: the reviewer can read the rollup, the cost, and every
    criterion with its top citation in under a screen of terminal.
    Long source_text is truncated; the JSON dump is the source of
    truth for full audit detail.
    """
    lines: list[str] = []
    lines.append(
        f"\nPatient {result.patient_id}  vs  Trial {result.nct_id}"
        f"   (as_of={result.as_of.isoformat()})"
    )
    lines.append(f"  Eligibility rollup: {result.eligibility.upper()}")
    meta = result.extraction_meta
    total_tokens = (meta.input_tokens or 0) + (meta.output_tokens or 0)
    cost_str = f"${meta.cost_usd:.4f}" if meta.cost_usd is not None else "n/a"
    latency_str = f"{meta.latency_ms:.0f}ms" if meta.latency_ms is not None else "n/a"
    lines.append(
        f"  Extraction: model={meta.model} prompt={meta.prompt_version} "
        f"cost={cost_str} tokens={total_tokens} latency={latency_str}"
    )
    lines.append(
        f"  Verdicts: total={result.summary.total_criteria}  "
        + "  ".join(f"{k}={v}" for k, v in sorted(result.summary.by_verdict.items()))
    )
    if result.summary.by_reason:
        lines.append(
            "  Reasons:  "
            + "  ".join(f"{k}={v}" for k, v in sorted(result.summary.by_reason.items()))
        )

    lines.append("")
    for i, v in enumerate(result.verdicts, start=1):
        polarity_tag = "[INC]" if v.criterion.polarity == "inclusion" else "[EXC]"
        verdict_tag = {"pass": "PASS", "fail": "FAIL", "indeterminate": "????"}[v.verdict]
        kind_tag = v.criterion.kind
        text = v.criterion.source_text.replace("\n", " ").strip()
        if len(text) > 80:
            text = text[:77] + "..."
        lines.append(f"  {i:>3}. {polarity_tag} {verdict_tag} ({kind_tag}) {text}")
        lines.append(f"       reason={v.reason}: {v.rationale}")
        for ev in v.evidence[:2]:
            lines.append(f"       evidence[{ev.kind}]: {ev.note}")
        if len(v.evidence) > 2:
            lines.append(f"       (+ {len(v.evidence) - 2} more evidence row(s))")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--patient-id",
        required=True,
        help="Patient id from the curated cohort (data/curated/cohort_manifest.json).",
    )
    parser.add_argument(
        "--nct-id",
        required=True,
        help="Trial id from the curated trials manifest (data/curated/trials_manifest.json).",
    )
    parser.add_argument(
        "--as-of",
        type=date.fromisoformat,
        default=None,
        help="ISO date for the eligibility evaluation (defaults to the cohort manifest's as_of).",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Refuse to call the LLM; require a cached extraction. Fails loudly if the cache miss.",
    )
    parser.add_argument(
        "--force-extract",
        action="store_true",
        help="Ignore the cached extraction and re-extract from scratch.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Write the ScorePairResult as JSON to stdout instead of the pretty table.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    )

    try:
        trial = _load_trial(args.nct_id)
        patient = _load_patient(args.patient_id)
    except (FileNotFoundError, ValueError) as e:
        logger.error("setup error: %s", e)
        return 2

    as_of = args.as_of
    if as_of is None:
        cohort = json.loads(COHORT_MANIFEST.read_text())
        as_of = date.fromisoformat(cohort["as_of"])

    try:
        cache_decision = _resolve_extraction(
            nct_id=args.nct_id, force_extract=args.force_extract, no_llm=args.no_llm
        )
    except FileNotFoundError as e:
        logger.error("%s", e)
        return 2

    extraction = None
    if cache_decision is not None:
        cached, cache_file = cache_decision
        assert cached
        logger.info("loading cached extraction from %s", cache_file)
        extraction = load_cached_extraction(cache_file)
    else:
        logger.info("extracting trial %s from scratch (LLM call) …", args.nct_id)

    try:
        result = score_pair(patient, trial, as_of, extraction=extraction)
    except ExtractorRefusalError as e:
        logger.error("extractor refused: %s", e.refusal_text)
        return 3
    except ExtractorError as e:
        logger.error("extractor error: %s", e)
        return 3

    if args.json:
        sys.stdout.write(result.model_dump_json(indent=2))
        sys.stdout.write("\n")
    else:
        sys.stdout.write(_format_pretty(result))

    return 0


if __name__ == "__main__":
    from clinical_demo.observability import flush as _flush_traces

    rc = main()
    _flush_traces()
    raise SystemExit(rc)
