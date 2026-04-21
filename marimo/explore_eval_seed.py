"""Quick tour of the eval seed-set.

Run with:  uv run marimo edit marimo/explore_eval_seed.py

Assumes the seed manifest exists at data/curated/eval_seed.json (built
via scripts/build_eval_seed.py).
"""

import marimo

__generated_with = "0.9.0"
app = marimo.App(width="medium")


@app.cell
def _() -> tuple:
    import json
    from collections import Counter
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[1]
    return Counter, Path, json, repo_root


@app.cell
def _(json, repo_root):
    seed_path = repo_root / "data/curated/eval_seed.json"
    seed = json.loads(seed_path.read_text()) if seed_path.exists() else {}
    print(f"loaded seed manifest: {len(seed.get('pairs', []))} pairs from {seed_path}")
    if not seed:
        print("run scripts/build_eval_seed.py if this manifest has not been built yet")
    return (seed,)


@app.cell
def _(seed):
    policy = seed.get("selection_policy", {})
    if not policy:
        print("no selection policy found in seed manifest")
    else:
        for _k, _v in policy.items():
            print(f"  {_k:42s}  {_v}")
    return


@app.cell
def _(Counter, seed):
    pairs = seed.get("pairs", [])
    if not pairs:
        print("no eval pairs found in seed manifest")
    else:
        by_slice = Counter(p["slice"] for p in pairs)
        print("pairs by slice:")
        for _s in sorted(by_slice):
            print(f"  {_s:24s}  {by_slice[_s]}")
    return (pairs,)


@app.cell
def _(Counter, pairs):
    if not pairs:
        print("no structured verdicts to summarize")
    else:
        by_verdict = Counter(v["verdict"] for p in pairs for v in p["structured_verdicts"])
        by_field = Counter(v["criterion"]["field"] for p in pairs for v in p["structured_verdicts"])
        print("mechanical verdicts:")
        for _k in ("pass", "fail", "indeterminate"):
            print(f"  {_k:16s}  {by_verdict.get(_k, 0)}")
        print()
        print("by criterion field:")
        for _k, _n in by_field.most_common():
            print(f"  {_k:24s}  {_n}")
    return


@app.cell
def _(pairs):
    if not pairs:
        print("no free-text review summary available")
    else:
        free_total = sum(p["free_text_criteria_count"] for p in pairs)
        pending = sum(1 for p in pairs if p["free_text_review_status"] == "pending")
        print(f"free-text criteria pending human review: {free_total}")
        print(f"pairs marked complete: {len(pairs) - pending} / {len(pairs)}")
    return


@app.cell
def _(pairs):
    if not pairs:
        print("no patient/trial diversity summary available")
    else:
        distinct_patients = len({p["patient_id"] for p in pairs})
        distinct_trials = len({p["nct_id"] for p in pairs})
        print(f"distinct patients: {distinct_patients}")
        print(f"distinct trials:  {distinct_trials}")
    return


@app.cell
def _(pairs):
    if not pairs:
        print("no example pair available")
    else:
        print("--- one example pair ---")
        p = pairs[0]
        print(f"pair_id: {p['pair_id']}  slice: {p['slice']}")
        print(f"free-text criteria pending: {p['free_text_criteria_count']}")
        for _v in p["structured_verdicts"]:
            print(f"  {_v['criterion']['field']:18s}  {_v['verdict']:14s}  {_v['rationale']}")
    return


if __name__ == "__main__":
    app.run()
