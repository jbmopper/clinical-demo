"""Eval harness CLI: `eval run` and `eval report`.

One-shot subcommands. `run` scores every (or a subset of) pair
in the eval seed, persists the run to `eval/runs.sqlite`, and
prints a one-screen summary. `report` re-renders that summary
for any persisted run by id.

Examples
--------
    # full run on the imperative score_pair, cached extractions only
    uv run python scripts/eval.py run --no-llm \\
        --notes "score_pair imperative, cached extractions"

    # smoke run via the LangGraph orchestrator on 3 pairs
    uv run python scripts/eval.py run --orchestrator graph --limit 3 \\
        --notes "score_pair_graph smoke"

    # re-render a past run's summary
    uv run python scripts/eval.py report --run-id <id>

    # see what runs are persisted
    uv run python scripts/eval.py report
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

from clinical_demo.data.chia import ChiaDocument
from clinical_demo.data.chia import iter_trials as iter_chia_trials
from clinical_demo.data.clinicaltrials import trial_from_raw
from clinical_demo.data.synthea import iter_bundles
from clinical_demo.domain.patient import Patient
from clinical_demo.domain.trial import Trial
from clinical_demo.evals.diagnostics import (
    build_diagnostics,
    diagnostics_to_json,
    load_diagnostics,
    load_layer_one,
    render_diagnostics,
    write_diagnostics,
)
from clinical_demo.evals.layer_one import build_layer_one_report
from clinical_demo.evals.layer_two import build_layer_two_report, score_chia_document
from clinical_demo.evals.report_layer_one import render_layer_one
from clinical_demo.evals.report_layer_two import render_layer_two
from clinical_demo.evals.run import EvalCase, RunResult, load_dataset, run_eval
from clinical_demo.evals.store import list_runs, load_run, open_store, save_run
from clinical_demo.extractor import ExtractionResult, extract_criteria
from clinical_demo.scoring import (
    StoredExtraction,
    cache_path_for,
    load_cached_extraction,
    score_pair,
)
from clinical_demo.scoring.score_pair import ScorePairResult

# Default paths mirror scripts/score_pair.py to keep the demo
# loops aligned. The DB path is configurable but defaults to the
# `eval/` directory at the repo root (PLAN §6 task 2.7).
DEFAULT_SEED = Path("data/curated/eval_seed.json")
DEFAULT_DB = Path("eval/runs.sqlite")
CURATED_TRIALS_DIR = Path("data/curated/trials")
COHORT_MANIFEST = Path("data/curated/cohort_manifest.json")
EXTRACTIONS_DIR = Path("data/curated/extractions")
DEFAULT_CHIA_DIR = Path("data/raw/chia")
CHIA_EXTRACTIONS_DIR = Path("data/curated/chia_extractions")
DEFAULT_CHIA_SAMPLE_SEED = 20260430

ChiaDocRef = tuple[str, str, ChiaDocument]


# --------------------- loaders (shared with scripts/score_pair.py)
# Kept inline here rather than promoted to clinical_demo.data;
# when a third caller appears, refactor.


def _load_trial(nct_id: str) -> Trial:
    raw_path = CURATED_TRIALS_DIR / f"{nct_id}.json"
    return trial_from_raw(json.loads(raw_path.read_text()))


_patient_cache: dict[str, Patient] = {}


def _load_patient(patient_id: str) -> Patient:
    """Locate one patient by id; cache across calls.

    The eval runner scores N pairs in one process; iterating the
    full Synthea bundle directory once per pair is wasteful. Cache
    by patient_id so repeated patients (the seed allows up to
    `max_pairs_per_patient=2`) cost one bundle parse, not two."""
    if patient_id in _patient_cache:
        return _patient_cache[patient_id]
    if not COHORT_MANIFEST.exists():
        raise FileNotFoundError(
            f"Cohort manifest not found at {COHORT_MANIFEST}; run "
            f"`uv run python scripts/curate_cohort.py` first."
        )
    cohort = json.loads(COHORT_MANIFEST.read_text())
    synthea_dir = Path(cohort["synthea_dir"])
    for patient in iter_bundles(synthea_dir):
        _patient_cache[patient.patient_id] = patient
        if patient.patient_id == patient_id:
            return patient
    raise FileNotFoundError(f"patient_id {patient_id!r} not found under {synthea_dir}.")


# --------------------- scorer factories


def _make_scorer(
    orchestrator: Literal["imperative", "graph"],
    *,
    no_llm: bool,
    critic_enabled: bool,
):
    """Build a `Scorer` callable bound to one orchestrator + one
    extraction policy. The runner is orchestrator-agnostic (D-59);
    the *script* knows which one to assemble."""

    def _scorer(case: EvalCase) -> ScorePairResult:
        patient = _load_patient(case.patient_id)
        trial = _load_trial(case.nct_id)
        extraction = None
        if no_llm:
            cache_file = cache_path_for(case.nct_id, EXTRACTIONS_DIR)
            if not cache_file.exists():
                raise FileNotFoundError(f"--no-llm requires cached extraction at {cache_file}")
            extraction = load_cached_extraction(cache_file)
        if orchestrator == "imperative":
            return score_pair(patient, trial, case.as_of, extraction=extraction)
        from clinical_demo.graph import score_pair_graph

        return score_pair_graph(
            patient,
            trial,
            case.as_of,
            extraction=extraction,
            critic_enabled=critic_enabled,
        )

    return _scorer


def _apply_binding_strategy(strategy: Literal["alias", "two_pass"] | None) -> None:
    """Override the process-wide binding strategy for this CLI run.

    Settings are cached, and the terminology resolver is cached from
    settings. If the operator passes `--binding-strategy`, clear both
    before the scorer starts so every criterion in the run sees the
    same requested mode."""
    if strategy is None:
        return
    os.environ["BINDING_STRATEGY"] = strategy
    from clinical_demo.settings import get_settings
    from clinical_demo.terminology.resolver import get_resolver

    get_settings.cache_clear()
    get_resolver.cache_clear()


# --------------------- Chia layer-2 helpers


def _iter_chia_documents(chia_dir: Path) -> Iterator[ChiaDocRef]:
    for trial in iter_chia_trials(chia_dir):
        if trial.inclusion is not None:
            yield trial.nct_id, "inclusion", trial.inclusion
        if trial.exclusion is not None:
            yield trial.nct_id, "exclusion", trial.exclusion


def _chia_eval_text(document: ChiaDocument, *, section: str) -> str:
    header = "Inclusion Criteria" if section == "inclusion" else "Exclusion Criteria"
    return f"{header}:\n{document.source_text}"


def _select_chia_documents(
    docs: list[ChiaDocRef],
    *,
    sample_size: int | None,
    sample_seed: int,
) -> list[ChiaDocRef]:
    """Optionally take a deterministic retained sample of Chia documents."""

    if sample_size is None:
        return docs
    if sample_size < 1:
        raise ValueError("--sample-size must be positive")
    if sample_size >= len(docs):
        return docs
    selected = random.Random(sample_seed).sample(docs, sample_size)
    return sorted(selected, key=lambda row: row[2].doc_id)


def _write_chia_sample_manifest(
    path: Path,
    *,
    chia_dir: Path,
    docs: list[ChiaDocRef],
    sample_size: int | None,
    sample_seed: int,
) -> None:
    """Persist the exact Chia docs included in a retained-sample run."""

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "chia_dir": str(chia_dir),
        "sample_size": sample_size,
        "sample_seed": sample_seed,
        "documents": [
            {
                "doc_id": document.doc_id,
                "nct_id": nct_id,
                "section": section,
            }
            for nct_id, section, document in docs
        ],
    }
    path.write_text(json.dumps(payload, indent=2) + "\n")


def _load_or_extract_chia_document(
    document: ChiaDocument,
    *,
    section: str,
    cache_dir: Path,
    force: bool,
    no_llm: bool,
) -> ExtractionResult:
    cache_file = cache_path_for(document.doc_id, cache_dir)
    if cache_file.exists() and not force:
        return load_cached_extraction(cache_file)
    if no_llm:
        raise FileNotFoundError(f"--no-llm requires cached extraction at {cache_file}")

    result = extract_criteria(_chia_eval_text(document, section=section))
    cache_dir.mkdir(parents=True, exist_ok=True)
    stored = StoredExtraction(
        nct_id=document.doc_id,
        extraction=result.extracted,
        meta=result.meta,
    )
    cache_file.write_text(stored.model_dump_json(indent=2) + "\n")
    return result


# --------------------- summary rendering


def _summarize(run: RunResult) -> str:
    """One-screen summary of a run; same shape `eval report` prints."""
    elig: dict[str, int] = {"pass": 0, "fail": 0, "indeterminate": 0}
    by_slice: dict[str, dict[str, int]] = {}
    total_cost = 0.0
    total_latency = 0.0
    n_with_cost = 0
    for c in run.cases:
        if c.result is None:
            continue
        elig[c.result.eligibility] = elig.get(c.result.eligibility, 0) + 1
        slot = by_slice.setdefault(
            c.case.slice or "(none)",
            {"pass": 0, "fail": 0, "indeterminate": 0},
        )
        slot[c.result.eligibility] = slot.get(c.result.eligibility, 0) + 1
        if c.result.extraction_meta.cost_usd is not None:
            total_cost += c.result.extraction_meta.cost_usd
            n_with_cost += 1
        total_latency += c.scoring_latency_ms

    lines: list[str] = []
    lines.append(f"\nRun {run.run_id}")
    lines.append(f"  notes: {run.notes or '(none)'}")
    lines.append(f"  dataset: {run.dataset_path}")
    lines.append(
        f"  started: {run.started_at.isoformat(timespec='seconds')}"
        f"  finished: {run.finished_at.isoformat(timespec='seconds')}"
    )
    lines.append(
        f"  cases: {run.n_cases}  errors: {run.n_errors}"
        f"  total scoring latency: {total_latency / 1000:.1f}s"
    )
    if n_with_cost:
        lines.append(f"  extraction cost (sum over {n_with_cost} cases): ${total_cost:.4f}")
    lines.append("  eligibility: " + "  ".join(f"{k}={v}" for k, v in sorted(elig.items())))
    if by_slice:
        lines.append("  by slice:")
        for slice_, counts in sorted(by_slice.items()):
            counts_str = "  ".join(f"{k}={v}" for k, v in sorted(counts.items()))
            lines.append(f"    {slice_:<24} {counts_str}")
    if run.n_errors:
        lines.append("  failed cases:")
        for c in run.cases:
            if c.error:
                lines.append(f"    {c.case.pair_id}: {c.error}")
    lines.append("")
    return "\n".join(lines)


# --------------------- subcommands


def _cmd_run(args: argparse.Namespace) -> int:
    seed_path = Path(args.dataset)
    if not seed_path.exists():
        print(f"error: dataset {seed_path} not found", file=sys.stderr)
        return 1
    _apply_binding_strategy(args.binding_strategy)

    pair_ids = set(args.pair_id) if args.pair_id else None
    cases = load_dataset(seed_path, pair_ids=pair_ids, limit=args.limit)
    if not cases:
        print("error: no cases matched the filter", file=sys.stderr)
        return 1

    scorer = _make_scorer(
        args.orchestrator,
        no_llm=args.no_llm,
        critic_enabled=args.critic_enabled,
    )

    def _progress(record):
        status = "ok" if record.error is None else "ERR"
        print(
            f"  [{status:>3}] {record.case.pair_id}  ({record.scoring_latency_ms:.0f}ms)",
            file=sys.stderr,
        )

    print(f"running {len(cases)} case(s)...", file=sys.stderr)
    run = run_eval(
        scorer,
        cases,
        dataset_path=seed_path,
        notes=_notes_with_binding_strategy(args.notes, args.binding_strategy),
        on_case_done=_progress,
    )

    with open_store(args.db) as conn:
        save_run(conn, run)

    print(_summarize(run))
    return 0 if run.n_errors == 0 else 2


def _cmd_chia(args: argparse.Namespace) -> int:
    chia_dir = Path(args.chia_dir)
    if not chia_dir.exists():
        print(f"error: Chia directory {chia_dir} not found", file=sys.stderr)
        return 1
    if args.limit is not None and args.sample_size is not None:
        print("error: use either --limit or --sample-size, not both", file=sys.stderr)
        return 1

    selected = set(args.doc_id) if args.doc_id else None
    docs = [
        (nct_id, section, document)
        for nct_id, section, document in _iter_chia_documents(chia_dir)
        if selected is None or document.doc_id in selected
    ]
    try:
        docs = _select_chia_documents(
            docs[: args.limit] if args.limit is not None else docs,
            sample_size=args.sample_size,
            sample_seed=args.sample_seed,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not docs:
        print("error: no Chia documents matched the filter", file=sys.stderr)
        return 1
    if args.write_sample_manifest is not None:
        _write_chia_sample_manifest(
            Path(args.write_sample_manifest),
            chia_dir=chia_dir,
            docs=docs,
            sample_size=args.sample_size,
            sample_seed=args.sample_seed,
        )

    cache_dir = Path(args.cache_dir)
    reports = []
    total_cost = 0.0
    n_with_cost = 0
    print(f"running Chia layer-2 eval on {len(docs)} document(s)...", file=sys.stderr)
    for nct_id, section, document in docs:
        try:
            extraction = _load_or_extract_chia_document(
                document,
                section=section,
                cache_dir=cache_dir,
                force=args.force,
                no_llm=args.no_llm,
            )
        except Exception as exc:
            print(f"error: {document.doc_id}: {exc}", file=sys.stderr)
            return 2
        if extraction.meta.cost_usd is not None:
            total_cost += extraction.meta.cost_usd
            n_with_cost += 1
        report = score_chia_document(
            document,
            extraction.extracted,
            nct_id=nct_id,
            section=section,
        )
        reports.append(report)
        print(
            f"  [ ok] {document.doc_id}  gold={report.gold} pred={report.predicted} "
            f"tp={report.true_positive} f1={report.f1 if report.f1 is not None else 'n/a'}",
            file=sys.stderr,
        )

    aggregate = build_layer_two_report(reports)
    if args.output_json is not None:
        out = Path(args.output_json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(aggregate.model_dump_json(indent=2) + "\n")
    if n_with_cost:
        print(
            f"extraction cost (sum over {n_with_cost} document(s)): ${total_cost:.4f}",
            file=sys.stderr,
        )
    if args.format == "json":
        print(aggregate.model_dump_json(indent=2))
    else:
        print(render_layer_two(aggregate))
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"error: no store at {db_path} (run an eval first?)", file=sys.stderr)
        return 1

    with open_store(args.db) as conn:
        if args.run_id is None:
            runs = list_runs(conn)
            if not runs:
                print("(no runs persisted)", file=sys.stderr)
                return 0
            for r in runs:
                print(
                    f"  {r['run_id']}  {r['started_at']}"
                    f"  cases={r['n_cases']:<4} errors={r['n_errors']}"
                    f"  notes={r['notes']!r}"
                )
            return 0
        try:
            run = load_run(conn, args.run_id)
        except KeyError:
            print(f"error: no run with id {args.run_id!r}", file=sys.stderr)
            return 1

    if args.layer == 1:
        report = build_layer_one_report(run)
        if args.format == "json":
            print(report.model_dump_json(indent=2))
        else:
            print(render_layer_one(report))
        return 0
    if args.diagnostics:
        diagnostics = build_diagnostics(run)
        if args.write_diagnostics is not None:
            write_diagnostics(args.write_diagnostics, diagnostics)
        if args.format == "json":
            print(diagnostics_to_json(diagnostics))
        else:
            baseline = (
                load_diagnostics(args.baseline_diagnostics) if args.baseline_diagnostics else None
            )
            baseline_layer_one = (
                load_layer_one(args.baseline_layer1) if args.baseline_layer1 else None
            )
            print(
                render_diagnostics(
                    diagnostics,
                    baseline=baseline,
                    layer_one=build_layer_one_report(run),
                    baseline_layer_one=baseline_layer_one,
                )
            )
        return 0
    if args.format == "json":
        print(run.model_dump_json(indent=2))
    else:
        print(_summarize(run))
    return 0


def _notes_with_binding_strategy(
    notes: str,
    strategy: Literal["alias", "two_pass"] | None,
) -> str:
    if strategy is None:
        return notes
    suffix = f"binding_strategy={strategy}"
    return f"{notes}; {suffix}" if notes else suffix


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="Score the dataset and persist results.")
    p_run.add_argument("--dataset", default=str(DEFAULT_SEED))
    p_run.add_argument("--db", default=str(DEFAULT_DB))
    p_run.add_argument(
        "--orchestrator",
        choices=("imperative", "graph"),
        default="imperative",
        help="Which scoring entry point to use.",
    )
    p_run.add_argument(
        "--no-llm",
        action="store_true",
        help="Require a cached extraction; never call the LLM.",
    )
    p_run.add_argument(
        "--critic-enabled",
        action="store_true",
        help="Only meaningful with --orchestrator=graph.",
    )
    p_run.add_argument(
        "--binding-strategy",
        choices=("alias", "two_pass"),
        default=None,
        help="Override Settings.binding_strategy for this run.",
    )
    p_run.add_argument(
        "--pair-id",
        action="append",
        default=None,
        help="Filter to one or more pair_ids (repeatable).",
    )
    p_run.add_argument("--limit", type=int, default=None)
    p_run.add_argument("--notes", default="")
    p_run.set_defaults(func=_cmd_run)

    p_chia = sub.add_parser(
        "chia",
        help="Run layer-2 extractor entity-mention F1 against Chia.",
    )
    p_chia.add_argument("--chia-dir", default=str(DEFAULT_CHIA_DIR))
    p_chia.add_argument("--cache-dir", default=str(CHIA_EXTRACTIONS_DIR))
    p_chia.add_argument("--limit", type=int, default=None)
    p_chia.add_argument(
        "--sample-size",
        type=int,
        default=None,
        help="Deterministically sample this many Chia docs before running.",
    )
    p_chia.add_argument(
        "--sample-seed",
        type=int,
        default=DEFAULT_CHIA_SAMPLE_SEED,
        help="Seed for --sample-size retained-sample selection.",
    )
    p_chia.add_argument(
        "--write-sample-manifest",
        default=None,
        help="Optional path to persist the exact selected Chia document ids.",
    )
    p_chia.add_argument(
        "--doc-id",
        action="append",
        default=None,
        help="Filter to one or more Chia document ids like NCT00050349_inc.",
    )
    p_chia.add_argument(
        "--no-llm",
        action="store_true",
        help="Require cached Chia extractions; never call the LLM.",
    )
    p_chia.add_argument(
        "--force",
        action="store_true",
        help="Ignore existing Chia extraction cache and regenerate.",
    )
    p_chia.add_argument("--format", choices=("text", "json"), default="text")
    p_chia.add_argument(
        "--output-json",
        default=None,
        help="Optional path to write the layer-2 report JSON.",
    )
    p_chia.set_defaults(func=_cmd_chia)

    p_report = sub.add_parser("report", help="Render a persisted run; or list runs.")
    p_report.add_argument("--db", default=str(DEFAULT_DB))
    p_report.add_argument("--run-id", default=None)
    p_report.add_argument("--format", choices=("text", "json"), default="text")
    p_report.add_argument(
        "--diagnostics",
        action="store_true",
        help="Render D-69 slice-5 diagnostics for a run.",
    )
    p_report.add_argument(
        "--baseline-diagnostics",
        default=None,
        help="Optional EvalDiagnostics JSON baseline for --diagnostics deltas.",
    )
    p_report.add_argument(
        "--baseline-layer1",
        default=None,
        help="Optional LayerOneReport JSON baseline for --diagnostics agreement/coverage deltas.",
    )
    p_report.add_argument(
        "--write-diagnostics",
        default=None,
        help="Write the computed diagnostics JSON to this path.",
    )
    p_report.add_argument(
        "--layer",
        type=int,
        choices=(1,),
        default=None,
        help="Layer-specific report (currently only layer 1 implemented).",
    )
    p_report.set_defaults(func=_cmd_report)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
