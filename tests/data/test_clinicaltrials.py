"""Tests for the ClinicalTrials.gov client and Trial domain mapping.

Network is never touched: the client is constructed with a custom
`httpx.Client` backed by a `MockTransport`, which lets us assert on
requests *and* control responses.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from clinical_demo.data.clinicaltrials import (
    API_BASE,
    ClinicalTrialsClient,
    build_search_params,
    trial_from_raw,
)
from clinical_demo.domain import Trial

FIXTURE = Path(__file__).parent.parent / "fixtures" / "clinicaltrials" / "sample_trial.json"


@pytest.fixture
def sample_raw() -> dict[str, Any]:
    with FIXTURE.open() as f:
        return json.load(f)


def _client_with_responses(responses: list[httpx.Response]) -> ClinicalTrialsClient:
    """Return a CT.gov client whose transport replays the given responses in order.

    Each request consumes one response; if the client makes more requests than
    we provided, the test will fail with an `IndexError` on the next call.
    """
    it: Iterator[httpx.Response] = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        return next(it)

    transport = httpx.MockTransport(handler)
    inner = httpx.Client(base_url=API_BASE, transport=transport)
    return ClinicalTrialsClient(client=inner)


# ---------- trial_from_raw ----------


def test_trial_from_raw_maps_all_core_fields(sample_raw: dict[str, Any]) -> None:
    trial = trial_from_raw(sample_raw)
    assert isinstance(trial, Trial)
    assert trial.nct_id.startswith("NCT")
    assert trial.title  # non-empty
    assert trial.phase  # non-empty list
    assert trial.overall_status == "RECRUITING"
    assert trial.sponsor_name
    assert trial.sponsor_class in {
        "INDUSTRY",
        "NIH",
        "FED",
        "OTHER_GOV",
        "INDIV",
        "NETWORK",
        "OTHER",
    }
    assert trial.eligibility_text
    assert (
        "Inclusion Criteria" in trial.eligibility_text
        or "inclusion" in trial.eligibility_text.lower()
    )


def test_trial_from_raw_accepts_bare_protocol_section(sample_raw: dict[str, Any]) -> None:
    bare = sample_raw["protocolSection"]
    trial = trial_from_raw(bare)
    assert trial.nct_id.startswith("NCT")
    assert trial.eligibility_text


def test_trial_from_raw_handles_missing_optional_fields() -> None:
    minimal = {
        "protocolSection": {
            "identificationModule": {"nctId": "NCT99999999", "briefTitle": "stub"},
            "statusModule": {"overallStatus": "RECRUITING"},
            "sponsorCollaboratorsModule": {"leadSponsor": {"name": "X", "class": "OTHER"}},
            "eligibilityModule": {"eligibilityCriteria": "Inclusion: alive."},
        }
    }
    trial = trial_from_raw(minimal)
    assert trial.nct_id == "NCT99999999"
    assert trial.phase == []
    assert trial.conditions == []
    assert trial.intervention_types == []
    assert trial.sex == "ALL"
    assert trial.minimum_age is None
    assert trial.healthy_volunteers is False


# ---------- build_search_params ----------


def test_build_search_params_with_all_filters() -> None:
    params = build_search_params(
        condition="type 2 diabetes",
        phases=["PHASE2", "PHASE3"],
        sponsor_class="INDUSTRY",
        overall_status="RECRUITING",
        page_size=20,
    )
    assert params["query.cond"] == "type 2 diabetes"
    assert params["pageSize"] == 20
    assert params["format"] == "json"
    assert params["filter.overallStatus"] == "RECRUITING"
    advanced = params["filter.advanced"]
    assert "AREA[StudyType]INTERVENTIONAL" in advanced
    assert "AREA[Phase](PHASE2 OR PHASE3)" in advanced
    assert "AREA[LeadSponsorClass]INDUSTRY" in advanced


def test_build_search_params_minimum() -> None:
    params = build_search_params(
        condition="hypertension",
        phases=None,
        sponsor_class=None,
        overall_status=None,
        page_size=5,
    )
    assert "filter.overallStatus" not in params
    advanced = params["filter.advanced"]
    assert advanced == "AREA[StudyType]INTERVENTIONAL"


def test_build_search_params_status_list_joined() -> None:
    params = build_search_params(
        condition="x",
        phases=None,
        sponsor_class=None,
        overall_status=["RECRUITING", "ACTIVE_NOT_RECRUITING"],
        page_size=5,
    )
    assert params["filter.overallStatus"] == "RECRUITING,ACTIVE_NOT_RECRUITING"


# ---------- pagination + iteration ----------


def test_iter_raw_studies_follows_pagination(sample_raw: dict[str, Any]) -> None:
    page1 = httpx.Response(
        200,
        json={"studies": [sample_raw, sample_raw], "nextPageToken": "tok-2"},
    )
    page2 = httpx.Response(
        200,
        json={"studies": [sample_raw]},
    )
    client = _client_with_responses([page1, page2])
    studies = list(
        client.iter_raw_studies(
            condition="diabetes",
            phases=["PHASE2"],
            sponsor_class=None,
            page_size=2,
            max_results=10,
        )
    )
    assert len(studies) == 3


def test_iter_raw_studies_respects_max_results(sample_raw: dict[str, Any]) -> None:
    page1 = httpx.Response(
        200,
        json={"studies": [sample_raw] * 5, "nextPageToken": "tok-2"},
    )
    client = _client_with_responses([page1])
    studies = list(
        client.iter_raw_studies(
            condition="diabetes",
            page_size=20,
            max_results=3,
        )
    )
    assert len(studies) == 3


def test_iter_raw_studies_stops_when_no_next_token(sample_raw: dict[str, Any]) -> None:
    page1 = httpx.Response(200, json={"studies": [sample_raw, sample_raw]})
    client = _client_with_responses([page1])
    studies = list(
        client.iter_raw_studies(
            condition="diabetes",
            page_size=20,
            max_results=100,
        )
    )
    assert len(studies) == 2


def test_search_yields_parsed_trials(sample_raw: dict[str, Any]) -> None:
    page1 = httpx.Response(200, json={"studies": [sample_raw]})
    client = _client_with_responses([page1])
    trials = list(client.search(condition="diabetes", max_results=1))
    assert len(trials) == 1
    assert isinstance(trials[0], Trial)
    assert trials[0].nct_id.startswith("NCT")


def test_fetch_single_trial(sample_raw: dict[str, Any]) -> None:
    response = httpx.Response(200, json=sample_raw)
    client = _client_with_responses([response])
    trial = client.fetch("NCT07321678")
    assert isinstance(trial, Trial)
    assert trial.nct_id.startswith("NCT")
