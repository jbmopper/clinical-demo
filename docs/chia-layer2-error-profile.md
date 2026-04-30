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
