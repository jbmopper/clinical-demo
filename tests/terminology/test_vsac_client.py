"""Tests for the VSAC FHIR `$expand` client.

The client is exercised against a recorded fixture (a trimmed real
VSAC response for the eCQM Diabetes value set,
OID 2.16.840.1.113883.3.464.1003.103.12.1001). Tests run offline so
CI / fresh checkouts pass without an NLM key. A live one-off probe
lives in `scripts/probe_vsac.py` for the rare moments we want to
re-record the fixture against the real server.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from clinical_demo.terminology import VSACClient, VSACError, VSACExpansion

DIABETES_OID = "2.16.840.1.113883.3.464.1003.103.12.1001"
FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "vsac" / "diabetes_expansion.json"


def _load_fixture() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text())


def _client_with_response(
    *,
    status: int,
    body: bytes,
    content_type: str = "application/fhir+json",
) -> tuple[VSACClient, list[httpx.Request]]:
    """Build a VSACClient whose transport returns a single canned response.

    Returns the captured request list so tests can assert on the URL,
    params, and Authorization header without needing a real network."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(status, content=body, headers={"content-type": content_type})

    transport = httpx.MockTransport(handler)
    client = VSACClient(api_key="dummy-key", transport=transport)
    return client, captured


# ---------- happy path ----------


def test_expand_parses_recorded_diabetes_expansion() -> None:
    body = FIXTURE_PATH.read_bytes()
    client, _ = _client_with_response(status=200, body=body)

    expansion = client.expand(DIABETES_OID, name="Diabetes")

    assert isinstance(expansion, VSACExpansion)
    assert expansion.oid == DIABETES_OID
    assert expansion.version == "20210220"
    assert expansion.concept_set.name == "Diabetes"
    assert expansion.concept_set.system == "http://snomed.info/sct"
    # Sanity-check that both T2DM and T1DM codes survive the parse —
    # the fixture's whole point is showing the eCQM "Diabetes" set is
    # broader than our hand-curated T2DM ConceptSet.
    assert "44054006" in expansion.concept_set.codes  # T2DM
    assert "46635009" in expansion.concept_set.codes  # T1DM
    assert "11687002" in expansion.concept_set.codes  # gestational
    assert len(expansion.concept_set.codes) == 6


def test_expand_sends_basic_auth_to_read_by_id_endpoint() -> None:
    body = FIXTURE_PATH.read_bytes()
    client, captured = _client_with_response(status=200, body=body)

    client.expand(DIABETES_OID)

    assert len(captured) == 1
    request = captured[0]
    # Read-by-id form: GET /ValueSet/<OID>/$expand. VSAC rejects the
    # `?url=urn:oid:<OID>` form with 404; read-by-id is what works.
    assert request.url.path.endswith(f"/ValueSet/{DIABETES_OID}/$expand")
    assert "url" not in request.url.params
    # httpx encodes Basic auth as `Basic base64(user:pass)`; we just
    # assert the header exists and is the Basic scheme rather than
    # decode it — the wire format is httpx's contract, not ours.
    assert request.headers["authorization"].startswith("Basic ")


def test_expand_defaults_name_to_oid_when_caller_omits_it() -> None:
    body = FIXTURE_PATH.read_bytes()
    client, _ = _client_with_response(status=200, body=body)

    expansion = client.expand(DIABETES_OID)

    assert expansion.concept_set.name == DIABETES_OID


def test_expand_accepts_oid_already_prefixed_with_urn() -> None:
    body = FIXTURE_PATH.read_bytes()
    client, captured = _client_with_response(status=200, body=body)

    client.expand(f"urn:oid:{DIABETES_OID}")

    # urn: prefix stripped before constructing the read-by-id path —
    # no double-prefix on the URL.
    assert captured[0].url.path.endswith(f"/ValueSet/{DIABETES_OID}/$expand")


# ---------- failure modes (matcher must degrade, not crash) ----------


def test_expand_raises_vsac_error_on_http_404() -> None:
    client, _ = _client_with_response(status=404, body=b"Not found")

    with pytest.raises(VSACError, match="404"):
        client.expand(DIABETES_OID)


def test_expand_raises_vsac_error_on_non_json_body() -> None:
    client, _ = _client_with_response(
        status=200, body=b"<html>oops</html>", content_type="text/html"
    )

    with pytest.raises(VSACError, match="not JSON"):
        client.expand(DIABETES_OID)


def test_expand_raises_when_payload_has_no_expansion() -> None:
    body = json.dumps({"resourceType": "ValueSet", "id": DIABETES_OID}).encode()
    client, _ = _client_with_response(status=200, body=body)

    with pytest.raises(VSACError, match="no `expansion`"):
        client.expand(DIABETES_OID)


def test_expand_raises_when_expansion_contains_is_empty() -> None:
    body = json.dumps({"resourceType": "ValueSet", "expansion": {"contains": []}}).encode()
    client, _ = _client_with_response(status=200, body=body)

    with pytest.raises(VSACError, match="no concepts"):
        client.expand(DIABETES_OID)


def test_expand_raises_on_multi_system_expansion() -> None:
    """v0 ConceptSet is single-system by construction (D-25). A
    multi-system value set must surface explicitly rather than
    silently dropping codes from one system."""
    payload = _load_fixture()
    payload["expansion"]["contains"].append(
        {"system": "http://loinc.org", "code": "4548-4", "display": "HbA1c"}
    )
    client, _ = _client_with_response(status=200, body=json.dumps(payload).encode())

    with pytest.raises(VSACError, match="multiple coding"):
        client.expand(DIABETES_OID)


def test_expand_with_system_filter_slices_multi_system_expansion() -> None:
    """Real eCQM value sets (Diabetes, Hypertension, …) span SNOMED +
    ICD-10-CM. Callers that know the patient's coding system pass a
    `system_filter` to get a clean single-system ConceptSet."""
    payload = _load_fixture()
    payload["expansion"]["contains"].extend(
        [
            {"system": "http://hl7.org/fhir/sid/icd-10-cm", "code": "E11.9"},
            {"system": "http://hl7.org/fhir/sid/icd-10-cm", "code": "E10.9"},
        ]
    )
    client, _ = _client_with_response(status=200, body=json.dumps(payload).encode())

    expansion = client.expand(
        DIABETES_OID,
        name="Diabetes",
        system_filter="http://snomed.info/sct",
    )

    assert expansion.concept_set.system == "http://snomed.info/sct"
    assert "44054006" in expansion.concept_set.codes
    assert "E11.9" not in expansion.concept_set.codes
    assert "E10.9" not in expansion.concept_set.codes


def test_expand_with_system_filter_raises_when_filter_matches_nothing() -> None:
    payload = _load_fixture()
    client, _ = _client_with_response(status=200, body=json.dumps(payload).encode())

    with pytest.raises(VSACError, match="no codes from system"):
        client.expand(
            DIABETES_OID,
            system_filter="http://www.nlm.nih.gov/research/umls/rxnorm",
        )


def test_expand_raises_on_network_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    client = VSACClient(api_key="dummy-key", transport=httpx.MockTransport(handler))

    with pytest.raises(VSACError, match="VSAC request failed"):
        client.expand(DIABETES_OID)


# ---------- credential plumbing ----------


def test_constructor_pulls_api_key_from_settings_when_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from clinical_demo.settings import Settings
    from clinical_demo.terminology import vsac_client as vsac_module

    monkeypatch.setattr(
        vsac_module,
        "get_settings",
        lambda: Settings(umls_api_key=SecretStr("from-env-key")),
    )

    client = VSACClient()
    assert client._auth == ("apikey", "from-env-key")


def test_constructor_raises_when_no_key_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from clinical_demo.settings import Settings
    from clinical_demo.terminology import vsac_client as vsac_module

    # Patch the symbol where it's *used* (vsac_client imported
    # `get_settings` directly into its namespace), and hand back a
    # Settings instance with no env file so a real .env on disk
    # doesn't sneak the key back in.
    monkeypatch.setattr(
        vsac_module,
        "get_settings",
        lambda: Settings.model_construct(umls_api_key=None),
    )

    with pytest.raises(VSACError, match="UMLS_API_KEY is not set"):
        VSACClient()


def test_settings_accepts_two_pass_binding_strategy() -> None:
    """Slice 4 wired the matcher-side terminology resolver behind
    `two_pass`, so the literal must accept it. Pinned alongside the
    `one_pass` reject test below so a future enum-shape change has
    to update both ends explicitly."""
    from clinical_demo.settings import Settings

    s = Settings.model_validate({"binding_strategy": "two_pass"})
    assert s.binding_strategy == "two_pass"


def test_settings_rejects_unwired_binding_strategies() -> None:
    """`one_pass` requires extractor-side schema changes that are
    out of scope for D-69 slice 4; accepting it as config would
    silently mark eval runs as terminology-backed when they are
    not. Reject explicitly until the wire-up lands."""
    from clinical_demo.settings import Settings

    with pytest.raises(ValidationError, match="binding_strategy"):
        Settings.model_validate({"binding_strategy": "one_pass"})
