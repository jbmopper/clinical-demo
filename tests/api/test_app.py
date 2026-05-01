"""Smoke tests for the FastAPI surface.

We monkeypatch the loader entry points and the scorer, so the
tests exercise the API plumbing (routing, request validation,
response serialization, error mapping) without needing a curated
cohort on disk.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clinical_demo.api import app as api_app
from clinical_demo.api import create_app
from clinical_demo.api import loaders as api_loaders
from clinical_demo.domain.patient import Patient
from clinical_demo.domain.trial import Trial
from clinical_demo.evals.run import CaseRecord, EvalCase, RunResult
from clinical_demo.evals.store import open_store, save_run
from tests.evals._fixtures import make_age_verdict, make_score_pair_result


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


@pytest.fixture
def stub_patient() -> Patient:
    return Patient(
        patient_id="P-1",
        birth_date=date(1980, 1, 1),
        sex="male",
        conditions=[],
    )


@pytest.fixture
def stub_trial() -> Trial:
    return Trial(
        nct_id="NCT00000001",
        title="Stub trial",
        overall_status="RECRUITING",
        sponsor_name="Stub Sponsor",
        sponsor_class="OTHER",
        eligibility_text="adult patients",
        minimum_age="18 Years",
        maximum_age=None,
        sex="ALL",
        healthy_volunteers=False,
    )


# ---------------- meta + catalog


def test_health_returns_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_patients_returns_manifest_rows(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        api_app,
        "list_patients",
        lambda: [{"patient_id": "P-1", "score": 5, "slice": "diabetes"}],
    )
    response = client.get("/patients")
    assert response.status_code == 200
    assert response.json() == [{"patient_id": "P-1", "score": 5, "slice": "diabetes"}]


def test_patients_503_when_curated_data_missing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _missing() -> list[dict]:
        raise api_loaders.CuratedDataMissing("manifest gone")

    monkeypatch.setattr(api_app, "list_patients", _missing)
    response = client.get("/patients")
    assert response.status_code == 503
    assert "manifest gone" in response.json()["detail"]


def test_trials_returns_rows(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        api_app,
        "list_trials",
        lambda: [{"nct_id": "NCT00000001", "title": "Stub trial"}],
    )
    response = client.get("/trials")
    assert response.status_code == 200
    assert response.json() == [{"nct_id": "NCT00000001", "title": "Stub trial"}]


# ---------------- eval calibration


def test_eval_runs_lists_persisted_runs(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runs.sqlite"
    _save_stub_run(db_path)
    monkeypatch.setattr(api_app, "DEFAULT_EVAL_DB", db_path)

    response = client.get("/eval/runs")

    assert response.status_code == 200
    body = response.json()
    assert body[0]["run_id"] == "run-1"
    assert body[0]["n_cases"] == 1


def test_layer3_calibration_returns_rows_with_existing_labels(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runs.sqlite"
    labels_path = tmp_path / "labels.json"
    _save_stub_run(db_path)
    labels_path.write_text(
        '[{"pair_id":"p1__T1","criterion_index":0,"label":"correct","rationale":"ok"}]\n'
    )
    monkeypatch.setattr(api_app, "DEFAULT_EVAL_DB", db_path)
    monkeypatch.setattr(api_app, "DEFAULT_LAYER3_LABELS", labels_path)

    response = client.get("/layer3/calibration", params={"run_id": "run-1", "limit": 1})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["label_path"] == str(labels_path)
    assert body["rows"][0]["pair_id"] == "p1__T1"
    assert body["rows"][0]["matcher_verdict"] == "pass"
    assert body["rows"][0]["existing_label"]["label"] == "correct"


def test_layer3_calibration_save_merges_labels(
    client: TestClient,
    tmp_path: Path,
) -> None:
    labels_path = tmp_path / "labels.json"
    labels_path.write_text(
        '[{"pair_id":"keep","criterion_index":1,"label":"incorrect","rationale":"keep"}]\n'
    )

    response = client.post(
        "/layer3/calibration",
        json={
            "label_path": str(labels_path),
            "labels": [
                {
                    "pair_id": "p1__T1",
                    "criterion_index": 0,
                    "label": "correct",
                    "rationale": "supported",
                }
            ],
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["saved"] == 2
    saved = labels_path.read_text()
    assert '"pair_id": "keep"' in saved
    assert '"pair_id": "p1__T1"' in saved


# ---------------- /score happy path


def test_score_imperative_round_trips_score_pair_result(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    stub_patient: Patient,
    stub_trial: Trial,
) -> None:
    seen: dict[str, object] = {}

    def _stub_score_pair(patient, trial, as_of, *, extraction=None):
        seen["patient_id"] = patient.patient_id
        seen["nct_id"] = trial.nct_id
        seen["as_of"] = as_of
        seen["extraction"] = extraction
        return make_score_pair_result(patient_id=patient.patient_id, nct_id=trial.nct_id)

    monkeypatch.setattr(api_app, "load_patient", lambda _: stub_patient)
    monkeypatch.setattr(api_app, "load_trial", lambda _: stub_trial)
    monkeypatch.setattr(api_app, "score_pair", _stub_score_pair)

    response = client.post(
        "/score",
        json={
            "patient_id": "P-1",
            "nct_id": "NCT00000001",
            "as_of": "2025-01-01",
            "use_cached_extraction": False,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["patient_id"] == "P-1"
    assert body["nct_id"] == "NCT00000001"
    assert body["eligibility"] in {"pass", "fail", "indeterminate"}
    assert seen["as_of"] == date(2025, 1, 1)
    assert seen["extraction"] is None


def test_score_defaults_as_of_to_today(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    stub_patient: Patient,
    stub_trial: Trial,
) -> None:
    seen: dict[str, object] = {}

    def _stub_score_pair(patient, trial, as_of, *, extraction=None):
        seen["as_of"] = as_of
        return make_score_pair_result()

    monkeypatch.setattr(api_app, "load_patient", lambda _: stub_patient)
    monkeypatch.setattr(api_app, "load_trial", lambda _: stub_trial)
    monkeypatch.setattr(api_app, "score_pair", _stub_score_pair)

    response = client.post(
        "/score",
        json={
            "patient_id": "P-1",
            "nct_id": "NCT00000001",
            "use_cached_extraction": False,
        },
    )
    assert response.status_code == 200
    assert seen["as_of"] == date.today()


# ---------------- /score error mapping


def test_score_404_when_patient_unknown(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    stub_trial: Trial,
) -> None:
    def _missing(_id: str) -> Patient:
        raise FileNotFoundError("no such patient")

    monkeypatch.setattr(api_app, "load_patient", _missing)
    monkeypatch.setattr(api_app, "load_trial", lambda _: stub_trial)
    response = client.post(
        "/score",
        json={"patient_id": "P-X", "nct_id": "NCT00000001"},
    )
    assert response.status_code == 404
    assert "no such patient" in response.json()["detail"]


def test_score_404_when_trial_unknown(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    stub_patient: Patient,
) -> None:
    def _missing(_id: str) -> Trial:
        raise FileNotFoundError("no such trial")

    monkeypatch.setattr(api_app, "load_patient", lambda _: stub_patient)
    monkeypatch.setattr(api_app, "load_trial", _missing)
    response = client.post(
        "/score",
        json={"patient_id": "P-1", "nct_id": "NCT99999999"},
    )
    assert response.status_code == 404


def test_score_503_when_curated_data_missing(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    stub_trial: Trial,
) -> None:
    def _missing(_id: str) -> Patient:
        raise api_loaders.CuratedDataMissing("cohort manifest absent")

    monkeypatch.setattr(api_app, "load_patient", _missing)
    monkeypatch.setattr(api_app, "load_trial", lambda _: stub_trial)
    response = client.post(
        "/score",
        json={"patient_id": "P-1", "nct_id": "NCT00000001"},
    )
    assert response.status_code == 503


def test_score_500_when_scorer_raises(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    stub_patient: Patient,
    stub_trial: Trial,
) -> None:
    def _boom(*args, **kwargs):
        raise RuntimeError("scorer exploded")

    monkeypatch.setattr(api_app, "load_patient", lambda _: stub_patient)
    monkeypatch.setattr(api_app, "load_trial", lambda _: stub_trial)
    monkeypatch.setattr(api_app, "score_pair", _boom)

    response = client.post(
        "/score",
        json={
            "patient_id": "P-1",
            "nct_id": "NCT00000001",
            "use_cached_extraction": False,
        },
    )
    assert response.status_code == 500
    assert "scorer exploded" in response.json()["detail"]


def test_score_400_on_missing_required_field(client: TestClient) -> None:
    response = client.post("/score", json={"patient_id": "P-1"})
    assert response.status_code == 422


# ---------------- orchestrator switch


def test_score_dispatches_to_graph_when_requested(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    stub_patient: Patient,
    stub_trial: Trial,
) -> None:
    """The route should call score_pair_graph (not score_pair) when
    `orchestrator='graph'`. We swap the import target and assert it
    fired."""
    seen: dict[str, object] = {}

    def _stub_graph(patient, trial, as_of, *, extraction=None, critic_enabled=False):
        seen["called"] = True
        seen["critic_enabled"] = critic_enabled
        return make_score_pair_result(patient_id=patient.patient_id, nct_id=trial.nct_id)

    import clinical_demo.graph as graph_pkg

    monkeypatch.setattr(api_app, "load_patient", lambda _: stub_patient)
    monkeypatch.setattr(api_app, "load_trial", lambda _: stub_trial)
    monkeypatch.setattr(graph_pkg, "score_pair_graph", _stub_graph)

    response = client.post(
        "/score",
        json={
            "patient_id": "P-1",
            "nct_id": "NCT00000001",
            "orchestrator": "graph",
            "critic_enabled": True,
            "use_cached_extraction": False,
        },
    )
    assert response.status_code == 200, response.text
    assert seen == {"called": True, "critic_enabled": True}


def _save_stub_run(db_path: Path) -> None:
    case = EvalCase(
        pair_id="p1__T1",
        patient_id="p1",
        nct_id="T1",
        as_of=date(2025, 1, 1),
        slice="test",
    )
    run = RunResult(
        run_id="run-1",
        started_at=datetime(2025, 1, 1, 0, 0, 0),
        finished_at=datetime(2025, 1, 1, 0, 0, 1),
        dataset_path="seed.json",
        notes="api calibration test",
        cases=[
            CaseRecord(
                case=case,
                result=make_score_pair_result(
                    patient_id="p1",
                    nct_id="T1",
                    verdicts=[make_age_verdict()],
                ),
                scoring_latency_ms=1.0,
            )
        ],
    )
    with open_store(db_path) as conn:
        save_run(conn, run)
