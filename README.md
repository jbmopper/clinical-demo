# clinical-demo

Clinical Trial Eligibility Co-Pilot — a portfolio demo built for a Generative
AI Forward Deployed Engineer interview.

> **Status: Phase 2 in progress.** Phase 1 deliverables (curated
> data, extractor v0, deterministic matcher v0, end-to-end
> `score_pair`, Langfuse tracing) are complete. Phase 2 task 2.1
> landed the LangGraph orchestrator (`score_pair_graph()`) with
> per-criterion fan-out, a deterministic-first router, an LLM
> matcher node for free-text criteria, and a join+rollup. Phase 2
> task 2.2 has now landed the **aggregator + critic loop**
> (`rollup → critic → [revise → rollup | finalize]`) with closed-enum
> process findings, bounded revisions, no-progress detection, and
> an opt-in `interrupt_before` human checkpoint on `finalize`.
> Imperative `score_pair()` and the graph version run side-by-side.
> Phase 2 task 2.3 landed the eval harness scaffolding;
> task 2.4 added a layer-1 (deterministic) eval (`agreement` +
> `coverage` per field, `eval report --layer 1`); task 2.9
> landed the **FastAPI backend** (`/health`, `/patients`,
> `/trials`, `POST /score`) ahead of order so the reviewer UI
> has something to call. Task 2.8 then landed the **reviewer UI**
> as a SvelteKit dev rig under `web/` — single-page, picks a
> patient + trial, dispatches `POST /score`, and renders the
> per-criterion verdicts as colored pills with click-to-expand
> evidence. First end-to-end demo run surfaced a length-overflow
> on the largest curated trial; D-65 promotes the LLM-call token
> caps into `Settings`, raises the extractor cap from 4096 to the
> model's 16384 ceiling, and converts
> `openai.LengthFinishReasonError` from a 500 into a graceful
> empty extraction with cost preserved. Second run surfaced a
> stale-cache hit re-pumping a malformed criterion through the
> matcher; D-66 makes that fail soft (matcher emits
> `indeterminate(extractor_invariant_violation)` per-criterion
> instead of crashing the trial) and revs the cache filename to
> embed the prompt version, an 8-char schema fingerprint, and the
> extractor model — so any of those three changing automatically
> invalidates stale envelopes (the schema fingerprint hashes the
> live `ExtractedCriteria.model_json_schema()`, so adding a field
> invalidates for free).
> **391 tests passing** (Python; the UI is a thin
> presentation layer over the API and is exercised manually).
> Up next: the remaining eval layers and a baseline regression
> run; the UI gets ported into `juliusm.com` for the
> public-facing demo.

## What it is (one paragraph)

Given a patient record and a clinical trial protocol, return a per-criterion
eligibility verdict (`eligible | ineligible | indeterminate`) with citations
back to source criteria text and supporting patient evidence. A clinical
research coordinator reviews and decides whether to pursue the patient. The
system never autonomously enrolls anyone.

## Source-of-truth docs

- [`PLAN.md`](./PLAN.md) — build plan, hour estimates, scope cuts, decision log.
- [`description.md`](./description.md) — narrative + architecture + diagram.

## Setup

Requires [`uv`](https://docs.astral.sh/uv/). Python 3.12 is fetched by `uv` if
not already installed.

```bash
uv sync
cp .env.example .env  # then fill in keys as needed
uv run pre-commit install
```

Optional (for the secret-scan pre-commit hook to vendor its own binary, no
local install needed). The hook will download `gitleaks` on first run.

## Common commands

```bash
uv run pytest                              # tests
uv run ruff check .                        # lint
uv run ruff format .                       # format
uv run mypy                                # type check
uv run pre-commit run --all-files
uv run marimo edit marimo/explore_synthea.py    # patient cohort tour
uv run marimo edit marimo/explore_trials.py     # trial set tour
uv run marimo edit marimo/explore_chia.py       # Chia annotation tour
uv run marimo edit marimo/explore_eval_seed.py  # eval seed-set tour
uv run python scripts/score_pair.py --help        # imperative orchestrator
uv run python scripts/score_pair_graph.py --help  # LangGraph orchestrator
uv run python scripts/eval.py --help              # eval harness CLI
uv run python scripts/serve.py                    # FastAPI demo server (127.0.0.1:8000)
(cd web && npm run dev)                           # SvelteKit reviewer UI (127.0.0.1:5173)
```

## Data

Source data is gitignored under `data/raw/`. Download the Synthea FHIR R4
sample (PLAN.md §4):

```bash
mkdir -p data/raw/synthea && cd data/raw/synthea
curl -sL -o synthea.zip 'https://raw.githubusercontent.com/synthetichealth/synthea-sample-data/main/downloads/synthea_sample_data_fhir_r4_nov2021.zip'
unzip -q synthea.zip   # creates ./fhir/ with ~557 patient bundles
```

Pull the curated trial set from ClinicalTrials.gov v2 (~30 trials,
~1.5 seconds, no key needed):

```bash
uv run python scripts/curate_trials.py
# writes data/curated/trials/<NCT_ID>.json + data/curated/trials_manifest.json
```

Build the working patient cohort (150 cardiometabolic-tilted patients
from Synthea, scored by multi-condition richness):

```bash
uv run python scripts/curate_cohort.py
# writes data/curated/cohort_manifest.json
```

Download the Chia corpus (1,000 hand-annotated trials in BRAT format,
2.5 MB, CC BY 4.0):

```bash
mkdir -p data/raw/chia && cd data/raw/chia
curl -sL -A 'Mozilla/5.0' -o chia_with_scope.zip 'https://ndownloader.figshare.com/files/21728850'
unzip -q chia_with_scope.zip
# yields ~4000 .txt/.ann pairs across 1000 trials
```

Build the eval seed set (49 pairs across 7 trial slices, with
mechanical pre-labels for structured fields and free-text criterion
counts pending human review):

```bash
uv run python scripts/build_eval_seed.py
# writes data/curated/eval_seed.json
```

Patient profiles for matcher / labeler use:

```python
from datetime import date
from clinical_demo.data.synthea import iter_bundles
from clinical_demo.profile import PatientProfile, ThresholdResult
from clinical_demo.profile.concept_sets import T2DM, HBA1C

patient = next(iter_bundles("data/raw/synthea/fhir"))
profile = PatientProfile(patient, date(2025, 1, 1))

profile.has_active_condition_in(T2DM)                         # bool
profile.latest_lab("4548-4", max_age_days=90)                 # LabObservation | None
profile.meets_threshold("4548-4", ">=", 7.0, "%", max_age_days=90)
# -> ThresholdResult.MEETS / DOES_NOT_MEET / NO_DATA / STALE_DATA / UNIT_MISMATCH
```

Run the criterion extractor (LLM, OpenAI structured outputs) on a
small sample of curated trials. Requires `OPENAI_API_KEY` in `.env`.

```bash
uv run python scripts/extract_criteria.py --dry-run     # render the prompt only; no API call
uv run python scripts/extract_criteria.py               # 5 trials by default
uv run python scripts/extract_criteria.py --limit 0     # all curated trials
# writes data/curated/extractions/<NCT_ID>.json (one envelope per trial)
```

Use the extractor library directly:

```python
from clinical_demo.extractor import extract_criteria

result = extract_criteria(trial.eligibility_text)
for c in result.extracted.criteria:
    print(c.kind, c.polarity, c.source_text)
print(f"prompt={result.meta.prompt_version} cost=${result.meta.cost_usd:.4f}")
```

Run the deterministic matcher (no LLM, no network) over an extraction
+ a `PatientProfile`:

```python
from datetime import date

from clinical_demo.matcher import match_extracted
from clinical_demo.profile import PatientProfile

profile = PatientProfile(patient, as_of=date(2025, 1, 1))
verdicts = match_extracted(result.extracted.criteria, profile, trial)

for v in verdicts:
    print(v.verdict, v.reason, v.criterion.source_text)
    for ev in v.evidence:
        print(" ", ev.kind, ev.note)
```

Each `MatchVerdict` carries a closed `reason` enum (`ok`, `no_data`,
`stale_data`, `unit_mismatch`, `unmapped_concept`, `unsupported_kind`,
`unsupported_mood`, `human_review_required`, `ambiguous_criterion`),
a one-line `rationale` for the reviewer UI, and a typed `Evidence`
list (`LabEvidence`, `ConditionEvidence`, `MedicationEvidence`,
`DemographicsEvidence`, `TrialFieldEvidence`, `MissingEvidence`).
Polarity and negation are applied as a single XOR flip after
dispatch, so each per-kind matcher answers the criterion's *raw*
predicate; `indeterminate` verdicts pass through unchanged.

Score one (patient, trial) pair end-to-end from the CLI:

```bash
# cheapest sane invocation: cached extraction, pretty output
uv run python scripts/score_pair.py \
    --patient-id 9ef4db86-c427-ddfe-a607-737f08ffb0c1 \
    --nct-id NCT06000462

# refuse to spend tokens; require a cached extraction (CI-friendly)
uv run python scripts/score_pair.py \
    --patient-id <id> --nct-id <nct> --no-llm

# re-extract from scratch even if a cached envelope exists
uv run python scripts/score_pair.py \
    --patient-id <id> --nct-id <nct> --force-extract

# machine-readable
uv run python scripts/score_pair.py \
    --patient-id <id> --nct-id <nct> --json > out.json
```

The script prints the conservative top-level eligibility rollup
(`PASS`, `FAIL`, or indeterminate), the extraction's model / prompt
version / cost / token count, verdict counts, and a per-criterion
table with the source bullet, the matcher's `reason` and `rationale`,
and the top two evidence rows (lab values, condition records,
demographics) that drove the decision.

Use the scoring library directly:

```python
from datetime import date

from clinical_demo.scoring import score_pair

result = score_pair(patient, trial, as_of=date(2025, 1, 1))
print(result.eligibility, result.summary.by_verdict)
for v in result.verdicts:
    print(v.verdict, v.reason, v.criterion.source_text)
```

## Orchestration (LangGraph)

Phase 2 introduces `clinical_demo.graph`, a LangGraph
implementation of the same pipeline. Same input, same
`ScorePairResult` envelope; the difference is internal — fan-out
parallelism over criteria, an LLM matcher node for free-text
criteria the deterministic matcher cannot decide structurally, and
the critic loop described below.

Graph shape:

```
START → extract → [conditional fan-out: one Send per criterion]
                       ├─ deterministic_match  (kind != free_text)
                       └─ llm_match            (kind == free_text)
                  → rollup → critic → [revise → rollup | finalize] → END
```

(The critic and revise nodes are skipped when `critic_enabled=False`,
which is the v0 default; in that case the graph collapses to
`rollup → finalize → END`.)

The two implementations run side by side until the eval harness
in 2.3 confirms parity. Drive the graph from a script:

```bash
uv run python scripts/score_pair_graph.py \
    --patient-id <id> --nct-id <nct>
```

Or from Python:

```python
from datetime import date
from clinical_demo.graph import score_pair_graph

result = score_pair_graph(patient, trial, as_of=date(2025, 1, 1))
```

Routing rule (v0): `kind == "free_text"` → LLM matcher; everything
else → deterministic matcher. The LLM matcher receives a small
typed patient snapshot (age, sex, active conditions, current
medications) — never narrative text — to keep the prompt-injection
surface narrow before Phase 3.4's red-team set lands. Polarity
(inclusion/exclusion) and negation are applied downstream by the
node itself, not the model, so the LLM only ever decides the
predicate of the criterion. The LLM matcher's verdicts carry
`matcher_version="llm-matcher-v0.1"` so eval consumers can pivot
on which path produced each verdict.

### Critic loop (Phase 2.2)

The critic is an LLM reviewer that runs *after* the rollup. It
does **not** decide eligibility — it identifies process problems
with how the matcher reached its current verdicts and emits
closed-enum findings that the revise node turns into targeted
re-runs. Every verdict in the trace was still produced by a
matcher; every revision has a recorded reason, action, and
`verdict_changed` flag.

Finding kinds (closed enum, pinned by `LLM_CRITIC_VERSION`):

- `polarity_smell` — the matcher's rationale describes the
  patient as meeting the predicate but the verdict contradicts
  the polarity (e.g. exclusion criterion, verdict=pass). Almost
  always an extractor polarity bug. **Action:** flip polarity and
  re-match.
- `extraction_disagreement_with_text` — the criterion's source
  text mentions a constraint the structured fields don't reflect
  (e.g. `"≥18 AND non-pregnant"` extracted as age only).
  **Action:** re-extract that one criterion.
- `low_confidence_indeterminate` — `indeterminate(no_data)` on a
  free-text criterion where the rationale itself hints there's
  signal nearby. **Action:** re-run the LLM matcher with the
  finding's rationale as focus.

Severities are `info` (recorded but not acted on), `warning`
(triggers a revision), `blocker` (reserved for the heuristic
critic and human checkpoints; the LLM critic does not emit these
in v0).

Termination is layered. The loop ends on the *earliest* of:

1. The critic returns no actionable warnings.
2. `max_critic_iterations` is hit (default `2`: one critique +
   one revision + one re-critique that confirms convergence).
3. Fingerprint-based no-progress detection — the set of
   `(criterion_index, finding_kind)` pairs from this iteration
   matches the previous iteration. The critic isn't finding new
   process problems, just re-flagging the same ones.

LangGraph's `recursion_limit` stays configured as a runtime
backstop in case any of the above checks have a bug.

The critic is **opt-in** in v0: enable per call via
`critic_enabled=True`. When disabled the graph collapses to a
single `rollup → finalize → END` and adds no LLM cost beyond the
extractor + matcher.

```python
from datetime import date
from clinical_demo.graph import score_pair_graph

result = score_pair_graph(
    patient, trial,
    as_of=date(2025, 1, 1),
    critic_enabled=True,
    max_critic_iterations=2,
)
```

`result` is the same `ScorePairResult` envelope produced by the
non-critic path (D-58); the audit trail (per-iteration findings,
per-revision action + `verdict_changed`, total iteration count)
lives in the Langfuse trace. The parent `score_pair_graph` span
is tagged with `critic_iterations`, `revisions_total`, and
`revisions_changed_verdict`; per-revise spans carry
`criterion_index`, `action`, `finding_kind`, and `verdict_changed`.
Phase 2.3 may surface a subset on a richer envelope once the eval
harness has a concrete consumer.

### Human checkpoint

Set `human_checkpoint=True` to compile the graph with an
`InMemorySaver` checkpointer and `interrupt_before=[finalize]`.
The graph then pauses just before `finalize` and the caller gets
back the in-progress state for the given `thread_id`:

```python
from langgraph.types import Command

result = score_pair_graph(
    patient, trial,
    as_of=date(2025, 1, 1),
    human_checkpoint=True,
    thread_id="pair-42",
)
# inspect / override; then resume by re-invoking the underlying
# graph on the same thread with Command(resume=...)
```

The v0 wiring is the LangGraph-native pause/resume primitive
rather than a wrapped helper — Phase 2.8 will wrap it behind the
reviewer UI's API once the override semantics are settled. By
default (`human_checkpoint=False`) `finalize` runs inline — same
node, two modes, no graph fork.

## Eval harness (Phase 2.3)

`clinical_demo.evals` is the scorer-agnostic plumbing the next
three tasks (layer-1/2/3 evals) sit on top of:

- `EvalCase` / `CaseRecord` / `RunResult` Pydantic envelopes.
- `load_dataset(seed_path, pair_ids=…, limit=…)` — reads the
  existing `data/curated/eval_seed.json`; no parallel format.
- `run_eval(scorer, cases, dataset_path, notes=…)` — calls
  `scorer(case)` per pair, catches per-case exceptions onto the
  case row (one bad pair doesn't tank a 50-pair run), returns a
  `RunResult` with aggregate latency / cost.
- `evals.store.open_store(db_path)` — append-only SQLite at
  `eval/runs.sqlite` (gitignored). Two tables: `runs` and
  `cases`; the full `ScorePairResult` lives on
  `cases.result_json` so layer code reads strongly-typed results
  back without reaching into the schema.

The scorer is just a `Callable[[EvalCase], ScorePairResult]`, so
the imperative `score_pair()`, the LangGraph
`score_pair_graph()`, and any future critic-on/critic-off split
are all "just a scorer" — no orchestrator registry inside the
harness.

```bash
# Score every pair in the seed via the imperative orchestrator
uv run python scripts/eval.py run \
    --orchestrator imperative \
    --notes "score_pair v0 baseline"

# Smoke-run 3 pairs through the LangGraph orchestrator with critic on
uv run python scripts/eval.py run \
    --orchestrator graph --critic-enabled --limit 3 \
    --notes "graph + critic, 3-pair smoke"

# Re-render any persisted run; or list runs (run_id omitted)
uv run python scripts/eval.py report --run-id <id>
uv run python scripts/eval.py report
```

Layer-specific reporting (deterministic accuracy in 2.4, Chia
agreement in 2.5, judge calibration in 2.6) reads `runs.sqlite`
directly when those tasks land — the schema is stable and
queryable from day one.

## HTTP API (Phase 2.9)

`clinical_demo.api.create_app()` returns a FastAPI app with four
routes:

- `GET /health` — liveness probe.
- `GET /patients` — cohort manifest rows (id, score, slice).
- `GET /trials` — `{nct_id, title}` per curated trial.
- `POST /score` — score one (patient, trial) pair; returns the
  same `ScorePairResult` envelope the CLI emits.

The route layer is intentionally thin (request validation +
loader dispatch + scorer call); all logic lives in
`clinical_demo.scoring` / `.graph` so the CLI, the eval harness,
and the API call exactly the same code path. Loader helpers live
in `api/loaders.py` with process-scope caches so repeated
requests for the same patient or trial are O(1).

```bash
# Boot the demo server (defaults to 127.0.0.1:8000)
uv run python scripts/serve.py

# Or directly via uvicorn
uv run uvicorn clinical_demo.api:create_app --factory --port 8000
```

```bash
# Score one pair via the imperative orchestrator
curl -s -X POST http://127.0.0.1:8000/score \
  -H 'content-type: application/json' \
  -d '{"patient_id": "<id>", "nct_id": "<nct>", "as_of": "2025-01-01"}'

# Same pair via the graph orchestrator with the critic loop
curl -s -X POST http://127.0.0.1:8000/score \
  -H 'content-type: application/json' \
  -d '{"patient_id": "<id>", "nct_id": "<nct>",
       "orchestrator": "graph", "critic_enabled": true}'
```

CORS is wide-open for the v0 demo (the reviewer UI and the API
will be served from different ports during dev). Lock down
`allow_origins` before any non-demo deployment.

## Reviewer UI (Phase 2.8)

A minimal SvelteKit single-page app lives under `web/`. It is
intentionally a **dev rig**: the production reviewer surface lives
in the `juliusm.com` repo, and this one exists so the API +
scoring pipeline can be exercised through a real UI without
publishing anything. Same FastAPI backend, same `ScorePairResult`
shape; the only thing different is where the bytes get served.

The page picks a patient and a trial from the catalog endpoints,
posts `/score`, and renders the per-criterion verdicts as colored
pills (`pass` / `fail` / `indeterminate`) with click-to-expand
rationale + typed evidence rows. Toggles for the imperative vs.
graph orchestrator, the critic loop, and cached extraction are
wired through to the request body so the demo can show all four
combinations without leaving the page.

```bash
# In one terminal: boot the API
uv run python scripts/serve.py

# In another: install + run the dev UI (first run only)
cd web
npm install
npm run dev          # http://127.0.0.1:5173
```

The UI hits `http://127.0.0.1:8000` by default. Override with
`VITE_API_BASE` (see `web/.env.example`) — useful when porting
into `juliusm.com` against a deployed API.

## Observability

Langfuse v4 traces every LLM call and every scoring run. Tracing is
opt-in via env: set both `LANGFUSE_PUBLIC_KEY` and
`LANGFUSE_SECRET_KEY` (and optionally `LANGFUSE_HOST` or its alias
`LANGFUSE_BASE_URL`) and the next CLI invocation ships its spans;
omit them and the observability shim is a no-op (no SDK construction,
no exporter thread, no warnings). The shim is also defensive: a
failing tracer is logged at WARNING and never breaks the application
path it instruments.

Span shape:

- `score_pair` (or `score_pair_graph` for the LangGraph orchestrator)
  — one parent `span` per (patient, trial) pair, tagged with
  `patient_id`, `nct_id`, `eligibility`, and verdict counts in
  metadata so the Langfuse UI can pivot on any of them without
  joins. The graph variant additionally tags `orchestrator=langgraph`
  so the two implementations can be compared without filter gymnastics.
- `extract_criteria` — one nested `generation` per LLM call, with
  `model`, `prompt_version`, the eligibility text as input, the
  parsed criteria as output, token usage, estimated USD cost, and
  latency. Refusals are tagged `WARNING` with the refusal text on
  `output`; missing-parsed errors and other exceptions are tagged
  `ERROR`.
- `llm_match` — one nested `generation` per free-text criterion the
  LLM matcher decides, with `model`, `prompt_version`,
  `criterion_index` (so a multi-free-text-criterion run can be
  decomposed in the dashboard), the prompt as input, the parsed
  verdict as output, usage and cost.

To trace your own LLM calls (later phases), use the same shim:

```python
from clinical_demo.observability import traced

with traced("aggregate_verdicts", as_type="generation",
            model="gpt-4o-mini", input=prompt) as span:
    response = client.chat.completions.create(...)
    span.update(output=response.choices[0].message.content,
                usage_details={"input": response.usage.prompt_tokens,
                               "output": response.usage.completion_tokens})
```

## License

MIT — see [`LICENSE`](./LICENSE).
