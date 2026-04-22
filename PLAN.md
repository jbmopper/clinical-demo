# Clinical Trial Eligibility Co-Pilot — Project Plan

> **Purpose.** Build a portfolio-grade demo for a 20-minute presentation in the
> final round of a Generative AI Forward Deployed Engineer interview at KPMG's
> AI & Data Labs practice.
>
> **Companion docs.** See `description.md` for the user/workflow narrative and
> high-level architecture; this file is the working build plan, scope contract,
> and decision log. When the two disagree, this file wins.

---

## 0. Current state (updated before every commit)

> Single source of truth for "where are we." If you're a future
> session resuming from a summary, **trust this section over the
> summary** — it's git-tracked and last-touched right before the
> head commit. Detailed task history lives in §6; per-decision
> rationale lives in §12.

- **Active phase:** Phase 2 — Workflow + eval.
- **Last completed:** Task 2.9 (FastAPI backend) — commit
  `087d6a2` (`feat(api): FastAPI backend over score_pair…`).
  Built ahead of 2.5/2.6/2.7 to unblock the reviewer UI per
  user direction (bias to usable end-to-end demo over
  completing the eval layers in order). `clinical_demo.api`
  package with `/health`, `/patients`, `/trials`, `/score`
  over the existing `score_pair` / `score_pair_graph`.
- **Next:** Reviewer UI v0 (Svelte; PLAN task 2.8). The UI
  consumes `/score` for the per-criterion verdict pills + the
  trial/patient catalog endpoints for selection. After that:
  loop back to layer-2/3 evals + baseline + deploy.
- **Gates at HEAD:** `mypy` clean (99 src files); `ruff check` +
  `ruff format` clean (111 files); `pytest` 385 passing, 1
  pre-existing failure (see follow-ups).
- **Branch:** `main`, pushed to `origin`.

### Non-trivial open follow-ups

These are *not* blockers for the next task; they're tracked here
so they don't get lost between sessions.

- **Pre-existing test failure**:
  `tests/observability/test_langfuse_shim.py::test_settings_helper_detects_configuration`.
  `monkeypatch.delenv` doesn't override values that
  `pydantic-settings` has already loaded from `.env`. Fix is a
  fixture that resets the cached settings or a `pytest-env`
  config that ignores `.env` for that test. Predates Phase 2.
- **Eval seed human-review pass** (Phase 1 task 1.6): ~856
  free-text criteria across 49 pairs are still
  `free_text_review_status="pending"`. End-to-end matcher evals
  cannot be claimed as ground truth until this pass is complete.
  See the open-question list in §13.
- **Critic iteration default re-validation** (Phase 2 task 2.7):
  default `max_critic_iterations=2` was picked on theory, not
  data. Re-validate against the real revision manifest after the
  first baseline regression run; if 95%+ of revisions land in
  iteration 1, drop to 1.

### Maintenance contract for this section

When closing out a task:

1. Update **Last completed** with task id and the commit SHA(s).
2. Update **Next** with the next task id from §6.
3. Update **Gates at HEAD** with the actual numbers from a fresh
   `mypy` + `ruff check` + `ruff format --check` + `pytest -q`.
4. Add/remove **follow-ups** as they appear/resolve. Don't let
   this list grow past ~5 items; promote chronic ones into §13
   open questions or into a new task row.

---

## 1. North Star

A clinical research coordinator (CRC) loads a patient and a trial. The system
returns, for every eligibility criterion, one of `eligible | ineligible |
indeterminate`, with a citation to the source criterion text and a citation to
the supporting (or missing) patient evidence. The CRC accepts, overrides, or
flags. Aggregated verdict + a "missing data" worklist are produced.

Two entry directions, one engine:

- **Patient → Trials.** Given one patient, surface candidate trials.
- **Trial → Patients.** Given one trial, screen and rank a cohort.

The system never autonomously enrolls anyone.

---

## 2. What the JD is actually testing (and how this project answers it)

| JD signal | How this project demonstrates it |
|---|---|
| End-to-end shipping | Deployed demo on `juliusm.com`, not slides. |
| Context engineering | Trial protocols + multi-year FHIR records do not fit naively in context — explicit pre-extraction, retrieval, structured intermediate representations. |
| Evaluation discipline | Three-layer eval: deterministic (numeric criteria vs. Synthea ground truth), reference-based (extraction vs. Chia annotations), LLM-as-judge with calibration against hand-graded examples. Regression harness. Red-team set. |
| Model strategy fluency | Cost/quality sweep across 4–5 models. Documented routing policy with quantified savings vs. naive frontier-everywhere. |
| Auditable / observable | Langfuse traces from day one. Every verdict cites both criterion text and patient evidence. |
| Production discipline for enterprise | Deployment readiness doc framing PHI handling, prompt injection, model risk management (SR 11-7 / FDA GMLP / NIST AI RMF), rollout phases. |
| Coaching while building | Pod-composition section in deployment readiness doc — what 3 engineers + an account lead each own; what a junior dev's first ticket looks like. |
| Bias to action | Ship the ugly path end-to-end before polishing any one part. |

---

## 3. Domain scope

**Primary cluster: cardiometabolic.** Type 2 diabetes, hypertension,
hyperlipidemia, related CKD. Picked because Synthea models this domain richly
(longitudinal HbA1c, BP, lipids, eGFR, multiple meds, complications) *and*
trials in this space lean heavily on numeric criteria — which gives clean
deterministic ground truth for the eval.

**Stretch domain: lung cancer.** One or two trials. Picked to demonstrate
generalization to a domain with shallower Synthea data and more
categorical/temporal/biomarker criteria (TNM staging, histology, EGFR/ALK
status, prior lines of therapy). Expect to supplement with hand-crafted
patient profiles. The goal is to *show* the system's confidence appropriately
drops and routing escalates — not to claim oncology mastery.

**Explicitly out of scope:** all other Synthea modules. If asked "why not X?"
in the interview, the answer is "I prioritized depth in domains where I could
prove correctness over breadth I couldn't validate."

---

## 4. Data trinity

| Source | Role | Risks |
|---|---|---|
| **Synthea v4.0.0** (sample data, FHIR R4) | Synthetic patient records. Provides deterministic ground truth for numeric criteria. | Oncology depth is shallow; will need supplementation. |
| **ClinicalTrials.gov v2 API** | Real trial protocols (eligibility text, conditions, phase, sponsor). | Eligibility criteria are free text — extraction is the hard part. |
| **Chia corpus** (Phase IV, 1,000 trials, hand-annotated) | Golden ground truth for the criterion-extraction step (entities + relationships per the Chia schema). | Doesn't overlap perfectly with our chosen domains; use the schema everywhere, use the labels where they fit. |

**Curated working set targets:**

- ~150 Synthea patients tilted to the cardiometabolic profile, with
  multi-condition overlap (a patient with T2DM + HTN + CKD3 is realistic and
  great for stress-testing).
- ~30 trials from CT.gov (≈25 cardiometabolic, ≈5 lung cancer). Mix of
  industry Phase 2/3 and NIH-sponsored to vary criterion style.
- ~50–100 Chia-annotated trials retained as extraction golden set, filtered
  toward overlap with our domains.

---

## 5. Architecture (one paragraph)

A trial is parsed and its eligibility text is run through a **Criterion
Extractor** (cheap model, JSON-schema output following Chia entities). A
patient is parsed by a **Patient Profiler** (deterministic FHIR parsers; light
LLM only for unstructured notes). For each (patient, trial) pair, a **LangGraph
workflow** fans out per criterion: a deterministic matcher attempts the verdict
first (numeric thresholds, age, sex, active conditions); only on miss does it
escalate to an LLM matcher with the relevant patient slice as context.
Per-criterion verdicts are joined and passed to an **Aggregator + Critic** loop
(frontier model) that checks for contradictions, missed deterministic matches,
and hallucinated criteria, with a bounded number of revision iterations. The
final per-criterion + aggregate result is rendered in a **Svelte reviewer UI**
on `juliusm.com`, side-by-side with sources, with accept/override/flag
controls whose feedback is captured into the eval dataset. Every step is traced
in **Langfuse**.

Architecture diagram (Mermaid + ASCII) lives in `description.md`.

---

## 6. Build plan with hour estimates

Estimates assume focused work, alone, with normal blockers. Total budget is
~80–120 hours across three phases plus a polish/buffer phase. If I'm running
hot or slow, the *scope* gives, not the deadline — see §9.

### Phase 1 — Data + skeleton (target: end-to-end ugly path running)

| # | Task | Est. (hr) |
|---|---|---|
| 1.1 | Project scaffolding: Python 3.12, `uv`, repo layout, ruff/black, pre-commit, `.env.example`, README stub, dependency pinning. | 2 |
| 1.2 | Pull Synthea sample data; write loader that yields parsed Patient objects (demographics, conditions, observations, medications) from per-patient FHIR bundles. *Done.* | 4 |
| 1.3 | Curate the working patient cohort (~150) by querying the loader for cardiometabolic profiles; persist a manifest. *Done.* | 2 |
| 1.4 | Pull ~30 trials from CT.gov v2 API; persist raw JSON + a normalized trial record. *Done.* | 3 |
| 1.5 | Pull Chia corpus, parse the BRAT annotations, build a Pydantic representation of the Chia schema (entities + relations). *Done.* | 4 |
| 1.6 | Hand-pick ~30 trials and ~50 (patient, trial) pairs as the **eval seed set**. Hand-label expected per-criterion verdicts for the pairs (this is the most boring, most important task in the whole project — block out a real afternoon). *Skeleton + mechanical pass done; free-text human pass owed (~856 criteria across 49 pairs).* | 6 |
| 1.7 | Patient Profiler v0: deterministic FHIR → typed Python objects with `as_of_date` slicing. *Done — `PatientProfile` wrapper, 5-state threshold primitives (meets/does_not_meet/no_data/stale_data/unit_mismatch), curated SNOMED+LOINC ConceptSets, eval seed labelers refactored to use the profile.* | 4 |
| 1.8 | Criterion Extractor v0: single model, single prompt, JSON-schema output mirroring the Chia entity types. No retries, no router. *Done — OpenAI structured outputs (`gpt-4o-mini-2024-07-18` default), matcher-ready discriminated schema, 2 few-shot examples drawn from real eligibility text, prompt versioned at `extractor-v0.1`, smoke-script `extract_criteria.py`, 34 unit tests with stub client.* | 4 |
| 1.9 | Deterministic matcher v0: covers numeric criteria, age, sex, active condition presence/absence. Returns `pass | fail | indeterminate`. *Done — `MatchVerdict` with typed `Evidence` rows, 8-kind dispatcher (age, sex, condition_present/absent, medication_present/absent, measurement_threshold, temporal_window, free_text), polarity/negation XOR truth-table, surface-form → ConceptSet lookup table for cardiometabolic conditions and labs, 79 unit tests (per-kind pass/fail/indeterminate + integration), matcher pinned at `matcher-v0.1`.* | 4 |
| 1.10 | Glue script: `score_pair(patient, trial) -> List[CriterionVerdict]`, runs from the CLI. *Done — `clinical_demo.scoring.score_pair()` library entry returns a `ScorePairResult` (verdicts + extraction + summary + conservative `eligibility` rollup), `scripts/score_pair.py` CLI with `--no-llm` replay mode, `--force-extract`, `--json`, on-disk extraction cache shared with `extract_criteria.py`, 11 unit tests pinning the rollup truth table and the cache round-trip.* | 2 |
| 1.11 | Wire Langfuse from day one — every LLM call traced; project name `clinical-demo`. *Done — `clinical_demo.observability` shim wraps Langfuse v4 (`@observe`-style `traced(...)` context manager that no-ops when keys are absent and is defensive on every call), `extract_criteria` emits one `generation` per call (model + prompt_version + input/output + tokens + cost + latency, refusals tagged `WARNING`), `score_pair` opens a parent `span` per (patient, trial) pair tagged with `patient_id`/`nct_id`/`eligibility`/verdict counts so the extractor's generation nests under it; CLI scripts `flush()` at exit; 15 unit tests pin the no-op + recording-client contracts.* | 2 |
| **Phase 1 total** | | **~37 hr** |
| **Exit criterion** | One CLI command takes one patient + one trial and prints per-criterion verdicts with citations. Ugly is fine. | |

### Phase 2 — Workflow + eval

| # | Task | Est. (hr) |
|---|---|---|
| 2.1 | LangGraph migration: per-criterion fan-out, deterministic-first conditional routing, LLM matcher node, join. *Done — `clinical_demo.graph` package: `ScoringState` TypedDict with an `operator.add` reducer over `(criterion_index, MatchVerdict)` tuples; nodes for `extract`, deterministic match (thin wrapper over `match_criterion`), LLM match (new — strict structured-output OpenAI call gated on `kind == "free_text"`, with stub-friendly Protocol client), and `rollup` (sort indices, reuse imperative `_summarize`/`_rollup`); routing via `fan_out_criteria` returning `Send` objects (or rollup name when zero criteria); `score_pair_graph()` mirrors `score_pair()` with the same `ScorePairResult` envelope; opens a parent `score_pair_graph` span tagged `orchestrator=langgraph` so extractor + per-criterion `llm_match` generations nest under it. Side-by-side mirror script `scripts/score_pair_graph.py`. 35 new tests pin state, routing, both matcher nodes, end-to-end, and span structure (299 total passing). Decisions D-45..D-49.* | 5 |
| 2.2 | Aggregator + Critic loop: bounded revision iterations, termination conditions, human-checkpoint hook. *Done — `clinical_demo.graph` package gains `critic_node`, `revise_node`, `finalize_node` and a `route_after_critic` conditional edge wired as `rollup → critic → [revise → rollup` \| `finalize]`. The critic is a separate LLM call with its own pinned prompt (`LLM_CRITIC_VERSION = "llm-critic-v0.1"`) that emits closed-enum **process** findings (`polarity_smell`, `extraction_disagreement_with_text`, `low_confidence_indeterminate`) with `info` \| `warning` \| `blocker` severities; it never re-decides eligibility itself. Revise picks the highest-severity warning, dispatches to a closed-enum action (`rerun_match_with_focus`, `flip_polarity_and_rematch`, `rerun_extract_for_criterion`), and re-runs the existing matcher path so revisions stay auditable. Loop terminates on (a) no actionable findings, (b) `max_critic_iterations` budget (default 2), (c) no-progress detection comparing the current iteration's finding fingerprints to the previous; LangGraph's `recursion_limit` is a runtime config backstop. New `merge_indexed_verdicts` reducer gives `indexed_verdicts` replace-by-index semantics so revised verdicts supersede rather than coexist. Human checkpoint is opt-in (`human_checkpoint=True`): graph compiles with `InMemorySaver(serde=JsonPlusSerializer(pickle_fallback=True))` and `interrupt_before=[FINALIZE_NODE]`, requires a `thread_id`, and resumes via the same `score_pair_graph()` entry. Observability tags critic/revise/finalize spans with iteration + action + criterion-index + verdict-changed metadata, plus per-pair `critic_iterations` / `revisions_total` / `revisions_changed_verdict` on the parent. 32 new tests across `test_critic_node.py`, `test_revise_node.py`, `test_route_after_critic.py`, `test_critic_loop_e2e.py`, `test_human_checkpoint.py` cover defensive index filtering, refusal handling, fingerprint snapshots, action dispatch (free-text vs deterministic, polarity flip, no-op recording), termination conditions, e2e parity when the critic is disabled, and HITL pause/resume — plus expansions to `test_state.py` (new reducer + state keys) and `test_observability.py` (new spans). 340 total passing. Decisions D-50..D-58.* | 4 |
| 2.3 | Eval harness scaffolding: dataset format, runner, results store, basic CLI (`eval run`, `eval report`). *Done — new `clinical_demo.evals` package adds `EvalCase` / `CaseRecord` / `RunResult` pydantic envelopes, `load_dataset()` reusing the existing `eval_seed.json` shape, and a one-call `run_eval(scorer, cases)` that's deliberately orchestrator-agnostic (the scorer is a `Callable[[EvalCase], ScorePairResult]`, so `score_pair()`, `score_pair_graph()`, and any future variants are all "just a scorer"). SQLite store (`evals.store`) is two append-only tables — `runs` plus `cases` carrying flat per-case summary cols **and** the full `ScorePairResult` as a `result_json` blob (D-60); a normalized verdicts table is deferred until a layer query motivates it. Per-case scorer exceptions are caught and recorded on the row instead of failing the run (D-62). New `scripts/eval.py` exposes `run` (with `--orchestrator`, `--no-llm`, `--critic-enabled`, `--pair-id`, `--limit`, `--notes`) and `report` (id-or-list, `--format text\|json`); `eval/runs.sqlite` is gitignored. 20 new tests across `test_run.py` (dataset round-trip, filtering, runner success + failure isolation + callback ordering) and `test_store.py` (idempotent schema + `user_version`, save/load round-trip including `extraction_meta`, append-only enforcement, failed-case persistence, listing newest-first). 360 total passing. Decisions D-59..D-63.* | 4 |
| 2.4 | Layer 1 eval — deterministic: per-criterion accuracy on numeric/structured criteria. *Done — `evals/layer_one.py` aligns seed `CriterionVerdict`s against matcher `MatchVerdict`s per field (`min_age`, `max_age`, `sex`; `healthy_volunteers` documented uncoverable in v0), produces `LayerOneCell`s with `agree`/`disagree`/`missing` status, and rolls up per-field + overall agreement (excludes missing) and coverage (includes missing). `evals/report_layer_one.py` is a one-screen text renderer; `scripts/eval.py report --layer 1` dispatches to it (`--format json` also supported). 13 new tests. 373 total passing.* | 2 |
| 2.5 | Layer 2 eval — reference-based: criterion extraction F1 vs. Chia annotations. | 4 |
| 2.6 | Layer 3 eval — LLM-as-judge: rubric, prompt, calibration against ~30–50 hand-graded examples; report inter-rater agreement. | 6 |
| 2.7 | First baseline regression run; commit numbers to repo as `eval/baselines/`. | 2 |
| 2.8 | Svelte reviewer UI v0: side-by-side trial criteria + patient evidence; per-criterion verdict pills; click-to-source. | 8 |
| 2.9 | Backend: minimal FastAPI endpoint that the Svelte UI calls; CORS; deploy plan for `juliusm.com`. *Done — `clinical_demo.api` package: `create_app()` factory exposing `GET /health`, `GET /patients`, `GET /trials`, `POST /score`. `/score` accepts `patient_id`, `nct_id`, optional `as_of` (defaults to today), `orchestrator` (`imperative` or `graph`), `critic_enabled`, `use_cached_extraction`, returns the existing `ScorePairResult` envelope verbatim. Loader helpers promoted out of `scripts/` into `api/loaders.py` (third caller threshold) with process-scope caches and a `CuratedDataMissing` exception for clean 503 mapping. Wide-open CORS for the v0 demo (lock down before public deploy). `scripts/serve.py` boots uvicorn. 12 new TestClient tests pin /health, listing endpoints, scoring round-trip, error mapping (404 unknown patient/trial, 503 missing curated data, 500 scorer raises, 422 missing field), and the orchestrator switch. Built ahead of 2.4-2.7 per user direction to bias toward end-to-end usability. 385 total passing.* | 3 |
| **Phase 2 total** | | **~38 hr** |
| **Exit criterion** | Full pipeline runs through LangGraph; baseline eval numbers committed; UI shows real results from real data. | |

### Phase 3 — Cost optimization, red-team, polish, writeup

| # | Task | Est. (hr) |
|---|---|---|
| 3.1 | Model abstraction layer that lets the same node call any of 4–5 models with consistent JSON-schema enforcement. | 3 |
| 3.2 | Cost/quality sweep: same 50–100 pairs, every model at every node, log cost + composite quality score. | 4 |
| 3.3 | Define and implement the routing policy; re-run eval; produce the "money slide" dashboard (cost vs. quality, before/after policy). | 4 |
| 3.4 | Red-team set: prompt injection in patient narrative fields, adversarial negation, unit confusion, temporal traps, OOD criteria. ~15–20 cases. | 4 |
| 3.5 | Run red-team set; document failures; implement at least the cheap mitigations (input sanitization, structured-output enforcement, suspicious-pattern detection). | 4 |
| 3.6 | Reviewer UI v1: accept/override/flag with feedback persistence; basic auth gate (single-user is fine); polish. | 4 |
| 3.7 | Deploy to `juliusm.com`; smoke test; capture a screen-recording fallback in case live demo dies. | 3 |
| 3.8 | **Deployment readiness doc** — see §7. Includes a real revision pass. | 11 |
| 3.9 | 20-minute presentation deck — see §8. | 4 |
| 3.10 | Project README and repo polish (architecture diagram, eval results table, "how to reproduce", honest limitations section). | 3 |
| **Phase 3 total** | | **~44 hr** |
| **Exit criterion** | Deployed demo, dashboard, writeup, deck. The whole story can be told in 20 minutes. | |

### Phase 4 — Buffer / dogfood

| # | Task | Est. (hr) |
|---|---|---|
| 4.1 | Run the demo cold five times; fix what breaks. | 3 |
| 4.2 | Stretch oncology: add 1–2 hand-crafted lung cancer patient profiles + 1 oncology trial; show generalization slide. | 6 |
| 4.3 | Anything that overflowed earlier phases. | flex |
| **Phase 4 total** | | **~10–15 hr** |

**Grand total target: ~130 hours**, with hard scope cuts available (§9).

---

## 7. Deployment readiness doc — outline

A 6–10 page Markdown doc in the repo, written for a KPMG partner who is
technically literate but not an AI engineer. This is the differentiator. Sections:

1. **Problem & persona.** Who the CRC is, what their day looks like, what
   "good" means in business terms (eligible-patients-not-missed, time per
   screening, enrollment-deadline misses avoided).
2. **System overview.** One paragraph + the architecture diagram. No more.
3. **What it does and does not do.** Especially the "does not."
4. **Eval methodology + current numbers.** Including known weaknesses and the
   threshold below which I would not deploy.
5. **Cost analysis + routing policy.** Actual dollars per (patient, trial) at
   target quality. Naive baseline vs. routed.
6. **Risk register.** Hallucination, PHI exposure, prompt injection, model
   drift, demographic bias, regulatory (FDA SaMD-adjacent under 21 CFR 820.30),
   over-reliance / automation bias on the CRC's part.
7. **Model risk management framing.** Reference SR 11-7 (since financial
   services parallels carry weight at KPMG), FDA's Good Machine Learning
   Practice principles, NIST AI RMF. One paragraph each — *this is where the
   BA brain shines*.
8. **Rollout plan.** Pilot → expansion → scale. Concrete gates.
9. **Pod composition.** What 3 engineers + an account lead each own; first
   ticket for a junior; "coaching while building" example.
10. **Open questions for the client.** Real ones. This signals seniority.

---

## 8. 20-minute presentation arc

Brutal time budget. Practice with a stopwatch. Suggested split:

| Minutes | Beat | Slide(s) |
|---|---|---|
| 0:00–2:00 | Problem framing + who the user is. Why trial enrollment matters. | 1–2 |
| 2:00–6:00 | Live demo of the full workflow on one patient + trial. | UI, no slides |
| 6:00–8:00 | Architecture: one diagram, why LangGraph, why this split. | 1 |
| 8:00–14:00 | **The spike: eval methodology + cost-quality dashboard.** Show the routing policy and the savings. This is the money portion. | 3–4 |
| 14:00–17:00 | Deployment readiness highlights: risk register, MRM framing, rollout. | 1–2 |
| 17:00–20:00 | Honest limitations + open questions + what I'd build next with the pod. | 1 |

Q&A is separate. Stop at 20:00 even if mid-sentence — that *is* the demo of
production discipline.

---

## 9. Scope cuts (in priority order if time runs out)

Cut from the bottom up:

1. Drop the oncology stretch domain entirely — present only cardiometabolic.
2. Drop reviewer UI accept/override; show read-only verdicts.
3. Drop one of the 4–5 models from the cost sweep (keep at least 3 spanning a real price range).
4. Drop the FastAPI deployment to `juliusm.com`; demo locally with a screen recording as backup.
5. ~~Drop the Critic loop; show a single-pass aggregator.~~
   *(N/A as of Phase 2.2 — the critic loop is built and gated by
   `critic_enabled=False`. If we need to "cut" it for the demo,
   we just don't pass the flag; no work to remove.)*
6. Drop the LangGraph migration; keep an async Python orchestrator. (Acknowledge this in the deck — explain *why* you didn't migrate, which is itself a senior-engineer answer.)

**Do not cut**, ever:

- The eval harness with at least one full layer working.
- The deployment readiness doc.
- The cost sweep across at least 3 models.
- A working live demo of a single (patient, trial) pair.
- Langfuse traces.

---

## 10. Stack

| Concern | Choice | Why |
|---|---|---|
| Language (backend) | Python 3.12 | LLM ecosystem is Python-native. |
| Package mgmt | `uv` | Fast, modern, lockfile. |
| LLM orchestration | LangGraph | Conditional routing, fan-out/join, critique loop, human checkpoint — real uses, not resume-padding. Acknowledge trade-offs in the deck. |
| Observability | Langfuse | Already familiar; matches "auditable" requirement. |
| Validation | Pydantic v2 | Structured outputs, schema enforcement. |
| FHIR parsing | `fhir.resources` (Pydantic-based) | Avoid hand-rolling. |
| HTTP / API | FastAPI | Minimal surface; pairs with Pydantic. |
| Frontend | Svelte (existing `juliusm.com` Astro setup) | Reuse personal-site infra; integration is itself a portfolio signal. Fall back to Streamlit if Svelte becomes a slog. |
| Eval | Custom harness, deliberately. Reference Inspect / OpenAI Evals / Promptfoo in the writeup but build something *we* control. | Eval design is the spike; using a black-box tool would undercut the demo. |
| Models | A spanning set across price tiers (e.g., a frontier, a mid-tier, a cheap) from at least two providers. Final choice locked in Phase 3 based on what's current. | Avoid single-provider lock-in narrative. |

---

## 11. Success criteria

How I'll know the project succeeded *before* the interview happens:

- A naive observer can use the deployed UI to evaluate a (patient, trial) pair
  in under 60 seconds and understand what the verdict means.
- The eval harness produces a single command that prints a results table
  comparable across runs, committed to the repo.
- The cost-quality dashboard shows a routing policy that beats
  "frontier-everywhere" on cost by ≥3× at ≥95% of the quality.
- The deployment readiness doc would survive being forwarded to a KPMG
  partner without requiring me in the room.
- The 20-minute deck has been delivered out loud, twice, to a friendly human,
  and finished within time.

---

## 12. Decision log

Captured so any choice can be defended in the interview without mid-flight
rationalization. Each entry: *what was decided, what was rejected, why.*

### D-1. Domain: cardiometabolic primary, lung cancer stretch
**Rejected:** credit memos (banking is "meh", domain authenticity risk),
KYC (even more synthetic-document-heavy and low downstream complexity), FDA
drug labels (too summarization-shaped, weak workflow), prior auth (highest
real-world value but most domain-folklore-dependent).
**Why:** clinical trial eligibility has the cleanest data trinity (Synthea +
CT.gov + Chia) and the workflow has real branching, real decisions, and a
real human-in-the-loop. Cardiometabolic specifically because Synthea is
strongest there *and* the criteria are most numeric — which gives clean
deterministic ground truth for the eval. Lung cancer added as a generalization
probe, not as a domain claim.

### D-2. Project shape: workflow assistant, not chatbot, not pure eval harness
**Rejected:** chatbot integrations (KPMG explicitly past this), pure
eval/cost-optimization framework as a product (lacks tangible application),
audit workpaper assistant (data authenticity risk; KPMG-on-the-nose to the
point of looking hand-tailored).
**Why:** the JD's repeated emphasis on shipped systems with real workflows
and human checkpoints. A workflow assistant lets us demonstrate *and* talk
about eval/cost discipline as the technical spike, without making it
abstract.

### D-3. LangGraph as the orchestration layer
**Rejected:** plain async Python orchestration.
**Why:** the workflow genuinely needs conditional routing (deterministic →
LLM escalation), fan-out/join (per-criterion parallelism), a bounded
critique loop, and a human checkpoint. These are LangGraph's actual value
prop, not a contrived use. Will explicitly acknowledge in the deck that for
strictly linear pipelines a function-of-functions is fine — *that* is the
senior-engineer signal, not blind framework enthusiasm.

### D-4. Synthetic data, no real PHI, but write the policy as if real
**Rejected:** trying to use real de-identified data (MIMIC requires
credentialing, slow, and unnecessary for a demo).
**Why:** Synthea is realistic enough to demo and removes any conceivable
data-handling risk. The PHI/security writeup is *more* valuable than real
data because it forces the same engineering discipline.

### D-5. Frontend on `juliusm.com` (Svelte/Astro), with Streamlit fallback
**Rejected:** Streamlit-only, Next.js/React rebuild.
**Why:** integrating into an existing portfolio site is itself a signal of
full-stack chops and is a venue the interviewer can revisit. Streamlit is
the documented escape hatch if Svelte becomes a time sink — that is an
engineering-judgment call to be made at the Phase 2 UI checkpoint, not now.

### D-6. Build the eval harness ourselves, do not adopt Inspect/OpenAI
Evals/Promptfoo wholesale
**Rejected:** off-the-shelf eval frameworks.
**Why:** eval design is *the* technical spike of this project. Using a
black-box framework would undercut the demonstration. Will reference the
ecosystem in the writeup to show awareness; will adopt patterns (golden
sets, regression suites, judge calibration) without ceding architectural
control.

### D-7. Cost optimization is a first-class deliverable, not a footnote
**Rejected:** treating cost as something to mention briefly.
**Why:** named explicitly by Julius as a strength and as something that
flows naturally from being able to eval. Most AI engineers stop at "does it
work." The routing-policy slide is the centerpiece of the technical
portion of the presentation.

### D-8. Hand-labeling the eval seed set is the most important boring task
**Rejected:** synthesizing labels with an LLM, or skipping ground truth and
relying on LLM-as-judge alone.
**Why:** without honest hand-labels, every downstream eval number is
self-referential and a senior interviewer will see through it immediately.
The afternoon spent labeling 50 pairs is what makes everything else credible.

### D-10. Synthea sample data: per-patient bundles, not bulk ndjson
**Rejected:** bulk-FHIR ndjson (one file per resource type).
**Why:** the upstream `synthea-sample-data` artifact ships per-patient
`Bundle` JSON files (latest dated Nov 2021 — Synthea the generator has
v4.0.0 from Mar 2026 but the sample-data artifact lags). Per-patient
bundles map 1:1 to our `Patient` domain object and the streaming benefit
of ndjson is irrelevant at the 555-patient scale. The PLAN.md task
description is updated accordingly.

### D-11. Encounters/Procedures/Allergies/Immunizations excluded from v0
**Rejected:** parsing every FHIR resource type Synthea emits.
**Why:** none of the cardiometabolic eligibility criteria we expect to
encounter in Phase 1 require these. Add when an actual criterion needs
them rather than building speculatively. Each new resource type costs
a parser, tests, and a domain-model decision.

### D-12. `is_clinical` flag on Condition is necessary-but-not-sufficient
**Rejected:** stronger upstream filtering at load time.
**Why:** Synthea categorizes most social findings (e.g.,
"Full-time employment", "Stress") as `encounter-diagnosis`, not
`social-history`. Filtering them out reliably needs either a curated
clinical-codes allowlist, a SNOMED-hierarchy walk, or matcher-side
reasoning. That decision belongs to task 1.3 (cohort curation) and the
matcher (task 1.9). The loader does the cheap-and-correct half and
hands off the hard half to a layer that can make a domain-informed call.

### D-13. Trial curation: sliced search over CT.gov, no hand-cherry-picking
**Rejected:** hand-curating each of the 30 trials, or one big query with
thousands of results then sampling.
**Why:** the curation script (`scripts/curate_trials.py`) splits the
target ~30 trials into seven labeled slices (T2DM industry/academic,
hypertension industry/academic, hyperlipidemia, CKD, NSCLC), each issuing
its own filtered CT.gov query and taking the first N hits with
eligibility text ≥200 chars. This trades a bit of curation noise for
reproducibility and an honest cross-section of what real CT.gov queries
return. The resulting noise (e.g., "ocular hypertension" matching the
hypertension query, a portal-hypertension chemo trial in the academic
hypertension slice) is *kept on purpose* — handling off-target trials
gracefully is part of what the extractor and matcher must demonstrate.
If demo polish demands it, we can hand-substitute a few trials in
Phase 3 and document that move.

### D-14. Trial domain model: keep CT.gov's structured fields verbatim
**Rejected:** parsing `minimum_age` / `maximum_age` strings into ints,
collapsing `phase` to a single value, looking up sponsor-class codes
into long names.
**Why:** CT.gov uses out-of-band conventions ("18 Years", "N/A",
"PHASE2" with optional second value, sponsor-class enums whose meaning
shifts) that we don't want to silently lose or normalize away. The
domain model holds them as the source provides them; downstream
consumers (matcher, UI) parse on demand with the right amount of
domain context. This is the same "convert once at the boundary, only
the fields you'll use" rule that the patient model follows.

### D-15. Cohort curation by weighted score, not random sample
**Rejected:** random sample of cardiometabolic patients; hand-curation
of all 150.
**Why:** the eligible Synthea pool is 267 patients with at least one
cardiometabolic SNOMED Condition active in 2025. A random 150 would be
dominated by prediabetes-only patients (Synthea's most common
cardiometabolic finding), giving boring "indeterminate" verdicts on
T2DM trials. Instead, score = `2 * core_count + prediabetes_count`
where the core set is T2DM, essential HTN, hypertensive disorder,
hyperlipidemia, and pure hypercholesterolemia. The 2x weight pulls
multi-condition patients to the top while still admitting
prediabetes-only patients as long-tail near-miss cases. CKD is
*excluded* from the cohort filter because Synthea emits ~12 CKD
patients across all 555 bundles — too sparse to slice meaningfully.
The curation date (`as_of`) is hard-coded in the script (2025-01-01)
and persisted in the manifest so the cohort is reproducible without
depending on the system clock. Final cohort: 150 patients, 74% with
≥2 cardiometabolic conditions, 100% SBP / 93% LDL / 50% HbA1c / 50%
eGFR availability.

### D-16. BP-panel components fixed in loader as part of cohort work
**Rejected:** deferring the loader bug to task 1.7 (Patient Profiler).
**Why:** while profiling Synthea for cohort curation we discovered the
loader was silently dropping every blood pressure measurement: Synthea
encodes BP as a panel under LOINC 85354-9 with no top-level value, and
the loader only handled top-level `valueQuantity`. Without the fix, 0
of 555 patients had a systolic BP and the cohort manifest would have
shown that fake limitation as a real one. Fix is a small generalization
(one `_parse_observation` returns a list; expands `component[]` when
the wrapper has no value) and adds two tests. Loader docstring updated
accordingly. The bug was real, hidden, and exactly the kind of thing
the cohort sanity-check exists to surface.

### D-17. Chia loader keeps the entity/relation type vocabulary open
**Rejected:** typing entity / relation labels as a closed enum drawn
from the published BRAT `annotation.conf`.
**Why:** scanning the actual corpus before designing the model
revealed that reality differs from the documentation in two
directions. The schema config lists 24 entity types, but the corpus
uses 31 — including process-of-annotation markers (`Parsing_Error`,
`Grammar_Error`, `Context_Error`), judgement annotations
(`Subjective_judgement`, `Undefined_semantics`, `Not_a_criteria`),
and one apparent typo (`c-Requires_causality`). Relations are even
worse: 5 documented, 14 in use (`Has_value`, `Has_temporal`,
`Has_qualifier`, `Subsumes`, `Has_index`, etc.). A closed enum would
force one of two bad choices: drop ~3% of annotations, or churn the
enum every time a new corpus is added. Instead we keep types as
plain strings, expose two `frozenset` constants
(`DOCUMENTED_ENTITY_TYPES`, `DOCUMENTED_RELATION_TYPES`) so consumers
can validate explicitly when they want to, and let downstream code
(extractor / matcher) decide what to use, ignore, or normalize. The
truth is that this corpus is messier than its spec — the model
should reflect that.

### D-18. Discontinuous spans and n-ary equivalence groups are first-class
**Rejected:** flattening discontinuous-span entities to their
bounding range, and splaying n-ary `OR` groups into pairwise binary
relations.
**Why:** 1,822 of the 48,870 entities (~3.7%) have discontinuous
spans — usually clinically meaningful pulls like
"major impairment of [renal] function" + "[hepatic]" from a single
phrase. Collapsing them to a bounding range loses the distinction
between "renal function" and "hepatic function" (the conjoined
words live in different parts of the surface). N-ary OR groups in
the corpus go up to 25 members; the cardinality matters semantically
(a 25-way OR is a clinically broad permission, not an arbitrary
nesting of binary ORs). So `ChiaEntity.spans` is a list, and
`ChiaEquivalenceGroup.member_ids` is a list — both faithful to the
BRAT structure. Cost: callers that just want a single (start, end)
get a `.start` / `.end` convenience pair on the entity.

### D-19. Eval seed splits "mechanical" from "human-review" verdicts
**Rejected:** producing a single flat list of (patient, criterion,
verdict) triples without distinguishing how each label was derived.
**Why:** the structured fields a trial gives us (minimum_age,
maximum_age, sex, healthy_volunteers) are deterministic to verdict
against the typed patient model — those labels are defensibly
correct on day one. Free-text criteria (clinical-judgement
language, prior-therapy exclusions, hard thresholds in narrative
text) need a human reviewer; pretending we labeled them honestly
would seed-train the matcher to match my mistakes. The schema
encodes the split as a `method` field on every `CriterionVerdict`
(`"mechanical"` vs `"human_review"`), plus a per-pair
`free_text_criteria_count` and `free_text_review_status`. Eval
consumers wanting strict ground truth filter to `human_review`;
consumers measuring structured-field handling include both. The
deployment writeup will state the split explicitly:
"49 pairs, 82 mechanical structured-field verdicts (66 pass / 16
fail / 0 indeterminate), 856 free-text criteria pending CRC review
before the matcher can be evaluated end-to-end."

### D-20. Slice-aware patient ranking for pair selection
**Rejected:** uniformly sampling (patient, trial) pairs at random.
**Why:** uniform sampling produces mostly low-information pairs —
e.g., a patient with no diabetes paired with a T2DM trial yields
"`fail` because no Type 2 diabetes" for nearly every criterion,
which doesn't exercise the matcher's harder paths. Instead we rank
the cohort per slice by `(slice-topical, has-required-lab,
cohort-score, age)` so each slice gets pairs likely to *test*
something: a T2DM trial paired with a high-score diabetic patient
who has HbA1c on file actually exercises threshold matching, lab
freshness, and condition-evidence reasoning. The NSCLC slice is an
intentional exception — the cardiometabolic cohort has no NSCLC
patients, so all pairs there test the matcher's "fail gracefully on
out-of-domain trials" path.

### D-22. Patient Profiler is a wrapper, not a materialized snapshot
**Rejected:** materializing each query result into a frozen
`PatientProfileSnapshot` Pydantic model.
**Why:** the underlying `Patient` is already immutable Pydantic and
the lookups are cheap (one filter scan, occasionally a max). A
materialized snapshot would add a copy step at construction, raise
the question of "which view is canonical when the patient updates",
and serialize the answers we don't need. The wrapper is a thin
view: `PatientProfile(patient, as_of)` with the as-of date baked in,
so all queries share consistent semantics without re-passing the
date everywhere. The matcher, the seed labeler, and any future
component that needs as-of semantics use the same surface.

### D-23. Threshold checks are a 5-state tri-state, not a boolean
**Rejected:** `meets_threshold(...) -> bool` (or `bool | None`).
**Why:** the matcher's verdict is itself tri-state
(pass / fail / indeterminate), and the cause of indeterminacy
matters for downstream eval and human review. The profile returns
`ThresholdResult.{MEETS, DOES_NOT_MEET, NO_DATA, STALE_DATA,
UNIT_MISMATCH}`. The matcher maps the last three to `indeterminate`
*with a reason*, so a reviewer can tell "we don't have this lab"
from "we have an old one" from "we can't compare units" — three
very different actions: order the lab, refresh the data, or
normalize the protocol.

### D-24. Unit handling fails closed when units aren't in the alias table
**Rejected:** silently coercing all unit strings to a single
canonical numeric value via a generic UCUM library.
**Why:** for the few labs we care about at v0 (HbA1c, LDL, eGFR,
BP), the patient-side units are well-known and a tiny per-LOINC
alias table covers them. UCUM-style auto-conversion adds a real
risk of nonsense conversions ("HbA1c 53 mmol/mol" → "53 %"
silently if the conversion isn't actually implemented for that
quantity), and the failure mode is *correctness*, not
*availability*. The profile returns `UNIT_MISMATCH` instead, the
matcher emits `indeterminate (unit_mismatch)`, and a human (or a
later version with an explicit conversion table) can resolve.

### D-25. ConceptSet carries the coding system URI
**Rejected:** ConceptSet as just a `frozenset[str]` of code values.
**Why:** clinical codes are unique only within their coding system.
A SNOMED 73211000 is "Neoplasm of bone of upper limb"; an ICD-10
73211000 doesn't exist; an HCC 73211000 means something else again.
A bare set of code strings invites silent cross-system matches.
ConceptSet pairs `codes` with `system` and the profile primitives
filter by *both* when given a ConceptSet (raw-string callers opt
out of the system check, useful for ad-hoc tests). This costs one
field on the model and saves one entire class of silent-correctness
bugs.

### D-21. Cap each patient at N appearances across the seed manifest
**Rejected:** letting the slice-rank winner dominate every slice
(7 slices × top-1 ranked patient = the same person in 7 of 49 pairs).
**Why:** the highest-scoring cohort patient happens to satisfy every
slice's "topical" filter (they have all four cardiometabolic
conditions and all four labs). Without a cap, the seed set's
49 pairs would be drawn from ~7 distinct patients — useless for
exercising the matcher's behavior across diverse profiles.
`MAX_PAIRS_PER_PATIENT=2` produces 27 distinct patients × 30
distinct trials, giving the matcher real coverage on both axes.

### D-26. Extractor schema is matcher-shaped, not Chia-shaped
**Rejected:** mirror Chia's full annotation graph (entities + binary
relations + n-ary equivalence groups + scopes) into the extractor
output.
**Why:** Chia is a research-grade representation aimed at *humans*
reading annotation files. The matcher just needs "what kind of
criterion is this and which slots does it bind?" — not the full
relational graph. Forcing the LLM to produce the Chia graph would
(a) explode the prompt and output token cost for a benefit only the
eval consumes, and (b) push hard reasoning (resolving relations into
matcher-actionable claims) onto the model rather than into
deterministic post-processing. So the schema is a discriminated
`kind`+payload (age / sex / condition_present / condition_absent /
medication_present / medication_absent / measurement_threshold /
temporal_window / free_text). The Chia entity vocabulary is preserved
as a flat `mentions` list per criterion, audit-only — never read by
the matcher. This keeps the extractor cheap and the matcher's
dispatch table boringly explicit.

### D-27. `free_text` as a first-class extractor output
**Rejected:** silently dropping criteria the LLM can't structure.
**Why:** "I don't know how to structure this" is itself a
load-bearing signal — both for the eval (what fraction of real
eligibility text resists structured extraction?) and for the
operator UI (these are the rows a human reviewer must adjudicate).
Carrying a `free_text` row through the same envelope means the
matcher emits a `human_review_pending` verdict for it instead of the
trial appearing more checkable than it is. This pairs cleanly with
the eval seed set's existing mechanical / human-review-pending
split (D-19) and the same accounting flows end-to-end.

### D-28. OpenAI Structured Outputs over JSON Mode
**Rejected:** prompt-instructed JSON output with client-side schema
validation and retry-on-malformed.
**Why:** strict structured outputs (`response_format=PydanticModel`,
`strict: true`) give server-side schema enforcement, including
required-field, enum, and union discipline. The matcher then sees
either a well-formed payload or a typed `refusal`, never malformed
JSON. The cost: schema authors lose a few JSON-Schema features
(`additionalProperties`, defaults, optional-without-explicit-null,
open dicts) — all features that would have made the matcher's life
*harder* anyway by widening the input contract. Net win.

### D-29. Single model snapshot pinned in v0; router/sweep is Phase 3
**Rejected:** building the model abstraction layer alongside the
extractor.
**Why:** the project plan explicitly partitions "make it work" (Phase
1) from "make it cheap and routed" (Phase 3). Starting v0 with the
abstraction layer means we can't measure baseline quality of the
single-model path against the routed path — the eval would have
nothing to compare against. So v0 is `gpt-4o-mini-2024-07-18` only,
no fallbacks, no retries, structured outputs strict mode. The price
table for cost estimation is hard-coded with two models (mini + 4o)
because two is enough to write the bookkeeping correctly without
overcommitting to a Phase-3 design.

### D-30. Prompt versioning via constant, persisted with every run
**Rejected:** treating the prompt as part of the code revision and
relying on git-blame for attribution.
**Why:** the prompt and the schema are the load-bearing artifacts
for extraction quality, but they evolve faster than the code around
them. A `PROMPT_VERSION = "extractor-v0.1"` constant gets persisted
inside every `ExtractorRunMeta`, so when an eval shows a regression
or improvement the analyst can attribute it to a specific prompt
revision in seconds — no git archaeology required. Bumping the
version is a deliberate act when the prompt's behaviour is meant to
change, not a side-effect of a typo fix.

### D-31. Settings via `pydantic-settings` + `SecretStr`
**Rejected:** ad-hoc `os.getenv` calls at the call-sites.
**Why:** centralising in `clinical_demo.settings` gives a typed,
documented config surface; `SecretStr` prevents accidental key
leakage into logs/exception messages; `lru_cache` on the accessor
makes the env-parse cost a one-time event; tests can construct an
explicit `Settings` instance to exercise edge cases without touching
the process env.

### D-32. Two parallel verdict types: `CriterionVerdict` (eval seed) and `MatchVerdict` (matcher)
**Rejected:** widening the existing `evals.seed.CriterionVerdict` to
also carry matcher output.
**Why:** they answer different questions over different inputs.
`CriterionVerdict` wraps a `StructuredCriterion` (CT.gov-derived)
with a hand-applied label and a `method ∈ {mechanical, human_review}`
field — its job is *ground truth*. `MatchVerdict` wraps an
`ExtractedCriterion` (LLM-derived) with a typed `Evidence` list and
`matcher_version` — its job is *system output*. Both share the
`Verdict = Literal["pass", "fail", "indeterminate"]` enum so the
eval harness can compare them; everything else diverges. Conflating
them would force one schema to carry foreign fields it has no use
for and would couple the two release cadences (every matcher rev
would touch the eval-seed migration). Cost of keeping them separate:
one alignment function in the eval harness later. Cost of merging
them: a model with two purposes serving neither well.

### D-33. Closed `VerdictReason` enum, not free-text rationale only
**Rejected:** a single `rationale: str` field carrying everything.
**Why:** free-text rationales are great for the reviewer UI tooltip,
but they're a nightmare for regression analysis. With a closed
`VerdictReason` enum (`ok`, `no_data`, `stale_data`, `unit_mismatch`,
`unmapped_concept`, `unsupported_kind`, `unsupported_mood`,
`human_review_required`, `ambiguous_criterion`) an analyst can pivot
"matcher's `unmapped_concept` rate jumped 30% between revisions" in
SQL, no NLP. The free-text `rationale` stays for human consumption.
Adding a new reason is a deliberate act — exactly the property we
want when trying to keep matcher behaviour auditable.

### D-34. Surface-form → ConceptSet lookup is hand-curated and small
**Rejected:** UMLS/RxNorm normalisation, embedding-based concept
resolution, or LLM mapping.
**Why:** the matcher's value comes from *predictability*. A reviewer
should be able to read `concept_lookup.py` in 30 seconds and see
exactly which surface forms the matcher recognises. Any unmapped
surface form lands as `indeterminate (unmapped_concept)`, which is
the *honest* signal — it tells the eval harness exactly where the
matcher's vocabulary needs to grow. Phase 2+ will extend this; v0
intentionally trades recall for traceability. The medication table
is empty in v0 because we haven't done the RxNorm work and would
rather under-promise than fuzzy-match `"metformin"` against an
arbitrary RxNorm code.

### D-35. Polarity / negation as XOR flip applied after dispatch
**Rejected:** baking polarity into each per-kind handler.
**Why:** the polarity and negation rules are uniform across all
criterion kinds, so the per-kind handlers compute the *raw* answer
to the criterion's predicate ("does the patient have T2DM?") and
the dispatcher applies a single XOR flip. Eight cases collapse to
one truth table that gets unit-tested exhaustively. `indeterminate`
verdicts are invariant under both flips — no amount of polarity can
turn "we don't know" into a decision.

### D-36. Typed `Evidence` discriminated union, with `MissingEvidence`
**Rejected:** an opaque `dict[str, Any]` evidence blob, or
omitting evidence entirely on `fail` verdicts.
**Why:** every verdict — including `fail` and `indeterminate` — must
cite the records the matcher actually consulted. A `MissingEvidence`
row that says "no HbA1c lab on or before 2025-01-01" makes a
`no_data` indeterminate verdict legible in a way that an empty
evidence list never could. Typed `Evidence` (`LabEvidence`,
`ConditionEvidence`, `MedicationEvidence`, `DemographicsEvidence`,
`TrialFieldEvidence`, `MissingEvidence`) lets the reviewer UI render
each row appropriately and lets the eval harness count by evidence
kind without parsing strings.

### D-37. Hypothetical mood and `within_future` short-circuit to indeterminate
**Rejected:** treating planned events as if they had occurred, or
inferring them from "intent to" language.
**Why:** v0 has no patient-side data on planned events (Synthea
doesn't generate planned-event records, and we have no source that
does). Quietly returning `fail` on "planned bariatric surgery"
would be wrong; quietly returning `pass` would be worse. The
`unsupported_mood` indeterminate is the matcher saying "the data
exists somewhere — just not on this profile" and the eval harness
will show whether this affects enough criteria to be worth a Phase 2
fix.

### D-38. Conservative top-level rollup: any-fail → fail, else any-indeterminate → indeterminate
**Rejected:** majority-vote, weighted scoring, "soft" rollups that
ignore unmapped concepts.
**Why:** at v0 the rollup is the single signal a non-clinician
consumer of the system reads first. Clinical screening reality is
also conservative: one missed exclusion is disqualifying. The rule
("any `fail` wins; else any `indeterminate` wins; else `pass`") is
trivially auditable, matches what the reviewer would do manually,
and is exactly the surface a Phase-2 critic loop will refine —
e.g. "override an `unmapped_concept` indeterminate when a textual
match is present" or "weight inclusion failures against exclusion
failures." Empty verdict lists collapse to `pass` (vacuously true);
callers must check `summary.total_criteria == 0` themselves before
trusting that as positive evidence — documented and tested.

### D-39. ScorePairResult is a single envelope, not a tuple
**Rejected:** returning `(verdicts, summary, eligibility, meta)`
tuples or expecting callers to bundle their own.
**Why:** every consumer wants the verdicts plus the run metadata —
the CLI needs cost to print, the eval harness needs prompt+matcher
versions to attribute regressions, the reviewer UI needs the
patient/trial/as_of triple to render headers. Bundling them in one
Pydantic model means each consumer picks what it needs without an
ad-hoc tuple-unpacking contract that would have to change every
time the envelope grew a new field. Persisting `ScorePairResult`
to disk for evals is a free side-benefit.

### D-40. On-disk extractor cache + `--no-llm` replay mode
**Rejected:** re-extracting on every CLI invocation, or building
an LRU memory cache that doesn't survive process restarts.
**Why:** the extractor is the only LLM-cost surface in the pipeline
and the demo loop is iterative — the developer/operator wants to
re-render verdicts after touching the matcher, the lookup tables,
or the rollup rules without paying tokens each time. The cache
file is a `StoredExtraction` JSON keyed by NCT id, written by
`extract_criteria.py` and read by `score_pair.py`. `--no-llm`
makes the contract explicit: refuse to make a network call; fail
loudly on cache miss. This also makes CI-grade end-to-end tests
possible without an API key.

### D-41. Observability shim that no-ops when unconfigured
**Rejected:** importing `langfuse.openai` as a drop-in replacement
for the OpenAI client (the SDK's own quickstart pattern), and
crashing if Langfuse keys aren't set.
**Why:** two reasons. (1) The OpenAI drop-in routes *every* call
through Langfuse's wrapper, including the ones in unit tests that
inject a stub client via the `_ClientLike` Protocol — a bad seam
to fight every time we want to add a parallel evaluator or a
non-OpenAI provider. Wrapping at *our* extractor boundary keeps
observability decoupled from the LLM SDK and matches the seam
where we already control prompt-version, cost, and refusal
handling. (2) A fresh checkout, CI run, or local dev session
without Langfuse credentials must work. The shim returns a
`_NoopSpan` sentinel whose `.update()` / `.end()` accept any
kwargs and discard them, so the call sites have one shape:
`with traced(...) as span:`. No `if span is None` everywhere.

### D-42. Defensive on every Langfuse call (observability never breaks the app)
**Rejected:** letting SDK exceptions escape to the application.
**Why:** an analytics provider going down (or a new SDK version
changing a method signature) cannot be allowed to break an
eligibility verdict path. Every call through the shim is
try/except'd, with failures logged at WARNING and execution
continuing with a no-op span. We tolerate a lost trace; we do not
tolerate a lost or wrong verdict because the tracer panicked.
Symmetric to: pre-commit gitleaks blocks credential leaks, the
`SecretStr` fields in Settings prevent log spillage, and the
shim's "fail open" stance prevents observability failures from
becoming application failures.

### D-43. One generation per LLM call, one parent span per scoring pair
**Rejected:** a single trace per CLI invocation, or a span per
matcher kind, or a flat list of generations with no parent.
**Why:** the unit of decision in this system is the (patient,
trial) pair, so that's the parent observation. The extractor's
`generation` (which is what carries cost / tokens / model in the
Langfuse UI) nests under that parent automatically because we use
`start_as_current_observation`. Pivoting on `eligibility`,
`patient_id`, `nct_id`, or verdict counts in the dashboard becomes
a one-row query rather than a join across spans. The matcher does
*not* emit per-criterion observations: it's deterministic, has no
cost, and emitting one span per criterion would balloon the
ingest volume without adding signal — the per-criterion verdicts
are already on the parent's `output`. If/when matcher v0.2 grows
expensive components (a vector lookup, an LLM-backed concept
mapper), they earn their own generation.

### D-44. Tag with metadata, not user/session
**Rejected:** mapping `patient_id` → Langfuse `user_id` and the
CLI invocation → `session_id`.
**Why:** Langfuse's user/session model is built around a human
end-user with a chat history; in our system the "user" is the
clinician operating the reviewer UI, not the patient being
screened, and the "session" semantics don't fit batch eligibility
runs at all. Putting patient/trial ids into `metadata` instead
preserves the full pivot capability without abusing the schema.
This leaves `user_id` and `session_id` available later for the
reviewer UI to populate correctly.

### D-45. State as `TypedDict` + `operator.add` reducer, not Pydantic
**Rejected:** `ScoringState` as a Pydantic `BaseModel` with custom
field validators.
**Why:** LangGraph reducers fire on every concurrent state update,
and Pydantic re-validates the model on each call. That's wrong on
two axes — it's slow on the hot path, and it's incorrect because
intermediate states *must* violate the "all criteria scored"
invariant by design (verdicts accumulate one branch at a time during
fan-in). `TypedDict + Annotated[list, operator.add]` is what every
LangGraph example uses for a reason. Domain models that *are*
Pydantic (Patient, Trial, MatchVerdict, ExtractionResult) sit
*inside* the dict — Pydantic's invariants apply to them
individually; the dict is just the carrier.

### D-46. Carry `(criterion_index, MatchVerdict)` in the reducer, not bare verdicts
**Rejected:** reducer slot of `list[MatchVerdict]`, sort verdicts
later by some derived key (criterion_id, source_text hash).
**Why:** `ExtractedCriterion` has no stable id field today, and
adding one would touch the extractor schema and every existing
matcher fixture. Parallel fan-in does not preserve arrival order,
so for deterministic verdict ordering (which we want for eval,
replay, human review) we need an explicit index. Carrying it as
the first element of a 2-tuple keeps the reducer cheap (concat) and
the ordering restoration trivial (sort on key 0). The rollup node
strips the indices when constructing the public `MatchVerdict`
list.

### D-47. Per-criterion routing inside `fan_out_criteria`, not a separate router node
**Rejected:** `extract → router_node → fan_out_to_matchers`.
**Why:** A bookkeeping node that does nothing visible adds depth to
the trace tree, an extra hop in the runtime, and zero correctness
value over inlining the routing decision in the conditional edge
function. The decision is per-criterion; making it inside the same
function that emits the `Send` objects keeps it co-located with the
fan-out (so future routing rules — say, the v0.2 deterministic →
LLM fallback — land in one place). The empty-criteria edge case is
handled by returning the rollup node name directly (a `str`
return), not an empty `Send` list, which would leave the graph
stuck after `extract`.

### D-48. LLM matcher is a separate prompt + node, not the extractor reused
**Rejected:** repurposing the extractor's prompt to also emit a
verdict on free-text criteria.
**Why:** The extractor's job is *structuring*; the matcher's job is
*deciding*. They have different system prompts, different output
schemas, and different cost / quality trade-offs (matchers run N
times per trial, extractors once). Conflating them would make the
prompt longer (worse cache hit rate), the schema looser (worse
validation), and the eval harder (you can't pivot extraction
quality independently from matching quality). Costs the same in
tokens to keep them separate and pays back in clarity.

The LLM matcher's patient snapshot is a *typed bundle* (age, sex,
active conditions, current medications) — never narrative text. Two
reasons: (a) it keeps the prompt-injection surface narrow before
Phase 3.4 builds the red-team set, and (b) for the kind of
free-text criteria v0 sees (mobility, allergies, informed consent,
geography), the typed snapshot is usually sufficient or
`indeterminate` is the honest answer.

### D-49. Side-by-side `score_pair()` and `score_pair_graph()` for one cycle
**Rejected:** rename + replace the imperative `score_pair()` with
the graph version in one commit.
**Why:** Side-by-side gives a cheap A/B regression test for free —
the eval harness in 2.3 can run both orchestrators on the same
inputs and surface any divergence, which is also the cleanest way
to validate the LLM matcher's behaviour against the deterministic
baseline. The cost is one extra script file (`score_pair_graph.py`)
and ~50 lines of mostly-shared CLI plumbing. Once eval confirms
parity (or surfaces the intended differences), the imperative path
will delegate to the graph and the duplicate disappears.

### D-50. Critic identifies process problems; the matcher decides eligibility
**Rejected:** an LLM critic that takes the verdicts and emits a
revised verdict directly ("the patient is actually a pass on
criterion 3").
**Why:** if the critic can change the answer in one shot, the
audit trail collapses into "the model changed its mind." Instead
the critic emits closed-enum **process findings** —
`polarity_smell`, `extraction_disagreement_with_text`,
`low_confidence_indeterminate` — each tied to one criterion index,
each with a one-sentence rationale and an `info|warning|blocker`
severity. The revise node then dispatches the finding to a
closed-enum **action** (re-run the LLM matcher with focus, flip
polarity and re-match, re-extract that one criterion) and the
*existing* matcher path produces the new verdict. So every
verdict in the trace was produced by a matcher; every revision
has a recorded reason, action, and `verdict_changed` flag. This
is the discipline the deployment-readiness writeup needs and the
shape an eval pivot ("critic interventions that actually changed
an answer") relies on.

### D-51. `merge_indexed_verdicts` replace-by-index reducer
**Rejected:** keeping `operator.add` on `indexed_verdicts` and
filtering duplicates at read time.
**Why:** `operator.add` was the right choice for the initial
parallel fan-out (D-46), but the critic loop *replaces* the
verdict at index N rather than appending another one. Filtering
at read time would mean every consumer of the rollup has to know
about revision history, and the LangGraph reducer contract
becomes a lie: state would no longer be the source of truth, the
read function would be. Custom reducer keeps the invariant —
exactly one verdict per criterion index in state — and pushes
revision history into the dedicated `critic_revisions` audit log
(append-only via `operator.add`), where it belongs.

### D-52. Layered termination: budget + no-progress + recursion backstop
**Rejected:** a single `max_critic_iterations` cap.
**Why:** any one of those signals is the wrong one to trust
alone. A pure budget keeps spending tokens on revisions that
aren't moving anything. Pure no-progress detection is fragile
when the LLM emits the same finding with different rationale
text. Pure `recursion_limit` only fires after the loop has
already gone wrong. So the loop terminates on the *earliest* of:
(a) the critic returns no actionable warnings; (b)
`max_critic_iterations` is hit (default 2 — one critique + one
revision + one re-critique that confirms convergence); (c)
fingerprint-based no-progress check (the set of
`(criterion_index, finding_kind)` pairs is unchanged from the
previous iteration). LangGraph's `recursion_limit` stays
configured as a runtime backstop in case any of those checks have
a bug. Two iterations is what the manifest will end up actually
spending in 95% of pairs; the budget is there for the long tail.

### D-53. Critic is a separate prompt and a separate node, not the matcher reused
**Rejected:** asking the same LLM matcher to also produce a
"would I revise this?" annotation as a side output.
**Why:** the critic's job (review verdicts, emit findings, never
decide eligibility) and the matcher's job (decide one verdict,
return a `MatchVerdict`) have different inputs (matcher: one
criterion + restricted snapshot; critic: all verdicts + the
trial's eligibility text), different outputs, different
prompts, and crucially different versioning concerns: the
matcher prompt is regression-tested against the eval seed, the
critic prompt is regression-tested against the *manifest of
critic-driven revisions*. Collapsing them into one prompt would
mean a prompt change for one job invalidates eval baselines for
the other. Cost is one extra LLM call per pair when the critic
is enabled, which is why it stays opt-in (`critic_enabled=False`
by default in v0).

### D-54. Revise re-uses the LLM matcher node, doesn't introduce a "re-matcher"
**Rejected:** a dedicated revision-time matcher with its own
prompt that takes "previous verdict + critic finding" as
context.
**Why:** another prompt to version, another set of eval baselines
to maintain, and a second code path through which a verdict can
be produced. The revise node instead constructs a
`{criterion, patient}` input identical to the matcher's normal
input and calls the existing `llm_match_node` (or the
deterministic matcher, for non-`free_text` criteria). The
"focus" from the critic finding is recorded in the revise span's
input and in the `CriticRevision.rationale`, but the matcher
prompt is unchanged. Same prompt version, same eval baselines.
For deterministic criteria the revise node is a no-op
(deterministic matchers are already stable); the no-op is still
recorded as a `CriticRevision` with `verdict_changed=False` so
the audit trail stays complete.

### D-55. Human checkpoint as an opt-in `interrupt_before` on `finalize`
**Rejected:** (a) a separate "human review" graph; (b) always
interrupting and requiring an explicit "approve" call.
**Why:** the v0 demo doesn't have a human reviewer in the loop,
so the default path must run end-to-end without one. But the
deployment-readiness writeup needs a real seam where a human can
review and override before the verdict is "final." LangGraph's
`interrupt_before` on a designated node is the clean way: the
`finalize` node is a deliberately-empty pass-through whose only
purpose is to be that seam. When `human_checkpoint=True` the
graph compiles with an `InMemorySaver` checkpointer and pauses
before `finalize`; the caller resumes via the same
`score_pair_graph(thread_id=...)` entry. When the flag is off,
`finalize` runs inline and emits its span like any other node.
One node, two modes, no graph fork.

### D-56. `pickle_fallback=True` on the InMemorySaver
**Rejected:** making `PatientProfile` Pydantic so it serialises
via msgpack like the other state.
**Why:** `PatientProfile` is a thin wrapper around the parsed
FHIR bundle and isn't meant to be a wire type — its purpose is
in-process access. Forcing it to be Pydantic would either bloat
the profile with thousands of fields or hide most of the bundle
behind opaque dicts. The HITL checkpointer is in-process by
construction (it's an `InMemorySaver`, not durable storage), so
`pickle_fallback=True` on the serializer is the right pragmatic
choice: the typed state still goes through msgpack via the
Pydantic types, the profile pickles. When/if Phase 4 adds a
durable checkpoint store, the profile will be re-hydrated from
the bundle on resume rather than serialised at all, and this
fallback can be removed.

### D-57. Critic span tagging surfaces revisions in the trace
**Rejected:** tagging only the parent `score_pair_graph` span
with critic stats.
**Why:** the parent-only view is enough for cohort-level metrics
("X% of pairs had critic revisions") but not enough to debug a
single pair: the trace would show that a verdict changed without
showing *why* the critic flagged it or *what* the revise node
did. So critic spans carry `critic_iteration` and the count of
findings; revise spans carry `criterion_index`, `action`,
`finding_kind`, `verdict_changed`. The parent still gets
`critic_iterations`, `revisions_total`, `revisions_changed_verdict`
for the cohort view. Cost is metadata only — no new generations,
no new spans beyond the ones already added for the loop — but
the debug experience is the difference between "verdict
changed mid-pair, who knows why" and "verdict flipped because
the critic flagged a polarity smell on criterion 3 and the
revise node ran `flip_polarity_and_rematch`."

### D-58. Critic audit data lives in the trace, not on `ScorePairResult`
**Rejected:** extending `ScoringSummary` (or `ScorePairResult`)
with `critic_revisions: list[CriticRevision]` and
`critic_iterations: int`.
**Why:** the imperative `score_pair()` and the graph
`score_pair_graph(critic_enabled=False)` have to keep returning
the same envelope so the eval harness in 2.3 can A/B them
without branching on which orchestrator produced the result
(D-49). Adding critic fields to the envelope either breaks that
parity or saddles the imperative path with optional fields it
will never populate. The audit data is fully captured in the
Langfuse trace (D-57), and the in-process caller can still read
it off the graph's `final_state` if it needs to. Phase 2.3 may
introduce a richer `ScoreRunResult` envelope once the eval
harness lands; deferred until there's a concrete consumer.

### D-59. Eval harness scorer is a `Callable`, not a registered orchestrator
**Picked:** `run_eval(scorer, cases)` where
`scorer: Callable[[EvalCase], ScorePairResult]`.
**Rejected:** an `Orchestrator` enum + dispatch table inside the
harness; or a base class the imperative and graph paths both
subclass.
**Why:** the harness's job is to score N cases and persist the
result. Knowing *how* the scorer works (which model, which
prompt, critic on/off, which extraction policy) is the caller's
responsibility, and a `Callable` is the smallest contract that
respects that. A registry would force every new orchestrator
variant to land code in `evals/` even when the variant is
genuinely orthogonal — a critic-enabled vs critic-disabled run
is not a new orchestrator, it's a different `partial`. The
script (`scripts/eval.py`) carries the bridging logic; the
library doesn't.

### D-60. Two-table schema with a `result_json` blob, not a normalized verdicts table
**Picked:** `runs` + `cases` tables; the full `ScorePairResult`
serializes into `cases.result_json`. Per-case summary columns
(eligibility, verdict counts, extraction cost, latency) are
flattened onto `cases` so an operator can `SELECT eligibility,
COUNT(*)` without `json_extract` gymnastics.
**Rejected:** a third `verdicts` table with one row per
criterion verdict from day one.
**Why:** v0 doesn't have a query that needs per-verdict joins —
layer-1 (deterministic vs Synthea) walks the structured-criterion
verdicts in a single dict comparison; layer-2 (Chia) is a
separate dataset entirely; layer-3 (LLM judge) is the same
shape. JSON blob storage costs ~one Pydantic round-trip on read
and gains zero meaningful lookup speed at the dataset sizes we
care about (49 pairs today, ~500 once we burn down the
human-review backlog). When a real query motivates a
`verdicts` table, the migration is `INSERT INTO verdicts
SELECT … FROM cases CROSS JOIN json_each(result_json, '$.verdicts')` —
fully recoverable from the blob.

### D-61. Runs are append-only; same `run_id` is a hard error
**Picked:** `save_run` raises `IntegrityError` on duplicate
`run_id`. Re-scoring a dataset gets a new id.
**Rejected:** "upsert" semantics that overwrite a previous run
in place.
**Why:** the eval store is the audit trail for "what did the
system look like on date X." Silently overwriting a run
destroys baselines and makes regressions invisible; explicit
re-runs with new ids preserve the lineage. Storage cost is
trivial.

### D-62. Per-case scorer failures recorded on the row, not allowed to abort the run
**Picked:** `run_eval` wraps each `scorer(case)` in a
`try/except`; on failure, persist `error TEXT` and NULL out the
per-case summary cols. `n_errors` is a top-level field on
`RunResult`.
**Rejected:** propagating the first exception and aborting; or
silently skipping the case with no record.
**Why:** in a 50-pair run, a single 429 or transient extraction
failure shouldn't lose 49 successes. Recording the error keeps
the failure visible for layer-1 to reason about, but doesn't
gate progress. v0 doesn't surface a failure-rate metric in the
reporter (the count is one digit at the bottom of the summary);
that earns its place once we have a real production baseline.

### D-63. No layer-specific eval logic in 2.3
**Picked:** `evals/run.py` and `evals/store.py` are pure
plumbing. They don't know what "deterministic accuracy" or
"LLM judge calibration" mean.
**Rejected:** baking layer-1 metrics (e.g. structured-verdict
agreement rate) into the runner or the reporter so 2.3 ships
with "real" numbers.
**Why:** layer-specific logic belongs in tasks 2.4-2.6, where
each layer can pick its own metric, output format, and
red-team set without retrofitting the harness. A reporter
abstraction was considered and cut: `eval report` is a one-screen
pretty-printer of `RunResult` summary counts, and that's all v0
needs. Layer reporters can read `runs.sqlite` directly when they
land — the schema is stable and queryable.

### D-9. Defer KPMG-specific framing of the writeup until Phase 3
**Rejected:** writing the deployment readiness doc up front.
**Why:** the writeup should be *informed by what was actually built*, not
projected onto it. Premature writing leads to the system being shaped to
match the writeup rather than the other way around.

---

## 13. Open questions (to keep visible during build)

- **Eval seed-set human-review pass (Phase 1 task 1.6).** The
  mechanical labeler produced 82 structured-field verdicts across
  49 pairs, but the seed set has ~856 free-text criteria pending
  human review (in `data/curated/eval_seed.json`, every pair carries
  `free_text_review_status="pending"`). End-to-end matcher evals
  cannot be claimed as ground truth until this pass is complete.
  Plan: budget a real afternoon to walk through every pair, mark
  the obvious ones (clearly satisfied/violated by the patient
  record), flag the clinical-judgement ones as `indeterminate`
  with rationale. Flip `free_text_review_status` to `"complete"`
  pair by pair as you go. Owed labels are surfaced in the manifest
  summary so progress is visible.
- Will the Chia entity schema be sufficient as the criterion structured
  representation, or will it need extension for our domains? (Decided in
  Phase 1 task 1.5: the Chia vocabulary is **rich enough** for the
  extractor's structural targets — Condition, Drug, Measurement, Value,
  Temporal, Qualifier, Negation cover the criteria types in our chosen
  trial slices. We will *not* try to extend the schema; instead the
  matcher will normalize Chia surface text against the patient model
  separately. Open variant: whether to surface `Non-representable` /
  `Not_a_criteria` as a "skip" verdict in the matcher — defer to
  task 1.9.)
- How many critique-loop iterations are useful before diminishing
  returns? (Default of 2 picked in Phase 2 task 2.2 — one
  critique + one revision + one re-critique to confirm
  convergence — paired with no-progress fingerprint detection so
  the loop also terminates earlier when findings are stable.
  Re-validate against the real revision manifest in Phase 2 task
  2.7 after the first baseline regression run; if 95%+ of
  revisions land in iteration 1, drop the default to 1.)
- For the LLM-as-judge calibration, is there enough human-judge agreement on
  the borderline cases for the metric to mean anything? (Decide after Phase
  2 task 2.6 — if calibration is poor, simplify the rubric.)
- Will the Svelte reviewer UI integration land cleanly into the Astro
  routing on `juliusm.com`, or should it be a sibling subdomain? (Decide at
  Phase 2 task 2.9.)
- Cost sweep: which exact models to include, given pricing and availability
  at the time of Phase 3? (Decide at the start of Phase 3 task 3.2.)
