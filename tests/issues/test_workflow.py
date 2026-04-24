from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from clinical_demo.issues import (
    AgentDispatchResult,
    IssueArtifact,
    IssueBatchManifest,
    ReviewRefreshResult,
    build_issue_specification,
    manifest_convergence_sha256,
    parse_review_findings,
    process_review_findings_file,
    refresh_review_findings_file,
)
from clinical_demo.issues import workflow as issues_workflow
from clinical_demo.observability import langfuse_client

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_FINDINGS = PROJECT_ROOT / "issues/review_findings/2026-04-22-codex-review.md"
PROMPT_TEMPLATE = PROJECT_ROOT / "issues/templates/fix_from_issue_spec.md.tmpl"
REFRESH_TEMPLATE = PROJECT_ROOT / "issues/templates/refresh_review_findings.md.tmpl"


class _RecordingSpan:
    def __init__(self, name: str, kwargs: dict[str, Any]) -> None:
        self.name = name
        self.start_kwargs = kwargs
        self.updates: list[dict[str, Any]] = []

    def update(self, **kwargs: Any) -> None:
        self.updates.append(kwargs)

    def __enter__(self) -> _RecordingSpan:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class _RecordingClient:
    def __init__(self) -> None:
        self.spans: list[_RecordingSpan] = []

    def start_as_current_observation(self, **kwargs: Any) -> _RecordingSpan:
        span = _RecordingSpan(kwargs.get("name", "<unnamed>"), kwargs)
        self.spans.append(span)
        return span

    def flush(self) -> None:
        return None


@pytest.fixture
def recording_client(monkeypatch: pytest.MonkeyPatch) -> _RecordingClient:
    client = _RecordingClient()
    langfuse_client.get_client.cache_clear()
    monkeypatch.setattr(langfuse_client, "get_client", lambda: client)
    return client


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "issues/review_findings").mkdir(parents=True)
    (repo / "issues/templates").mkdir(parents=True)
    (repo / "tests/graph").mkdir(parents=True)

    (repo / "issues/review_findings/findings.md").write_text(SAMPLE_FINDINGS.read_text())
    (repo / "issues/templates/prompt.md.tmpl").write_text(PROMPT_TEMPLATE.read_text())
    (repo / "issues/templates/refresh.md.tmpl").write_text(REFRESH_TEMPLATE.read_text())
    (repo / "tests/graph/test_score_pair_graph.py").write_text(
        "def test_placeholder():\n    pass\n"
    )
    (repo / "tests/graph/test_human_checkpoint.py").write_text(
        "def test_placeholder():\n    pass\n"
    )
    (repo / "tests/graph/test_revise_node.py").write_text("def test_placeholder():\n    pass\n")
    return repo


def test_parse_review_findings_reads_priority_title_body_and_location() -> None:
    findings = parse_review_findings(SAMPLE_FINDINGS.read_text())

    assert len(findings) == 2
    assert findings[0].finding_number == 1
    assert findings[0].priority == "P1"
    assert findings[0].title == "Public HITL entry point cannot resume a paused run"
    assert findings[0].location.file == "src/clinical_demo/graph/score_pair_graph.py"
    assert findings[0].location.start_line == 79
    assert findings[0].location.end_line == 101
    assert "InMemorySaver" in findings[0].body


def test_build_issue_specification_infers_related_tests_and_acceptance_criteria(
    repo_root: Path,
) -> None:
    findings = parse_review_findings((repo_root / "issues/review_findings/findings.md").read_text())

    resume_spec = build_issue_specification(findings[0], repo_root=repo_root)
    focus_spec = build_issue_specification(findings[1], repo_root=repo_root)

    assert "tests/graph/test_score_pair_graph.py" in resume_spec.related_tests
    assert any("pause/resume flow works" in item for item in resume_spec.acceptance_criteria)
    assert "tests/graph/test_revise_node.py" in focus_spec.related_tests
    assert any(
        "focused re-run path passes distinct reviewer context" in item
        for item in focus_spec.acceptance_criteria
    )


def test_process_review_findings_file_writes_specs_prompts_and_manifest(repo_root: Path) -> None:
    manifest = process_review_findings_file(
        repo_root / "issues/review_findings/findings.md",
        repo_root=repo_root,
        output_root=repo_root / "issues/generated",
        template_path=repo_root / "issues/templates/prompt.md.tmpl",
        run_label="sample-run",
    )

    assert len(manifest.issues) == 2

    first_issue = manifest.issues[0]
    spec_path = repo_root / first_issue.spec_path
    prompt_path = repo_root / first_issue.prompt_path
    manifest_path = repo_root / "issues/generated/sample-run/manifest.json"

    assert spec_path.exists()
    assert prompt_path.exists()
    assert manifest_path.exists()

    spec = json.loads(spec_path.read_text())
    prompt = prompt_path.read_text()

    assert spec["issue_id"] == first_issue.issue_id
    assert spec["title"] == first_issue.title
    assert "Acceptance Criteria" in prompt
    assert "Raw Review Finding" in prompt
    assert first_issue.spec_sha256
    assert first_issue.prompt_sha256
    assert manifest.source_sha256
    assert manifest.template_sha256
    assert manifest.convergence_sha256


def test_manifest_convergence_sha_ignores_transient_dispatch_fields() -> None:
    base = IssueBatchManifest(
        run_label="sample-run",
        created_at=datetime(2026, 4, 23, tzinfo=UTC),
        repo_root="/tmp/repo",
        source_path="issues/review_findings/findings.md",
        template_path="issues/templates/prompt.md.tmpl",
        source_sha256="source-hash",
        template_sha256="template-hash",
        issues=[
            IssueArtifact(
                issue_id="finding-001-example",
                priority="P1",
                title="Example finding",
                spec_path="issues/generated/sample/finding-001-example/issue_spec.json",
                prompt_path="issues/generated/sample/finding-001-example/agent_prompt.md",
                spec_sha256="spec-hash",
                prompt_sha256="prompt-hash",
                dispatch=AgentDispatchResult(
                    status="failed",
                    return_code=1,
                    error="agent exited with code 1",
                    thread_id="thread-a",
                    log_path="issues/generated/sample/finding-001-example/dispatch/codex.jsonl",
                    last_message_path=(
                        "issues/generated/sample/finding-001-example/dispatch/last_message.md"
                    ),
                ),
            )
        ],
    )
    changed_only_transients = base.model_copy(deep=True)
    changed_only_transients.created_at = datetime(2026, 4, 24, tzinfo=UTC)
    changed_only_transients.issues[0].dispatch.thread_id = "thread-b"
    changed_only_transients.issues[0].dispatch.log_path = "other-log.jsonl"
    changed_only_transients.issues[0].dispatch.last_message_path = "other-message.md"
    changed_only_transients.issues[0].dispatch.reused_previous = True
    changed_only_transients.issues[0].dispatch.skip_reason = "unchanged"

    changed_effective_output = base.model_copy(deep=True)
    changed_effective_output.issues[0].dispatch.status = "succeeded"
    changed_effective_output.issues[0].dispatch.return_code = 0
    changed_effective_output.issues[0].dispatch.error = None

    assert manifest_convergence_sha256(base) == manifest_convergence_sha256(changed_only_transients)
    assert manifest_convergence_sha256(base) != manifest_convergence_sha256(
        changed_effective_output
    )


def test_refresh_review_findings_file_emits_next_iteration_source(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = repo_root / "issues/review_findings/findings.md"
    refreshed_path = repo_root / "issues/generated/refresh-run/.finder/iteration-001-findings.md"
    findings = parse_review_findings(source_path.read_text())

    def _fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        command = args[0]
        output_index = command.index("-o") + 1
        Path(command[output_index]).write_text(findings[1].raw_markdown + "\n")
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='{"type":"thread.started","thread_id":"thread-refresh"}\n',
            stderr="",
        )

    monkeypatch.setattr(issues_workflow.subprocess, "run", _fake_run)
    result = refresh_review_findings_file(
        source_path,
        repo_root=repo_root,
        refreshed_path=refreshed_path,
        template_path=repo_root / "issues/templates/refresh.md.tmpl",
    )

    assert isinstance(result, ReviewRefreshResult)
    assert result.status == "succeeded"
    assert result.unchanged is False
    assert result.thread_id == "thread-refresh"
    assert refreshed_path.exists()
    refreshed_findings = parse_review_findings(refreshed_path.read_text())
    assert len(refreshed_findings) == 1
    assert refreshed_findings[0].issue_id == findings[1].issue_id


def test_refresh_review_findings_file_marks_unchanged_when_verbatim_source_repeats(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = repo_root / "issues/review_findings/findings.md"
    refreshed_path = repo_root / "issues/generated/refresh-run/.finder/iteration-002-findings.md"
    source_text = source_path.read_text()

    def _fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        command = args[0]
        output_index = command.index("-o") + 1
        Path(command[output_index]).write_text(source_text)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='{"type":"thread.started","thread_id":"thread-refresh"}\n',
            stderr="",
        )

    monkeypatch.setattr(issues_workflow.subprocess, "run", _fake_run)
    result = refresh_review_findings_file(
        source_path,
        repo_root=repo_root,
        refreshed_path=refreshed_path,
        template_path=repo_root / "issues/templates/refresh.md.tmpl",
    )

    assert result.status == "succeeded"
    assert result.unchanged is True


def test_process_review_findings_file_dispatches_codex_runs_when_requested(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CODEX_HOME", raising=False)
    seen_envs: list[dict[str, str]] = []

    def _fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        command = args[0]
        output_index = command.index("-o") + 1
        Path(command[output_index]).write_text("Fixed the issue.\n")
        seen_envs.append(dict(kwargs["env"]))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='{"type":"thread.started","thread_id":"thread-123"}\n{"type":"turn.completed"}\n',
            stderr="",
        )

    monkeypatch.setattr(issues_workflow.subprocess, "run", _fake_run)

    manifest = process_review_findings_file(
        repo_root / "issues/review_findings/findings.md",
        repo_root=repo_root,
        output_root=repo_root / "issues/generated",
        template_path=repo_root / "issues/templates/prompt.md.tmpl",
        run_label="dispatch-run",
        dispatch_agents=True,
    )

    dispatch = manifest.issues[0].dispatch
    assert dispatch.status == "succeeded"
    assert dispatch.thread_id == "thread-123"
    assert dispatch.log_path is not None
    assert (repo_root / dispatch.log_path).exists()
    assert "CODEX_HOME" not in seen_envs[0]


def test_resume_skip_existing_reuses_unchanged_artifacts_and_successful_dispatches(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CODEX_HOME", raising=False)

    def _first_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        command = args[0]
        output_index = command.index("-o") + 1
        Path(command[output_index]).write_text("Fixed the issue.\n")
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='{"type":"thread.started","thread_id":"thread-123"}\n',
            stderr="",
        )

    monkeypatch.setattr(issues_workflow.subprocess, "run", _first_run)
    process_review_findings_file(
        repo_root / "issues/review_findings/findings.md",
        repo_root=repo_root,
        output_root=repo_root / "issues/generated",
        template_path=repo_root / "issues/templates/prompt.md.tmpl",
        run_label="resume-run",
        dispatch_agents=True,
    )

    def _should_not_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise AssertionError("dispatch should have been reused, not rerun")

    monkeypatch.setattr(issues_workflow.subprocess, "run", _should_not_run)
    resumed = process_review_findings_file(
        repo_root / "issues/review_findings/findings.md",
        repo_root=repo_root,
        output_root=repo_root / "issues/generated",
        template_path=repo_root / "issues/templates/prompt.md.tmpl",
        run_label="resume-run",
        dispatch_agents=True,
        resume=True,
        skip_existing=True,
    )

    assert all(issue.reused_existing for issue in resumed.issues)
    assert all(issue.dispatch.status == "succeeded" for issue in resumed.issues)
    assert all(issue.dispatch.reused_previous for issue in resumed.issues)
    assert all(issue.dispatch.skip_reason for issue in resumed.issues)


def test_retry_failed_only_reruns_previously_failed_issues(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CODEX_HOME", raising=False)
    first_run_calls = {"count": 0}

    def _first_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        first_run_calls["count"] += 1
        command = args[0]
        output_index = command.index("-o") + 1
        Path(command[output_index]).write_text("Attempted the issue.\n")
        if first_run_calls["count"] == 1:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout='{"type":"thread.started","thread_id":"thread-success"}\n',
                stderr="",
            )
        return subprocess.CompletedProcess(
            command,
            1,
            stdout='{"type":"thread.started","thread_id":"thread-failed"}\n',
            stderr="agent exited with code 1\n",
        )

    monkeypatch.setattr(issues_workflow.subprocess, "run", _first_run)
    process_review_findings_file(
        repo_root / "issues/review_findings/findings.md",
        repo_root=repo_root,
        output_root=repo_root / "issues/generated",
        template_path=repo_root / "issues/templates/prompt.md.tmpl",
        run_label="retry-run",
        dispatch_agents=True,
    )

    retried_inputs: list[str] = []

    def _retry_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        retried_inputs.append(kwargs["input"])
        command = args[0]
        output_index = command.index("-o") + 1
        Path(command[output_index]).write_text("Fixed after retry.\n")
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='{"type":"thread.started","thread_id":"thread-retry"}\n',
            stderr="",
        )

    monkeypatch.setattr(issues_workflow.subprocess, "run", _retry_run)
    resumed = process_review_findings_file(
        repo_root / "issues/review_findings/findings.md",
        repo_root=repo_root,
        output_root=repo_root / "issues/generated",
        template_path=repo_root / "issues/templates/prompt.md.tmpl",
        run_label="retry-run",
        dispatch_agents=True,
        resume=True,
        skip_existing=True,
        retry_failed=True,
    )

    assert len(retried_inputs) == 1
    assert "finding-002-rerun-match-with-focus-doesnt-add-any-focus" in retried_inputs[0]
    assert resumed.issues[0].dispatch.reused_previous is True
    assert resumed.issues[0].dispatch.status == "succeeded"
    assert resumed.issues[1].dispatch.status == "succeeded"
    assert resumed.issues[1].dispatch.reused_previous is False


def test_isolated_codex_home_bootstraps_auth_and_config(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_home = tmp_path / "source-codex-home"
    source_home.mkdir()
    (source_home / "auth.json").write_text('{"token":"x"}\n')
    (source_home / "config.toml").write_text('model = "gpt-5.4"\n')
    (source_home / "version.json").write_text('{"version":"1"}\n')
    (source_home / "installation_id").write_text("install-1\n")
    (source_home / "rules").mkdir()
    (source_home / "rules/default.rules").write_text("# rules\n")
    monkeypatch.setenv("CODEX_HOME", str(source_home))

    seen_envs: list[dict[str, str]] = []

    def _fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen_envs.append(dict(kwargs["env"]))
        command = args[0]
        output_index = command.index("-o") + 1
        Path(command[output_index]).write_text("Fixed the issue.\n")
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='{"type":"thread.started","thread_id":"thread-123"}\n',
            stderr="",
        )

    monkeypatch.setattr(issues_workflow.subprocess, "run", _fake_run)

    isolated_root = repo_root / "isolated-codex-home"
    manifest = process_review_findings_file(
        repo_root / "issues/review_findings/findings.md",
        repo_root=repo_root,
        output_root=repo_root / "issues/generated",
        template_path=repo_root / "issues/templates/prompt.md.tmpl",
        run_label="isolated-run",
        dispatch_agents=True,
        codex_home_root=isolated_root,
    )

    dispatch = manifest.issues[0].dispatch
    assert dispatch.status == "succeeded"
    isolated_home = Path(seen_envs[0]["CODEX_HOME"])
    assert isolated_home.exists()
    assert (isolated_home / "auth.json").read_text() == '{"token":"x"}\n'
    assert (isolated_home / "config.toml").read_text() == 'model = "gpt-5.4"\n'
    assert (isolated_home / "rules/default.rules").read_text() == "# rules\n"


def test_dispatch_surfaces_auth_failures_as_actionable_errors(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CODEX_HOME", raising=False)

    def _fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        command = args[0]
        return subprocess.CompletedProcess(
            command,
            1,
            stdout=(
                '{"type":"thread.started","thread_id":"thread-123"}\n'
                '{"type":"turn.failed","error":{"message":"401 Unauthorized"}}\n'
            ),
            stderr="Missing bearer or basic authentication in header\n",
        )

    monkeypatch.setattr(issues_workflow.subprocess, "run", _fake_run)

    manifest = process_review_findings_file(
        repo_root / "issues/review_findings/findings.md",
        repo_root=repo_root,
        output_root=repo_root / "issues/generated",
        template_path=repo_root / "issues/templates/prompt.md.tmpl",
        run_label="auth-fail-run",
        dispatch_agents=True,
    )

    dispatch = manifest.issues[0].dispatch
    assert dispatch.status == "failed"
    assert dispatch.thread_id == "thread-123"
    assert dispatch.error is not None
    assert "missing Codex authentication" in dispatch.error


def test_process_review_findings_file_emits_observability_spans(
    repo_root: Path,
    recording_client: _RecordingClient,
) -> None:
    process_review_findings_file(
        repo_root / "issues/review_findings/findings.md",
        repo_root=repo_root,
        output_root=repo_root / "issues/generated",
        template_path=repo_root / "issues/templates/prompt.md.tmpl",
        run_label="trace-run",
    )

    names = [span.name for span in recording_client.spans]
    assert names == [
        "issue_agent_batch",
        "scan_review_findings",
        "materialize_issue_artifacts",
    ]
    batch = recording_client.spans[0]
    assert batch.start_kwargs["metadata"]["run_label"] == "trace-run"
    assert batch.updates[-1]["metadata"]["issues_count"] == "2"
