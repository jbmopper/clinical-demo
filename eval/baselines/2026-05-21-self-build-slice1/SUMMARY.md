Public-Artifact-Safety: synthetic

# 2026-05-21 Self-Build Slice 1

Purpose: smoke-test D1, deterministic mention-to-composite promotion, after the
cost/quality baseline was captured.

## Provenance

- Run ID: `5f3acbeb448f`
- Eval dataset: `data/curated/eval_seed.json`
- Matcher execution source: `compiled_predicates`
- Matcher assumption mode: `closed_world_eval`
- LLM use level: `none`
- Binding strategy: `two_pass`
- Resolver execution policy: `cached_only`

## What Changed

`fix_extracted_criteria` now lets free-text criteria emit native
`composite_groups: any_of` when all of these are true:

- the source text matches a supported deterministic shape,
- subchecks are built 1:1 from existing `Condition` or `Drug` mentions,
- every promoted mention already has a reviewed terminology or reviewed
  medication-class decision.

The supported fixture shapes are:

- parenthetical comma-separated condition lists,
- inline disjunctions with a temporal qualifier,
- `Treatment with any of the following...` medication lists.

The reviewed-decision guard is deliberate. An earlier local smoke pass showed
that promoting every mention eagerly reopened opaque compiler
`unmapped_concept` debt. The committed D1 slice instead waits for D3/review
packets before unreviewed mentions enter executable composite groups.

## Smoke Result

| Metric | Frozen baseline | D1 smoke |
|---|---:|---:|
| Case rollup | 40 fail / 5 indeterminate / 2 pass_pending_review | 40 fail / 5 indeterminate / 2 pass_pending_review |
| Checkable predicates | 368 | 368 |
| Unresolved compiler gaps | 280 | 280 |
| `unmapped_concept` gaps | 0 | 0 |
| Closed-world blocking cases | 43 | 43 |
| Closed-world blocking findings | 379 | 379 |

The frozen compiler diagnostics gate passes byte-for-byte at the threshold
level. This first safe D1 slice adds the deterministic detector and tests, but
does not yet move frozen-run counts because the relevant current-cache examples
contain at least one unreviewed mention surface.

## Patient-Evidence Check

The same 21-label file was rerun against `5f3acbeb448f`. Six labels match the
`closed_world_eval` assumption mode, and the report is 2 / 6 correct with
66.7% abstention, matching the refreshed pre-D1 closed-world context.

## Files

- `compiled_predicates_diagnostics.json`
- `patient_evidence_post_d1_report.md`
