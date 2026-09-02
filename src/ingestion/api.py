"""FastAPI surface for dispute ingestion.

Mounted by ``src.main`` as ``from src.ingestion.api import router``.

Endpoints:
    POST /webhook/dispute-created        -> ingest one Razorpay dispute webhook
    GET  /disputes/{id}/phase-history    -> the phase/status trail (reopens included)
    GET  /classification/table           -> the full reason-code -> category mapping
    GET  /classification/{network}/{code}-> classify one reason code

Table creation is lazy (``init_system_tables`` on first request) so importing
this module has no side effects — ``src.main`` imports it at startup.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.ingestion import init_system_tables
from src.ingestion.classification import (
    classification_table,
    classify_reason_code,
)
from src.ingestion.service import (
    WebhookPayloadError,
    get_dispute_case,
    get_phase_history,
    ingest_dispute_event,
    was_reopened,
)

router = APIRouter()

_tables_ready = False


def _ensure_tables() -> None:
    global _tables_ready
    if not _tables_ready:
        init_system_tables()
        _tables_ready = True


class IngestResponse(BaseModel):
    dispute_id: str
    outcome: str = Field(description="created | phase_advance | status_change | out_of_order | redelivery_noop")
    transition_type: str
    is_reopen: bool
    out_of_order: bool
    prev_phase: str | None
    prev_status: str | None
    phase: str
    status: str
    category: str
    needs_manual_classification: bool
    reopen_count: int
    event_count: int


@router.post("/webhook/dispute-created", response_model=IngestResponse)
def webhook_dispute_created(payload: dict[str, Any]) -> IngestResponse:
    _ensure_tables()
    try:
        outcome = ingest_dispute_event(payload)
    except WebhookPayloadError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return IngestResponse(**outcome.__dict__)


@router.get("/disputes/{dispute_id}/phase-history")
def dispute_phase_history(dispute_id: str) -> dict[str, Any]:
    _ensure_tables()
    case = get_dispute_case(dispute_id)
    if case is None:
        raise HTTPException(status_code=404, detail=f"unknown dispute id: {dispute_id}")
    history = get_phase_history(dispute_id)
    return {
        "dispute_id": dispute_id,
        "current_phase": case["phase"],
        "current_status": case["status"],
        "reopen_count": case["reopen_count"],
        "was_reopened": was_reopened(dispute_id),
        "history": [h.__dict__ for h in history],
    }


@router.get("/classification/table")
def get_classification_table() -> dict[str, Any]:
    return {
        "rows": [row.__dict__ for row in classification_table()],
        "note": (
            "Derived from src/common/reason_codes.py (shared source of truth). "
            "Unlisted real network codes route to needs_manual_classification."
        ),
    }


@router.get("/classification/{network}/{code}")
def classify_one(network: str, code: str) -> dict[str, Any]:
    return classify_reason_code(network, code).__dict__
