# D-69 Slice 5 — Two-Pass Terminology Eval

Slice-5 rerun against the 2026-04-21 D-68 baseline, using the
imperative scorer, cached `extractor-v0.2` extractions, and
`binding_strategy="two_pass"`.

## Provenance

- **Run id:** `98568ccd090d`
- **Command:** `uv run python scripts/eval.py run --no-llm --binding-strategy two_pass`
- **Dataset:** `data/curated/eval_seed.json`
- **Cases:** 49, 0 errors
- **Extraction cache:** 30 trials freshly cached before the run
- **Extraction cost:** $0.0750 to populate cache; cached eval itself reused those files
- **Scoring latency:** 13.5s total, 275ms/case average

## Headline

| Metric | D-68 baseline | Slice 5 | Delta |
|---|---:|---:|---:|
| total per-criterion verdicts | 841 | 1086 | +245 |
| `unmapped_concept` count | 689 | 660 | -29 |
| `unmapped_concept` rate | 81.9% | 60.8% | -21.2 pp |
| `indeterminate` count | 772 | 1016 | +244 |
| `indeterminate` rate | 91.8% | 93.6% | +1.8 pp |
| layer-1 agreement | 81.0% | 88.3% | +7.4 pp |
| layer-1 coverage | 55.3% | 98.7% | +43.5 pp |

Read the denominator shift honestly: Rule 13 moved many compound
criteria from fake structured rows into `free_text`, so total criteria
increased from 841 to 1086 and `human_review_required` rose sharply.
That is expected and desirable for honesty, but it means count deltas
and rate deltas tell different stories.

## What Moved

- Structured-field enrichment worked: layer-1 coverage is now 98.7%
  with one remaining max-age miss.
- `unmapped_concept` count fell only 29, but rate fell 21.2 points
  because the extractor now emits substantially more reviewable
  `free_text` rows instead of compound fake-structured rows.
- `human_review_required` rose from 50 to 296, which is the Rule-13
  honesty trade: fewer silent vocabulary failures, more explicit human
  review / LLM-matcher candidates.
- Registered terminology surfaces observed in the run all resolved:
  26/26 total, split condition=11, lab=14, medication=1. This is a
  binding-resolution outcome, not a clinical precision claim; true
  precision still needs manual review of the moved criteria.

## Remaining Gaps

Top current unmapped surfaces are now dominated by non-cardiometabolic
or not-yet-bound trial measures:

- `body mass index`
- `hemoglobin`
- `platelet count`
- `pregnancy or breastfeeding`
- pulmonary hypertension measures and exclusions
- `liver and kidney function tests`

The next registry expansion should be evidence-led from this report:
BMI, common CBC labs, pregnancy/breastfeeding, and selected pulmonary
hypertension concepts now look more valuable than further diabetes-only
surface work.

## Files

- `slice5_two_pass_diagnostics.txt` — text report with D-68 deltas.
- `slice5_two_pass_diagnostics.json` — machine-readable diagnostics.
- `slice5_two_pass_layer1.txt` — layer-1 text report.
- `slice5_two_pass_layer1.json` — machine-readable layer-1 report.
