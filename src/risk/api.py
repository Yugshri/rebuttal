"""``GET /accounts/{account_id}/risk-profile`` — read-only profile lookup.

The handler calls :func:`src.risk.service.get_risk_profile` and nothing else. It
does not import ``src.risk.batch`` or ``src.risk.graph``; it cannot trigger a
graph recomputation. If no profile has been computed yet, it returns 404 (run
the nightly batch), never a synchronous compute.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from src.risk.service import ProfileNotFound, get_risk_profile

router = APIRouter(tags=["risk-graph-service"])


@router.get("/accounts/{account_id}/risk-profile")
def read_risk_profile(account_id: str) -> dict:
    try:
        return get_risk_profile(account_id)
    except ProfileNotFound as exc:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No precomputed risk profile for account '{account_id}'. "
                "Profiles are produced by the nightly graph-risk batch "
                "(src.risk.batch.run_nightly_batch); this endpoint never "
                "recomputes the graph on request."
            ),
        ) from exc


__all__ = ["router"]
