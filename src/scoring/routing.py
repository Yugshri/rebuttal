"""Routing: score -> queue, with a STORED sortable priority.

High confidence and no hard gate -> ``draft_for_submit`` queue. That queue is
still logged and still requires an explicit human action
(``POST /disputes/{id}/review``) to actually dispatch — ``draft_for_submit`` is a
real intermediate state, not a rename over an auto-submit. Everything else ->
``human_review`` queue.

Priority
--------
The spec names it "amount x time-to-deadline", with the stated intent that a
**higher amount and a closer deadline both push a case up**. Taken literally,
multiplying by hours-remaining would do the opposite (more time => higher
priority), so we implement the stated intent: priority = amount(rupees) x
urgency, where urgency = HORIZON / clamp(hours_to_deadline). Closer deadline =>
larger urgency => higher priority; bigger amount => higher priority. The value is
written to ``ReviewQueueEntry.priority`` (indexed) so the queue is an
``ORDER BY priority DESC`` read.

Reopen
------
A dispute the outcome log already recorded (typically ``won``) that comes back
via ingestion at a later phase (``pre_arbitration``) must RE-ENTER review. When
``score_and_route`` sees ``was_reopened`` is true and the current active queue
entry is for an earlier phase, it marks that entry ``superseded`` and writes a
fresh ``queue_generation`` — the reopened dispute lands back in ``human_review``
rather than resting on its old ``won`` record.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from sqlalchemy import select

from src.common.db import system_session
from src.common.models_base import RecommendedAction
from src.ingestion.models import DisputeCase
from src.scoring.models import (
    QUEUE_DRAFT_FOR_SUBMIT,
    QUEUE_HUMAN_REVIEW,
    ReviewQueueEntry,
)
from src.scoring.scorer import (
    CONFIDENCE_THRESHOLD,
    GATE_ASSEMBLY_PENDING,
    GATE_DEADLINE_PRESSURE,
    GATE_LATE_PHASE,
    GATE_NEEDS_MANUAL_CLASSIFICATION,
    GATE_NO_EVIDENCE_BUNDLE,
    GATE_REOPENED,
    GATE_RISK_PROFILE_UNKNOWN,
    ScoreResult,
    score_dispute,
)

# --- priority tunables (documented in docs/failure-taxonomy.md) ------------
PRIORITY_MIN_HOURS = 1.0          # imminent / overdue cases clamp here -> max urgency
PRIORITY_HORIZON_HOURS = 720.0   # 30 days: deadlines beyond this stop adding urgency


def compute_priority(amount_paise: int, hours_to_deadline: float | None) -> float:
    """Stored sortable priority. Higher amount and closer deadline both raise it."""
    amount_rupees = (amount_paise or 0) / 100.0
    h = PRIORITY_HORIZON_HOURS if hours_to_deadline is None else hours_to_deadline
    h = min(max(h, PRIORITY_MIN_HOURS), PRIORITY_HORIZON_HOURS)
    urgency = PRIORITY_HORIZON_HOURS / h  # 1.0 .. 720.0
    return round(amount_rupees * urgency, 4)


def _hours_to_deadline(respond_by: int | None, now: int) -> float | None:
    if not respond_by:
        return None
    return round((respond_by - now) / 3600.0, 4)


@dataclass
class RoutingDecision:
    dispute_id: str
    queue: str
    recommended_action: str
    confidence_score: float | None
    priority: float
    hard_gates: list[str]
    rationale: str
    queue_generation: int
    re_entered: bool


# --------------------------------------------------------------------------- #
# rationale — a cited sentence, not a bare number
# --------------------------------------------------------------------------- #
_GATE_PHRASING = {
    GATE_NEEDS_MANUAL_CLASSIFICATION: "reason code is unclassified (needs manual classification)",
    GATE_NO_EVIDENCE_BUNDLE: "no evidence bundle has been assembled yet",
    GATE_ASSEMBLY_PENDING: "evidence assembly is still pending",
    GATE_RISK_PROFILE_UNKNOWN: "counterparty risk is UNKNOWN (no precomputed profile) — not assumed low",
    GATE_REOPENED: "dispute was reopened into a later phase and must be re-reviewed",
    GATE_LATE_PHASE: "dispute is at pre-arbitration or later (own clock, higher stakes)",
    GATE_DEADLINE_PRESSURE: "respond-by is inside 48h and the evidence bundle is not complete",
}


def build_rationale(result: ScoreResult, queue: str, priority: float) -> str:
    si = result.inputs
    category = si.reason_category if si else "unknown"
    parts: list[str] = []

    if queue == QUEUE_DRAFT_FOR_SUBMIT:
        parts.append(
            f"route to draft-for-submit queue (a human still dispatches it) — "
            f"confidence {result.confidence_score:.2f} >= {CONFIDENCE_THRESHOLD:.2f}"
        )
    else:
        if result.hard_gates:
            reasons = "; ".join(
                _GATE_PHRASING.get(g, g) for g in result.hard_gates
            )
            score_txt = (
                f"confidence {result.confidence_score:.2f}"
                if result.confidence_score is not None
                else "no confidence score"
            )
            parts.append(
                f"route to human review — {reasons} ({score_txt})"
            )
        else:
            parts.append(
                f"route to human review — confidence {result.confidence_score:.2f} "
                f"< {CONFIDENCE_THRESHOLD:.2f}"
            )

    if si is not None:
        comp = (
            f"{si.completeness:.0%}" if si.completeness is not None else "n/a"
        )
        dev = (
            f"{si.baseline_deviation:.1f}"
            if si.baseline_deviation is not None
            else "UNKNOWN"
        )
        parts.append(
            f"evidence {comp} complete for {category}; "
            f"counterparty baseline_deviation {dev}"
        )

    if result.compliance_citations:
        cited = sorted(
            {
                f"{c['citation']['title']}"
                for c in result.compliance_citations
                if c.get("citation")
            }
        )
        if cited:
            parts.append("applicable frameworks: " + "; ".join(cited))

    parts.append(f"queue priority {priority:.1f}")
    return " | ".join(parts)


# --------------------------------------------------------------------------- #
# route + persist
# --------------------------------------------------------------------------- #
def decide_queue(result: ScoreResult) -> tuple[str, str]:
    """(queue, recommended_action) from a score result. Pure."""
    if GATE_NEEDS_MANUAL_CLASSIFICATION in result.hard_gates:
        return QUEUE_HUMAN_REVIEW, RecommendedAction.NEEDS_MANUAL_CLASSIFICATION.value
    if result.hard_gates:
        return QUEUE_HUMAN_REVIEW, RecommendedAction.HUMAN_REVIEW.value
    if result.confidence_score is None:
        return QUEUE_HUMAN_REVIEW, RecommendedAction.HUMAN_REVIEW.value
    if result.confidence_score >= CONFIDENCE_THRESHOLD:
        return QUEUE_DRAFT_FOR_SUBMIT, RecommendedAction.DRAFT_FOR_SUBMIT.value
    return QUEUE_HUMAN_REVIEW, RecommendedAction.HUMAN_REVIEW.value


def score_and_route(
    dispute_id: str, *, now_epoch: int | None = None
) -> RoutingDecision:
    """Score the dispute, decide the queue, and persist the queue entry.

    Never writes ``DisputeCase.status`` and never writes ``reviewed_by`` — those
    only move through ``POST /disputes/{id}/review``. It does write the advisory
    ``confidence_score`` / ``recommended_action`` fields on the case.
    """
    now = int(now_epoch if now_epoch is not None else time.time())
    result = score_dispute(dispute_id, now_epoch=now)
    si = result.inputs
    queue, action = decide_queue(result)

    hours = _hours_to_deadline(si.respond_by, now)
    priority = compute_priority(si.amount, hours)
    rationale = build_rationale(result, queue, priority)

    with system_session() as s:
        active = s.execute(
            select(ReviewQueueEntry)
            .where(
                ReviewQueueEntry.dispute_id == dispute_id,
                ReviewQueueEntry.superseded.is_(False),
            )
            .order_by(ReviewQueueEntry.queue_generation.desc())
        ).scalars().first()

        re_entered = False
        generation = 1
        if active is not None:
            # A genuine reopen: the dispute advanced past the phase the active
            # entry was scored at, AND ingestion flagged the reopen. Supersede
            # and start a fresh generation so it re-enters the queue.
            reopened_forward = (
                si.was_reopened
                and si.phase_rank > active.phase_rank
                and (active.dispatched or _has_outcome(s, dispute_id, active.queue_generation))
            )
            if reopened_forward:
                active.superseded = True
                active.updated_at = now
                generation = active.queue_generation + 1
                re_entered = True
                entry = ReviewQueueEntry(
                    dispute_id=dispute_id, queue_generation=generation, created_at=now
                )
                s.add(entry)
            else:
                entry = active
                generation = active.queue_generation
        else:
            entry = ReviewQueueEntry(
                dispute_id=dispute_id, queue_generation=generation, created_at=now
            )
            s.add(entry)

        entry.queue = queue
        entry.recommended_action = action
        entry.confidence_score = result.confidence_score
        entry.score_breakdown = result.breakdown
        entry.hard_gates = list(result.hard_gates)
        entry.priority = priority
        entry.amount = si.amount
        entry.respond_by = si.respond_by
        entry.hours_to_deadline = hours
        entry.phase = si.phase
        entry.phase_rank = si.phase_rank
        entry.status = si.status
        entry.reason_category = si.reason_category
        entry.needs_manual_classification = si.needs_manual_classification
        entry.was_reopened = si.was_reopened
        entry.rationale = rationale
        entry.compliance_citations = list(result.compliance_citations)
        entry.updated_at = now
        # dispatched is NEVER set here.

        # advisory fields on the case (not status, not reviewed_by)
        case = s.get(DisputeCase, dispute_id)
        if case is not None:
            case.confidence_score = result.confidence_score
            case.recommended_action = action

    return RoutingDecision(
        dispute_id=dispute_id,
        queue=queue,
        recommended_action=action,
        confidence_score=result.confidence_score,
        priority=priority,
        hard_gates=list(result.hard_gates),
        rationale=rationale,
        queue_generation=generation,
        re_entered=re_entered,
    )


def _has_outcome(session, dispute_id: str, generation: int) -> bool:
    from src.scoring.models import OutcomeLogEntry

    return (
        session.query(OutcomeLogEntry)
        .filter(
            OutcomeLogEntry.dispute_id == dispute_id,
            OutcomeLogEntry.queue_generation == generation,
        )
        .first()
        is not None
    )


# --------------------------------------------------------------------------- #
# read helpers
# --------------------------------------------------------------------------- #
def get_active_entry(dispute_id: str) -> dict | None:
    with system_session() as s:
        entry = s.execute(
            select(ReviewQueueEntry)
            .where(
                ReviewQueueEntry.dispute_id == dispute_id,
                ReviewQueueEntry.superseded.is_(False),
            )
            .order_by(ReviewQueueEntry.queue_generation.desc())
        ).scalars().first()
        return entry.as_dict() if entry is not None else None


def get_queue(queue: str, *, include_dispatched: bool = False) -> list[dict]:
    """A queue, priority-sorted (most urgent first)."""
    with system_session() as s:
        q = select(ReviewQueueEntry).where(
            ReviewQueueEntry.queue == queue,
            ReviewQueueEntry.superseded.is_(False),
        )
        if not include_dispatched:
            q = q.where(ReviewQueueEntry.dispatched.is_(False))
        q = q.order_by(ReviewQueueEntry.priority.desc(), ReviewQueueEntry.id.asc())
        return [e.as_dict() for e in s.execute(q).scalars().all()]


def get_review_queue(**kw) -> list[dict]:
    return get_queue(QUEUE_HUMAN_REVIEW, **kw)


def get_draft_queue(**kw) -> list[dict]:
    return get_queue(QUEUE_DRAFT_FOR_SUBMIT, **kw)


__all__ = [
    "PRIORITY_MIN_HOURS",
    "PRIORITY_HORIZON_HOURS",
    "compute_priority",
    "RoutingDecision",
    "build_rationale",
    "decide_queue",
    "score_and_route",
    "get_active_entry",
    "get_queue",
    "get_review_queue",
    "get_draft_queue",
]
