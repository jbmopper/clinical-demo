# Baseline 2026-04-21 — first end-to-end eval snapshot

First baseline regression numbers, captured under PLAN task 2.7.
The intent of this directory is **a numerical anchor**: every
later prompt rev, vocabulary expansion, model swap, or matcher
tweak compares back to *these* numbers, so we can claim "X%
better" honestly. Two runs captured — imperative and
graph + critic — to baseline the orchestrator-A/B as well.

## Provenance

- **Repo HEAD:** see git log; the commit that added these files.
- **Extractor:** `gpt-4o-mini-2024-07-18`, prompt `extractor-v0.1`,
  schema fingerprint `1977cd68`, Settings caps from D-65 (extractor
  16384, llm_match 1024, critic 2048).
- **Matcher:** `matcher-v0.1` (D-66 soft-fail on extractor invariant
  violations active).
- **Cache:** all 30 trials freshly re-extracted under the D-66
  cache-key scheme (`<NCT>.<prompt>.<schema_fp>.<model>.json`),
  570 criteria total at $0.067.
- **Eval seed:** `data/curated/eval_seed.json`, 49 patient×trial
  pairs, 7 slices × 7 pairs each (T2DM industry/academic,
  hypertension industry/academic, hyperlipidemia, CKD, NSCLC).

## Layer-1 snapshot

Layer-1 measures the **deterministic structured-field cells** —
where the eval seed has a mechanically-derivable label
(`min_age`, `max_age`, `sex`) and the matcher should be able to
agree without any subjective call.

| metric              | imperative | graph+critic |
|---------------------|------------|--------------|
| cells               | 76         | 76           |
| overall agreement   | **81.0%**  | 81.0%        |
| overall coverage    | **55.3%**  | 55.3%        |
| per-field — min_age | 22 / 8 / 19  (agree / disagree / missing) | 22 / 8 / 19 |
| per-field — max_age | 10 / 0 / 15  | 10 / 0 / 15 |
| per-field — sex     | 2 / 0 / 0    | 2 / 0 / 0    |
| skipped (uncoverable) | healthy_volunteers=6 | healthy_volunteers=6 |

**Imperative ≡ graph+critic at layer 1.** As expected — the critic
acts on rollup/rationale, not per-criterion dispatch for structured
kinds. The two orchestrators *do* diverge at the
eligibility-rollup layer (critic moves 2 pairs `indeterminate→fail`
in this run, see `graph_critic_layer1.json` notes vs.
`imperative_layer1.json` notes), but layer 1 doesn't surface that.

## Reading the layer-1 numbers honestly

**81% agreement is a depressed number, not a true accuracy.**
Eight of the eight `min_age` "disagreements" are cases where the
mechanical labeler said `pass` (because the trial's `min_age`
field alone said the patient qualified) but the matcher said
`fail` because it correctly evaluated the *combined* age
criterion (e.g. `"Males and females aged 18-65"` — patient is
69, fails the upper bound). The matcher is *correct* on every
disagreement; the seed labels are partial. A future eval-seed
fix (look up the v0 mechanical labeler and either upgrade it to
parse the full age range or label these cells as
"compound — manual review required") would lift agreement
toward 100%. Tracked as a Phase-3 follow-up.

**55% coverage is real and is the headline.** When a structured
seed cell exists and the matcher couldn't find a matching
extracted criterion, that's an extractor recall problem. 34
missing cells (mostly `max_age` and many `min_age`s) means the
extractor isn't surfacing the age criterion that the trial's
CT.gov-structured `min_age` / `max_age` fields make obvious. The
extractor reads the *eligibility text*, not the structured trial
fields, so when the eligibility text doesn't restate the age
bound (relying instead on the structured field), no criterion is
extracted. This is the right next investment: either teach the
extractor to honor the structured age fields directly (small
prompt patch), or post-process the extraction to add an
implicit age criterion when the structured fields are present
but the extracted criteria don't include one.

## Eligibility rollup snapshot

Across 49 pairs:

| slice                 | fail | indeterminate | pass | imp/graph notes |
|-----------------------|------|---------------|------|-----------------|
| t2dm-industry         | 6    | 1             | 0    | works           |
| t2dm-academic         | 6    | 1             | 0    | works           |
| ckd                   | 2-3  | 4-5           | 0    | partial         |
| hyperlipidemia        | 2    | 5             | 0    | partial         |
| nsclc                 | 1-2  | 5-6           | 0    | partial         |
| hypertension-industry | 0-1  | 6-7           | 0    | **uncovered**   |
| hypertension-academic | 0    | 7             | 0    | **uncovered**   |

**T2DM works, hypertension doesn't.** Concept-lookup vocabulary
has solid diabetes coverage and ~zero hypertension coverage
(D-34 — medications all return `unmapped_concept`, and
hypertension trials lean heavily on medication-history criteria
plus measurement criteria for which v0's lab vocabulary is
incomplete). This is the next concrete investment after the
extractor age-recall fix.

**0 passes anywhere across 49 pairs.** Worth flagging: the
synthea cohort and the curated trials are not deeply aligned
on inclusion criteria. The eligibility *rollup* is conservative
(any single `fail` collapses the trial to `fail`), so every pair
where any matcher hit `fail` lands `fail`. This is correct
deterministic behavior; "0 passes" is more a comment on the
cohort/trial alignment than on matcher quality. If we want
non-vacuous `pass` examples for the demo, we either need to
either (a) curate trials known to admit synthea-style cohorts,
or (b) confirm via per-criterion review that *no* synthea
patient actually satisfies all of any of these 30 trials (which
is possible — these are real industry/academic trials with
strict criteria).

## What this baseline is *not*

- **Not a layer-2 (extraction-F1 vs. Chia) measurement.** Pending
  task 2.5; the eval seed's free-text criteria
  (856 of them) have `free_text_review_status="pending"` —
  zero human-graded labels for the LLM matcher's free-text
  predictions. Layer 2 needs that pass.
- **Not a layer-3 (LLM-as-judge) measurement.** Pending task 2.6.
- **Not a cost/latency baseline.** Captured here for context but
  not the headline metric: imperative 13s total scoring latency
  (cache-warm), graph+critic 312s (≈6s per pair via the full
  graph). Both orchestrators reuse the same cached extractions
  so the LLM cost is the extraction one-time cost ($0.067 for
  all 30 trials). Critic-loop tokens are separately rolled up;
  layer-3 will need its own cost banner.

## Files

- `imperative_layer1.json` — full layer-1 report, run id
  `b55783ff962f`, every cell carried.
- `graph_critic_layer1.json` — same shape, run id
  `ae7ac16936b8`.
- `imperative_run.txt` and `graph_critic_run.txt` — pretty
  one-screen run summaries (the same `_summarize()` output that
  ran in the terminal).

## Next baseline conditions

This file's numbers should be re-snapshotted whenever **any** of:
the extractor prompt revs (would shift schema fingerprint and
filename), the matcher version bumps (`MATCHER_VERSION`), the
concept-lookup vocabulary expands enough to move coverage on a
named slice, or a new orchestrator/critic strategy lands. Each
re-snapshot writes to a new dated directory, never overwrites.
