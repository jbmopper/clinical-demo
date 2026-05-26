# Implementation Plan

**Scope:** the next ~3 weeks. Through the interview demo (week 1) and into the
first post-demo self-building slice (weeks 2-3). For full history, decisions,
and the long Phase-3/Phase-4 backlog see `PLAN.md`. This doc is the
**executable** plan: tracks, dependencies, inputs/outputs, and the explicit
trigger points for regenerating calibration data and re-running evals.

**Authoritative cross-references:**

- `PLAN.md` §0 — current state, gates, frozen baseline.
- `PLAN.md` §6 — task table; tasks referenced here as `§2.X` / `§3.X` are §6 rows.
- `PLAN.md` §12 — decision log; `D-74` is the self-building track decision.
- `eval/baselines/2026-05-11-compiler-rollout/SUMMARY.md` — frozen demo baseline.

**Execution note (2026-05-15).** The first cost/quality slice was initially
run with 10 / 26 usable labels by explicit operator decision, then refreshed
after the label file reached **21 / 26 usable labels**. The resulting artifacts
live under `eval/baselines/2026-05-21-cost-quality/` and should be read as a
calibrated demo slice: 15 usable `open_world` rows, 6 usable
`closed_world_eval` rows, the planned ≥ 20 / 26 gate met, and no statistically
durable production routing claim. SQLite remains the local eval store;
DB-writing eval runs were kept serial instead of replacing the store or adding
a merge step.

**Execution note (2026-05-26).** D2 shipped as a typed compiler gap rather than
a matcher-verdict change. The smoke artifacts live under
`eval/baselines/2026-05-26-interview-required/`: total unresolved compiler gaps
rise from 280 to 282 because two previously implicit free-text review blockers
are now explicit `interview_required` gaps. Closed-world blocking cases,
blocking findings, and `unsupported_predicate` counts stay flat.

**Pause note (2026-05-26).** D3 is intentionally paused. The next work is
presentation-demo packaging: pick concrete patient/trial walkthroughs, export
score outputs, make the "pull in a new trial/patient and match" flow visible,
and only then decide whether the bounded patch-proposal workflow is worth the
extra machinery for this demo.

---

## 1. Tracks at a glance

Four tracks run during the next three weeks. Track A is the critical path to
the demo; Tracks B and C run in parallel; Track D is post-demo.

```
                     week 1 (2026-05-14 → 2026-05-21)            │ week 2-3 (post-demo)
─────────────────────────────────────────────────────────────────┼──────────────────────
Track A  [A1 fill labels] → [A2 cost/quality sweep]              │
(critical path)            ↘                                     │
                            [A3 routing policy diff]             │
                                                                 │
Track B  [B1 deployment-readiness doc skeleton] ───── (parallel) │
                                                                 │
Track C  [C1 README polish] ─────────────────────── (parallel)   │
                                                                 │
Track D                                          [D1 §2.24]      │ [D2 interview_required]
(self-building)                                       │          │ [D3 paused]
                                                      ▼          │
                                            (re-run A2 against   │
                                             post-§2.24 system)  │

Track P                                          [P1 casebook]   │ [P2 import flow]
(presentation)                                      │            │ [P3 slide assets]
                                                     ▼            │
                                        concrete reviewer demo    │
```

**Hard dependencies:**

- `A2` blocks on `A1 ≥ 20/26 labels`.
- `A3` blocks on `A2`.
- `D1 (§2.24)` blocks on `A2` (so the frozen baseline is captured before the fixer narrows `free_text` rows).
- `D3 (§3.3d)` blocks on `D1` (its scope is "rows §2.24 didn't atomize").
- `P2` blocks on `P1` only for demo polish; the underlying CLI/API already
  scores curated patients and trials.

**Soft dependencies / parallelism:**

- `B1` and `C1` can run any time after `A1` is in flight; they don't depend on it numerically.
- `D2 (interview_required)` is independent of `D1` but post-demo by sequencing choice.
- `P1` and `P3` can run while `P2` is still being implemented.

---

## 2. Per-step detail

Each step lists **functionality** (what gets built/done), **inputs**, **outputs**,
**eval triggers** (when this step re-runs an eval), and **calibration
regeneration triggers** (when the patient-evidence packet must be rebuilt).

### A1. Fill patient-evidence calibration labels (human)

- **Functionality.** Walk the 26-row calibration packet in the Svelte reviewer
  GUI; assign `label`, `expected_matcher_verdict`, `cited_source_row_ids` where
  evidence exists, `reviewer`, and a one-line `rationale`. Hit the gate at
  ≥ 20 / 26; aim for 26 / 26.
- **Inputs.** `eval/calibration/patient_evidence_candidates.json` (26 rows,
  built against frozen run `b47ada00d6a7`), patient FHIR rows surfaced in the
  GUI via `/patient-evidence/source-rows`.
- **Outputs.** `eval/calibration/patient_evidence_labels.json` (atomically
  saved by `POST /patient-evidence/calibration`).
- **Tooling.** `uv run python scripts/serve.py` + `npm --prefix web run dev` →
  open `http://localhost:5173` → **Patient evidence** mode.
- **Done-when.** A `--min-usable-labels 20` patient-evidence-report run exits 0
  (gate command below in §3).
- **No eval/calibration re-trigger** — this step *unblocks* the first patient-evidence eval, it doesn't generate one.

### A2. Cost/quality sweep against the frozen baseline (agent)

- **Functionality.** Run the patient-evidence report across at least three
  LLM-use levels — `none`, `retrieval_only`, and one `bounded_adjudication`
  model — against the frozen extractor cache and the labels from A1. Persist
  per-row pass/fail, judge disagreement, token/USD telemetry from
  `ScorePairResult.llm_calls` and the `runs.sqlite` v3 `adjudicator_*` columns.
- **Inputs.**
  - `eval/calibration/patient_evidence_labels.json` (≥ 20 attributed rows).
  - `eval/baselines/2026-05-11-compiler-rollout/` (frozen extractor cache + diagnostics).
  - Run id `b47ada00d6a7` (the frozen baseline).
- **Outputs.** New baseline directory
  `eval/baselines/2026-05-21-cost-quality/` containing one
  `patient_evidence_<mode>_diagnostics.json` per mode, a
  `patient_evidence_modes_summary.json`, and a `SUMMARY.md` written in the
  same shape as the 2026-05-11 rollout summary.
- **Eval triggers.** This *is* the eval. Frozen compiler-diagnostics gate must
  still pass *before* the sweep starts (§3 below).
- **No calibration re-trigger.**

### A3. Routing policy diff (agent)

- **Functionality.** Pick a default LLM-use level per
  `criterion_kind × matcher_assumption_mode` cell based on A2's cost/quality
  table. Express the policy as a small JSON config (`routing_policy.json`)
  and write a "before-vs-after-policy" delta into the same baseline directory.
  The policy says **when the system has enough deterministic support to flag a
  possible match and when it must abstain**.
- **Inputs.** A2 outputs.
- **Outputs.**
  - `eval/baselines/2026-05-21-cost-quality/routing_policy.json` (the policy itself).
  - `eval/baselines/2026-05-21-cost-quality/routing_policy_delta.md` (the before-vs-after writeup).
- **Eval triggers.** Re-run patient-evidence report under the chosen policy
  configuration (subset of A2 modes) and persist as
  `patient_evidence_policy_diagnostics.json`.
- **No calibration re-trigger.**

### B1. Deployment-readiness doc skeleton (agent + human)

- **Functionality.** Stand up `docs/deployment-readiness.md` with section
  stubs and the numbers already in hand: Layer-1 89.0% / 98.6%, frozen
  compiler baseline (0 unmapped, 280 typed gaps, 43 blocking cases),
  matcher-mode semantics, calibration gate ≥ 20/26. Real prose pass
  follows A2/A3.
- **Inputs.** `eval/baselines/2026-05-11-compiler-rollout/SUMMARY.md`,
  `docs/matcher-assumption-modes.md`, `docs/evaluation-layers-and-gates.md`.
- **Outputs.** `docs/deployment-readiness.md`.
- **No eval/calibration re-trigger.**

### C1. README polish (agent)

- **Functionality.** Add architecture-diagram pointer, "how to reproduce the
  frozen baseline" recipe (single `uv run pytest && uv run python
  scripts/check_compiler_diagnostics.py ...` block), and an honest
  limitations section pointing at `docs/known-limitations-and-scope.md`.
- **Inputs.** Existing `README.md`, `docs/system-architecture-walkthrough.md`,
  `docs/known-limitations-and-scope.md`.
- **Outputs.** Updated `README.md`.
- **No eval/calibration re-trigger.**

### D1. §2.24 — Deterministic mention-to-composite promotion (agent)

- **Functionality.** Extend `clinical_demo.extractor.fix.fix_extracted_criteria`
  to recognize compound shapes in `kind=free_text` criteria and decompose them
  into native `composite_groups: any_of` / `all_of` using the typed `mentions`
  the extractor already emitted.
  Recognized shapes (acceptance is **at least three** distinct patterns):
    1. **Parenthetical comma-separated lists** —
       `"Disorders... (Paget's, malignancy, Cushing's, COPD)"`
       (NCT06524960 idx 18, the worked example in §2.24).
    2. **Inline disjunctions with a temporal qualifier** —
       `"Pregnancy or actively breastfeeding within 6 months"`
       (NCT06524960 idx 20, surfaced by patient-evidence labeling row 6).
    3. **"Treatment with any of the following: A, B, C, D"** —
       (NCT06524960 idx 16; the medication-class composite row).
- **Hard constraints.**
  - Subchecks must be **1:1 with extractor mentions** — no invented concepts.
  - Each subcheck inherits the parent's `polarity`, `negated`, `mood`.
  - Unmappable mentions become typed gaps (`unmapped_concept`, `composite_unhandled`), not silent `free_text`.
  - Parent's source `criterion_source_text` is preserved verbatim; the GUI still shows the original bundle as one row.
  - **Frozen compiler-diagnostics gate must pass byte-for-byte on rows the new fixer does not touch.**
- **Inputs.** Frozen extractor cache under `data/curated/extractions/`,
  reviewed terminology registry under `data/terminology/`.
- **Outputs.**
  - Code: `src/clinical_demo/extractor/fix.py` + new fixture tests under `tests/extractor/test_fix.py`.
  - Smoke diagnostics: `eval/baselines/2026-05-21-self-build-slice1/compiled_predicates_diagnostics.json` documenting the `free_text` count drop.
  - **(Conditional) regenerated calibration packet** — see §4 below for the trigger logic.
- **Eval triggers.**
  - Re-run frozen compiler-diagnostics gate as a regression check (must pass on un-touched rows; the new typed gap counts may shift, document in the smoke summary).
  - Re-run patient-evidence report against the **same** label set to demonstrate which previously-`indeterminate` rows the slice resolved (Pregnancy row 6 is the predicted flip).
- **Calibration triggers.** **Conditional.** See §4.

### D2. `interview_required` typed gap (post-demo, small)

- **Functionality.** Extend the criterion fixer with a small curated phrase
  allow-list that rewrites criteria fundamentally outside FHIR to a new typed
  gap `interview_required`. First fixture is patient-evidence row 7 (NCT06964087
  idx 28, "participating in another clinical trial").
- **Inputs.** Existing fixer module, the reviewer-GUI typed-gap renderer.
- **Outputs.**
  - Code: `src/clinical_demo/extractor/fix.py` (allow-list) +
    `src/clinical_demo/compiler/schema.py` (gap-kind enum).
  - Reviewer GUI bucket: a small `web/src/lib/PatientEvidenceCalibration.svelte` rendering tweak.
- **Eval triggers.** Cached compiler-diagnostics regression check. If a row was
  previously over-promoted into an executable trial-exposure predicate, rerun
  the patient-evidence report against the same labels and document the rollup
  movement.
- **Calibration triggers.** None — the new gap kind preserves
  `indeterminate / human_review_required` for the affected rows so calibration
  agreement is unchanged.

### D3. §3.3d — LLM-driven bounded patch-proposal workflow

**Status: paused before implementation.** Resume only after the presentation
demo proves the current score/reviewer loop on concrete patient/trial examples.

- **Functionality.** For criteria where D1's deterministic detector did *not*
  fire and the row remains `kind=free_text` despite the extractor emitting
  multiple typed mentions, an opt-in LLM pass proposes a composite-group
  decomposition. **Strictly bounded outputs.** Either:
    - (a) `composite_groups: any_of` / `all_of` whose subchecks are a strict
      subset of the extractor's existing `mentions` (no invented concepts),
      with span citations back into the criterion source text; or
    - (b) `"leave_unresolved"` with a one-line typed rationale
      (`mixed_kinds`, `nested_clarifier`, `not_atomizable`).
  Second iteration extends to reviewed-registry surface → ConceptSet patches
  and medication/condition code-list expansions.
- **Hard invariants (D-74).**
  - **All proposals land in `data/terminology/review_inbox/` as JSON packets.**
  - Promotion path: deterministic validator (schema + 1:1-with-mentions check) → existing test suite → human approval → `git mv` into `data/terminology/reviewed_*.json`.
  - The deterministic compiler stays the trust boundary. **Nothing LLM-authored ever enters the executable registry or compiled predicates without passing through the deterministic validators and a human sign-off.**
- **Inputs.** Residual `free_text` rows from D1 outputs + reviewed-registry
  gaps surfaced by `scripts/check_compiler_diagnostics.py`.
- **Outputs.**
  - Code: a new `src/clinical_demo/self_build/` module containing the prompt, validator, and proposal-emit pipeline.
  - Proposal packets: `data/terminology/review_inbox/<timestamp>-<kind>.json`.
  - Telemetry: per-proposal `model`, `prompt_version`, `tokens`, `usd`, `validator_pass` into `runs.sqlite` (extend schema as needed; see §5).
- **Eval triggers.** No automatic eval. Each promoted packet → frozen
  compiler-diagnostics gate must pass.
- **Calibration triggers.** Regenerate the patient-evidence packet only after
  a *batch* of promoted packets lands (avoid one regen per promotion).

### P1. Presentation casebook and run packet

- **Functionality.** Choose 3-5 patient/trial pairs that show the system's
  range: a clean fail, a possible match / pass-pending-review, an
  indeterminate with retrieved evidence, and an `interview_required` abstention.
- **Inputs.** Existing curated cohort/trial manifests, `/score` UI, and the
  baseline summaries listed in `docs/presentation-demo-plan.md`.
- **Outputs.**
  - `eval/baselines/<date>-presentation-demo/` with one JSON and one readable
    Markdown summary per selected pair.
  - Screenshot checklist for the reviewer UI.
- **Eval triggers.** None. These are presentation exports, not a calibrated
  eval denominator.
- **Calibration triggers.** None.

### P2. New trial / patient import demo path

- **Functionality.** Make the "bring in a new trial/patient and match it" path
  demoable. Minimum viable version:
    1. import one NCT ID into `data/curated/trials/`,
    2. ensure/extract cached criteria for that trial,
    3. choose or append a Synthea patient to the cohort manifest,
    4. score the pair through CLI and UI.
- **Inputs.** ClinicalTrials.gov v2 via existing trial client, Synthea FHIR
  bundles, `scripts/score_pair.py`, FastAPI `/score`.
- **Outputs.** A short runbook plus one successful imported-pair output.
- **Eval triggers.** None unless the imported pair becomes part of a future
  calibration/eval seed.
- **Calibration triggers.** None.

### P3. Presentation slide assets

- **Functionality.** Convert the existing measured artifacts into deck-ready
  tables/screenshots: readiness snapshot, cost/quality routing table,
  patient-evidence denominator, D1/D2 self-building guardrail examples, and one
  live matching walkthrough.
- **Inputs.** `docs/presentation-demo-plan.md` and baseline summaries.
- **Outputs.** Image/table snippets under a presentation asset directory or
  copied directly into the deck workspace.
- **Eval triggers.** None.
- **Calibration triggers.** None.

---

## 3. Eval re-trigger table

| Trigger event                                          | Eval to run                                                                                          | Output location                                                  |
|--------------------------------------------------------|------------------------------------------------------------------------------------------------------|------------------------------------------------------------------|
| Any compiler-pipeline change                           | `scripts/check_compiler_diagnostics.py` against frozen thresholds (`PLAN.md` §0 has the command)     | stdout exit code (0 = pass)                                      |
| Any reviewed-registry change                           | Same as above                                                                                        | stdout exit code                                                 |
| Any fixer change (D1, D2)                              | Compiler diagnostics gate + targeted `pytest tests/extractor/test_fix.py tests/compiler/test_pipeline.py`             | stdout / pytest exit code                                        |
| A1 reaches ≥ 20 labels                                 | `uv run python scripts/eval.py patient-evidence --run-id b47ada00d6a7 --labels ... --min-usable-labels 20` | `eval/baselines/2026-05-21-cost-quality/patient_evidence_<mode>_diagnostics.json` |
| A2 / A3 ship                                           | Cost/quality sweep across `none`, `retrieval_only`, one `bounded_adjudication` model                 | `eval/baselines/2026-05-21-cost-quality/SUMMARY.md` + per-mode diagnostics |
| D1 ships                                               | Re-run frozen compiler-diagnostics gate **and** patient-evidence report against the *same* labels    | `eval/baselines/2026-05-21-self-build-slice1/`                  |
| D2 ships                                               | Re-run cached compiler diagnostics; rerun patient-evidence report if trial-participation rows now abstain | `eval/baselines/2026-05-26-interview-required/`                 |
| P1 presentation casebook ships                         | Run selected pairs through CLI/UI; export presentation-only JSON/Markdown                                 | `eval/baselines/<date>-presentation-demo/`                      |
| P2 imports a new trial/patient                         | Score the imported pair; do not add it to calibrated metrics by default                                   | Presentation packet / stdout                                    |
| D3 promotes a batch of patches into the executable registry | Compiler diagnostics gate **byte-for-byte regression check** against the previous snapshot     | New `eval/baselines/<date>-self-build-batch-N/` snapshot         |
| Any matcher-semantics change (no current plan)         | Layer-1 + frozen compiler-diagnostics + patient-evidence reports                                     | New baseline directory                                           |
| Extractor model change (no current plan)               | Layer-1 + Layer-2 + frozen compiler-diagnostics gate                                                 | New baseline directory                                           |

**Frozen compiler-diagnostics gate command (canonical, from `PLAN.md` §0):**

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

---

## 4. Calibration regeneration triggers

The patient-evidence calibration packet is expensive to regenerate (it
invalidates labeling work in progress). Regenerate only when the
**candidate-selection logic** would meaningfully differ, not whenever a metric
moves.

| Trigger                                                                                          | Action                                                                                                                                                                | Rationale                                                                                                  |
|--------------------------------------------------------------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------|------------------------------------------------------------------------------------------------------------|
| A1 in flight, fewer than 20/26 labeled                                                          | **Do not regenerate.** Finish labels first.                                                                                                                            | Avoid throwing away partial labeling work.                                                                  |
| D1 ships and the `free_text` count drops materially                                              | If A1 is **already complete (≥ 20)**: regenerate, run `--prune-labels` to preserve still-valid labels, archive old packet at `eval/calibration/*.20260514-pre-self-build.json`. | The decomposed rows now expose subchecks that didn't exist when labels were drawn; some labels still apply, some don't. |
| D1 ships **before** A1 reaches 20                                                                | **Do not regenerate.** Freeze D1 outputs locally; finish labeling against the current packet so the sweep has a baseline.                                              | Self-building wedge must not unseat the demo's quality story.                                              |
| D3 promotes a *batch* of patches into the executable registry                                    | Regenerate once per batch (not per patch), prune-preserve labels, archive prior packet.                                                                                | Same logic as D1; amortize the labeling cost.                                                              |
| Extractor model / version changes                                                                | Full regeneration — labels are tied to the extractor output ids.                                                                                                       | Mentions / composite_groups schema may have changed; old labels are not safely portable.                   |
| Matcher version changes (semantics, not bug fix)                                                 | Full regeneration.                                                                                                                                                     | Expected verdicts may shift; agreement metric loses meaning otherwise.                                     |
| Reviewed-registry expansion (e.g. a new medication class)                                        | **Do not regenerate.** Labels remain valid; only the matcher's verdict for those rows may flip. The patient-evidence report captures the movement directly.            | This is the *whole point* of having stable labels.                                                         |

**Regeneration command (from existing tooling):**

```bash
uv run python scripts/build_patient_evidence_calibration.py \
  --run-id <new run id> \
  --output-candidates eval/calibration/patient_evidence_candidates.json \
  --output-labels eval/calibration/patient_evidence_labels.json \
  --prune-labels \
  --preserve-labels eval/calibration/patient_evidence_labels.json
```

`--prune-labels` keeps labels for rows still present in the new candidate set
and drops rows that no longer appear. Always archive the prior packet first.

---

## 5. New calibration rows: when and why

Distinct from "regenerate the packet": **adding** calibration rows means
*expanding* the row count beyond the current 26.

| Situation                                                                                          | Add rows?                                                                                          | Where they come from                                                                              |
|----------------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------|---------------------------------------------------------------------------------------------------|
| A2 reveals a mode-disagreement bucket the current 26 don't cover (e.g. all `bounded_adjudication`-flipped rows are concentrated in one criterion kind) | Yes — add 5-10 rows targeting that bucket via `--candidate-buckets` to the calibration builder. | Pull from the same frozen run id, filter by `judge_label` or `matcher_reason` to over-sample the gap. |
| D1 produces new composite-decomposed rows that weren't in the candidate set                       | Optional — only if the *parent* row's label needs to be split into per-subcheck labels for the deck. | Build a small *supplementary* labels file rather than replacing the main packet.                  |
| Post-demo: scope expands beyond cardiometabolic (oncology, etc.)                                    | Yes — but treat as a separate calibration packet (`eval/calibration/patient_evidence_oncology_*`). | New cohort, new pairs, new run id.                                                                |
| Red-team scenarios from §3.4 (prompt injection, OOD criteria, adversarial negation)               | Yes — add a `patient_evidence_red_team_*.json` packet, kept separate from the main calibration.    | Hand-crafted; not drawn from a run.                                                              |

**Operating rule.** Keep the main 26-row packet stable through the demo.
Any expansion lives as a supplementary or sibling packet so the deck's
headline agreement number is computed against a fixed denominator.

---

## 6. Self-building telemetry schema (sketch)

For D3, the proposal pipeline needs to record cost/quality per proposal so the
self-building track has the same observability as the adjudicator. Extending
`eval/runs.sqlite` (currently at schema v3, which has
`adjudicator_cost_usd`, `adjudicator_input_tokens`, `adjudicator_output_tokens`,
`adjudicator_calls`):

```sql
-- new table, additive
CREATE TABLE IF NOT EXISTS self_build_proposals (
  id INTEGER PRIMARY KEY,
  ts_utc TEXT NOT NULL,
  model TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  proposal_kind TEXT NOT NULL,         -- 'composite_decomposition' | 'registry_patch' | 'leave_unresolved'
  source_pair_id TEXT,                 -- pair_id this proposal was generated against, if any
  source_criterion_index INTEGER,
  input_tokens INTEGER NOT NULL,
  output_tokens INTEGER NOT NULL,
  cost_usd REAL NOT NULL,
  validator_pass INTEGER NOT NULL,     -- 0/1
  validator_failure_kind TEXT,         -- nullable
  packet_path TEXT NOT NULL,           -- data/terminology/review_inbox/<...>.json
  promotion_status TEXT NOT NULL       -- 'proposed' | 'human_approved' | 'rejected' | 'merged'
);
```

This is a sketch, not a final schema. The point: telemetry lives in the same
store as the adjudicator telemetry so a single dashboard can show "what did
auto-augmentation cost us this week and how many proposals survived
validation."

---

## 7. Parallelization checklist

Things that **can** run in parallel:

- B1 (doc skeleton) and C1 (README polish) — independent of each other and of A2/A3 numerically.
- D2 (`interview_required`) — independent of D1; could ship before or after D3.
- §3.3a (TrialGPT/TREC scaffold — already landed first slice) and §3.3c (CT.gov corpus + hybrid retrieval) — both are read-only candidate/context layers; do not change the runtime path.

Things that **must** be serial:

- A1 → A2 → A3 (each gates on the previous).
- A2 → D1 (capture the frozen baseline before the fixer changes anything).
- D1 → D3 (D3's scope is *defined* relative to D1's outputs).
- D1 → packet regeneration (don't regenerate while D1 work is in flight; flush the slice first).

Things to **never** do in parallel:

- Two compiler/registry changes targeting overlapping criteria — the regression gate is sensitive to row-level diffs.
- A patient-evidence label fill pass and a candidate-packet regeneration — guaranteed to lose labeling work.

---

## 8. Demo readiness checklist

The deck story needs five concrete numbers and one decision artifact. Each
maps directly to an output of the steps above:

| Deck slide          | Number / artifact                                                          | Comes from |
|---------------------|----------------------------------------------------------------------------|------------|
| Extractor accuracy  | Layer-1 89.0% agreement / 98.6% coverage                                  | Frozen baseline (already in hand) |
| Compiler closure    | 0 / 1076 `unmapped_concept`; 280 typed gaps (all classified)              | Frozen baseline (already in hand) |
| Reviewer signal     | ≥ 20 / 26 patient-evidence agreement, broken out by criterion kind        | A1 + A2 |
| Cost / quality      | USD per pair × 3 modes (none / retrieval_only / bounded_adjudication)    | A2 |
| Routing policy win  | Composite quality at chosen policy vs `bounded_adjudication`-everywhere   | A3 |
| Self-building hook  | "We are also shipping a small slice this week that lets the fixer..."   | D1 |

**Demo is ready when:** A1, A2, A3 outputs exist in
`eval/baselines/2026-05-21-cost-quality/`; the deployment-readiness doc has
its first prose pass; D1/D2 have smoke summaries; and P1 has a concrete
casebook with at least one end-to-end patient/trial match that can be run live
or replayed from an exported result. D3 is explicitly paused.

---

## 9. What this plan deliberately does **not** include

- New compiler-closure slices (any `unsupported_predicate` / `ambiguous_mapping` reduction).
- New terminology binding passes.
- MIMIC-IV access track.
- Official TREC / TrialGPT benchmark ingestion (§3.3b).
- CT.gov corpus + hybrid retrieval (§3.3c).
- Nested composite groups (deferred to post-3.3d).
- Oncology stretch (Phase 4.2).
- Layer-3 calibration expansion beyond the existing artifacts.

All of these remain tracked in `PLAN.md`. They re-enter the executable plan
after the demo, in a new revision of this doc.
