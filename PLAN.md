# Clinical Trial Eligibility Co-Pilot — Project Plan

> **Purpose.** Build a portfolio-grade demo for a 20-minute presentation in the
> final round of a Generative AI Forward Deployed Engineer interview at KPMG's
> AI & Data Labs practice.
>
> **Companion docs.** See `description.md` for the user/workflow narrative and
> high-level architecture; this file is the working build plan, scope contract,
> and decision log. When the two disagree, this file wins.

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
| 1.5 | Pull Chia corpus, parse the BRAT annotations, build a Pydantic representation of the Chia schema (entities + relations). | 4 |
| 1.6 | Hand-pick ~30 trials and ~50 (patient, trial) pairs as the **eval seed set**. Hand-label expected per-criterion verdicts for the pairs (this is the most boring, most important task in the whole project — block out a real afternoon). | 6 |
| 1.7 | Patient Profiler v0: deterministic FHIR → typed Python objects with `as_of_date` slicing. | 4 |
| 1.8 | Criterion Extractor v0: single model, single prompt, JSON-schema output mirroring the Chia entity types. No retries, no router. | 4 |
| 1.9 | Deterministic matcher v0: covers numeric criteria, age, sex, active condition presence/absence. Returns `pass | fail | indeterminate`. | 4 |
| 1.10 | Glue script: `score_pair(patient, trial) -> List[CriterionVerdict]`, runs from the CLI. | 2 |
| 1.11 | Wire Langfuse from day one — every LLM call traced; project name `clinical-demo`. | 2 |
| **Phase 1 total** | | **~37 hr** |
| **Exit criterion** | One CLI command takes one patient + one trial and prints per-criterion verdicts with citations. Ugly is fine. | |

### Phase 2 — Workflow + eval

| # | Task | Est. (hr) |
|---|---|---|
| 2.1 | LangGraph migration: per-criterion fan-out, deterministic-first conditional routing, LLM matcher node, join. | 5 |
| 2.2 | Aggregator + Critic loop: bounded revision iterations, termination conditions, human-checkpoint hook. | 4 |
| 2.3 | Eval harness scaffolding: dataset format, runner, results store, basic CLI (`eval run`, `eval report`). | 4 |
| 2.4 | Layer 1 eval — deterministic: per-criterion accuracy on numeric/structured criteria. | 2 |
| 2.5 | Layer 2 eval — reference-based: criterion extraction F1 vs. Chia annotations. | 4 |
| 2.6 | Layer 3 eval — LLM-as-judge: rubric, prompt, calibration against ~30–50 hand-graded examples; report inter-rater agreement. | 6 |
| 2.7 | First baseline regression run; commit numbers to repo as `eval/baselines/`. | 2 |
| 2.8 | Svelte reviewer UI v0: side-by-side trial criteria + patient evidence; per-criterion verdict pills; click-to-source. | 8 |
| 2.9 | Backend: minimal FastAPI endpoint that the Svelte UI calls; CORS; deploy plan for `juliusm.com`. | 3 |
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
5. Drop the Critic loop; show a single-pass aggregator.
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

### D-9. Defer KPMG-specific framing of the writeup until Phase 3
**Rejected:** writing the deployment readiness doc up front.
**Why:** the writeup should be *informed by what was actually built*, not
projected onto it. Premature writing leads to the system being shaped to
match the writeup rather than the other way around.

---

## 13. Open questions (to keep visible during build)

- Will the Chia entity schema be sufficient as the criterion structured
  representation, or will it need extension for our domains? (Decide after
  Phase 1 task 1.5.)
- How many critique-loop iterations are useful before diminishing returns?
  (Decide after Phase 2 task 2.2 with real measurements.)
- For the LLM-as-judge calibration, is there enough human-judge agreement on
  the borderline cases for the metric to mean anything? (Decide after Phase
  2 task 2.6 — if calibration is poor, simplify the rubric.)
- Will the Svelte reviewer UI integration land cleanly into the Astro
  routing on `juliusm.com`, or should it be a sibling subdomain? (Decide at
  Phase 2 task 2.9.)
- Cost sweep: which exact models to include, given pricing and availability
  at the time of Phase 3? (Decide at the start of Phase 3 task 3.2.)
