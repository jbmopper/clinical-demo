# Synthea FHIR Generation Research

Last researched: 2026-04-30

## Why This Matters

This project currently uses the November 2021 Synthea FHIR R4 sample under
`data/raw/synthea/fhir/`, then curates a 150-patient adult cardiometabolic
cohort for trial matching. That sample is good enough for early loader,
matcher, and UI work, but it is stale relative to the Synthea generator and
too small to reliably produce aligned positive eligibility examples.

The next data milestone should be a reproducible generated population, not an
ad hoc replacement of the sample files. The generation recipe should preserve
the current loader contract: one FHIR R4 `Bundle` JSON file per patient.

## Current Project Assumptions

- Loader input is per-patient FHIR R4 JSON bundles, not Bulk FHIR NDJSON.
- Cohort curation expects `data/raw/synthea/fhir/` unless `scripts/curate_cohort.py`
  is pointed somewhere else.
- The scoring reference date is fixed at `2025-01-01`.
- The useful domain is adult cardiometabolic disease: type 2 diabetes,
  hypertension, hyperlipidemia, related CKD, labs, vitals, and medications.
- The current cohort policy keeps adults aged 18-95, scores multi-condition
  cardiometabolic overlap higher, and records availability for HbA1c, LDL,
  eGFR, and systolic BP.

## Synthea Generation Process

Synthea can be run either from the binary distribution:

```bash
java -jar synthea-with-dependencies.jar [options] [state [city]]
```

or from a developer checkout:

```bash
./run_synthea [options] [state [city]]
```

Useful command-line controls:

- `-s <seed>`: deterministic population seed for reproducibility.
- `-cs <seed>`: deterministic clinician/provider seed.
- `-p <count>`: number of living patients to generate.
- `-r YYYYMMDD`: reference date for the run.
- `-a min-max`: age range.
- `-c <file>`: local Synthea properties override file.
- `-k <module>`: keep-patients filter module.
- `--setting=value`: override any Synthea property from the command line.

Useful FHIR/export settings:

- `exporter.fhir.export=true`: emit FHIR R4.
- `exporter.fhir.bulk_data=false`: keep per-patient bundles instead of NDJSON.
- `exporter.fhir.transaction_bundle=true`: emit transaction bundles.
- `exporter.fhir.use_us_core_ig=true`: emit US Core-shaped R4 resources.
- `exporter.fhir.us_core_version=6.1.0`: match the current Synthea default family
  used by the sample-like output.
- `exporter.years_of_history=0`: retain full longitudinal history.
- `exporter.baseDirectory=...`: write output somewhere explicit.

If importing the generated transaction bundles into a FHIR server, also export
and load practitioner and hospital bundles first:

- `exporter.practitioner.fhir.export=true`
- `exporter.hospital.fhir.export=true`

For this repo's direct file parsing, those supporting bundles are not required,
but leaving them enabled is useful if the same generated dataset later backs a
FHIR-server integration demo.

## Recommended Baseline Run

Generate a broad adult Massachusetts population first. This keeps realistic
background comorbidities and avoids relying on module filters.

```bash
java -jar synthea-with-dependencies.jar \
  -s 20260430 \
  -cs 20260430 \
  -p 5000 \
  -a 18-95 \
  -r 20250101 \
  --exporter.baseDirectory=./data/raw/synthea/generated-v4 \
  --exporter.fhir.export=true \
  --exporter.fhir.bulk_data=false \
  --exporter.fhir.transaction_bundle=true \
  --exporter.fhir.use_us_core_ig=true \
  --exporter.fhir.us_core_version=6.1.0 \
  --exporter.years_of_history=0 \
  Massachusetts
```

Rationale:

- `-r 20250101` aligns generated records with the project's `as_of` date.
- `-a 18-95` matches cohort curation and avoids pediatric patients that cannot
  satisfy adult chronic-disease criteria.
- `-p 5000` gives enough pool depth for diabetes, hypertension, lipid disorder,
  and CKD-ish cases without turning local iteration into a large-data project.
- `exporter.years_of_history=0` prevents old labs, encounters, or diagnosis
  context from being filtered out before the matcher can inspect them.
- Per-patient transaction bundles keep compatibility with `clinical_demo.data.synthea`.

After generation, either copy/symlink the chosen `output/fhir` directory to
`data/raw/synthea/fhir`, or update `scripts/curate_cohort.py` to read the new
directory. Then run:

```bash
uv run python scripts/curate_cohort.py
uv run marimo edit marimo/explore_synthea.py
```

## Enriched Cardiometabolic Run

The baseline run is useful for realism, but it may still produce too few
patients who satisfy full real trial criteria. For demo and eval coverage,
create a second enriched batch using a keep-patients module.

Do not use `-m metabolic*` as the main strategy. Synthea's module filter
excludes modules; it does not force disease onset. It can also remove module
dependencies that generate vitals, labs, medications, or comorbidities.

Use `-k keep-cardiometabolic.json` with an adult age range. The keep module
should only decide whether a fully generated candidate patient is retained.
Good initial keep criteria:

- Active type 2 diabetes: SNOMED `44054006`.
- Active diabetes mellitus: SNOMED `73211009`.
- Active essential hypertension: SNOMED `59621000`.
- Active hypertensive disorder: SNOMED `38341003`.
- Active hyperlipidemia: SNOMED `55822004`.
- Active pure hypercholesterolemia: SNOMED `267432004`.
- Optional later: CKD codes, if a broad run shows enough patients to slice.

For stricter positive examples, use a keep module that requires combinations,
for example diabetes AND hypertension, or diabetes AND an HbA1c observation.
If the filter is rare, raise:

```bash
--generate.max_attempts_to_keep_patient=10000
```

Avoid impossible filters. For example, an all-ages run with a diabetes-only
keep filter may waste many attempts on children who cannot satisfy the module.

## Project-Specific Follow-Up Work

1. Add generation manifests.

   Each generated dataset should persist the Synthea version, command, seed,
   reference date, config overrides, output directory, and post-generation
   curation results. Raw bundles are gitignored, so the manifest is the durable
   reproducibility contract.

2. Parameterize Synthea input paths.

   `scripts/curate_cohort.py` currently hardcodes `data/raw/synthea/fhir`.
   Add a CLI flag or config setting so the repo can compare the Nov 2021 sample,
   a broad v4 generated population, and an enriched cardiometabolic population.

3. Build a robust generated cohort.

   Regenerate at least one broad adult population and one enriched adult
   cardiometabolic population. Re-run the cohort manifest, trial-pair selection,
   and baseline evals against both.

4. Create positive and near-miss eval pairs.

   The current eval baseline has zero full eligibility passes across 49 pairs.
   Keep realistic fail/indeterminate cases, but add deliberate positive and
   near-miss cases so the demo exercises pass, fail, and missing-data behavior.

5. Add synthetic-data gap fixtures.

   Preserve clean Synthea records for deterministic tests, but add perturbed
   FHIR fixtures that mimic real exports: local codes, missing displays,
   multiple codings, unit drift, stale meds, duplicated conditions, missing
   effective dates, note-vs-structured contradictions, and partial history.

6. Decide how to model out-of-Synthea facts.

   Oncology staging, histology, biomarkers, prior therapy lines, ECOG,
   pregnancy status, smoking history, and clinician notes are shallow or absent
   in the cardiometabolic Synthea data. Either hand-author supplemental profiles
   or add narrow synthetic fixtures rather than pretending Synthea covers them.

## Where Synthea Diverges From Real Patient Files

Synthea is useful because it is safe, parseable, and repeatable. It is not a
drop-in substitute for real EHR exports.

Important divergences:

- Clinical histories are generated by simulator modules and public statistics,
  not by observed patient-level data.
- Care is often cleaner and more guideline-shaped than real care, with less
  unexplained nonadherence, undertreatment, loss to follow-up, or outcome
  heterogeneity.
- Comorbidity correlations can be weaker or more hand-authored than real
  chronic-disease cascades.
- Coding is unusually consistent: SNOMED, LOINC, RxNorm, and US Core profiles
  appear cleaner than many production FHIR feeds.
- Real records often include local lab codes, ICD-only diagnoses, multiple
  codings, missing displays, invalid units, duplicated resources, stale problem
  lists, and source-system-specific extensions.
- Narrative evidence is thin. Real screening often depends on notes, imaging,
  pathology, procedures, clinician assessment, and temporally ambiguous text.
- Missingness is unrealistic. Real data can be absent because of referral
  boundaries, failed interfaces, outside labs, patient behavior, or chart
  migration, not just because the simulator did not emit an event.
- The `exporter.years_of_history` setting is not a strict window. Synthea can
  retain clinically active concepts outside the cutoff, and strict time windows
  require post-processing.
- Generated demographics, names, addresses, identifiers, providers, and
  utilization are safe for demos but should not support claims about local
  prevalence or operational performance.

## Research References

- Synthea Basic Setup and Running: https://github.com/synthetichealth/synthea/wiki/Basic-Setup-and-Running
- Synthea Common Configuration: https://github.com/synthetichealth/synthea/wiki/Common-Configuration
- Synthea FHIR Transaction Bundles: https://github.com/synthetichealth/synthea/wiki/FHIR-Transaction-Bundles
- Synthea Keep Patients Module: https://github.com/synthetichealth/synthea/wiki/Keep-Patients-Module
- Synthea v4.0.0 release: https://github.com/synthetichealth/synthea/releases/tag/v4.0.0
- Synthea quality-measure validation study: https://pmc.ncbi.nlm.nih.gov/articles/PMC6416981/
