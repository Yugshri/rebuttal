"""FastAPI application entry point.

API surface (CLAUDE.md):
    POST /webhook/dispute-created        -> dispute-ingestion-router
    GET  /disputes/{id}/recommendation   -> confidence-scorer-review
    POST /disputes/{id}/review           -> confidence-scorer-review
    GET  /accounts/{id}/risk-profile     -> risk-graph-service

Plus read-only helpers each module adds within its own boundary, and one
integration endpoint (``POST /pipeline/run/{dispute_id}``) that runs an
already-ingested dispute through assembly -> risk enrichment -> scoring.

Startup creates the system-store tables (read/write) and, if the risk profile
table is empty, runs the graph batch once so the app is usable immediately. The
read-only external store must already exist — build it with
``python -m src.synthetic.build``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException


@asynccontextmanager
async def lifespan(_app: FastAPI):
    from src.init_db import init_all_tables

    init_all_tables()
    try:
        from src.pipeline import ensure_risk_profiles

        ensure_risk_profiles()
    except FileNotFoundError:
        # external.db not built yet — endpoints that need it will 4xx clearly.
        pass
    yield


app = FastAPI(
    title="Track 02 — AI Risk Manager",
    summary="Chargeback evidence responder + graph-based counterparty risk enrichment",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/pipeline/run/{dispute_id}")
def pipeline_run(dispute_id: str) -> dict:
    """Assemble evidence, enrich with risk, score and route one ingested dispute.

    Never dispatches — the result lands in the draft-for-submit or human-review
    queue and still requires ``POST /disputes/{dispute_id}/review``.
    """
    from dataclasses import asdict

    from src.pipeline import run_pipeline

    try:
        return asdict(run_pipeline(dispute_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _mount_routers() -> None:
    """Attach each module's router."""
    for module_path in (
        "src.ingestion.api",
        "src.risk.api",
        "src.evidence.api",
        "src.scoring.api",
    ):
        try:
            module = __import__(module_path, fromlist=["router"])
            app.include_router(module.router)
        except ImportError:
            pass


_mount_routers()
