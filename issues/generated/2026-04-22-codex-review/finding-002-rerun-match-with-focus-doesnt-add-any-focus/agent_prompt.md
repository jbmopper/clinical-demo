Fix the issue described in `issues/generated/2026-04-22-codex-review/finding-002-rerun-match-with-focus-doesnt-add-any-focus/issue_spec.json` inside `/Users/juliusmopper/Dev/clinical-demo`.

Issue
- id: `finding-002-rerun-match-with-focus-doesnt-add-any-focus`
- priority: `P1`
- title: rerun_match_with_focus doesn't add any focus
- location: `src/clinical_demo/graph/nodes/revise.py:199-245`

Summary
The revise loop claims to prepend the critic rationale as a reviewer note, but
`_focused_match_state()` never carries that rationale forward and
`llm_match_node()` builds its prompt from only the criterion plus the patient
snapshot. For free-text findings this means the "focused" re-run is the same
request as the original LLM match. With temperature pinned to `0.0`, the
`low_confidence_indeterminate` and `extraction_disagreement_with_text`
revisions are effectively inert, so the critic loop cannot make real progress
on two of its three finding kinds.

Problem Statement
The revise loop claims to prepend the critic rationale as a reviewer note, but
`_focused_match_state()` never carries that rationale forward and
`llm_match_node()` builds its prompt from only the criterion plus the patient
snapshot. For free-text findings this means the "focused" re-run is the same
request as the original LLM match. With temperature pinned to `0.0`, the
`low_confidence_indeterminate` and `extraction_disagreement_with_text`
revisions are effectively inert, so the critic loop cannot make real progress
on two of its three finding kinds.

Related Tests
- tests/graph/test_revise_node.py
- tests/graph/__init__.py
- tests/graph/_fixtures.py
- tests/graph/test_critic_loop_e2e.py
- tests/graph/test_critic_node.py

Acceptance Criteria
- The focused re-run path passes distinct reviewer context into the follow-up matcher call.
- Regression coverage proves the focused prompt/context differs from the original free-text matcher invocation.
- Implement the smallest correct fix for the behavior described in the review finding.
- Add or update automated coverage that would fail before the fix and pass after it.
- Keep unrelated behavior and interfaces unchanged unless the finding explicitly requires a contract change.

Constraints
- Do not revert unrelated local changes in the worktree.
- Start from the code around src/clinical_demo/graph/nodes/revise.py:199-245 and expand scope only when the fix truly needs it.
- If comments or docstrings currently promise the broken behavior, update them so the implementation and docs agree.

Suggested Verification
1. uv run pytest tests/graph/test_revise_node.py tests/graph/__init__.py tests/graph/_fixtures.py tests/graph/test_critic_loop_e2e.py tests/graph/test_critic_node.py
2. uv run pytest
3. uv run ruff check .
4. uv run mypy

Raw Review Finding
```markdown
## Finding 2 (src/clinical_demo/graph/nodes/revise.py:199-245) [added]
[P1] rerun_match_with_focus doesn't add any focus

The revise loop claims to prepend the critic rationale as a reviewer note, but
`_focused_match_state()` never carries that rationale forward and
`llm_match_node()` builds its prompt from only the criterion plus the patient
snapshot. For free-text findings this means the "focused" re-run is the same
request as the original LLM match. With temperature pinned to `0.0`, the
`low_confidence_indeterminate` and `extraction_disagreement_with_text`
revisions are effectively inert, so the critic loop cannot make real progress
on two of its three finding kinds.
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
