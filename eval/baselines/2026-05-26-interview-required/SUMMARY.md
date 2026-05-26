Public-Artifact-Safety: synthetic

# 2026-05-26 Interview-Required Gap Slice

Purpose: smoke-test D2, the `interview_required` compiler gap for criteria
that ask for patient/site interview information rather than structured FHIR
evidence.

## Provenance

- Run ID: `c8cee5709c35`
- Eval dataset: `data/curated/eval_seed.json`
- Matcher execution source: `compiled_predicates`
- Matcher assumption mode: `closed_world_eval`
- LLM use level: `none`
- Binding strategy: `two_pass`
- Resolver execution policy: `cached_only`

## What Changed

Clinical-trial participation free-text criteria now compile to a
`free_text_review` predicate with an `interview_required` gap. This makes the
system abstain instead of treating trial participation as ordinary executable
trial-exposure evidence.

The first targeted row is `1293efbb__NCT06964087` criterion 28. The same
allow-list also found `e7d52393__NCT06568471` criterion 22, another
clinical-trial participation exclusion.

## Smoke Result

| Metric | Frozen baseline | D2 smoke |
|---|---:|---:|
| Case rollup | 40 fail / 5 indeterminate / 2 pass_pending_review | 29 fail / 17 indeterminate / 1 pass_pending_review |
| Checkable predicates | 368 | 367 |
| Unresolved compiler gaps | 280 | 282 |
| `unmapped_concept` gaps | 0 | 0 |
| `interview_required` gaps | 0 | 2 |
| `unsupported_predicate` gaps | 252 | 252 |
| Closed-world blocking cases | 43 | 43 |
| Closed-world blocking findings | 379 | 379 |

The D2 gate is intentionally denominator-aware: the new typed gap makes two
clinical-trial participation blockers explicit, so total unresolved compiler
gaps rises from 280 to 282 while closed-world blockers and
`unsupported_predicate` remain flat. The case rollup shifts toward
`indeterminate` because these criteria now abstain instead of compiling as
ordinary trial-exposure predicates.

## Patient-Evidence Check

The 21-label file was rerun against `c8cee5709c35`. Six labels match the
`closed_world_eval` assumption mode, and the report remains 2 / 6 correct with
66.7% abstention. The denominator and accuracy match the refreshed pre-D2
closed-world context; the case rollup now reflects the D2 abstention behavior.

## Verification

```bash
uv run pytest tests/extractor/test_fix.py tests/compiler/test_pipeline.py \
  tests/compiler/test_reviewer_queue.py tests/evals/test_patient_evidence.py

uv run python scripts/check_compiler_diagnostics.py \
  --diagnostics eval/baselines/2026-05-26-interview-required/compiled_predicates_diagnostics.json \
  --max-unresolved-gaps 282 \
  --max-closed-world-blocking-cases 43 \
  --max-closed-world-blocking-findings 379 \
  --max-gap-kind unmapped_concept=0 \
  --max-gap-kind unsupported_predicate=252 \
  --max-gap-kind ambiguous_mapping=8 \
  --max-gap-kind insufficient_source=10 \
  --max-gap-kind normal_range_unknown=4 \
  --max-gap-kind provenance_required=6 \
  --max-gap-kind interview_required=2
```

## Files

- `compiled_predicates_diagnostics.json`
- `compiler_gap_review.json`
- `compiler_gap_review_groups.json`
- `patient_evidence_post_d2_report.md`
