"""Exploratory tour of the Chia corpus.

Run with:  uv run marimo edit marimo/explore_chia.py

Assumes the Chia corpus has been unzipped under data/raw/chia/
(see PLAN.md Phase 1 task 1.5; see README.md for the download step).
"""

import marimo

__generated_with = "0.9.0"
app = marimo.App(width="medium")


@app.cell
def _() -> tuple:
    from collections import Counter
    from pathlib import Path

    from clinical_demo.data.chia import iter_trials

    repo_root = Path(__file__).resolve().parents[1]
    return Counter, Path, iter_trials, repo_root


@app.cell
def _(iter_trials, repo_root):
    chia_dir = repo_root / "data/raw/chia"
    trials = list(iter_trials(chia_dir)) if chia_dir.exists() else []
    print(f"loaded {len(trials)} trials from {chia_dir}")
    if not trials:
        print("expected BRAT files like NCTxxxx_inc.txt and NCTxxxx_inc.ann under data/raw/chia")
    return (trials,)


@app.cell
def _(trials):
    n_with_inc = sum(1 for t in trials if t.inclusion is not None)
    n_with_exc = sum(1 for t in trials if t.exclusion is not None)
    print(f"trials with inclusion doc: {n_with_inc}")
    print(f"trials with exclusion doc: {n_with_exc}")
    return


@app.cell
def _(trials):
    docs = [d for t in trials for d in (t.inclusion, t.exclusion) if d is not None]
    n_ent = sum(len(d.entities) for d in docs)
    n_rel = sum(len(d.relations) for d in docs)
    n_eq = sum(len(d.equivalence_groups) for d in docs)
    n_attr = sum(len(d.attributes) for d in docs)
    n_disc = sum(1 for d in docs for e in d.entities.values() if len(e.spans) > 1)
    print(f"docs={len(docs)}  entities={n_ent}  relations={n_rel}  equiv={n_eq}  attrs={n_attr}")
    print(f"discontinuous-span entities: {n_disc}  ({n_disc / max(n_ent, 1):.1%})")
    return (docs,)


@app.cell
def _(Counter, docs):
    ent_types = Counter(e.type for d in docs for e in d.entities.values())
    print(f"distinct entity types: {len(ent_types)}")
    for _name, _n in ent_types.most_common():
        print(f"  {_n:6d}  {_name}")
    return


@app.cell
def _(Counter, docs):
    rel_types = Counter(r.type for d in docs for r in d.relations)
    print(f"distinct relation types: {len(rel_types)}")
    for _name, _n in rel_types.most_common():
        print(f"  {_n:6d}  {_name}")
    return


@app.cell
def _(Counter, docs):
    eq_types = Counter(g.type for d in docs for g in d.equivalence_groups)
    eq_arity = Counter(len(g.member_ids) for d in docs for g in d.equivalence_groups)
    print(f"equivalence types: {dict(eq_types)}")
    print("equivalence-group arity distribution:")
    for _k in sorted(eq_arity):
        print(f"  {_k}-way: {eq_arity[_k]}")
    return


@app.cell
def _(trials):
    # Inspect one trial in detail.
    if not trials:
        print("no Chia trials loaded, so there is no example trial to inspect")
    else:
        t = trials[0]
        print(f"trial: {t.nct_id}")
        print(f"\n--- inclusion source text ---\n{t.inclusion.source_text[:400]}")
        print("\n--- first 5 inclusion entities ---")
        for _e in list(t.inclusion.entities.values())[:5]:
            _spans = ", ".join(f"{_s.start}:{_s.end}" for _s in _e.spans)
            print(f"  {_e.id}  {_e.type:14s}  [{_spans}]  {_e.text!r}")
    return


if __name__ == "__main__":
    app.run()
