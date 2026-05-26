Public-Artifact-Safety: synthetic

# Patient Evidence Calibration Report

## Labels

- path: `eval/calibration/patient_evidence_labels.json`
- filled: 21/26
- usable for verdict metrics: 21/26

## Runs

| Run | LLM use | Comparable | Accuracy | Abstention | Citation agreement | Mode skipped | Retrieved rows | Decisive citations | Eligibility | Adjudicator |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `3c66a9b997c6` | `none` | 15/26 | 86.7% | 40.0% | n/a | 6 | (none) | (none) | fail=27 / indeterminate=18 / pass_pending_review=2 | 0 calls / $0.0000 |
| `af4c6414901a` | `retrieval_only` | 15/26 | 86.7% | 40.0% | n/a | 6 | condition=517 / demographics=6 / medication=105 / note=1394 / observation=426 / procedure=610 | (none) | fail=27 / indeterminate=18 / pass_pending_review=2 | 0 calls / $0.0000 |
| `beee2d70e76b` | `bounded_adjudication` | 15/26 | 86.7% | 40.0% | n/a | 6 | condition=348 / demographics=4 / medication=59 / note=864 / observation=307 / procedure=343 | condition=12 / medication=13 / note=130 / observation=12 / procedure=3 | fail=35 / indeterminate=12 | 695 calls / $0.1678 |

## Case Rollup Movement

| Baseline | Comparison | Changed cases | Movements |
|---|---|---:|---|
| `3c66a9b997c6` | `af4c6414901a` | 0 | (none) |
| `3c66a9b997c6` | `beee2d70e76b` | 9 | indeterminate->fail=7 / pass_pending_review->fail=1 / pass_pending_review->indeterminate=1 |
