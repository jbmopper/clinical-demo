# Indeterminacy diagnostic — 2026-04-21 baseline

Companion to `SUMMARY.md`. The eligibility-rollup picture in
SUMMARY.md leaves the question "*why* is everything indeterminate?"
visible but unanswered. This file walks every per-criterion verdict
in the imperative baseline run and bins by `(verdict, reason, kind)`.

The intent is that future investments (vocabulary expansion, prompt
revs, matcher v0.2) compare back to this exact distribution to
quantify "I moved 200 verdicts out of `unmapped_concept`."

## Headline

| metric                                | value          |
|---------------------------------------|----------------|
| total per-criterion verdicts          | **841**        |
| `indeterminate`                       | 772 (91.8%)    |
| `pass`                                | 48  (5.7%)     |
| `fail`                                | 21  (2.5%)     |

## Indeterminate breakdown by reason (n=772)

| reason                            |    n |  % of indet |
|-----------------------------------|-----:|------------:|
| `unmapped_concept`                |  689 |       89.2% |
| `human_review_required` (LLM matcher said "I don't know") | 50 | 6.5% |
| `unsupported_mood`                |   14 |        1.8% |
| `unit_mismatch`                   |   11 |        1.4% |
| `no_data` (right kind of patient signal absent) | 5 | 0.6% |
| `extractor_invariant_violation` (D-66 soft-fail) | 2 | 0.3% |
| `ambiguous_criterion`             |    1 |        0.1% |

**`unmapped_concept` is 89% of the indeterminacy and ~82% of all
verdicts overall.** Every other reason is sub-7% noise. The
matcher's vocabulary is the bottleneck, full stop.

## `unmapped_concept` decomposition by criterion kind (n=689)

| kind                  |   unmapped | unique surface forms |
|-----------------------|-----------:|---------------------:|
| `condition_absent`    |    361     |                  ~250 |
| `condition_present`   |    139     |                  ~100 |
| `measurement_threshold` | 117      |                   54 |
| `temporal_window`     |     29     |                    — |
| `medication_absent`   |     25     |                    — |
| `medication_present`  |     18     |                   30 |

**Conditions are 73% of unmapped events** (500 of 689). My pre-eval
hypothesis (D-34 medications) turns out to be small-magnitude:
medications are 6% of unmapped (43 of 689). The clinical concept
gap is **conditions, then labs, then meds.**

**Exclusion-side dominance.** Within `condition_*`: 361 of 500
(72%) are `condition_absent`, i.e. exclusion criteria. Trials are
written as long lists of "exclude if patient has [X, Y, Z, ...]"
and that is exactly what overwhelms the matcher's vocab. Adding
condition coverage will yield disproportionate movement here.

## Top unmapped surface forms

The actual strings the extractor produces and the matcher can't
map. These were cherry-picked at diagnostic time and should be the
input to the next vocabulary expansion.

### Conditions (top 10 by frequency)

```
 7  intrahepatic cholangiocarcinoma
 7  target lesion that met the recist 1.1 criteria
 7  systemic treatment
 7  severe liver dysfunction (child-pugh c grade) or significant jaundice or hepatic encephalopathy
 7  severe and uncorrectable coagulation dysfunction
 7  active hepatitis or severe infection
 7  cachexia or multiple organ failure
 3  pregnant or lactating
 3  type 1 or type 2 diabetes, cardiology, masld, general gi clinic
 3  food insecurity
```

The top 7 are all from one trial repeated across 7 patients (a
single trial's exclusion list dominating). **Two distinct issues:**

1. **Real vocabulary gaps:** `intrahepatic cholangiocarcinoma`,
   `pregnant or lactating`, `active hepatitis`, `homozygous
   familial hypercholesterolemia` — these are bona fide SNOMED
   codes the matcher should know.
2. **Compound criteria the extractor crammed into `condition_text`:**
   "severe liver dysfunction (child-pugh c grade) or significant
   jaundice or hepatic encephalopathy" is not one condition. It's
   a clinical judgment with three branches. Same for "type 1 or
   type 2 diabetes, cardiology, masld, general gi clinic" (which
   isn't a condition at all — it looks like the extractor chewed
   up a cohort-description sentence).

The first class is fixed by **vocabulary expansion**; the second
is fixed by **extractor prompt discipline** (route compounds to
`free_text` rather than fake-structuring them).

### Medications (top 10)

```
 4  metformin
 2  insulin
 2  antiretroviral therapy
 2  sglt2 inhibitor
 2  systemic steroids or anti-inflammatory/immune suppressant therapies
 2  glp-1 agonist
 2  short-acting lipid-lowering therapies, pcsk9 monoclonal antibodies, oral pcsk9 inhibitors, ...
 2  lipid and tg-lowering medications
 2  hepatocyte-targeted small interfering ribonucleic acid (sirna)
 2  hepatocyte targeted sirna or antisense oligonucleotide molecule
```

`metformin`, `insulin`, `glp-1 agonist`, `sglt2 inhibitor` are the
hard-to-defend gaps — all common diabetes drugs. RxNorm-class
mapping for these would be a one-shot vocab expansion.

### Labs (top 10)

```
 7  body mass index
 7  who/ecog ps
 5  platelets
 5  creatinine clearance
 4  bmi
 4  serum potassium
 4  who performance status
 4  forced expiratory volume in 1 second (fev1)
 4  absolute neutrophil count (anc)
 4  hemoglobin
```

These are the most-egregious gaps — `BMI`, `hemoglobin`,
`platelets`, `creatinine clearance`, `ECOG performance status` are
foundational clinical-trial labs/measures. Fix these and
`measurement_threshold` indeterminacy halves.

## Investment ranking

Three concrete next plays, sorted by expected impact-per-hour:

### A. Expand the concept-lookup vocabulary  (medium, **high impact**)

Add the top ~30 conditions, top ~10 medications, top ~15 labs to
`concept_lookup.py`. Each entry is a SNOMED/RxNorm/LOINC code list
+ display name. ~2-3 hours of careful lookup against OHDSI Athena
or BIOPORTAL.

**Estimated impact:** moves 200-300 verdicts out of
`unmapped_concept`. Most should land as `pass`/`fail` because the
patient genuinely has or doesn't have the condition. Also lifts
the eligibility-rollup picture in SUMMARY.md visibly: hypertension
slice goes from 0/14 covered to plausibly 8-10/14, T2DM slice
gains a few `pass`es it currently misses on metformin/insulin.

### B. Improve the extractor prompt for compound criteria  (low-medium, medium impact)

Add a rule: "If you can't isolate a single SNOMED-grade condition
or RxNorm-grade medication, route to `free_text`. Compound clauses
joined by 'or' / 'and' belong in `free_text`." Then the LLM matcher
node gets a swing at them.

**Estimated impact:** moves ~50-100 verdicts out of
`unmapped_concept` (where they're invisibly indeterminate) into
`human_review_required` (where the LLM matcher actually tries).
Smaller magnitude but cleaner *honesty* of the system — fewer
"silent-loss" verdicts.

### C. Wire the structured age/sex fields into extraction  (small, modest impact)

The 34 missing structured-field cells in the layer-1 baseline
(`max_age` 60% missing, `min_age` 39% missing) are because the
extractor reads only the eligibility text and not the CT.gov
`min_age`/`max_age` fields. Pass those into the prompt as a hint,
or post-process to add an implicit age criterion when missing.

**Estimated impact:** lifts layer-1 coverage 55% → ~95%. Doesn't
move the rollup needle much but makes layer-1 *honest* and
unblocks claiming "100% agreement on covered cells" in a demo.

### Recommendation

**A first.** Biggest visible delta, most directly demonstrates "we
read the data, we know what's missing, we fixed it." Then **B**
because it makes the whole system more honest about what it can
and can't do (an FDE values-aligned move). **C** is optional
polish — only worth doing once we have something to claim about
agreement rates.

## Reproduction

Diagnostic was generated from the `b55783ff962f` run via
`scripts/eval.py report --run-id b55783ff962f --format json`,
parsed and re-binned in a one-off script. The numbers above will
re-derive deterministically from the same run id; if we run a new
baseline, this file should be re-snapshotted next to it.
