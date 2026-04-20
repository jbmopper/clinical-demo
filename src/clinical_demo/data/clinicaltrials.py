"""ClinicalTrials.gov v2 API client.

Thin wrapper around the public REST API at
https://clinicaltrials.gov/api/v2/. Just enough to fetch a curated set
of trials for the demo and translate the response into our `Trial`
domain model.

API notes:
- The v2 API is unauthenticated and rate-limited generously for our
  scale (a few hundred requests for the whole project).
- `query.cond` is a free-text condition search; `filter.advanced` accepts
  Essie-style expressions like `AREA[Phase](PHASE2 OR PHASE3)` for
  structured filtering.
- `pageSize` ≤ 1000; we use a small page size and rely on `nextPageToken`
  for paging when needed.
- The response wraps each study under `protocolSection`, with predictable
  sub-modules (`identificationModule`, `eligibilityModule`, etc.).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import httpx

from clinical_demo.domain import Trial

logger = logging.getLogger(__name__)

API_BASE = "https://clinicaltrials.gov/api/v2"


class ClinicalTrialsClient:
    """Minimal CT.gov v2 client.

    Inject a custom `httpx.Client` for testing (e.g., with a `MockTransport`).
    The default client uses sensible timeouts and follows redirects.
    """

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(
            base_url=API_BASE,
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
            headers={"Accept": "application/json"},
        )

    def search(
        self,
        *,
        condition: str,
        phases: list[str] | None = None,
        sponsor_class: str | None = None,
        overall_status: str | list[str] | None = "RECRUITING",
        page_size: int = 20,
        max_results: int = 100,
    ) -> Iterator[Trial]:
        """Search trials, yielding parsed `Trial` objects."""
        for raw in self.iter_raw_studies(
            condition=condition,
            phases=phases,
            sponsor_class=sponsor_class,
            overall_status=overall_status,
            page_size=page_size,
            max_results=max_results,
        ):
            yield trial_from_raw(raw)

    def iter_raw_studies(
        self,
        *,
        condition: str,
        phases: list[str] | None = None,
        sponsor_class: str | None = None,
        overall_status: str | list[str] | None = "RECRUITING",
        page_size: int = 20,
        max_results: int = 100,
    ) -> Iterator[dict[str, Any]]:
        """Search trials, yielding raw study response dicts.

        - `condition`: free-text condition (e.g. "type 2 diabetes").
        - `phases`: list of CT.gov phase enum values (e.g. ["PHASE2", "PHASE3"]).
        - `sponsor_class`: e.g. "INDUSTRY" or "OTHER". None = no filter.
        - `overall_status`: one or more status enums; default "RECRUITING".
        - Yields up to `max_results` raw study dicts, paging as needed.

        Use this when you want to persist the source-of-truth JSON; use
        `search()` when you want parsed `Trial` objects only.
        """
        params = build_search_params(
            condition=condition,
            phases=phases,
            sponsor_class=sponsor_class,
            overall_status=overall_status,
            page_size=page_size,
        )
        yielded = 0
        while yielded < max_results:
            data = self._get_json("/studies", params=params)
            for raw in data.get("studies", []):
                yield raw
                yielded += 1
                if yielded >= max_results:
                    return
            token = data.get("nextPageToken")
            if not token:
                return
            params["pageToken"] = token

    def fetch(self, nct_id: str) -> Trial:
        """Fetch a single trial by NCT id."""
        data = self._get_json(f"/studies/{nct_id}")
        return trial_from_raw(data)

    def fetch_raw(self, nct_id: str) -> dict[str, Any]:
        """Fetch the raw `protocolSection`-bearing dict for a single trial.

        Useful for persisting source-of-truth JSON alongside the parsed
        domain object.
        """
        return self._get_json(f"/studies/{nct_id}")

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> ClinicalTrialsClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = self._client.get(path, params=params)
        resp.raise_for_status()
        return resp.json()


# ---------- request building ----------


def build_search_params(
    *,
    condition: str,
    phases: list[str] | None,
    sponsor_class: str | None,
    overall_status: str | list[str] | None,
    page_size: int,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "query.cond": condition,
        "pageSize": page_size,
        "format": "json",
    }
    if overall_status is not None:
        params["filter.overallStatus"] = (
            overall_status if isinstance(overall_status, str) else ",".join(overall_status)
        )

    advanced_clauses: list[str] = ["AREA[StudyType]INTERVENTIONAL"]
    if phases:
        joined = " OR ".join(phases)
        advanced_clauses.append(f"AREA[Phase]({joined})")
    if sponsor_class:
        advanced_clauses.append(f"AREA[LeadSponsorClass]{sponsor_class}")
    params["filter.advanced"] = " AND ".join(advanced_clauses)
    return params


# ---------- raw → domain translation ----------


def trial_from_raw(raw: dict[str, Any]) -> Trial:
    """Translate a CT.gov v2 study response (or `studies[i]`) into a `Trial`.

    Accepts either the `{protocolSection: ...}` wrapper or a bare
    protocolSection-bearing dict.
    """
    ps = raw.get("protocolSection") or raw
    ident = ps.get("identificationModule", {})
    status = ps.get("statusModule", {})
    design = ps.get("designModule", {})
    sponsor = ps.get("sponsorCollaboratorsModule", {}).get("leadSponsor", {})
    arms = ps.get("armsInterventionsModule", {})
    elig = ps.get("eligibilityModule", {})
    cond = ps.get("conditionsModule", {})

    return Trial(
        nct_id=ident.get("nctId", ""),
        title=ident.get("briefTitle", ""),
        phase=list(design.get("phases") or []),
        overall_status=status.get("overallStatus", "UNKNOWN"),
        conditions=list(cond.get("conditions") or []),
        sponsor_name=sponsor.get("name", ""),
        sponsor_class=sponsor.get("class", "UNKNOWN"),
        intervention_types=[
            i.get("type", "") for i in arms.get("interventions", []) if i.get("type")
        ],
        eligibility_text=elig.get("eligibilityCriteria", ""),
        minimum_age=elig.get("minimumAge"),
        maximum_age=elig.get("maximumAge"),
        sex=elig.get("sex", "ALL"),
        healthy_volunteers=bool(elig.get("healthyVolunteers", False)),
    )
