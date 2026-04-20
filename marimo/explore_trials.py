"""Exploratory tour of the curated CT.gov trial set.

Run with:  uv run marimo edit marimo/explore_trials.py

Assumes the curated trials have been pulled to data/curated/trials/
(see PLAN.md Phase 1 task 1.4; run scripts/curate_trials.py).
"""

from __future__ import annotations

import marimo

__generated_with = "0.9.0"
app = marimo.App(width="medium")


@app.cell
def _() -> tuple:
    import json
    from collections import Counter
    from pathlib import Path

    from clinical_demo.data.clinicaltrials import trial_from_raw

    return Counter, Path, json, trial_from_raw


@app.cell
def _(Path, json):
    manifest_path = Path("data/curated/trials_manifest.json")
    manifest = json.loads(manifest_path.read_text())["trials"] if manifest_path.exists() else []
    print(f"manifest: {len(manifest)} trials")
    return (manifest,)


@app.cell
def _(Path, json, trial_from_raw):
    trial_dir = Path("data/curated/trials")
    files = sorted(trial_dir.glob("*.json")) if trial_dir.exists() else []
    trials = [trial_from_raw(json.loads(f.read_text())) for f in files]
    print(f"loaded {len(trials)} trials from {trial_dir}")
    return (trials,)


@app.cell
def _(Counter, trials):
    sponsors = Counter(t.sponsor_class for t in trials)
    print("sponsor class:", dict(sponsors))
    return


@app.cell
def _(Counter, trials):
    phases = Counter(tuple(t.phase) for t in trials)
    print("phase combinations:")
    for _p, _n in phases.most_common():
        print(f"  {_n:3d}  {_p}")
    return


@app.cell
def _(trials):
    sizes = sorted((len(t.eligibility_text), t.nct_id) for t in trials)
    print("eligibility text size (sorted):")
    for _n, _nct in sizes:
        print(f"  {_n:5d} chars  {_nct}")
    return


@app.cell
def _(Counter, trials):
    conditions = Counter(c for t in trials for c in t.conditions)
    print("top 20 condition labels across the curated set:")
    for _name, _n in conditions.most_common(20):
        print(f"  {_n:3d}  {_name}")
    return


@app.cell
def _(trials):
    longest = max(trials, key=lambda t: len(t.eligibility_text))
    print(f"longest criteria: {longest.nct_id}  {longest.title}")
    print(f"({len(longest.eligibility_text)} chars)\n")
    print(longest.eligibility_text[:1500])
    print("\n... [truncated]")
    return


if __name__ == "__main__":
    app.run()
