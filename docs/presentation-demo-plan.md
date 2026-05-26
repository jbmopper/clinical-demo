# Presentation Demo Plan

This is the presentation checkpoint before D3. The goal is to show a working
clinical trial eligibility co-pilot on concrete patient/trial examples, while
using the calibrated artifacts for measured claims.

## Current Pause

D3 is paused. The repo already has enough measured material for the core story:

- deterministic scorer and reviewer UI,
- 21 / 26 patient-evidence labels,
- cost/quality routing comparison,
- D1 deterministic composite promotion,
- D2 `interview_required` abstention for non-chart evidence.

Do not start the bounded patch-proposal workflow until the presentation has a
casebook, screenshots, and a live or replayable matching walkthrough.

## Presentation Artifacts To Use

| Story beat | Artifact |
|---|---|
| Readiness snapshot | `docs/deployment-readiness.md` |
| Architecture walkthrough | `docs/system-architecture-walkthrough.md` |
| Scope boundaries | `docs/known-limitations-and-scope.md` |
| Compiler baseline | `eval/baselines/2026-05-11-compiler-rollout/SUMMARY.md` |
| Cost/quality slice | `eval/baselines/2026-05-21-cost-quality/SUMMARY.md` |
| Open-world patient evidence | `eval/baselines/2026-05-21-cost-quality/patient_evidence_open_world_report.md` |
| Routing decision | `eval/baselines/2026-05-21-cost-quality/routing_policy_delta.md` |
| D1 self-build example | `eval/baselines/2026-05-21-self-build-slice1/SUMMARY.md` |
| D2 interview-required example | `eval/baselines/2026-05-26-interview-required/SUMMARY.md` |

Avoid using the large `compiler_gap_review*.json` files directly in slides.
Use their summary numbers and pull one row only as an appendix example.

## What To Create Next

1. **Presentation casebook.**
   Pick 3-5 patient/trial pairs:
   - one clear fail,
   - one possible match / `pass_pending_review`,
   - one useful `retrieval_only` indeterminate,
   - one D2-style `interview_required` abstention.

   Current persisted runs do not show a fully clean `pass` rollup. That is
   acceptable for the demo: the honest positive example is
   `pass_pending_review`, meaning no deterministic fail was found but unresolved
   criteria still need coordinator review. Candidate already observed in the
   D2 smoke run: `c46a254d__NCT07335211`.

2. **Replayable score outputs.**
   Export each selected pair to JSON and a compact Markdown reading view under
   `eval/baselines/<date>-presentation-demo/`. These are presentation examples,
   not eval-denominator artifacts.

3. **Reviewer UI screenshots.**
   Capture the score page, expanded criterion rationale, retrieved evidence
   rows, and the patient-evidence labeling view. The screenshots should show
   the actual working surface, not only summary tables.

4. **New trial import path.**
   Add or document a one-NCT import flow so the demo can say: "Give me a trial
   ID, cache/extract its criteria, and match it against a selected synthetic
   patient."

5. **New patient selection path.**
   For the presentation, using an existing Synthea patient from the full raw
   bundle is enough. If the patient is outside the curated cohort, append a
   small presentation-only cohort manifest entry or add a CLI flag that allows
   scoring any local Synthea bundle by patient id.

## Existing Matching Commands

Run the API and reviewer UI:

```bash
uv run python scripts/serve.py
cd web
npm run dev
```

Score an existing curated pair:

```bash
uv run python scripts/score_pair.py \
  --patient-id <synthea-patient-id-from-data/curated/cohort_manifest.json> \
  --nct-id <nct-id-from-data/curated/trials_manifest.json> \
  --no-llm
```

Export a machine-readable score result:

```bash
uv run python scripts/score_pair.py \
  --patient-id <patient-id> \
  --nct-id <nct-id> \
  --json > eval/baselines/<date>-presentation-demo/<patient>__<nct>.json
```

Score through the API:

```bash
curl -s -X POST http://127.0.0.1:8000/score \
  -H 'content-type: application/json' \
  -d '{"patient_id":"<patient-id>","nct_id":"<nct-id>","as_of":"2025-01-01","llm_use_level":"retrieval_only"}'
```

## Import Flow To Make Demoable

The current repo can curate batches of ClinicalTrials.gov trials with
`scripts/curate_trials.py`, but the presentation wants a tighter one-trial
flow:

```bash
# target shape to create next
uv run python scripts/import_trial.py --nct-id NCT... --extract

uv run python scripts/score_pair.py \
  --patient-id <existing-or-imported-synthea-patient-id> \
  --nct-id NCT... \
  --llm-use-level retrieval_only
```

For patients, the minimum presentation path is:

```bash
# target shape to create next
uv run python scripts/select_demo_patient.py --condition type-2-diabetes --limit 5
```

That can emit candidate patient ids from local Synthea bundles, then the
existing scorer can run the selected patient/trial pair.

## Slide Claims To Keep Honest

- The data is synthetic Synthea plus public ClinicalTrials.gov metadata.
- The system supports reviewer decision-making; it does not enroll patients.
- Missing evidence is not proof of absence in `open_world`.
- `bounded_adjudication` is measured but not the default route.
- D1/D2 show guarded self-building and abstention behavior.
- D3 is a planned next loop, not part of the current shipped demo.
