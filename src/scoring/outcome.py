"""Outcome tracker — the feedback loop's labelled log.

``record_outcome(dispute_id, "won" | "lost")`` writes one :class:`OutcomeLogEntry`
pairing the **decision-time** confidence score (and recommended action / queue)
with the actual outcome, in a flat shape a retraining step could consume.

Which part is live vs. a documented next step (be explicit — the spec asks):
* LIVE: the labelled log. Every resolved dispute produces a
  ``(decision_confidence_score, actual_outcome)`` pair plus the feature blob.
* NEXT STEP (not live at demo time): the "feeds back into the Confidence Scorer"
  arrow. Nothing here retrains or adjusts ``src.scoring.scorer``'s weights. The
  log is the input a future retraining job would read; the loop is closed in
  data, not yet in code.

Decision-time confidence is taken from, in order of preference:
1. the latest ``HumanReviewDecision`` for this dispute+generation (a human acted), else
2. the active ``ReviewQueueEntry``'s ``confidence_score`` (recommendation stood,
   no human decision recorded yet — noted on the row).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from sqlalchemy import select

from src.common.db import system_session
from src.ingestion.models import DisputeCase
from src.ingestion.service import was_reopened
from src.scoring.models import (
    OUTCOMES,
    HumanReviewDecision,
    OutcomeLogEntry,
    ReviewQueueEntry,
)


class UnknownOutcome(ValueError):
    """Outcome must be one of ``won`` / ``lost``."""


class DisputeNotFound(Exception):
    pass


@dataclass
class OutcomeRecord:
    dispute_id: str
    queue_generation: int
    decision_confidence_score: float | None
    actual_outcome: str
    score_vs_outcome_agrees: bool | None
    was_reopened: bool


def _agreement(
    confidence: float | None, queue: str | None, dispatched: bool, outcome: str
) -> bool | None:
    """Did the decision-time confidence and the actual outcome point the same way?

    A dispatched draft-for-submit that won -> agree. A deferred / low-confidence
    case that lost -> agree (the caution was warranted). The mixed cases
    disagree. ``None`` when there is no score to compare.
    """
    from src.scoring.models import QUEUE_DRAFT_FOR_SUBMIT

    if confidence is None:
        return None
    high_conf = queue == QUEUE_DRAFT_FOR_SUBMIT
    if outcome == "won":
        return bool(high_conf)
    return bool(not high_conf)


def record_outcome(
    dispute_id: str,
    actual_outcome: str,
    *,
    now_epoch: int | None = None,
    features: dict | None = None,
    notes: str | None = None,
) -> OutcomeRecord:
    """Record the actual ``won`` / ``lost`` outcome and pair it with the score."""
    outcome = str(actual_outcome).strip().lower()
    if outcome not in OUTCOMES:
        raise UnknownOutcome(f"{actual_outcome!r} not in {OUTCOMES}")
    now = int(now_epoch if now_epoch is not None else time.time())

    with system_session() as s:
        case = s.get(DisputeCase, dispute_id)
        if case is None:
            raise DisputeNotFound(dispute_id)

        entry = s.execute(
            select(ReviewQueueEntry)
            .where(
                ReviewQueueEntry.dispute_id == dispute_id,
                ReviewQueueEntry.superseded.is_(False),
            )
            .order_by(ReviewQueueEntry.queue_generation.desc())
        ).scalars().first()
        generation = entry.queue_generation if entry is not None else 1

        decision = s.execute(
            select(HumanReviewDecision)
            .where(
                HumanReviewDecision.dispute_id == dispute_id,
                HumanReviewDecision.queue_generation == generation,
            )
            .order_by(HumanReviewDecision.decided_at.desc())
        ).scalars().first()

        if decision is not None:
            conf = decision.confidence_score_at_decision
            rec_action = decision.recommended_action_at_decision
            queue_at = decision.queue_at_decision
            reviewed_by = decision.reviewed_by
            decided_at = decision.decided_at
            dispatched = decision.decision == "submit"
            src_note = "decision-time confidence from HumanReviewDecision"
        elif entry is not None:
            conf = entry.confidence_score
            rec_action = entry.recommended_action
            queue_at = entry.queue
            reviewed_by = None
            decided_at = None
            dispatched = bool(entry.dispatched)
            src_note = (
                "no HumanReviewDecision recorded — using the standing "
                "ReviewQueueEntry recommendation as decision-time confidence"
            )
        else:
            conf = rec_action = queue_at = reviewed_by = decided_at = None
            dispatched = False
            src_note = "no queue entry or decision — dispute never scored"

        feat = {
            "reason_category": case.category,
            "phase_at_outcome": case.phase,
            "amount": case.amount,
            "confidence_score": conf,
            "recommended_action": rec_action,
            "queue": queue_at,
            "hard_gates": list(entry.hard_gates) if entry is not None else [],
            "completeness": (
                (entry.score_breakdown or {})
                .get("factors", {})
                .get("evidence_completeness", {})
                .get("value")
                if entry is not None
                else None
            ),
            "baseline_deviation": (
                (entry.score_breakdown or {})
                .get("factors", {})
                .get("risk", {})
                .get("baseline_deviation")
                if entry is not None
                else None
            ),
        }
        if features:
            feat.update(features)

        agrees = _agreement(conf, queue_at, dispatched, outcome)
        reopened = was_reopened(dispute_id)

        row = OutcomeLogEntry(
            dispute_id=dispute_id,
            queue_generation=generation,
            decision_confidence_score=conf,
            decision_recommended_action=rec_action,
            decision_queue=queue_at,
            decision_was_dispatched=dispatched,
            reviewed_by=reviewed_by,
            decided_at=decided_at,
            features=feat,
            actual_outcome=outcome,
            outcome_recorded_at=now,
            phase_at_outcome=case.phase,
            was_reopened=reopened,
            score_vs_outcome_agrees=agrees,
            notes=" | ".join(p for p in (src_note, notes) if p),
        )
        s.add(row)

    return OutcomeRecord(
        dispute_id=dispute_id,
        queue_generation=generation,
        decision_confidence_score=conf,
        actual_outcome=outcome,
        score_vs_outcome_agrees=agrees,
        was_reopened=reopened,
    )


def get_outcome_log(dispute_id: str | None = None) -> list[dict]:
    """The labelled log — all rows, or just one dispute's, oldest first."""
    with system_session() as s:
        q = select(OutcomeLogEntry).order_by(OutcomeLogEntry.id.asc())
        if dispute_id is not None:
            q = q.where(OutcomeLogEntry.dispute_id == dispute_id)
        return [r.as_dict() for r in s.execute(q).scalars().all()]


def training_pairs() -> list[dict]:
    """``(decision_confidence_score, actual_outcome)`` pairs a retrainer would read."""
    return [
        {
            "dispute_id": r["dispute_id"],
            "queue_generation": r["queue_generation"],
            "confidence_score": r["decision_confidence_score"],
            "outcome": r["actual_outcome"],
            "label": 1 if r["actual_outcome"] == "won" else 0,
            "features": r["features"],
            "agrees": r["score_vs_outcome_agrees"],
        }
        for r in get_outcome_log()
        if r["decision_confidence_score"] is not None
    ]


__all__ = [
    "record_outcome",
    "get_outcome_log",
    "training_pairs",
    "OutcomeRecord",
    "UnknownOutcome",
    "DisputeNotFound",
]
