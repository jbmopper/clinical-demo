"""Exploratory tour of the loaded Synthea cohort.

Run with:  uv run marimo edit marimo/explore_synthea.py

Assumes the Synthea sample data has been unzipped under
data/raw/synthea/fhir/ (see PLAN.md Phase 1 task 1.2).
"""

from __future__ import annotations

import marimo

__generated_with = "0.9.0"
app = marimo.App(width="medium")


@app.cell
def _() -> tuple:
    from collections import Counter
    from pathlib import Path

    from clinical_demo.data.synthea import iter_bundles

    return Counter, Path, iter_bundles


@app.cell
def _(Path):
    fhir_dir = Path("data/raw/synthea/fhir")
    files = sorted(fhir_dir.glob("*.json")) if fhir_dir.exists() else []
    print(f"{len(files)} bundles found at {fhir_dir.resolve() if files else fhir_dir}")
    return (files,)


@app.cell
def _(files, iter_bundles):
    sample = list(iter_bundles(files[0].parent)) if files else []
    print(f"loaded {len(sample)} patients")
    return (sample,)


@app.cell
def _(Counter, sample):
    sex_dist = Counter(p.sex for p in sample)
    print("sex distribution:", dict(sex_dist))
    return


@app.cell
def _(sample):
    from datetime import date

    today = date.today()
    ages = [p.age_years(today) for p in sample]
    if ages:
        print(f"age range: {min(ages)}-{max(ages)}, mean ~{sum(ages) // len(ages)}")
    return (today,)


@app.cell
def _(Counter, sample):
    cond_counts = Counter(
        c.concept.display
        for p in sample
        for c in p.conditions
        if c.is_clinical and c.concept.display
    )
    print("top 20 clinical conditions across cohort:")
    for name, n in cond_counts.most_common(20):
        print(f"  {n:5d}  {name}")
    return


@app.cell
def _(sample, today):
    cardiometabolic_snomed = {
        "44054006",  # T2DM
        "15777000",  # Prediabetes
        "73211009",  # Diabetes mellitus (unspecified)
        "59621000",  # Essential hypertension
        "38341003",  # Hypertensive disorder
        "55822004",  # Hyperlipidemia
        "267432004",  # Pure hypercholesterolemia
    }
    cardiometabolic = [
        p
        for p in sample
        if any(c.concept.code in cardiometabolic_snomed for c in p.active_conditions(today))
    ]
    print(f"{len(cardiometabolic)} of {len(sample)} patients have a cardiometabolic condition")
    return (cardiometabolic,)


if __name__ == "__main__":
    app.run()
