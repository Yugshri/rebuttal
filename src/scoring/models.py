"""ORM models owned by confidence-scorer-review.

All four tables live on the shared :class:`src.common.models_base.Base` and are
written to the **system** store only (``system.db``) via ``system_session()``.
This module never opens the read-only external store directly — it consumes the
account id resolved from ``payments`` (read-only) and the already-computed
``EvidenceBundle`` / ``AccountRiskProfile`` rows.

Tables
------
* :class:`ReviewQueueEntry` — one active row per dispute id (older rows kept and
  marked ``superseded`` when a dispute reopens). Carries a **stored, sortable**
  ``priority`` so the human review queue is an ``ORDER BY priority DESC`` read,
  not an in-memory re-sort. ``dispatched`` flips to ``True`` **only** through
  ``POST /disputes/{id}/review`` — no scoring-path function writes it.
* :class:`HumanReviewDecision` — the append-only record ``POST
  /disputes/{id}/review`` writes: who reviewed, what they decided, when, and the
  confidence score / recommended action **as they stood at decision time**.
* :class:`DeadlineFlag` — one active row per dispute flagged by
  :func:`src.scoring.deadline.run_deadline_scan` as within 48h of ``respond_by``
  and still unresolved.
* :class:`OutcomeLogEntry` — decision-time confidence paired with the actual
  ``won`` / ``lost`` outcome, in a flat shape a retraining step could consume as
  a labelled example. Append-only (a reopened dispute that resolves again adds a
  second row).

Types are deliberately generic (``String``, ``Integer``, ``Float``, ``Boolean``,
``JSON``, ``Text``) so the schema is identical on SQLite and Postgres — no native
DB enums, no SQLite-only features. Enum *values* are validated in Python against
``src.common.models_base`` before they reach a column.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from src.common.models_base import Base

# --- Queue identifiers --------------------------------------------------------
QUEUE_DRAFT_FOR_SUBMIT = "draft_for_submit"
QUEUE_HUMAN_REVIEW = "human_review"
QUEUES: tuple[str, ...] = (QUEUE_DRAFT_FOR_SUBMIT, QUEUE_HUMAN_REVIEW)

# --- Human review decision vocabulary ---------------------------------------
# `submit` is the only decision that advances a dispute toward a dispatched
# state, and it is a HUMAN action recorded here — never something the scoring
# path can emit (see src/common/models_base.py: RecommendedAction has no
# submitted member).
DECISION_SUBMIT = "submit"
DECISION_DECLINE = "decline"
DECISION_REQUEST_CHANGES = "request_changes"
DECISIONS: tuple[str, ...] = (
    DECISION_SUBMIT,
    DECISION_DECLINE,
    DECISION_REQUEST_CHANGES,
)

# --- Outcome vocabulary ----------------------------------------------------
OUTCOME_WON = "won"
OUTCOME_LOST = "lost"
OUTCOMES: tuple[str, ...] = (OUTCOME_WON, OUTCOME_LOST)


class ReviewQueueEntry(Base):
    """The routing decision for one dispute, plus its stored queue priority.

    One row per ``(dispute_id, queue_generation)``. The current entry is the
    highest-generation row with ``superseded = False``; when a dispute reopens
    (see :func:`src.scoring.routing.score_and_route`) the prior entry is marked
    ``superseded`` and a fresh generation is written, so a reopened dispute
    re-enters the queue instead of being ignored.
    """

    __tablename__ = "review_queue_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dispute_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    queue_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    queue: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    recommended_action: Mapped[str] = mapped_column(String(48), nullable=False)

    # --- the transparent confidence score + its full per-factor breakdown ---
    confidence_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    score_breakdown: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    hard_gates: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    # --- priority: STORED and sortable (higher = more urgent) --------------
    priority: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0, index=True
    )
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    respond_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    hours_to_deadline: Mapped[float | None] = mapped_column(Float, nullable=True)

    # --- snapshots of the dispute at scoring time -------------------------
    phase: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    phase_rank: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    reason_category: Mapped[str] = mapped_column(String(48), nullable=False, default="")
    needs_manual_classification: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    was_reopened: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # --- explainability: cited rationale, not a bare number ----------------
    rationale: Mapped[str] = mapped_column(Text, nullable=False, default="")
    compliance_citations: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list
    )

    # --- dispatch state: flips to True ONLY via POST /disputes/{id}/review --
    dispatched: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, index=True
    )
    dispatched_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    dispatched_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    superseded: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, index=True
    )

    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "dispute_id", "queue_generation", name="uq_review_queue_dispute_gen"
        ),
        Index("ix_review_queue_active", "superseded", "queue", "priority"),
    )

    def as_dict(self) -> dict:
        return {
            "dispute_id": self.dispute_id,
            "queue_generation": self.queue_generation,
            "queue": self.queue,
            "recommended_action": self.recommended_action,
            "confidence_score": self.confidence_score,
            "score_breakdown": dict(self.score_breakdown or {}),
            "hard_gates": list(self.hard_gates or []),
            "priority": self.priority,
            "amount": self.amount,
            "respond_by": self.respond_by,
            "hours_to_deadline": self.hours_to_deadline,
            "phase": self.phase,
            "phase_rank": self.phase_rank,
            "status": self.status,
            "reason_category": self.reason_category,
            "needs_manual_classification": self.needs_manual_classification,
            "was_reopened": self.was_reopened,
            "rationale": self.rationale,
            "compliance_citations": list(self.compliance_citations or []),
            "dispatched": self.dispatched,
            "dispatched_at": self.dispatched_at,
            "dispatched_by": self.dispatched_by,
            "superseded": self.superseded,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class HumanReviewDecision(Base):
    """Append-only record of one human decision via ``POST /disputes/{id}/review``.

    This is the ONLY table whose rows can move a dispute toward a dispatched /
    submitted state, and only a human writes it.
    """

    __tablename__ = "human_review_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dispute_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    queue_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    reviewed_by: Mapped[str] = mapped_column(String(128), nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # --- state as it stood at decision time (for the outcome / audit trail) ---
    confidence_score_at_decision: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    recommended_action_at_decision: Mapped[str | None] = mapped_column(
        String(48), nullable=True
    )
    queue_at_decision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    dispute_status_before: Mapped[str | None] = mapped_column(String(32), nullable=True)
    dispute_status_after: Mapped[str | None] = mapped_column(String(32), nullable=True)
    dispute_phase_at_decision: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )

    __table_args__ = (
        Index("ix_review_decision_dispute", "dispute_id", "decided_at"),
    )

    def as_dict(self) -> dict:
        return {
            "dispute_id": self.dispute_id,
            "queue_generation": self.queue_generation,
            "reviewed_by": self.reviewed_by,
            "decision": self.decision,
            "note": self.note,
            "decided_at": self.decided_at,
            "confidence_score_at_decision": self.confidence_score_at_decision,
            "recommended_action_at_decision": self.recommended_action_at_decision,
            "queue_at_decision": self.queue_at_decision,
            "dispute_status_before": self.dispute_status_before,
            "dispute_status_after": self.dispute_status_after,
            "dispute_phase_at_decision": self.dispute_phase_at_decision,
        }


class DeadlineFlag(Base):
    """A dispute flagged as within 48h of ``respond_by`` and still unresolved.

    One active (``resolved = False``) row per dispute id; a later scan updates it
    in place. ``run_deadline_scan`` sets ``resolved = True`` once the dispute
    reaches a terminal status.
    """

    __tablename__ = "deadline_flags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dispute_id: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )

    first_flagged_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_scanned_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    respond_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    hours_to_deadline: Mapped[float] = mapped_column(Float, nullable=False)
    overdue: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    phase: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    resolved: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, index=True
    )

    scan_run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    compliance_citations: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list
    )
    rationale: Mapped[str] = mapped_column(Text, nullable=False, default="")

    def as_dict(self) -> dict:
        return {
            "dispute_id": self.dispute_id,
            "first_flagged_at": self.first_flagged_at,
            "last_scanned_at": self.last_scanned_at,
            "respond_by": self.respond_by,
            "hours_to_deadline": self.hours_to_deadline,
            "overdue": self.overdue,
            "phase": self.phase,
            "status": self.status,
            "amount": self.amount,
            "resolved": self.resolved,
            "scan_run_id": self.scan_run_id,
            "compliance_citations": list(self.compliance_citations or []),
            "rationale": self.rationale,
        }


class OutcomeLogEntry(Base):
    """Decision-time confidence next to the actual outcome — a labelled example.

    Append-only. A reopened dispute that resolves a second time writes a second
    row (distinguished by ``queue_generation`` / ``phase_at_outcome``). The
    ``features`` blob is the flat input a retraining step would consume; the
    "feeds back into the scorer" arrow in the architecture diagram is a
    documented next step, not live at demo time (see failure taxonomy).
    """

    __tablename__ = "outcome_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dispute_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    queue_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # --- decision-time snapshot (the X of the training pair) --------------
    decision_confidence_score: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    decision_recommended_action: Mapped[str | None] = mapped_column(
        String(48), nullable=True
    )
    decision_queue: Mapped[str | None] = mapped_column(String(32), nullable=True)
    decision_was_dispatched: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    reviewed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    decided_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    features: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    # --- actual outcome (the y of the training pair) ---------------------
    actual_outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    outcome_recorded_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    phase_at_outcome: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    was_reopened: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # --- convenience: did the score and the outcome agree? --------------
    # score_vs_outcome_agrees is True when a high-confidence draft won, or a
    # low-confidence deferral lost. NULL when there is no confidence score.
    score_vs_outcome_agrees: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_outcome_dispute_gen", "dispute_id", "queue_generation"),
    )

    def as_dict(self) -> dict:
        return {
            "dispute_id": self.dispute_id,
            "queue_generation": self.queue_generation,
            "decision_confidence_score": self.decision_confidence_score,
            "decision_recommended_action": self.decision_recommended_action,
            "decision_queue": self.decision_queue,
            "decision_was_dispatched": self.decision_was_dispatched,
            "reviewed_by": self.reviewed_by,
            "decided_at": self.decided_at,
            "features": dict(self.features or {}),
            "actual_outcome": self.actual_outcome,
            "outcome_recorded_at": self.outcome_recorded_at,
            "phase_at_outcome": self.phase_at_outcome,
            "was_reopened": self.was_reopened,
            "score_vs_outcome_agrees": self.score_vs_outcome_agrees,
            "notes": self.notes,
        }


ALL_TABLES = (
    ReviewQueueEntry.__table__,
    HumanReviewDecision.__table__,
    DeadlineFlag.__table__,
    OutcomeLogEntry.__table__,
)

__all__ = [
    "ReviewQueueEntry",
    "HumanReviewDecision",
    "DeadlineFlag",
    "OutcomeLogEntry",
    "QUEUE_DRAFT_FOR_SUBMIT",
    "QUEUE_HUMAN_REVIEW",
    "QUEUES",
    "DECISION_SUBMIT",
    "DECISION_DECLINE",
    "DECISION_REQUEST_CHANGES",
    "DECISIONS",
    "OUTCOME_WON",
    "OUTCOME_LOST",
    "OUTCOMES",
    "ALL_TABLES",
]
