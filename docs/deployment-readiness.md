# Deployment readiness

This document is the demo-facing readiness stub for the clinical trial
eligibility co-pilot. It separates what is already measured from what still
needs review before anyone should treat the system as operational.

## Current readiness snapshot

| Area | Current evidence | Readiness interpretation |
|---|---:|---|
| Layer-1 structured fields | 89.0% agreement / 98.6% coverage | Presentable as a stable structured-field baseline. |
| Compiler closure | 0 / 1076 `unmapped_concept` rows | Opaque terminology misses are closed in the frozen demo snapshot. |
| Typed compiler gaps | 282 unresolved gaps | All remaining compiler blockers are classified, including 2 interview-required rows. |
| Closed-world validation | 43 blocking cases / 379 blocking findings | Compiled execution is still gated for closed-world use. |
| Patient-evidence calibration | 21 / 26 usable labels | Planned gate cleared; still a modest demo denominator. |
| Assumption modes | `open_world`, `closed_world_eval`, `closed_world_demo` | Missing chart evidence is handled explicitly rather than implied. |

Authoritative frozen compiler snapshot:
`eval/baselines/2026-05-11-compiler-rollout/SUMMARY.md`.

Current post-D2 compiler-gap snapshot:
`eval/baselines/2026-05-26-interview-required/SUMMARY.md`.

Current presentation checkpoint:
`docs/presentation-demo-plan.md`. D3 is intentionally paused until the
presentation casebook and new trial/patient matching flow are concrete.

## Product contract

The system screens a patient record against a trial protocol and returns
per-criterion eligibility verdicts with source-traceable evidence. It is a
review support tool for a clinical research coordinator. It does not enroll
patients, replace investigator judgment, or claim medical-device validation.

## Evidence assumptions

Read `docs/matcher-assumption-modes.md` before interpreting a score:

- `open_world` is the clinical default. Missing chart rows are not proof of
  absence for conditions, medication exposure, temporal windows, or procedures.
- `closed_world_eval` is for synthetic eval slices where the patient record is
  treated as complete enough to test absence-dependent behavior.
- Missing labs stay indeterminate in every mode.
- `unmapped_concept` stays indeterminate in every mode. Closed-world evaluation
  must not hide terminology failure.

## LLM-use levels

The patient-evidence report compares three levels:

| Level | Behavior | Deployment stance |
|---|---|---|
| `none` | Deterministic matcher only. | Cheapest baseline, easiest to audit. |
| `retrieval_only` | Attaches ranked patient rows to indeterminate verdicts without changing verdicts. | Good default reviewer aid. |
| `bounded_adjudication` | Lets an LLM adjudicate from retrieved rows only, with citation fail-closed rules. | Useful only where measured quality justifies token spend. |

The current 21-label slice is enough to show the reporting path and rough
mode behavior. It is not enough to set a durable production routing policy.

## Reproduction gate

Run the frozen compiler diagnostics gate before claiming compatibility with
the demo snapshot:

```bash
uv run python scripts/check_compiler_diagnostics.py \
  --diagnostics eval/baselines/2026-05-11-compiler-rollout/compiled_predicates_diagnostics.json \
  --max-unresolved-gaps 280 \
  --max-closed-world-blocking-cases 43 \
  --max-closed-world-blocking-findings 379 \
  --max-gap-kind unmapped_concept=0 \
  --max-gap-kind unsupported_predicate=252 \
  --max-gap-kind ambiguous_mapping=8 \
  --max-gap-kind insufficient_source=10 \
  --max-gap-kind normal_range_unknown=4 \
  --max-gap-kind provenance_required=6
```

For the post-D2 current-code snapshot, use
`eval/baselines/2026-05-26-interview-required/compiled_predicates_diagnostics.json`
with `--max-unresolved-gaps 282` and
`--max-gap-kind interview_required=2`.

## Remaining blockers

- Presentation casebook and replayable score outputs are not packaged yet.
- One-trial import and broader Synthea-patient selection flows need a short
  demo path before claiming "bring your own patient/trial" ergonomics.
- Finish the remaining 5 patient-evidence labels before freezing final demo
  claims, if time permits.
- Closed-world compiled execution still has validation blockers.
- Nested composite criteria and richer event extraction remain out of scope.
- Note-aware free-text behavior is not yet calibrated on a clinical note corpus.
- No MIMIC-IV adapter or governance-complete real-patient deployment path ships
  in this repository.

Related docs:
`docs/evaluation-layers-and-gates.md`,
`docs/matcher-assumption-modes.md`,
`docs/known-limitations-and-scope.md`,
`docs/system-architecture-walkthrough.md`.
