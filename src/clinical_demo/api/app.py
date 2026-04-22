"""FastAPI app: thin HTTP surface over `score_pair` / `score_pair_graph`.

The reviewer UI (Phase 2.8) and any external integration call
this. The library does the work; the API maps requests to one of
the existing scorers and serializes the existing
`ScorePairResult` envelope back. No new business logic should
land in this module — if a route grows logic, push it into the
library first and keep the route a 5-line adapter.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Literal

from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from ..scoring import cache_path_for, load_cached_extraction, score_pair
from ..scoring.score_pair import ScorePairResult
from .loaders import (
    EXTRACTIONS_DIR,
    CuratedDataMissing,
    list_patients,
    list_trials,
    load_patient,
    load_trial,
)

log = logging.getLogger(__name__)


# --------------------- request / response schemas


class ScoreRequest(BaseModel):
    """Score one (patient, trial) pair.

    `as_of` defaults to today server-side. `orchestrator` chooses
    between the imperative `score_pair` and the LangGraph
    `score_pair_graph`; `critic_enabled` is only meaningful with
    `graph`. `use_cached_extraction` short-circuits the LLM
    extraction call when a cache hit exists — useful for the demo
    so repeat scoring is fast and free."""

    patient_id: str = Field(..., description="Curated cohort patient id.")
    nct_id: str = Field(..., description="Curated trial NCT id.")
    as_of: date | None = Field(
        default=None,
        description="Eligibility evaluation date. Defaults to today.",
    )
    orchestrator: Literal["imperative", "graph"] = "imperative"
    critic_enabled: bool = False
    use_cached_extraction: bool = True


class TrialRow(BaseModel):
    nct_id: str
    title: str


class PatientRow(BaseModel):
    patient_id: str
    score: int | None = None
    slice: str | None = None


# --------------------- app


def create_app() -> FastAPI:
    """Factory so tests can spin up isolated app instances.

    Tests use `TestClient(create_app())` rather than importing a
    module-level singleton so a misconfigured global doesn't
    bleed across test files."""
    app = FastAPI(
        title="clinical-demo API",
        version="0.1.0",
        description=(
            "Eligibility scoring for the Clinical Trial Eligibility "
            "Co-Pilot demo. Wraps `score_pair`."
        ),
    )

    # Permissive CORS for the v0 demo. The reviewer UI is served
    # from the same origin in prod, but local dev hits the API
    # from a Vite/SvelteKit dev server on a different port. Lock
    # this down before any non-demo deployment.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    @app.get("/health", tags=["meta"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/patients", response_model=list[PatientRow], tags=["catalog"])
    def patients() -> list[dict]:
        try:
            return list_patients()
        except CuratedDataMissing as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
            ) from exc

    @app.get("/trials", response_model=list[TrialRow], tags=["catalog"])
    def trials() -> list[dict]:
        try:
            return list_trials()
        except CuratedDataMissing as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
            ) from exc

    @app.post("/score", response_model=ScorePairResult, tags=["scoring"])
    def score(req: ScoreRequest) -> ScorePairResult:
        try:
            patient = load_patient(req.patient_id)
        except CuratedDataMissing as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
            ) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

        try:
            trial = load_trial(req.nct_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

        extraction = None
        if req.use_cached_extraction:
            cache_file = cache_path_for(req.nct_id, EXTRACTIONS_DIR)
            if cache_file.exists():
                extraction = load_cached_extraction(cache_file)

        as_of = req.as_of or date.today()
        try:
            if req.orchestrator == "imperative":
                return score_pair(patient, trial, as_of, extraction=extraction)
            from ..graph import score_pair_graph

            return score_pair_graph(
                patient,
                trial,
                as_of,
                extraction=extraction,
                critic_enabled=req.critic_enabled,
            )
        except Exception as exc:
            log.exception("scoring failed for %s x %s", req.patient_id, req.nct_id)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"scoring failed: {type(exc).__name__}: {exc}",
            ) from exc

    return app


__all__ = ["ScoreRequest", "create_app"]
