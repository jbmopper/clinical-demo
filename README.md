# clinical-demo

Clinical Trial Eligibility Co-Pilot — a portfolio demo built for a Generative
AI Forward Deployed Engineer interview.

> **Status: Phase 2 in progress.** Phase 1 deliverables (curated
> data, extractor v0, deterministic matcher v0, end-to-end
> `score_pair`, Langfuse tracing) are complete. Phase 2 task 2.1 just
> landed: the scoring pipeline now runs through a LangGraph
> orchestrator (`score_pair_graph()`) with per-criterion fan-out, a
> deterministic-first router, an LLM matcher node for free-text
> criteria, and a join+rollup. Imperative `score_pair()` and the
> graph version run side-by-side until the eval harness in 2.3
> confirms parity. **299 tests passing.** Up next: aggregator/critic
> loop, eval harness, and the reviewer UI.

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
a clean seam where Phase 2.2's critic loop will plug in.

Graph shape:

```
START → extract → [conditional fan-out: one Send per criterion]
                       ├─ deterministic_match  (kind != free_text)
                       └─ llm_match            (kind == free_text)
                  → rollup → END
```

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
