"""Tests for the RxNorm REST client.

Same offline pattern as `test_vsac_client.py`: a recorded fixture
(trimmed real RxNav `/drugs.json?name=metformin` response) drives
the parser tests; a `httpx.MockTransport` fakes network responses
for the failure-mode tests so CI / fresh checkouts pass without
hitting the live RxNav server. A live one-off probe lives in
`scripts/probe_rxnorm.py` for the rare moments we want to
re-record the fixture against the real API.

RxNorm has no API key (different from VSAC), so there is no
credential-plumbing test here -- the constructor simply does not
take one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from clinical_demo.terminology import RxNormClient, RxNormConcepts, RxNormError
from clinical_demo.terminology.rxnorm_client import RXNORM_SYSTEM_URI

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "rxnorm" / "metformin_drugs.json"


def _load_fixture() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text())


def _client_with_response(
    *,
    status: int,
    body: bytes,
    content_type: str = "application/json",
) -> tuple[RxNormClient, list[httpx.Request]]:
    """Build an RxNormClient whose transport returns one canned response.

    Returns the captured request list so tests can assert on the
    URL and query params without touching the network."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(status, content=body, headers={"content-type": content_type})

    transport = httpx.MockTransport(handler)
    client = RxNormClient(transport=transport)
    return client, captured


# ---------- happy path ----------


def test_find_drug_concepts_parses_metformin_fixture() -> None:
    body = FIXTURE_PATH.read_bytes()
    client, _ = _client_with_response(status=200, body=body)

    result = client.find_drug_concepts("metformin")

    assert isinstance(result, RxNormConcepts)
    assert result.query == "metformin"
    assert result.concept_set.name == "metformin"
    assert result.concept_set.system == RXNORM_SYSTEM_URI
    # Sanity-check that codes from every populated tty bucket made
    # it through (IN ingredient, PIN precise ingredient, SCD
    # clinical drug, SBD branded drug). Empty BPCK group ignored.
    assert "6809" in result.concept_set.codes  # IN
    assert "236211" in result.concept_set.codes  # PIN
    assert "860974" in result.concept_set.codes  # SCD
    assert "861004" in result.concept_set.codes  # SBD (Glucophage)
    # Total = 1 IN + 1 PIN + 3 SCDs + 2 SBDs = 7
    assert len(result.concept_set.codes) == 7
    assert result.term_types == frozenset({"IN", "PIN", "SCD", "SBD"})


def test_find_drug_concepts_sends_name_param_to_drugs_endpoint() -> None:
    body = FIXTURE_PATH.read_bytes()
    client, captured = _client_with_response(status=200, body=body)

    client.find_drug_concepts("metformin")

    assert len(captured) == 1
    request = captured[0]
    assert request.url.path.endswith("/drugs.json")
    assert request.url.params["name"] == "metformin"
    # No Authorization header -- RxNav is a public, key-less API.
    assert "authorization" not in {k.lower() for k in request.headers}


def test_find_drug_concepts_query_preserved_verbatim_in_envelope() -> None:
    """The matcher uses the surface text from the criterion (e.g.
    "Glucophage") to label evidence; the envelope must echo it back
    rather than substituting RxNav's normalized name."""
    body = FIXTURE_PATH.read_bytes()
    client, _ = _client_with_response(status=200, body=body)

    result = client.find_drug_concepts("Glucophage")

    assert result.query == "Glucophage"
    assert result.concept_set.name == "Glucophage"


# ---------- tty_filter ----------


def test_find_drug_concepts_tty_filter_restricts_codes_to_chosen_term_types() -> None:
    """Slice-4 ablation: caller binds only at the ingredient level."""
    body = FIXTURE_PATH.read_bytes()
    client, _ = _client_with_response(status=200, body=body)

    result = client.find_drug_concepts("metformin", tty_filter=frozenset({"IN", "PIN"}))

    assert result.concept_set.codes == frozenset({"6809", "236211"})
    assert result.term_types == frozenset({"IN", "PIN"})


def test_find_drug_concepts_tty_filter_raises_when_no_codes_survive() -> None:
    """A filter that matches nothing in the response is loud, not
    silent -- empty result would otherwise tell the matcher 'no
    codes count', exactly the wrong default."""
    body = FIXTURE_PATH.read_bytes()
    client, _ = _client_with_response(status=200, body=body)

    with pytest.raises(RxNormError, match="no codes under TTYs"):
        client.find_drug_concepts("metformin", tty_filter=frozenset({"GPCK"}))


# ---------- failure modes (matcher must degrade, not crash) ----------


def test_find_drug_concepts_raises_on_http_500() -> None:
    client, _ = _client_with_response(status=500, body=b"server error")

    with pytest.raises(RxNormError, match="500"):
        client.find_drug_concepts("metformin")


def test_find_drug_concepts_raises_on_non_json_body() -> None:
    client, _ = _client_with_response(
        status=200, body=b"<html>oops</html>", content_type="text/html"
    )

    with pytest.raises(RxNormError, match="not JSON"):
        client.find_drug_concepts("metformin")


def test_find_drug_concepts_raises_when_payload_has_no_drug_group() -> None:
    """A unknown surface form produces an empty `drugGroup` with no
    `conceptGroup` rows in real RxNav responses; this test pins the
    "unmapped surface form" failure mode by sending a payload with
    no `drugGroup` at all."""
    body = json.dumps({"unrelated": True}).encode()
    client, _ = _client_with_response(status=200, body=body)

    with pytest.raises(RxNormError, match="no `drugGroup`"):
        client.find_drug_concepts("notarealmedication")


def test_find_drug_concepts_raises_when_no_concept_groups() -> None:
    """RxNav signals 'no match' by returning an empty conceptGroup
    list; the matcher needs this loud so it can route to
    unmapped_concept rather than treat empty as 'no codes count'."""
    body = json.dumps({"drugGroup": {"name": "asdf", "conceptGroup": []}}).encode()
    client, _ = _client_with_response(status=200, body=body)

    with pytest.raises(RxNormError, match="no `conceptGroup`"):
        client.find_drug_concepts("asdf")


def test_find_drug_concepts_raises_when_groups_have_only_empty_buckets() -> None:
    """Real responses include `tty`-only entries with no
    `conceptProperties`; if every group is empty we still have zero
    usable codes, which must be loud."""
    payload = {
        "drugGroup": {
            "name": "asdf",
            "conceptGroup": [{"tty": "BPCK"}, {"tty": "GPCK"}],
        }
    }
    client, _ = _client_with_response(status=200, body=json.dumps(payload).encode())

    with pytest.raises(RxNormError, match="zero usable"):
        client.find_drug_concepts("asdf")


def test_find_drug_concepts_raises_on_network_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    client = RxNormClient(transport=httpx.MockTransport(handler))

    with pytest.raises(RxNormError, match="RxNorm request failed"):
        client.find_drug_concepts("metformin")


# ---------- robustness ----------


def test_find_drug_concepts_skips_malformed_concept_property_rows() -> None:
    """A real response may carry rows with missing fields (rxcui/tty
    absent or non-string) on rare data corner cases; the parser
    should silently skip them rather than crash, but still raise if
    *every* row is malformed (no usable codes => loud)."""
    payload = {
        "drugGroup": {
            "name": "metformin",
            "conceptGroup": [
                {
                    "tty": "IN",
                    "conceptProperties": [
                        {"rxcui": "6809", "name": "metformin", "tty": "IN"},
                        {"name": "missing rxcui", "tty": "IN"},
                        {"rxcui": 12345, "tty": "IN"},  # wrong type
                    ],
                }
            ],
        }
    }
    client, _ = _client_with_response(status=200, body=json.dumps(payload).encode())

    result = client.find_drug_concepts("metformin")

    assert result.concept_set.codes == frozenset({"6809"})
