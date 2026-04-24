Fix the issue described in `issues/generated/2026-04-22-codex-review/finding-001-public-hitl-entry-point-cannot-resume-a-paused-run/issue_spec.json` inside `/Users/juliusmopper/Dev/clinical-demo`.

Issue
- id: `finding-001-public-hitl-entry-point-cannot-resume-a-paused-run`
- priority: `P1`
- title: Public HITL entry point cannot resume a paused run
- location: `src/clinical_demo/graph/score_pair_graph.py:79-101`

Summary
`score_pair_graph()` documents that callers can pause on `human_checkpoint=True`
and resume with the same `thread_id`, but this wrapper always rebuilds a fresh
graph and invokes it from a brand-new `initial_state`. Because `build_graph()`
allocates a new `InMemorySaver` per compilation, any checkpoint written on the
first call is lost before the second one. The graph-level tests pass only
because they keep the same compiled graph instance alive; callers that use the
public function cannot actually resume.

Problem Statement
`score_pair_graph()` documents that callers can pause on `human_checkpoint=True`
and resume with the same `thread_id`, but this wrapper always rebuilds a fresh
graph and invokes it from a brand-new `initial_state`. Because `build_graph()`
allocates a new `InMemorySaver` per compilation, any checkpoint written on the
first call is lost before the second one. The graph-level tests pass only
because they keep the same compiled graph instance alive; callers that use the
public function cannot actually resume.

Related Tests
- tests/graph/test_score_pair_graph.py
- tests/graph/__init__.py
- tests/graph/_fixtures.py
- tests/graph/test_critic_loop_e2e.py
- tests/graph/test_critic_node.py

Acceptance Criteria
- The documented pause/resume flow works through the public entry point, not only through a precompiled graph object.
- Regression coverage exercises a pause followed by a resume using the same external handle the finding calls out.
- Implement the smallest correct fix for the behavior described in the review finding.
- Add or update automated coverage that would fail before the fix and pass after it.
- Keep unrelated behavior and interfaces unchanged unless the finding explicitly requires a contract change.

Constraints
- Do not revert unrelated local changes in the worktree.
- Start from the code around src/clinical_demo/graph/score_pair_graph.py:79-101 and expand scope only when the fix truly needs it.
- If comments or docstrings currently promise the broken behavior, update them so the implementation and docs agree.

Suggested Verification
1. uv run pytest tests/graph/test_score_pair_graph.py tests/graph/__init__.py tests/graph/_fixtures.py tests/graph/test_critic_loop_e2e.py tests/graph/test_critic_node.py
2. uv run pytest
3. uv run ruff check .
4. uv run mypy

Raw Review Finding
```markdown
## Finding 1 (src/clinical_demo/graph/score_pair_graph.py:79-101) [added]
[P1] Public HITL entry point cannot resume a paused run

`score_pair_graph()` documents that callers can pause on `human_checkpoint=True`
and resume with the same `thread_id`, but this wrapper always rebuilds a fresh
graph and invokes it from a brand-new `initial_state`. Because `build_graph()`
allocates a new `InMemorySaver` per compilation, any checkpoint written on the
first call is lost before the second one. The graph-level tests pass only
because they keep the same compiled graph instance alive; callers that use the
public function cannot actually resume.
```

Execution Notes
1. Read the spec file plus the referenced source/test files before editing.
2. Confirm the current bug or contract gap in code, tests, or docs.
3. Implement the smallest correct fix.
4. Add or update regression coverage.
5. Run the most targeted verification first, then broader checks that are still reasonable.
6. Summarize the code changes, the verification you ran, and any residual risk.

Guardrails
- Do not revert unrelated worktree changes.
- Keep the scope tight to this issue unless the fix requires a small supporting refactor.
- If the finding text and the current code disagree, trust the code you can observe and explain the mismatch in the final summary.
