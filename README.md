# clinical-demo

Clinical Trial Eligibility Co-Pilot — a portfolio demo built for a Generative
AI Forward Deployed Engineer interview.

> **Status: scaffolding.** No working pipeline yet.

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
uv run marimo edit marimo/explore_synthea.py  # exploratory notebook
```

## Data

Source data is gitignored under `data/raw/`. Download the Synthea FHIR R4
sample (PLAN.md §4):

```bash
mkdir -p data/raw/synthea && cd data/raw/synthea
curl -sL -o synthea.zip 'https://raw.githubusercontent.com/synthetichealth/synthea-sample-data/main/downloads/synthea_sample_data_fhir_r4_nov2021.zip'
unzip -q synthea.zip   # creates ./fhir/ with ~557 patient bundles
```

## License

MIT — see [`LICENSE`](./LICENSE).
