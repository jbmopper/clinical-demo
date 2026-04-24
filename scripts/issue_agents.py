"""Materialize review findings into issue specs, prompts, and agent runs.

Examples
--------
    # write specs + prompts only
    uv run python scripts/issue_agents.py \
        --source issues/review_findings/2026-04-22-codex-review.md \
        --run-label 2026-04-22-codex-review

    # dispatch fixing agents and keep iterating until unresolved findings stabilize
    uv run python scripts/issue_agents.py \
        --source issues/review_findings/2026-04-22-codex-review.md \
        --run-label 2026-04-22-codex-review \
        --dispatch-agents
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from pathlib import Path

from clinical_demo.issues import (
    IssueBatchManifest,
    process_review_findings_file,
    refresh_review_findings_file,
)
from clinical_demo.observability import flush as flush_traces

DEFAULT_SOURCE = Path("issues/review_findings/2026-04-22-codex-review.md")
DEFAULT_TEMPLATE = Path("issues/templates/fix_from_issue_spec.md.tmpl")
DEFAULT_REFRESH_TEMPLATE = Path("issues/templates/refresh_review_findings.md.tmpl")
DEFAULT_OUTPUT_ROOT = Path("issues/generated")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    parser.add_argument("--refresh-template", type=Path, default=DEFAULT_REFRESH_TEMPLATE)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--run-label", default=None)
    parser.add_argument("--dispatch-agents", action="store_true")
    parser.add_argument(
        "--until-converged",
        action="store_true",
        help=(
            "Re-run the coordinator in fresh Python processes until a refreshed "
            "unresolved-findings review matches the previous iteration's input. "
            "This is enabled automatically with --dispatch-agents unless "
            "--single-pass is set."
        ),
    )
    parser.add_argument(
        "--single-pass",
        action="store_true",
        help="Disable recursive convergence and perform exactly one materialize/dispatch cycle.",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=5,
        help="Maximum recursive iterations when convergence mode is enabled.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume an existing batch directory instead of erroring when it already exists.",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help=(
            "When resuming, reuse unchanged spec/prompt artifacts and preserve prior "
            "successful dispatches instead of rewriting/rerunning them."
        ),
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help=(
            "When dispatching in resume mode, rerun only issues whose prior dispatch failed. "
            "New or changed issues still run."
        ),
    )
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument(
        "--codex-home-root",
        type=Path,
        default=None,
        help=(
            "Optional root for per-issue isolated CODEX_HOME directories. "
            "By default, dispatch inherits the current authenticated Codex home."
        ),
    )
    parser.add_argument("--model", default=None)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    )

    repo_root = Path(__file__).resolve().parents[1]
    source_path = _resolve(repo_root, args.source)
    template_path = _resolve(repo_root, args.template)
    refresh_template_path = _resolve(repo_root, args.refresh_template)
    output_root = _resolve(repo_root, args.output_root)
    run_label = args.run_label or source_path.stem

    try:
        if args.single_pass and args.until_converged:
            raise ValueError("--single-pass cannot be combined with --until-converged")
        if _use_convergence_loop(args):
            if args.max_iterations < 2:
                raise ValueError("--max-iterations must be at least 2 in convergence mode")
            manifest, exit_code, convergence_note = _run_until_converged(
                repo_root=repo_root,
                source_path=source_path,
                template_path=template_path,
                refresh_template_path=refresh_template_path,
                output_root=output_root,
                run_label=run_label,
                args=args,
            )
        else:
            manifest = _run_single_iteration(
                source_path=source_path,
                repo_root=repo_root,
                output_root=output_root,
                template_path=template_path,
                run_label=run_label,
                args=args,
            )
            exit_code = _exit_code_for_manifest(manifest)
            convergence_note = None
    except (FileNotFoundError, FileExistsError, ValueError) as exc:
        logging.getLogger(__name__).error("%s", exc)
        return 2

    if args.json:
        sys.stdout.write(manifest.model_dump_json(indent=2))
        sys.stdout.write("\n")
    else:
        sys.stdout.write(_render_summary(manifest, convergence_note=convergence_note))

    return exit_code


def _run_single_iteration(
    *,
    source_path: Path,
    repo_root: Path,
    output_root: Path,
    template_path: Path,
    run_label: str,
    args: argparse.Namespace,
) -> IssueBatchManifest:
    return process_review_findings_file(
        source_path,
        repo_root=repo_root,
        output_root=output_root,
        template_path=template_path,
        run_label=run_label,
        dispatch_agents=args.dispatch_agents,
        codex_bin=args.codex_bin,
        codex_home_root=args.codex_home_root,
        model=args.model,
        overwrite=args.overwrite,
        resume=args.resume,
        skip_existing=args.skip_existing,
        retry_failed=args.retry_failed,
    )


def _run_until_converged(
    *,
    repo_root: Path,
    source_path: Path,
    template_path: Path,
    refresh_template_path: Path,
    output_root: Path,
    run_label: str,
    args: argparse.Namespace,
) -> tuple[IssueBatchManifest, int, str]:
    logger = logging.getLogger(__name__)
    final_manifest: IssueBatchManifest | None = None
    current_source_path = source_path
    finder_dir = output_root / run_label / ".finder"

    for iteration in range(1, args.max_iterations + 1):
        logger.info("starting convergence iteration %s/%s", iteration, args.max_iterations)
        child_command = _build_iteration_command(
            repo_root=repo_root,
            source_path=current_source_path,
            template_path=template_path,
            output_root=output_root,
            run_label=run_label,
            args=args,
            iteration=iteration,
        )
        completed = subprocess.run(
            child_command,
            cwd=repo_root,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.stderr:
            sys.stderr.write(completed.stderr)
            sys.stderr.flush()
        if completed.returncode not in (0, 3):
            if completed.stdout:
                sys.stderr.write(completed.stdout)
                sys.stderr.flush()
            if final_manifest is None:
                raise ValueError(
                    "convergence iteration failed before producing a manifest "
                    f"(exit code {completed.returncode})"
                )
            note = f"stopped after iteration {iteration} returned exit code {completed.returncode}"
            logger.warning("%s for run %s", note, run_label)
            return final_manifest, completed.returncode, note

        try:
            manifest = IssueBatchManifest.model_validate_json(completed.stdout)
        except ValueError as exc:
            raise ValueError("convergence iteration did not return valid JSON output") from exc

        logger.info(
            "completed convergence iteration %s/%s for source %s",
            iteration,
            args.max_iterations,
            manifest.source_path,
        )
        final_manifest = manifest
        refreshed_path = finder_dir / f"iteration-{iteration:03d}-findings.md"
        finder_codex_home = (
            (args.codex_home_root / "_finder").resolve()
            if args.codex_home_root is not None
            else None
        )
        refresh_result = refresh_review_findings_file(
            current_source_path,
            repo_root=repo_root,
            refreshed_path=refreshed_path,
            template_path=refresh_template_path,
            codex_bin=args.codex_bin,
            codex_home=finder_codex_home,
            model=args.model,
        )
        if refresh_result.status != "succeeded":
            note = f"refresh failed after iteration {iteration}: {refresh_result.error}"
            logger.warning("%s", note)
            return manifest, 4, note

        if refresh_result.unchanged:
            note = (
                f"converged after {iteration} iterations "
                "(refreshed unresolved findings matched the input)"
            )
            logger.info("%s for run %s", note, run_label)
            return manifest, _exit_code_for_manifest(manifest), note

        current_source_path = refreshed_path

    assert final_manifest is not None
    note = f"did not converge after {args.max_iterations} iterations"
    logging.getLogger(__name__).warning("%s for run %s", note, run_label)
    return final_manifest, 4, note


def _build_iteration_command(
    *,
    repo_root: Path,
    source_path: Path,
    template_path: Path,
    output_root: Path,
    run_label: str,
    args: argparse.Namespace,
    iteration: int,
) -> list[str]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--source",
        str(source_path),
        "--template",
        str(template_path),
        "--output-root",
        str(output_root),
        "--run-label",
        run_label,
        "--json",
        "--single-pass",
    ]
    if args.dispatch_agents:
        command.append("--dispatch-agents")
    if args.verbose:
        command.append("--verbose")
    if args.codex_bin != "codex":
        command.extend(["--codex-bin", args.codex_bin])
    if args.codex_home_root is not None:
        command.extend(["--codex-home-root", str(_resolve(repo_root, args.codex_home_root))])
    if args.model is not None:
        command.extend(["--model", args.model])
    if iteration == 1:
        if args.overwrite:
            command.append("--overwrite")
        if args.resume:
            command.append("--resume")
        if args.skip_existing:
            command.append("--skip-existing")
    else:
        command.extend(["--resume", "--skip-existing"])
    if args.retry_failed:
        command.append("--retry-failed")
    return command


def _exit_code_for_manifest(manifest: IssueBatchManifest) -> int:
    failed_dispatches = sum(1 for issue in manifest.issues if issue.dispatch.status == "failed")
    return 0 if failed_dispatches == 0 else 3


def _use_convergence_loop(args: argparse.Namespace) -> bool:
    return args.until_converged or (args.dispatch_agents and not args.single_pass)


def _resolve(repo_root: Path, path: Path) -> Path:
    return path if path.is_absolute() else repo_root / path


def _render_summary(manifest: IssueBatchManifest, *, convergence_note: str | None = None) -> str:
    lines = [
        f"\nIssue batch {manifest.run_label}",
        f"  source: {manifest.source_path}",
        f"  template: {manifest.template_path}",
        f"  issues: {len(manifest.issues)}",
    ]
    if convergence_note is not None:
        lines.append(f"  convergence: {convergence_note}")
    for issue in manifest.issues:
        dispatch = issue.dispatch
        lines.append(f"  - {issue.issue_id} [{issue.priority}] {issue.title}")
        lines.append(f"    spec: {issue.spec_path}")
        lines.append(f"    prompt: {issue.prompt_path}")
        if issue.reused_existing:
            lines.append("    artifacts: reused")
        lines.append(f"    dispatch: {dispatch.status}")
        if dispatch.reused_previous:
            lines.append("    dispatch_reused: true")
        if dispatch.skip_reason:
            lines.append(f"    dispatch_note: {dispatch.skip_reason}")
        if dispatch.thread_id:
            lines.append(f"    thread_id: {dispatch.thread_id}")
        if dispatch.log_path:
            lines.append(f"    log: {dispatch.log_path}")
        if dispatch.error:
            lines.append(f"    error: {dispatch.error}")
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    rc = main()
    flush_traces()
    raise SystemExit(rc)
