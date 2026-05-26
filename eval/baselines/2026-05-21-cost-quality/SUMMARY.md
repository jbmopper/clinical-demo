Public-Artifact-Safety: synthetic

# 2026-05-21 Cost/Quality Calibration Slice

Purpose: compare `none`, `retrieval_only`, and `bounded_adjudication` against
the frozen compiler baseline using the current patient-evidence labels.

This is deliberately a modest demo slice. The label file has 21 / 26 usable
rows, split across 15 `open_world` rows and 6 `closed_world_eval` rows. The
planned 20 / 26 gate is now met, but the denominator remains too small for
durable production routing claims.

## Provenance

- Eval dataset: `data/curated/eval_seed.json`
- Extractor cache: frozen cached extraction path
- Matcher execution source: `compiled_predicates`
- Binding strategy: `two_pass`
- Resolver execution policy: `cached_only`
- Label file: `eval/calibration/patient_evidence_labels.json`
- Frozen compiler baseline: `eval/baselines/2026-05-11-compiler-rollout/`
- Frozen compiler diagnostics gate: passed before this sweep

## Label Denominator

| Assumption mode | Usable labels | Interpretation |
|---|---:|---|
| `open_world` | 15 | Main calibrated slice |
| `closed_world_eval` | 6 | Context only |
| Total | 21 | Planned 20 / 26 gate met |

## Open-World Mode Comparison

| Mode | Run ID | Comparable | Accuracy | Abstention | Case rollup | Adjudicator | Latency |
|---|---|---:|---:|---:|---:|---:|---:|
| `none` | `3c66a9b997c6` | 15 | 86.7% | 40.0% | 27 fail / 18 indeterminate / 2 pass_pending_review | 0 calls / $0.0000 | 19.6s |
| `retrieval_only` | `af4c6414901a` | 15 | 86.7% | 40.0% | 27 fail / 18 indeterminate / 2 pass_pending_review | 0 calls / $0.0000 | 20.7s |
| `bounded_adjudication` | `beee2d70e76b` | 15 | 86.7% | 40.0% | 35 fail / 12 indeterminate | 695 calls / $0.1678 | 1050.2s |

`retrieval_only` changed no verdicts by design, but surfaced patient rows for
reviewer inspection.

`bounded_adjudication` changed 9 case rollups relative to `none`: 7
`indeterminate -> fail`, 1 `pass_pending_review -> fail`, and 1
`pass_pending_review -> indeterminate`. It also reduced criterion-level
indeterminates across the full run from 914 to 858. The current calibrated
slice did not show a quality improvement over cheaper modes, so this is not
enough evidence to make bounded adjudication the default route.

## Closed-World Context

| Mode | Run ID | Comparable | Accuracy | Abstention | Case rollup |
|---|---|---:|---:|---:|---:|
| `none` | `b47ada00d6a7` | 6 | 33.3% | 66.7% | 40 fail / 5 indeterminate / 2 pass_pending_review |

Only 6 usable labels match `closed_world_eval`, so no broad closed-world
routing policy should be inferred from this slice.

## Routing Decision

The provisional policy is conservative:

- Batch scoring defaults to `none`.
- Reviewer-facing unresolved rows default to `retrieval_only`.
- `bounded_adjudication` stays opt-in for manual escalation or budgeted eval
  sweeps until a larger label set shows quality lift.

See `routing_policy.json` and `routing_policy_delta.md`.

## Files

- `patient_evidence_open_world_report.json`
- `patient_evidence_open_world_report.md`
- `patient_evidence_closed_world_eval_report.json`
- `patient_evidence_closed_world_eval_report.md`
- `patient_evidence_open_world_none_diagnostics.json`
- `patient_evidence_open_world_retrieval_only_diagnostics.json`
- `patient_evidence_open_world_bounded_adjudication_diagnostics.json`
- `patient_evidence_closed_world_eval_none_diagnostics.json`
- `patient_evidence_modes_summary.json`
- `routing_policy.json`
- `routing_policy_delta.md`
