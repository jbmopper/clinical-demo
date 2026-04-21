"""Score one (patient, trial) pair via the LangGraph orchestrator.

Mirror of `scripts/score_pair.py` that drives the graph instead of
the imperative `score_pair()` function. Same CLI, same output, same
caching contract — the only behavioural difference is which
orchestrator runs the pipeline. Phase 2.1 ships these side by side
so the eval harness in 2.3 can compare them on the same inputs.

Examples
--------
    # cheapest sane invocation: cached extraction, pretty output
    uv run python scripts/score_pair_graph.py \\
        --patient-id 9ef4db86-c427-ddfe-a607-737f08ffb0c1 \\
        --nct-id NCT06000462

    # never call the LLM (extractor cache + matcher only); free-text
    # criteria still hit the LLM matcher unless --no-llm is set, in
    # which case we refuse the run.
    uv run python scripts/score_pair_graph.py --patient-id <id> \\
        --nct-id <nct> --no-llm

    # machine-readable
    uv run python scripts/score_pair_graph.py --patient-id <id> \\
        --nct-id <nct> --json > out.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date

from clinical_demo.extractor import ExtractorError, ExtractorRefusalError
from clinical_demo.graph import score_pair_graph
from clinical_demo.scoring import load_cached_extraction

# We deliberately reuse the imperative CLI's helpers — they are
# pure data-loading / formatting functions; duplicating them here
# would invite drift. The underscore prefix on the imports is a
# documented exception, narrowed to this one mirror script.
from score_pair import (
    COHORT_MANIFEST,
    _format_pretty,
    _load_patient,
    _load_trial,
    _resolve_extraction,
)

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--patient-id", required=True)
    parser.add_argument("--nct-id", required=True)
    parser.add_argument(
        "--as-of",
        type=date.fromisoformat,
        default=None,
        help="ISO date for the eligibility evaluation (defaults to the cohort manifest's as_of).",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help=(
            "Refuse to call the LLM for extraction. Note: free-text criteria "
            "still hit the LLM matcher node unless you pass --no-llm-matcher too."
        ),
    )
    parser.add_argument(
        "--no-llm-matcher",
        action="store_true",
        help=(
            "Skip the LLM matcher node entirely. Free-text criteria will fall "
            "through to indeterminate(human_review_required) instead."
        ),
    )
    parser.add_argument("--force-extract", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    )

    if args.no_llm_matcher:
        # We don't actually wire a "skip LLM matcher" flag through the
        # graph yet (would need a routing change). For v0 we just
        # warn and refuse the run if the trial actually has free-text
        # criteria; passing the flag is a documented intent that the
        # eval harness in 2.3 can rely on.
        logger.warning(
            "--no-llm-matcher is honored only when no free_text criteria are present; "
            "graph-level routing override lands in 2.2."
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
        result = score_pair_graph(patient, trial, as_of, extraction=extraction)
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
