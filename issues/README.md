# Issue Agents

This directory holds the review-finding handoff workflow:

- `review_findings/` stores source markdown files copied from code-review output.
- `templates/` stores the prompt template used for each fixing agent.
- `generated/` stores materialized issue specs, prompts, manifests, and optional dispatch logs.

Generate specs and prompts from a review file:

```bash
uv run python scripts/issue_agents.py \
  --source issues/review_findings/2026-04-22-codex-review.md \
  --run-label 2026-04-22-codex-review \
  --overwrite
```

Dispatch fixing agents and recurse until unresolved findings stabilize:

```bash
uv run python scripts/issue_agents.py \
  --source issues/review_findings/2026-04-22-codex-review.md \
  --run-label 2026-04-22-codex-review \
  --dispatch-agents \
  --overwrite
```

Dispatch inherits the current authenticated Codex home by default so the
subprocess can reuse your existing login. Pass `--codex-home-root <dir>` only
when you explicitly want per-issue isolated homes.

With `--dispatch-agents`, the CLI now re-execs itself in fresh Python
processes until a refreshed unresolved-findings review matches the
previous iteration's input by default. Each round re-reviews the repo
against the current findings file, keeps still-valid findings verbatim,
and drops resolved ones. Use `--single-pass` to disable the recursion,
`--until-converged` to force the same behavior without dispatching
agents, and `--max-iterations` to cap the loop.

Coordinator-grade reruns:

```bash
# resume an existing batch, reusing unchanged artifacts and prior successful dispatches
uv run python scripts/issue_agents.py \
  --source issues/review_findings/2026-04-22-codex-review.md \
  --run-label 2026-04-22-codex-review \
  --dispatch-agents \
  --resume --skip-existing

# retry only previously failed dispatches (new/changed issues still run)
uv run python scripts/issue_agents.py \
  --source issues/review_findings/2026-04-22-codex-review.md \
  --run-label 2026-04-22-codex-review \
  --dispatch-agents \
  --resume --skip-existing --retry-failed
```

The script logs operator-visible progress through stdlib `logging` and traces the
scan/materialize/dispatch lifecycle through `clinical_demo.observability`.
