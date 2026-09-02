"""FastAPI surface for confidence-scorer-review.

Mounted by ``src.main`` as ``from src.scoring.api import router``.

Endpoints:
    GET  /disputes/{id}/recommendation   -> the confidence score, routing, cited rationale
    POST /disputes/{id}/review            -> record a human decision (the ONLY path
                                            that may move a dispute toward a
                                            dispatched / submitted state)
    GET  /review-queue                    -> human review queue, priority-sorted
    GET  /draft-queue                     -> draft-for-submit queue, priority-sorted
    POST /internal/deadline-scan          -> run the 48h deadline monitor now
    GET  /deadline-flags                  -> active deadline flags

``POST /disputes/{id}/review`` is the single structural gate: no scoring-path
function writes ``DisputeCase.status`` or ``ReviewQueueEntry.dispatched``. Only
this handler does, and only when a human posts a decision. ``RecommendedAction``
itself has no submitted member (see ``src/common/models_base.py``).
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.common.db import system_session
from src.common.models_base import DisputeStatus
from src.ingestion.models import DisputeCase
from src.scoring import init_system_tables
from src.scoring.deadline import get_active_deadline_flags, run_deadline_scan
from src.scoring.models import (
    DECISION_SUBMIT,
    DECISIONS,
    HumanReviewDecision,
    ReviewQueueEntry,
)
from src.scoring.outcome import record_outcome
from src.scoring.routing import (
    get_active_entry,
    get_draft_queue,
    get_review_queue,
    score_and_route,
)
from src.scoring.scorer import DisputeNotFound

router = APIRouter(tags=["confidence-scorer-review"])

_tables_ready = False


def _ensure_tables() -> None:
    global _tables_ready
    if not _tables_ready:
        init_system_tables()
        _tables_ready = True


# --------------------------------------------------------------------------- #
# GET /disputes/{id}/recommendation
# --------------------------------------------------------------------------- #
@router.get("/disputes/{dispute_id}/recommendation")
def get_recommendation(dispute_id: str, rescore: bool = False) -> dict[str, Any]:
    _ensure_tables()
    existing = None if rescore else get_active_entry(dispute_id)
    if existing is None:
        try:
            score_and_route(dispute_id)
        except DisputeNotFound as exc:
            raise HTTPException(
                status_code=404, detail=f"unknown dispute id: {dispute_id}"
            ) from exc
        existing = get_active_entry(dispute_id)
    return existing


# --------------------------------------------------------------------------- #
# POST /disputes/{id}/review  — the human-in-the-loop gate
# --------------------------------------------------------------------------- #
class ReviewRequest(BaseModel):
    reviewed_by: str = Field(min_length=1, description="Human reviewer id / name.")
    decision: str = Field(description=" | ".join(DECISIONS))
    note: str | None = None
    outcome: str | None = Field(
        default=None,
        description=(
            "Optional: if the final won/lost outcome is already known at review "
            "time, record it in the outcome log in the same call."
        ),
    )


@router.post("/disputes/{dispute_id}/review")
def post_review(dispute_id: str, body: ReviewRequest) -> dict[str, Any]:
    _ensure_tables()
    decision = body.decision.strip().lower()
    if decision not in DECISIONS:
        raise HTTPException(
            status_code=422,
            detail=f"decision must be one of {DECISIONS}, got {body.decision!r}",
        )
    now = int(time.time())

    with system_session() as s:
        case = s.get(DisputeCase, dispute_id)
        if case is None:
            raise HTTPException(
                status_code=404, detail=f"unknown dispute id: {dispute_id}"
            )
        entry = (
            s.query(ReviewQueueEntry)
            .filter(
                ReviewQueueEntry.dispute_id == dispute_id,
                ReviewQueueEntry.superseded.is_(False),
            )
            .order_by(ReviewQueueEntry.queue_generation.desc())
            .first()
        )
        if entry is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "no active recommendation for this dispute — GET "
                    f"/disputes/{dispute_id}/recommendation first"
                ),
            )

        status_before = case.status
        status_after = status_before

        # The ONLY place a dispute advances toward a dispatched state, and only
        # a human posting `submit` triggers it.
        if decision == DECISION_SUBMIT:
            entry.dispatched = True
            entry.dispatched_at = now
            entry.dispatched_by = body.reviewed_by
            entry.updated_at = now
            case.reviewed_by = body.reviewed_by
            if case.status == DisputeStatus.OPEN.value:
                case.status = DisputeStatus.UNDER_REVIEW.value
            status_after = case.status
        else:
            # decline / request_changes: recorded, nothing dispatched.
            case.reviewed_by = body.reviewed_by
            entry.updated_at = now

        record = HumanReviewDecision(
            dispute_id=dispute_id,
            queue_generation=entry.queue_generation,
            reviewed_by=body.reviewed_by,
            decision=decision,
            note=body.note,
            decided_at=now,
            confidence_score_at_decision=entry.confidence_score,
            recommended_action_at_decision=entry.recommended_action,
            queue_at_decision=entry.queue,
            dispute_status_before=status_before,
            dispute_status_after=status_after,
            dispute_phase_at_decision=case.phase,
        )
        s.add(record)
        result = {
            "dispute_id": dispute_id,
            "decision": decision,
            "reviewed_by": body.reviewed_by,
            "decided_at": now,
            "dispatched": bool(entry.dispatched),
            "dispute_status_before": status_before,
            "dispute_status_after": status_after,
            "confidence_score_at_decision": entry.confidence_score,
            "recommended_action_at_decision": entry.recommended_action,
            "queue_generation": entry.queue_generation,
        }

    if body.outcome:
        rec = record_outcome(dispute_id, body.outcome, now_epoch=now)
        result["outcome_logged"] = {
            "actual_outcome": rec.actual_outcome,
            "decision_confidence_score": rec.decision_confidence_score,
            "score_vs_outcome_agrees": rec.score_vs_outcome_agrees,
        }
    return result


# --------------------------------------------------------------------------- #
# queues + deadline monitor (read + schedulable trigger)
# --------------------------------------------------------------------------- #
@router.get("/review-queue")
def review_queue(include_dispatched: bool = False) -> dict[str, Any]:
    _ensure_tables()
    return {"queue": "human_review", "entries": get_review_queue(include_dispatched=include_dispatched)}


@router.get("/draft-queue")
def draft_queue(include_dispatched: bool = False) -> dict[str, Any]:
    _ensure_tables()
    return {
        "queue": "draft_for_submit",
        "note": "every entry here still needs POST /disputes/{id}/review to dispatch",
        "entries": get_draft_queue(include_dispatched=include_dispatched),
    }


@router.post("/internal/deadline-scan")
def deadline_scan() -> dict[str, Any]:
    _ensure_tables()
    return run_deadline_scan().as_dict()


@router.get("/deadline-flags")
def deadline_flags() -> dict[str, Any]:
    _ensure_tables()
    return {"flags": get_active_deadline_flags()}


__all__ = ["router"]
