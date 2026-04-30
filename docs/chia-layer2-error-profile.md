# Chia Layer-2 Error Profile

Last updated: 2026-04-30

## Purpose

Layer 2 currently measures extractor mention fidelity against Chia BRAT
entities. This is **entity-mention F1** over normalized `(type, surface)`
pairs, not full Chia graph F1. The current extractor emits flat
`criterion.mentions`; it does not emit Chia relations or equivalence groups.

This report exists to decide the next extraction move:

- Prompt-only mention discipline.
- Schema-level Chia graph extraction.
- Boundary-aware diagnostics for exact-match misses.

## Retained 50-Document Run

Command:

```bash
uv run python scripts/eval.py chia \
  --sample-size 50 \
  --sample-seed 20260430 \
  --write-sample-manifest eval/baselines/2026-04-30/layer2_chia_retained50_manifest.json \
  --output-json eval/baselines/2026-04-30/layer2_chia_retained50_entity_f1.json
```

Artifacts:

- Manifest: `eval/baselines/2026-04-30/layer2_chia_retained50_manifest.json`
- Report: `eval/baselines/2026-04-30/layer2_chia_retained50_entity_f1.json`

Result:

- Documents: 50
- Gold mentions: 923
- Predicted mentions: 573
- True positives: 257
- Micro precision: 44.9%
- Micro recall: 27.8%
- Micro F1: 34.4%
- Macro F1: 33.0%
- Extraction cost: $0.0588
- Runtime: ~8.4 minutes

## What Works

- `Condition` is meaningfully recoverable: 259 gold, 291 predicted, 162 true
  positives; F1 58.9%. It is the main reason retained-sample F1 is higher than
  the 5-document smoke.
- `Drug` remains strong: 82 gold, 47 predicted, 39 true positives; precision
  83.0%, F1 60.5%.
- `Measurement` has useful precision but weak recall: 77 gold, 30 predicted,
  23 true positives; precision 76.7%, recall 29.9%, F1 43.0%.

## Main Error Profile

- **Context labels are still the dominant miss.** `Scope` has 79 gold mentions
  and only 4 predicted, with 0 exact true positives. `Observation` has 32 gold
  and 0 true positives. `Multiplier` has 17 gold and 0 predicted. These are
  legal `EntityMention` labels today, so this is not yet evidence that the
  schema must change.
- **Temporal and value spans are mostly boundary mismatches.** `Temporal` has
  59 gold, 31 predicted, but only 2 true positives. `Value` has 87 gold, 64
  predicted, but only 9 true positives. The extractor often finds a number or
  duration but drops comparator words, anchors, or Chia's wider span.
- **Qualifier remains under-modeled.** `Qualifier` has 70 gold, 26 predicted,
  and 3 true positives. Common misses include severity, prior/current status,
  proof/diagnosis language, and exception modifiers.
- **Negation is present but poorly shaped.** `Negation` has 22 gold, 13
  predicted, and only 2 true positives. The extractor often labels larger
  phrases where Chia expects cue tokens like "without", "no", or "do not".
- **Some Chia labels are intentionally unsupported by the extractor schema.**
  The retained sample skipped 54 gold annotations across labels such as
  `Non-query-able`, `Post-eligibility`, `Pregnancy_considerations`,
  `Undefined_semantics`, and parsing/context error tags. These should not drive
  matcher-oriented extraction work unless the product goal shifts toward full
  Chia annotation reproduction.

## Decision

Proceed with a **prompt-only mention-discipline pass first**.

Rationale:

- The biggest losses are from labels already expressible in the current schema:
  `Scope`, `Temporal`, `Value`, `Qualifier`, `Observation`, `Negation`,
  `Reference_point`, and `Multiplier`.
- A schema-level graph would be premature while flat mention recall for those
  labels is still weak.
- Full Chia graph F1 will eventually require relation/equivalence output, but
  mention F1 should improve first so graph errors are not dominated by missing
  nodes.

## Next Prompt Pass

Target the extractor prompt with explicit rules and few-shot examples for:

- `Scope`: annotate the full boolean span that joins alternatives or modifier
  context, not just the clinical nouns.
- `Temporal`: include full Chia-style windows and anchors, e.g. "within the last
  6 months" rather than only "6 months".
- `Value`: include comparators and units, e.g. "greater than or equal to 18
  years" rather than only "18 years".
- `Negation`: label cue tokens or cue phrases, not whole negated clauses.
- `Qualifier`: separate status/severity/proof modifiers from conditions.
- `Observation`: distinguish clinical observations like history, smoker status,
  alcohol abuse, life expectancy, and growth-factor use from conditions.

After the prompt pass, rerun the same retained sample using
`--sample-size 50 --sample-seed 20260430` and compare exact mention F1 deltas
against the artifacts above.

## Prompt Pass Result: extractor-v0.3

Command:

```bash
uv run python scripts/eval.py chia \
  --sample-size 50 \
  --sample-seed 20260430 \
  --write-sample-manifest eval/baselines/2026-04-30/layer2_chia_retained50_v03_manifest.json \
  --output-json eval/baselines/2026-04-30/layer2_chia_retained50_entity_f1_v03.json
```

Artifacts:

- Manifest: `eval/baselines/2026-04-30/layer2_chia_retained50_v03_manifest.json`
- Report: `eval/baselines/2026-04-30/layer2_chia_retained50_entity_f1_v03.json`

Result versus retained baseline:

- Predicted mentions: 573 -> 675 (+102)
- True positives: 257 -> 300 (+43)
- Micro precision: 44.9% -> 44.4% (-0.4 pp)
- Micro recall: 27.8% -> 32.5% (+4.7 pp)
- Micro F1: 34.4% -> 37.5% (+3.2 pp)
- Macro F1: 33.0% -> 35.4% (+2.4 pp)
- Extraction cost: $0.0710
- Runtime: ~9.5 minutes

Largest improvements:

- `Value`: F1 11.9% -> 35.5% (+23.6 pp), true positives 9 -> 27.
- `Procedure`: F1 14.1% -> 32.7% (+18.6 pp), true positives 6 -> 17.
- `Reference_point`: F1 14.8% -> 30.8% (+16.0 pp), true positives 2 -> 4.
- `Temporal`: F1 4.4% -> 20.0% (+15.6 pp), true positives 2 -> 9.
- `Measurement`: F1 43.0% -> 51.6% (+8.6 pp), true positives 23 -> 32.

Regressions / new risks:

- `Drug`: F1 60.5% -> 52.9% (-7.6 pp). Precision fell as the prompt
  pushed the model toward more context-rich mention emission.
- `Condition`: F1 58.9% -> 57.6% (-1.3 pp). Small regression; still the
  strongest high-volume label.
- `Observation`: predicted mentions jumped 6 -> 70, but true positives only
  moved 0 -> 1. The prompt made the model over-label broad behavioral /
  history phrases as `Observation`; this needs tightening before another
  full retained run.
- `Scope`: predicted 4 -> 13, but true positives only 0 -> 1. The label is no
  longer absent, but exact-boundary recall remains effectively unsolved.

Interpretation:

The prompt-first strategy worked enough to justify staying on this path:
exact mention F1 improved without a precision collapse, and the targeted
labels (`Value`, `Temporal`, `Procedure`, `Reference_point`) moved in the
right direction. It is not yet good enough to move on as "solved": `Scope`
and `Observation` remain the core unresolved classes, and exact matching may
be underrating partial span improvements.

## Overlap / Containment Diagnostic: extractor-v0.3

Command:

```bash
uv run python scripts/eval.py chia \
  --sample-size 50 \
  --sample-seed 20260430 \
  --no-llm \
  --output-json eval/baselines/2026-04-30/layer2_chia_retained50_entity_f1_v03_overlap.json
```

Artifact:

- Report:
  `eval/baselines/2026-04-30/layer2_chia_retained50_entity_f1_v03_overlap.json`

Result:

- Exact true positives: 300
- Additional same-type partial matches: 159
- Exact micro F1: 37.5%
- Lenient micro precision: 68.0%
- Lenient micro recall: 49.7%
- Lenient micro F1: 57.4%
- Lenient macro F1: 54.3%

Largest exact-to-lenient gains:

- `Value`: 35.5% -> 73.7% F1 (+38.2 pp), with 29 partial matches.
- `Temporal`: 20.0% -> 51.1% F1 (+31.1 pp), with 14 partial matches.
- `Condition`: 57.6% -> 81.0% F1 (+23.4 pp), with 63 partial matches.
- `Person`: 26.7% -> 50.0% F1 (+23.3 pp), with 7 partial matches.
- `Drug`: 52.9% -> 74.3% F1 (+21.4 pp), with 15 partial matches.

Interpretation:

The strict Chia surface metric is underrating boundary progress. A large share
of the v0.3 misses are same-type spans that contain or substantially overlap
the gold span, especially for values, temporal windows, and clinical nouns. This
means the next prompt pass should avoid broad changes that optimize only exact
surface matching.

`Scope` is the important exception: it remains mostly a true missing-label
problem, not merely a boundary problem. It moved from 2.2% exact F1 to only 6.5%
lenient F1, with 2 partial matches across 79 gold spans. `Observation` also
remains risky: lenient F1 moves from 2.0% to 11.8%, but the model still predicts
70 observations for 32 gold spans.

Recommended next step:

Do a smaller v0.4 prompt tightening focused on `Observation` precision and
`Scope` recall/boundary examples, rerunning the same retained sample afterward.
Do not move to schema-level Chia relation/equivalence output yet; the flat
mention layer still has recoverable prompt-level misses.
