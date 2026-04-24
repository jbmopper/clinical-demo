# Review findings

## Finding 1 (src/clinical_demo/graph/score_pair_graph.py:79-101) [added]
[P1] Public HITL entry point cannot resume a paused run

`score_pair_graph()` documents that callers can pause on `human_checkpoint=True`
and resume with the same `thread_id`, but this wrapper always rebuilds a fresh
graph and invokes it from a brand-new `initial_state`. Because `build_graph()`
allocates a new `InMemorySaver` per compilation, any checkpoint written on the
first call is lost before the second one. The graph-level tests pass only
because they keep the same compiled graph instance alive; callers that use the
public function cannot actually resume.

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
