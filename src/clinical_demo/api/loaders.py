"""Patient + trial fetchers, cached at process scope.

These exist as a third caller of the curated-data helpers
(`scripts/score_pair.py` and `scripts/eval.py` are the other
two). Having three callers is the threshold I noted in 2.3 for
promoting them out of the script — done now, since the API
process scores many requests and re-iterating the Synthea bundle
directory per request is wasteful.

Caching is intentionally simple: dict at module scope. The API
process is short-lived and single-threaded for v0; an LRU or TTL
earns its place once we see actual memory pressure or a
re-curate flow."""

from __future__ import annotations

import json
from pathlib import Path

from ..data.clinicaltrials import trial_from_raw
from ..data.synthea import iter_bundles
from ..domain.patient import Patient
from ..domain.trial import Trial

CURATED_TRIALS_DIR = Path("data/curated/trials")
COHORT_MANIFEST = Path("data/curated/cohort_manifest.json")
EXTRACTIONS_DIR = Path("data/curated/extractions")

_patient_cache: dict[str, Patient] = {}
_trial_cache: dict[str, Trial] = {}


class CuratedDataMissing(FileNotFoundError):
    """Raised when curated artifacts the API depends on aren't on disk.

    Distinct from generic `FileNotFoundError` so the API layer can
    map it to a clean 503 — the deployment is misconfigured, not
    the request."""


def load_trial(nct_id: str) -> Trial:
    if nct_id in _trial_cache:
        return _trial_cache[nct_id]
    raw_path = CURATED_TRIALS_DIR / f"{nct_id}.json"
    if not raw_path.exists():
        raise FileNotFoundError(f"trial {nct_id!r} not found at {raw_path}")
    trial = trial_from_raw(json.loads(raw_path.read_text()))
    _trial_cache[nct_id] = trial
    return trial


def load_patient(patient_id: str) -> Patient:
    """Locate one patient by id; cache across calls.

    The first miss iterates the full Synthea bundle directory
    (slow); subsequent calls for *any* patient seen during that
    iteration are O(1) because we populate the cache eagerly."""
    if patient_id in _patient_cache:
        return _patient_cache[patient_id]
    if not COHORT_MANIFEST.exists():
        raise CuratedDataMissing(
            f"Cohort manifest not found at {COHORT_MANIFEST}; run "
            f"`uv run python scripts/curate_cohort.py` first."
        )
    cohort = json.loads(COHORT_MANIFEST.read_text())
    synthea_dir = Path(cohort["synthea_dir"])
    for patient in iter_bundles(synthea_dir):
        _patient_cache[patient.patient_id] = patient
        if patient.patient_id == patient_id:
            return patient
    raise FileNotFoundError(f"patient_id {patient_id!r} not found under {synthea_dir}.")


def list_patients() -> list[dict]:
    """Return the cohort manifest's member rows (id + score + slice).

    Reads the manifest only — does not parse FHIR bundles, so it's
    cheap to call from a UI listing endpoint."""
    if not COHORT_MANIFEST.exists():
        raise CuratedDataMissing(f"Cohort manifest not found at {COHORT_MANIFEST}")
    cohort = json.loads(COHORT_MANIFEST.read_text())
    return list(cohort.get("members", []))


def list_trials() -> list[dict]:
    """Return one row per curated trial: nct_id + a brief title.

    Title is best-effort: read the raw JSON, pluck the brief title
    if present, fall back to nct_id. Cheap and forgiving."""
    if not CURATED_TRIALS_DIR.exists():
        raise CuratedDataMissing(f"Curated trials directory missing at {CURATED_TRIALS_DIR}")
    out: list[dict] = []
    for path in sorted(CURATED_TRIALS_DIR.glob("*.json")):
        nct_id = path.stem
        try:
            raw = json.loads(path.read_text())
            title = (
                raw.get("protocolSection", {}).get("identificationModule", {}).get("briefTitle")
                or nct_id
            )
        except Exception:
            title = nct_id
        out.append({"nct_id": nct_id, "title": title})
    return out


def reset_caches() -> None:
    """Test hook — clear the module-level caches."""
    _patient_cache.clear()
    _trial_cache.clear()


__all__ = [
    "COHORT_MANIFEST",
    "CURATED_TRIALS_DIR",
    "EXTRACTIONS_DIR",
    "CuratedDataMissing",
    "list_patients",
    "list_trials",
    "load_patient",
    "load_trial",
    "reset_caches",
]
