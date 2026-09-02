"""FastAPI application entry point.

Routers are mounted by the orchestrator's integration pass as each module lands.
Until then this exposes only a health check so ``uvicorn src.main:app`` boots.

API surface (target):
    POST /webhook/dispute-created        -> dispute-ingestion-router
    GET  /disputes/{id}/recommendation   -> confidence-scorer-review
    POST /disputes/{id}/review           -> confidence-scorer-review
    GET  /accounts/{id}/risk-profile     -> risk-graph-service
"""

from __future__ import annotations

from fastapi import FastAPI

app = FastAPI(
    title="Track 02 — AI Risk Manager",
    summary="Chargeback evidence responder + graph-based counterparty risk enrichment",
    version="0.1.0",
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _mount_routers() -> None:
    """Attach each module's router if it exists yet. Called at import time."""
    try:
        from src.ingestion.api import router as ingestion_router

        app.include_router(ingestion_router)
    except ImportError:
        pass

    try:
        from src.risk.api import router as risk_router

        app.include_router(risk_router)
    except ImportError:
        pass

    try:
        from src.scoring.api import router as scoring_router

        app.include_router(scoring_router)
    except ImportError:
        pass


_mount_routers()
