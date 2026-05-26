Public-Artifact-Safety: synthetic

# Routing Policy Delta

This is a provisional policy delta from the 21-label cost/quality slice. It is
not a production-default recommendation; the planned 20 / 26 usable-label gate
is met, but the denominator is still modest.

## Before

The pre-policy posture was mode-level rather than routed:

- `none` for deterministic scoring.
- `retrieval_only` when reviewer evidence context was desired.
- `bounded_adjudication` for broad sweeps or manual experiments.

## After

The provisional policy keeps that posture conservative:

- Batch scoring defaults to `none`.
- Reviewer-facing unresolved rows default to `retrieval_only`.
- `bounded_adjudication` is an opt-in escalation, not a default route.

## Why

On the open-world slice:

| Mode | Run | Comparable | Accuracy | Abstention | Calls | Cost | Case movement |
|---|---|---:|---:|---:|---:|---:|---:|
| `none` | `3c66a9b997c6` | 15 | 86.7% | 40.0% | 0 | $0.0000 | baseline |
| `retrieval_only` | `af4c6414901a` | 15 | 86.7% | 40.0% | 0 | $0.0000 | 0 changed |
| `bounded_adjudication` | `beee2d70e76b` | 15 | 86.7% | 40.0% | 695 | $0.1678 | 9 changed |

`bounded_adjudication` reduced total criterion-level indeterminacy across the
full run, from 914 to 858 indeterminate criteria, and moved 9 case rollups.
The 15-label open-world slice did not show a quality gain over cheaper modes,
so the routing policy should not default to spending tokens yet.

Closed-world context is weaker: 6 usable labels match the frozen
`closed_world_eval` run, with 2 / 6 correct. That is useful as a warning, not a
routing foundation.

## Next Revisit

Revisit this policy after the remaining labels are filled, after D1 changes
the shape of the free-text/composite rows enough to justify a new calibration
packet, or after a broader patient/trial slice is available.
