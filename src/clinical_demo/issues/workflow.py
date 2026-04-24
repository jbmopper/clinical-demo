"""Review-finding ingestion -> issue specs -> agent prompts -> dispatch.

The repo already has a strong pattern for:
  - typed Pydantic envelopes for persisted artifacts,
  - one-shot CLIs under ``scripts/``, and
  - durable lifecycle tracing through ``clinical_demo.observability``.

This module follows that same shape. It turns a markdown review-findings
document into machine-readable issue specs plus agent-ready prompt files,
and can optionally dispatch one Codex run per issue.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from string import Template
from typing import Literal

from pydantic import BaseModel, Field

from ..observability import traced

logger = logging.getLogger(__name__)
_CODEX_HOME_BOOTSTRAP_FILES = ("auth.json", "config.toml", "version.json", "installation_id")
_CODEX_HOME_BOOTSTRAP_DIRS = ("rules",)

_FINDING_HEADER_RE = re.compile(
    r"^## Finding (?P<number>\d+) \((?P<location>.+?)\)(?: \[(?P<status>[^\]]+)\])?\s*$",
    re.MULTILINE,
)
_PRIORITY_LINE_RE = re.compile(r"^\[(?P<priority>P\d+)\]\s+(?P<title>.+)$")
_LOCATION_RE = re.compile(r"^(?P<file>.+?)(?::(?P<start>\d+)(?:-(?P<end>\d+))?)?$")


class IssueLocation(BaseModel):
    """File + line metadata attached to a review finding."""

    file: str
    start_line: int | None = None
    end_line: int | None = None


class ReviewFinding(BaseModel):
    """One parsed review finding from a markdown review note."""

    finding_number: int
    status: str | None = None
    priority: str
    title: str
    body: str
    location: IssueLocation
    raw_markdown: str

    @property
    def issue_id(self) -> str:
        slug = _slugify(self.title)
        return f"finding-{self.finding_number:03d}-{slug}"


class IssueSpecification(BaseModel):
    """Machine-readable issue handoff for an autonomous fixing agent."""

    issue_id: str
    finding_number: int
    status: str | None = None
    priority: str
    title: str
    summary: str
    problem_statement: str
    source_location: IssueLocation
    labels: list[str] = Field(default_factory=list)
    related_tests: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    verification_commands: list[str] = Field(default_factory=list)
    raw_finding_markdown: str


class AgentDispatchResult(BaseModel):
    """Outcome of an optional ``codex exec`` dispatch."""

    status: Literal["not_requested", "succeeded", "failed"] = "not_requested"
    command: list[str] = Field(default_factory=list)
    log_path: str | None = None
    last_message_path: str | None = None
    thread_id: str | None = None
    return_code: int | None = None
    error: str | None = None
    reused_previous: bool = False
    skip_reason: str | None = None


class IssueArtifact(BaseModel):
    """Paths and dispatch metadata for one materialized issue."""

    issue_id: str
    priority: str
    title: str
    spec_path: str
    prompt_path: str
    spec_sha256: str = ""
    prompt_sha256: str = ""
    reused_existing: bool = False
    dispatch: AgentDispatchResult = Field(default_factory=AgentDispatchResult)


class IssueBatchManifest(BaseModel):
    """Top-level record for one review-finding processing run."""

    run_label: str
    created_at: datetime
    repo_root: str
    source_path: str
    template_path: str
    source_sha256: str = ""
    template_sha256: str = ""
    convergence_sha256: str = ""
    issues: list[IssueArtifact]


class ReviewRefreshResult(BaseModel):
    """Outcome of re-reviewing the current repo against a findings file."""

    status: Literal["succeeded", "failed"]
    source_path: str
    refreshed_path: str
    source_sha256: str
    refreshed_sha256: str | None = None
    unchanged: bool = False
    command: list[str] = Field(default_factory=list)
    log_path: str | None = None
    last_message_path: str | None = None
    thread_id: str | None = None
    return_code: int | None = None
    error: str | None = None


def parse_review_findings(markdown: str) -> list[ReviewFinding]:
    """Parse the review-findings markdown format used in the repo review UI."""

    matches = list(_FINDING_HEADER_RE.finditer(markdown))
    findings: list[ReviewFinding] = []
    for index, match in enumerate(matches):
        block_start = match.start()
        block_end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        raw_block = markdown[block_start:block_end].strip()

        header_end = match.end()
        payload = markdown[header_end:block_end].strip()
        if not payload:
            raise ValueError(f"finding {match.group('number')} is missing its title/body block")

        first_line, *rest = payload.splitlines()
        priority_match = _PRIORITY_LINE_RE.match(first_line.strip())
        if priority_match is None:
            raise ValueError(
                f"finding {match.group('number')} does not start with a '[P?] Title' line"
            )

        location = _parse_location(match.group("location"))
        body = "\n".join(rest).strip()
        findings.append(
            ReviewFinding(
                finding_number=int(match.group("number")),
                status=match.group("status"),
                priority=priority_match.group("priority"),
                title=priority_match.group("title").strip(),
                body=body,
                location=location,
                raw_markdown=raw_block,
            )
        )
    return findings


def build_issue_specification(
    finding: ReviewFinding,
    *,
    repo_root: Path,
) -> IssueSpecification:
    """Derive a concrete issue spec from one parsed review finding."""

    related_tests = _discover_related_tests(repo_root, finding.location.file)
    return IssueSpecification(
        issue_id=finding.issue_id,
        finding_number=finding.finding_number,
        status=finding.status,
        priority=finding.priority,
        title=finding.title,
        summary=_summarize_finding(finding),
        problem_statement=finding.body,
        source_location=finding.location,
        labels=_build_labels(finding.location.file, finding.priority),
        related_tests=related_tests,
        acceptance_criteria=_acceptance_criteria_for(finding),
        constraints=_constraints_for(finding),
        verification_commands=_verification_commands_for(finding.location.file, related_tests),
        raw_finding_markdown=finding.raw_markdown,
    )


def render_agent_prompt(
    spec: IssueSpecification,
    *,
    repo_root: Path,
    spec_path: Path,
    template_text: str,
) -> str:
    """Render the agent prompt from a template plus one issue spec."""

    spec_rel_path = _relative_to_repo(repo_root, spec_path)
    payload = Template(template_text)
    return payload.substitute(
        repo_root=str(repo_root),
        spec_path=spec_rel_path,
        issue_id=spec.issue_id,
        priority=spec.priority,
        title=spec.title,
        location=_format_location(spec.source_location),
        summary=spec.summary,
        problem_statement=spec.problem_statement,
        related_tests=_render_bullets(spec.related_tests, empty="(no related tests inferred)"),
        acceptance_criteria=_render_bullets(spec.acceptance_criteria),
        constraints=_render_bullets(spec.constraints),
        verification_commands=_render_numbered(spec.verification_commands),
        raw_finding_markdown=spec.raw_finding_markdown,
    )


def render_refresh_prompt(
    *,
    repo_root: Path,
    source_path: Path,
    source_text: str,
    template_text: str,
) -> str:
    """Render the repo re-review prompt for unresolved findings refresh."""

    payload = Template(template_text)
    return payload.substitute(
        repo_root=str(repo_root),
        source_path=_relative_to_repo(repo_root, source_path),
        source_markdown=source_text,
    )


def process_review_findings_file(
    source_path: Path,
    *,
    repo_root: Path,
    output_root: Path,
    template_path: Path,
    run_label: str,
    dispatch_agents: bool = False,
    codex_bin: str = "codex",
    codex_home_root: Path | None = None,
    model: str | None = None,
    overwrite: bool = False,
    resume: bool = False,
    skip_existing: bool = False,
    retry_failed: bool = False,
) -> IssueBatchManifest:
    """Read a findings file, write specs/prompts, and optionally dispatch agents."""

    if retry_failed and not dispatch_agents:
        raise ValueError("--retry-failed requires --dispatch-agents")
    resume = resume or skip_existing or retry_failed

    source_text = source_path.read_text()
    template_text = template_path.read_text()
    batch_dir = output_root / run_label
    previous_manifest = _load_existing_manifest(batch_dir) if batch_dir.exists() else None
    if batch_dir.exists():
        if overwrite:
            _remove_tree(batch_dir)
            previous_manifest = None
        elif not resume:
            raise FileExistsError(f"output directory already exists: {batch_dir}")
    batch_dir.mkdir(parents=True, exist_ok=True)
    previous_artifacts = {
        artifact.issue_id: artifact
        for artifact in (previous_manifest.issues if previous_manifest else [])
    }
    source_sha256 = _sha256_text(source_text)
    template_sha256 = _sha256_text(template_text)

    with traced(
        "issue_agent_batch",
        as_type="span",
        input={
            "source_path": str(source_path),
            "dispatch_agents": dispatch_agents,
            "run_label": run_label,
            "resume": resume,
            "skip_existing": skip_existing,
            "retry_failed": retry_failed,
        },
        metadata={
            "run_label": run_label,
            "dispatch_agents": str(dispatch_agents).lower(),
            "resume": str(resume).lower(),
            "skip_existing": str(skip_existing).lower(),
            "retry_failed": str(retry_failed).lower(),
        },
    ) as batch_span:
        with traced(
            "scan_review_findings",
            as_type="span",
            input={"source_path": str(source_path)},
            metadata={"run_label": run_label},
        ) as scan_span:
            findings = parse_review_findings(source_text)
            scan_span.update(
                output={"findings": [f.model_dump(mode="json") for f in findings]},
                metadata={"findings_count": str(len(findings))},
            )

        artifacts: list[IssueArtifact] = []
        issue_changed: dict[str, bool] = {}
        with traced(
            "materialize_issue_artifacts",
            as_type="span",
            input={"batch_dir": str(batch_dir)},
            metadata={
                "run_label": run_label,
                "findings_count": str(len(findings)),
            },
        ) as materialize_span:
            for finding in findings:
                spec = build_issue_specification(finding, repo_root=repo_root)
                issue_dir = batch_dir / spec.issue_id
                issue_dir.mkdir(parents=True, exist_ok=True)
                spec_path = issue_dir / "issue_spec.json"
                prompt_path = issue_dir / "agent_prompt.md"
                spec_json = _canonical_json(spec.model_dump(mode="json")) + "\n"
                prompt_text = (
                    render_agent_prompt(
                        spec,
                        repo_root=repo_root,
                        spec_path=spec_path,
                        template_text=template_text,
                    )
                    + "\n"
                )
                spec_sha256 = _sha256_text(spec_json)
                prompt_sha256 = _sha256_text(prompt_text)
                previous_artifact = previous_artifacts.get(spec.issue_id)
                unchanged = _artifact_is_unchanged(
                    previous_artifact=previous_artifact,
                    spec_path=spec_path,
                    prompt_path=prompt_path,
                    spec_sha256=spec_sha256,
                    prompt_sha256=prompt_sha256,
                )

                if skip_existing and unchanged:
                    logger.info("reused issue artifacts %s", issue_dir)
                    artifact = IssueArtifact(
                        issue_id=spec.issue_id,
                        priority=spec.priority,
                        title=spec.title,
                        spec_path=_relative_to_repo(repo_root, spec_path),
                        prompt_path=_relative_to_repo(repo_root, prompt_path),
                        spec_sha256=spec_sha256,
                        prompt_sha256=prompt_sha256,
                        reused_existing=True,
                        dispatch=(
                            previous_artifact.dispatch.model_copy(deep=True)
                            if previous_artifact is not None
                            else AgentDispatchResult()
                        ),
                    )
                else:
                    spec_path.write_text(spec_json)
                    prompt_path.write_text(prompt_text)
                    logger.info("wrote issue spec %s", spec_path)
                    logger.info("wrote agent prompt %s", prompt_path)
                    artifact = IssueArtifact(
                        issue_id=spec.issue_id,
                        priority=spec.priority,
                        title=spec.title,
                        spec_path=_relative_to_repo(repo_root, spec_path),
                        prompt_path=_relative_to_repo(repo_root, prompt_path),
                        spec_sha256=spec_sha256,
                        prompt_sha256=prompt_sha256,
                        reused_existing=False,
                        dispatch=(
                            previous_artifact.dispatch.model_copy(deep=True)
                            if previous_artifact is not None and resume
                            else AgentDispatchResult()
                        ),
                    )

                artifacts.append(artifact)
                issue_changed[artifact.issue_id] = not unchanged

            materialize_span.update(
                output={"issues": [a.model_dump(mode="json") for a in artifacts]},
                metadata={
                    "issues_written": str(len(artifacts)),
                    "issues_reused": str(sum(1 for a in artifacts if a.reused_existing)),
                },
            )

        if dispatch_agents:
            for artifact in artifacts:
                spec_path = repo_root / artifact.spec_path
                prompt_path = repo_root / artifact.prompt_path
                issue_dir = spec_path.parent
                previous_artifact = previous_artifacts.get(artifact.issue_id)
                if _should_reuse_previous_dispatch(
                    previous_artifact=previous_artifact,
                    changed=issue_changed[artifact.issue_id],
                    skip_existing=skip_existing,
                    retry_failed=retry_failed,
                ):
                    assert previous_artifact is not None
                    previous_dispatch = previous_artifact.dispatch.model_copy(deep=True)
                    previous_dispatch.reused_previous = True
                    previous_dispatch.skip_reason = (
                        "unchanged issue already dispatched successfully"
                    )
                    artifact.dispatch = previous_dispatch
                    logger.info("reused successful dispatch for %s", artifact.issue_id)
                    continue
                if _should_skip_dispatch(
                    previous_artifact=previous_artifact,
                    changed=issue_changed[artifact.issue_id],
                    retry_failed=retry_failed,
                ):
                    artifact.dispatch = AgentDispatchResult(
                        status=(
                            previous_artifact.dispatch.status
                            if previous_artifact is not None
                            else "not_requested"
                        ),
                        command=(
                            list(previous_artifact.dispatch.command)
                            if previous_artifact is not None
                            else []
                        ),
                        log_path=previous_artifact.dispatch.log_path if previous_artifact else None,
                        last_message_path=(
                            previous_artifact.dispatch.last_message_path
                            if previous_artifact
                            else None
                        ),
                        thread_id=previous_artifact.dispatch.thread_id
                        if previous_artifact
                        else None,
                        return_code=previous_artifact.dispatch.return_code
                        if previous_artifact
                        else None,
                        error=previous_artifact.dispatch.error if previous_artifact else None,
                        reused_previous=previous_artifact is not None,
                        skip_reason="retry_failed requested and issue was not previously failed",
                    )
                    logger.info("skipped dispatch for %s", artifact.issue_id)
                    continue
                codex_home = (
                    ((codex_home_root / artifact.issue_id).resolve())
                    if codex_home_root is not None
                    else None
                )
                artifact.dispatch = _dispatch_issue_agent(
                    issue_id=artifact.issue_id,
                    prompt_path=prompt_path,
                    issue_dir=issue_dir,
                    repo_root=repo_root,
                    codex_home=codex_home,
                    codex_bin=codex_bin,
                    model=model,
                )

        manifest = IssueBatchManifest(
            run_label=run_label,
            created_at=datetime.now(UTC),
            repo_root=str(repo_root),
            source_path=_relative_to_repo(repo_root, source_path),
            template_path=_relative_to_repo(repo_root, template_path),
            source_sha256=source_sha256,
            template_sha256=template_sha256,
            issues=artifacts,
        )
        manifest.convergence_sha256 = manifest_convergence_sha256(manifest)
        manifest_path = batch_dir / "manifest.json"
        manifest_path.write_text(manifest.model_dump_json(indent=2) + "\n")
        logger.info("wrote batch manifest %s", manifest_path)

        batch_span.update(
            output=manifest.model_dump(mode="json"),
            metadata={
                "run_label": run_label,
                "issues_count": str(len(artifacts)),
                "dispatch_agents": str(dispatch_agents).lower(),
                "issues_reused": str(sum(1 for a in artifacts if a.reused_existing)),
            },
        )
    return manifest


def refresh_review_findings_file(
    source_path: Path,
    *,
    repo_root: Path,
    refreshed_path: Path,
    template_path: Path,
    codex_bin: str = "codex",
    codex_home: Path | None = None,
    model: str | None = None,
) -> ReviewRefreshResult:
    """Re-review the current repo and emit only findings that still apply.

    The refresher treats the source findings file as the current issue set.
    It asks Codex to inspect the repo state, keep unresolved findings verbatim,
    and omit resolved findings. The refreshed markdown becomes the input for the
    next recursive iteration.
    """

    source_text = source_path.read_text()
    template_text = template_path.read_text()
    prompt_text = render_refresh_prompt(
        repo_root=repo_root,
        source_path=source_path,
        source_text=source_text,
        template_text=template_text,
    )
    refreshed_path.parent.mkdir(parents=True, exist_ok=True)
    last_message_path = refreshed_path
    log_path = refreshed_path.with_suffix(".jsonl")
    with suppress(FileNotFoundError):
        refreshed_path.unlink()
    with suppress(FileNotFoundError):
        log_path.unlink()

    command = [
        codex_bin,
        "exec",
        "--json",
        "--full-auto",
        "-C",
        str(repo_root),
        "-o",
        str(last_message_path),
        "-",
    ]
    if model is not None:
        command[2:2] = ["-m", model]

    env = dict(os.environ)
    if codex_home is not None:
        prepared_codex_home = _prepare_isolated_codex_home(codex_home)
        env["CODEX_HOME"] = str(prepared_codex_home)

    source_sha256 = _sha256_text(source_text)
    with traced(
        "refresh_review_findings",
        as_type="span",
        input={"source_path": _relative_to_repo(repo_root, source_path)},
        metadata={"codex_bin": codex_bin},
    ) as span:
        try:
            completed = subprocess.run(
                command,
                cwd=repo_root,
                input=prompt_text,
                text=True,
                capture_output=True,
                check=False,
                env={**os.environ, **env},
            )
        except OSError as exc:
            message = f"{type(exc).__name__}: {exc}"
            span.update(level="ERROR", status_message=message)
            return ReviewRefreshResult(
                status="failed",
                source_path=_relative_to_repo(repo_root, source_path),
                refreshed_path=_relative_to_repo(repo_root, refreshed_path),
                source_sha256=source_sha256,
                command=command,
                error=message,
            )

        combined_output = completed.stdout
        if completed.stderr:
            combined_output += completed.stderr
        log_path.write_text(combined_output)
        thread_id = _extract_thread_id(combined_output)
        error_message = _summarize_codex_failure(combined_output, completed.returncode)
        if completed.returncode == 0 and not refreshed_path.exists():
            refreshed_path.write_text("")
        refreshed_text = refreshed_path.read_text() if refreshed_path.exists() else ""

        try:
            if refreshed_text.strip():
                parse_review_findings(refreshed_text)
        except ValueError as exc:
            message = f"refreshed findings were not valid review markdown: {exc}"
            span.update(
                level="ERROR",
                status_message=message,
                output={"log_path": _relative_to_repo(repo_root, log_path)},
            )
            return ReviewRefreshResult(
                status="failed",
                source_path=_relative_to_repo(repo_root, source_path),
                refreshed_path=_relative_to_repo(repo_root, refreshed_path),
                source_sha256=source_sha256,
                command=command,
                log_path=_relative_to_repo(repo_root, log_path),
                last_message_path=_relative_to_repo(repo_root, last_message_path),
                thread_id=thread_id,
                return_code=completed.returncode,
                error=message,
            )

        if completed.returncode != 0:
            span.update(
                level="WARNING",
                status_message=error_message,
                output={
                    "return_code": completed.returncode,
                    "thread_id": thread_id,
                    "log_path": _relative_to_repo(repo_root, log_path),
                },
            )
            return ReviewRefreshResult(
                status="failed",
                source_path=_relative_to_repo(repo_root, source_path),
                refreshed_path=_relative_to_repo(repo_root, refreshed_path),
                source_sha256=source_sha256,
                command=command,
                log_path=_relative_to_repo(repo_root, log_path),
                last_message_path=_relative_to_repo(repo_root, last_message_path),
                thread_id=thread_id,
                return_code=completed.returncode,
                error=error_message,
            )

        refreshed_sha256 = _sha256_text(refreshed_text)
        unchanged = refreshed_sha256 == source_sha256
        span.update(
            output={
                "refreshed_path": _relative_to_repo(repo_root, refreshed_path),
                "thread_id": thread_id,
                "log_path": _relative_to_repo(repo_root, log_path),
                "unchanged": unchanged,
            },
            metadata={"unchanged": str(unchanged).lower()},
        )
        logger.info(
            "refreshed review findings %s -> %s",
            source_path,
            refreshed_path,
        )
        return ReviewRefreshResult(
            status="succeeded",
            source_path=_relative_to_repo(repo_root, source_path),
            refreshed_path=_relative_to_repo(repo_root, refreshed_path),
            source_sha256=source_sha256,
            refreshed_sha256=refreshed_sha256,
            unchanged=unchanged,
            command=command,
            log_path=_relative_to_repo(repo_root, log_path),
            last_message_path=_relative_to_repo(repo_root, last_message_path),
            thread_id=thread_id,
            return_code=completed.returncode,
        )


def _dispatch_issue_agent(
    *,
    issue_id: str,
    prompt_path: Path,
    issue_dir: Path,
    repo_root: Path,
    codex_home: Path | None,
    codex_bin: str,
    model: str | None,
) -> AgentDispatchResult:
    """Run one non-interactive Codex task against one issue prompt."""

    dispatch_dir = issue_dir / "dispatch"
    dispatch_dir.mkdir(parents=True, exist_ok=True)
    log_path = dispatch_dir / "codex.jsonl"
    last_message_path = dispatch_dir / "last_message.md"

    command = [
        codex_bin,
        "exec",
        "--json",
        "--full-auto",
        "-C",
        str(repo_root),
        "-o",
        str(last_message_path),
        "-",
    ]
    if model is not None:
        command[2:2] = ["-m", model]

    prompt_text = prompt_path.read_text()
    env = dict(os.environ)
    if codex_home is not None:
        prepared_codex_home = _prepare_isolated_codex_home(codex_home)
        env["CODEX_HOME"] = str(prepared_codex_home)

    with traced(
        "dispatch_issue_agent",
        as_type="span",
        input={"issue_id": issue_id, "prompt_path": _relative_to_repo(repo_root, prompt_path)},
        metadata={"issue_id": issue_id, "codex_bin": codex_bin},
    ) as span:
        try:
            completed = subprocess.run(
                command,
                cwd=repo_root,
                input=prompt_text,
                text=True,
                capture_output=True,
                check=False,
                env={**os.environ, **env},
            )
        except OSError as exc:
            span.update(
                level="ERROR",
                status_message=f"{type(exc).__name__}: {exc}",
                metadata={"issue_id": issue_id},
            )
            return AgentDispatchResult(
                status="failed",
                command=command,
                error=f"{type(exc).__name__}: {exc}",
            )

        combined_output = completed.stdout
        if completed.stderr:
            combined_output += completed.stderr
        log_path.write_text(combined_output)
        thread_id = _extract_thread_id(combined_output)
        error_message = _summarize_codex_failure(combined_output, completed.returncode)

        status: Literal["succeeded", "failed"] = (
            "succeeded" if completed.returncode == 0 else "failed"
        )
        if status == "failed":
            span.update(
                level="WARNING",
                status_message=error_message,
                output={
                    "return_code": completed.returncode,
                    "thread_id": thread_id,
                    "log_path": _relative_to_repo(repo_root, log_path),
                },
                metadata={"issue_id": issue_id, "return_code": str(completed.returncode)},
            )
        else:
            span.update(
                output={
                    "return_code": completed.returncode,
                    "thread_id": thread_id,
                    "log_path": _relative_to_repo(repo_root, log_path),
                    "last_message_path": _relative_to_repo(repo_root, last_message_path),
                },
                metadata={"issue_id": issue_id, "return_code": str(completed.returncode)},
            )

    logger.info("dispatched issue %s via codex", issue_id)
    return AgentDispatchResult(
        status=status,
        command=command,
        log_path=_relative_to_repo(repo_root, log_path),
        last_message_path=_relative_to_repo(repo_root, last_message_path),
        thread_id=thread_id,
        return_code=completed.returncode,
        error=None if status == "succeeded" else error_message,
    )


def _parse_location(raw_location: str) -> IssueLocation:
    match = _LOCATION_RE.match(raw_location.strip())
    if match is None:
        raise ValueError(f"invalid finding location: {raw_location!r}")
    start = match.group("start")
    end = match.group("end")
    return IssueLocation(
        file=match.group("file"),
        start_line=int(start) if start is not None else None,
        end_line=int(end) if end is not None else (int(start) if start is not None else None),
    )


def _summarize_finding(finding: ReviewFinding) -> str:
    first_paragraph = finding.body.split("\n\n", maxsplit=1)[0].strip()
    if first_paragraph:
        return first_paragraph
    return finding.title


def _acceptance_criteria_for(finding: ReviewFinding) -> list[str]:
    lowered = f"{finding.title} {finding.body}".lower()
    criteria: list[str] = []
    if "resume" in lowered or "checkpoint" in lowered or "thread_id" in lowered:
        criteria.extend(
            [
                "The documented pause/resume flow works through the public entry point, not only through a precompiled graph object.",
                "Regression coverage exercises a pause followed by a resume using the same external handle the finding calls out.",
            ]
        )
    if "reviewer note" in lowered or "focus" in lowered or "rerun_match_with_focus" in lowered:
        criteria.extend(
            [
                "The focused re-run path passes distinct reviewer context into the follow-up matcher call.",
                "Regression coverage proves the focused prompt/context differs from the original free-text matcher invocation.",
            ]
        )
    criteria.extend(
        [
            "Implement the smallest correct fix for the behavior described in the review finding.",
            "Add or update automated coverage that would fail before the fix and pass after it.",
            "Keep unrelated behavior and interfaces unchanged unless the finding explicitly requires a contract change.",
        ]
    )
    return _dedupe(criteria)


def _constraints_for(finding: ReviewFinding) -> list[str]:
    location = _format_location(finding.location)
    return [
        "Do not revert unrelated local changes in the worktree.",
        f"Start from the code around {location} and expand scope only when the fix truly needs it.",
        "If comments or docstrings currently promise the broken behavior, update them so the implementation and docs agree.",
    ]


def _build_labels(file_path: str, priority: str) -> list[str]:
    path = Path(file_path)
    labels = ["review-finding", priority]
    if len(path.parts) >= 3 and path.parts[0] == "src" and path.parts[1] == "clinical_demo":
        labels.append(path.parts[2])
    else:
        labels.append(path.parent.name or "root")
    return labels


def _discover_related_tests(repo_root: Path, file_path: str) -> list[str]:
    tests_root = repo_root / "tests"
    if not tests_root.exists():
        return []
    source_path = Path(file_path)
    stem = source_path.stem.replace("test_", "")
    component = (
        source_path.parts[2]
        if len(source_path.parts) >= 3 and source_path.parts[0] == "src"
        else source_path.parent.name
    )

    scored: list[tuple[int, str]] = []
    for test_path in tests_root.rglob("*.py"):
        rel = _relative_to_repo(repo_root, test_path)
        score = 0
        if stem and stem in test_path.stem:
            score += 4
        if component and component in test_path.parts:
            score += 2
        if source_path.parent.name and source_path.parent.name in test_path.parts:
            score += 1
        if score:
            scored.append((score, rel))

    scored.sort(key=lambda item: (-item[0], item[1]))
    return _dedupe([path for _, path in scored])[:5]


def _verification_commands_for(file_path: str, related_tests: list[str]) -> list[str]:
    path = Path(file_path)
    commands: list[str] = []
    if related_tests:
        commands.append("uv run pytest " + " ".join(related_tests))
    elif len(path.parts) >= 3 and path.parts[0] == "src" and path.parts[1] == "clinical_demo":
        commands.append(f"uv run pytest tests/{path.parts[2]}")
    commands.extend(["uv run pytest", "uv run ruff check .", "uv run mypy"])
    return _dedupe(commands)


def _render_bullets(items: list[str], *, empty: str | None = None) -> str:
    if not items:
        return empty or ""
    return "\n".join(f"- {item}" for item in items)


def _render_numbered(items: list[str]) -> str:
    return "\n".join(f"{index}. {item}" for index, item in enumerate(items, start=1))


def _format_location(location: IssueLocation) -> str:
    if location.start_line is None:
        return location.file
    if location.end_line is None or location.end_line == location.start_line:
        return f"{location.file}:{location.start_line}"
    return f"{location.file}:{location.start_line}-{location.end_line}"


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower().replace("'", "")).strip("-")
    return slug[:72] or "issue"


def _dedupe(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _relative_to_repo(repo_root: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(repo_root.resolve()))
    except ValueError:
        return str(path.resolve())


def _remove_tree(path: Path) -> None:
    """Best-effort recursive delete with a retry for flaky temp/plugin dirs."""

    last_error: OSError | None = None
    for _ in range(3):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            last_error = exc
            _remove_tree_contents(path)
            time.sleep(0.05)
    if path.exists():
        raise last_error if last_error is not None else OSError(f"failed to delete {path}")


def _remove_tree_contents(root: Path) -> None:
    if not root.exists():
        return
    for current_root, dirnames, filenames in os.walk(root, topdown=False):
        current = Path(current_root)
        for filename in filenames:
            with suppress(FileNotFoundError):
                (current / filename).unlink()
        for dirname in dirnames:
            try:
                with suppress(FileNotFoundError):
                    (current / dirname).rmdir()
            except OSError:
                pass
    try:
        with suppress(FileNotFoundError):
            root.rmdir()
    except OSError:
        pass


def _load_existing_manifest(batch_dir: Path) -> IssueBatchManifest | None:
    manifest_path = batch_dir / "manifest.json"
    if not manifest_path.exists():
        return None
    return IssueBatchManifest.model_validate_json(manifest_path.read_text())


def _artifact_is_unchanged(
    *,
    previous_artifact: IssueArtifact | None,
    spec_path: Path,
    prompt_path: Path,
    spec_sha256: str,
    prompt_sha256: str,
) -> bool:
    if previous_artifact is None:
        return False
    return (
        previous_artifact.spec_sha256 == spec_sha256
        and previous_artifact.prompt_sha256 == prompt_sha256
        and spec_path.exists()
        and prompt_path.exists()
    )


def _should_reuse_previous_dispatch(
    *,
    previous_artifact: IssueArtifact | None,
    changed: bool,
    skip_existing: bool,
    retry_failed: bool,
) -> bool:
    if previous_artifact is None or changed:
        return False
    if not (skip_existing or retry_failed):
        return False
    return previous_artifact.dispatch.status == "succeeded"


def _should_skip_dispatch(
    *,
    previous_artifact: IssueArtifact | None,
    changed: bool,
    retry_failed: bool,
) -> bool:
    if not retry_failed or changed:
        return False
    if previous_artifact is None:
        return False
    return previous_artifact.dispatch.status != "failed"


def _extract_thread_id(output: str) -> str | None:
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "thread.started":
            thread_id = event.get("thread_id")
            if isinstance(thread_id, str):
                return thread_id
    return None


def manifest_convergence_payload(manifest: IssueBatchManifest) -> dict[str, object]:
    """Return a stable manifest-level fingerprint payload.

    This fingerprint intentionally ignores transient fields like
    timestamps, thread ids, log paths, and reuse bookkeeping. We only compare
    the durable, user-meaningful outputs of a run: which issues exist, the
    materialized spec/prompt content hashes, and the effective dispatch
    outcomes. The recursive CLI stop condition is stricter and is based on a
    refreshed unresolved-findings review, not this manifest snapshot alone.
    """

    issues = [
        {
            "issue_id": issue.issue_id,
            "priority": issue.priority,
            "title": issue.title,
            "spec_sha256": issue.spec_sha256,
            "prompt_sha256": issue.prompt_sha256,
            "dispatch": {
                "status": issue.dispatch.status,
                "return_code": issue.dispatch.return_code,
                "error": issue.dispatch.error,
            },
        }
        for issue in sorted(manifest.issues, key=lambda item: item.issue_id)
    ]
    return {
        "source_sha256": manifest.source_sha256,
        "template_sha256": manifest.template_sha256,
        "issues": issues,
    }


def manifest_convergence_sha256(manifest: IssueBatchManifest) -> str:
    """Hash the stable convergence payload for quick recursive comparisons."""

    return _sha256_text(_canonical_json(manifest_convergence_payload(manifest)))


def _canonical_json(payload: object) -> str:
    return json.dumps(payload, indent=2, sort_keys=True)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _prepare_isolated_codex_home(target_home: Path) -> Path:
    """Create an isolated Codex home that still has the auth/config needed to run."""

    target_home.mkdir(parents=True, exist_ok=True)
    source_home = _default_codex_home()
    if source_home == target_home:
        return target_home

    for name in _CODEX_HOME_BOOTSTRAP_FILES:
        source = source_home / name
        target = target_home / name
        if source.exists() and not target.exists():
            shutil.copy2(source, target)

    for name in _CODEX_HOME_BOOTSTRAP_DIRS:
        source = source_home / name
        target = target_home / name
        if source.exists():
            shutil.copytree(source, target, dirs_exist_ok=True)

    return target_home


def _default_codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".codex"


def _summarize_codex_failure(output: str, return_code: int | None) -> str:
    """Turn common Codex subprocess failures into an actionable summary."""

    lowered = output.lower()
    if "401 unauthorized" in lowered or "missing bearer or basic authentication" in lowered:
        return (
            "codex dispatch failed: missing Codex authentication for the subprocess "
            "(run `codex login`, or dispatch without an isolated CODEX_HOME)."
        )
    if "could not resolve host" in lowered or "failed to lookup address information" in lowered:
        return "codex dispatch failed: network or DNS error while reaching the OpenAI API."
    if return_code is None:
        return "codex dispatch failed"
    return f"agent exited with code {return_code}"
