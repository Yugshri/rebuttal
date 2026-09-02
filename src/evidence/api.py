"""Read-only FastAPI surface for assembled evidence.

Mounted by ``src.main`` as ``from src.evidence.api import router``.

Endpoints (all read-only — no write endpoints in this module):
    GET  /disputes/{id}/evidence          -> the persisted EvidenceBundle
    POST /disputes/{id}/evidence/assemble -> (re)run assembly for a dispute id

``POST .../assemble`` is a pipeline-internal recompute over data the system
already holds; it writes only this module's ``EvidenceBundle`` row and can never
move money, submit a dispute, or contact anyone (see ``src/common/db.py``).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from src.evidence import init_system_tables
from src.evidence.assembler import DisputeNotFound, assemble_evidence, get_evidence_bundle

router = APIRouter(tags=["evidence-assembler"])

_tables_ready = False


def _ensure_tables() -> None:
    global _tables_ready
    if not _tables_ready:
        init_system_tables()
        _tables_ready = True


@router.get("/disputes/{dispute_id}/evidence")
def read_evidence_bundle(dispute_id: str) -> dict[str, Any]:
    _ensure_tables()
    bundle = get_evidence_bundle(dispute_id)
    if bundle is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No evidence bundle for dispute '{dispute_id}'. Run assembly "
                "first (POST /disputes/{id}/evidence/assemble)."
            ),
        )
    return bundle


@router.post("/disputes/{dispute_id}/evidence/assemble")
def run_assembly(dispute_id: str) -> dict[str, Any]:
    _ensure_tables()
    try:
        result = assemble_evidence(dispute_id)
    except DisputeNotFound as exc:
        raise HTTPException(
            status_code=404, detail=f"unknown dispute id: {dispute_id}"
        ) from exc
    return {
        "dispute_id": result.dispute_id,
        "assembly_status": result.assembly_status,
        "completeness": result.completeness,
        "slot_status": result.slot_status,
        "needs_manual_classification": result.needs_manual_classification,
        "deadline_pressure": result.deadline_pressure,
        "explanation_letter_source": result.explanation_letter_source,
        "completeness_signal": result.completeness_signal(),
    }


__all__ = ["router"]
