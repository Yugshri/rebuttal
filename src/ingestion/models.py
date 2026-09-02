"""ORM models owned by dispute-ingestion-router.

Two tables, both on the shared :class:`src.common.models_base.Base` and written to
the **system** store only (never the read-only external store):

* :class:`DisputeCase` — mirrors Razorpay's Dispute entity. ``id`` is the
  idempotency key. One row per dispute id, updated *in place* as the dispute
  escalates through phases.
* :class:`DisputePhaseHistory` — an append-only trail of every webhook event we
  accepted for a dispute id, so "this was ``won`` at ``chargeback``, then came
  back ``under_review`` at ``pre_arbitration``" is fully reconstructable later
  (confidence-scorer-review's outcome tracker depends on this).

Types are deliberately generic (``BigInteger``, ``String``, ``JSON``, ``Float``,
``Boolean``) so the schema is identical on SQLite and Postgres — no native DB
enums, no SQLite-only pragmas. Enum *values* are validated in Python against the
shared enums in ``models_base`` before they ever reach a column.
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, Float, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from src.common.models_base import Base

# Transition classifications recorded on each DisputePhaseHistory row.
TRANSITION_INITIAL = "initial"
TRANSITION_REDELIVERY = "redelivery"
TRANSITION_PHASE_ADVANCE = "phase_advance"
TRANSITION_STATUS_CHANGE = "status_change"
TRANSITION_OUT_OF_ORDER = "out_of_order"

TRANSITION_TYPES: tuple[str, ...] = (
    TRANSITION_INITIAL,
    TRANSITION_REDELIVERY,
    TRANSITION_PHASE_ADVANCE,
    TRANSITION_STATUS_CHANGE,
    TRANSITION_OUT_OF_ORDER,
)


class DisputeCase(Base):
    """Mirror of Razorpay's Dispute entity plus this system's own working fields.

    One row per Razorpay dispute ``id``. The router only ever writes the columns
    down to ``category`` / ``needs_manual_classification``; ``assembled_evidence``,
    ``confidence_score``, ``recommended_action`` and ``reviewed_by`` are left
    ``NULL`` for downstream modules to populate.
    """

    __tablename__ = "dispute_cases"

    # --- Razorpay Dispute entity (the idempotency key is the dispute id) ---
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    payment_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    amount_deducted: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    reason_code: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_description: Mapped[str | None] = mapped_column(String(512), nullable=True)
    respond_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    phase: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    # --- classification (owned here; source of truth is common/reason_codes.py) ---
    network: Mapped[str] = mapped_column(String(32), nullable=False)
    category: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    needs_manual_classification: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, index=True
    )

    # --- phase-tracking bookkeeping ---
    phase_rank: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reopen_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    event_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    dispute_created_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    first_seen_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # --- filled in by downstream modules; router never writes these ---
    assembled_evidence: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    confidence_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    recommended_action: Mapped[str | None] = mapped_column(String(48), nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)

    phase_history: Mapped[list[DisputePhaseHistory]] = relationship(
        back_populates="dispute",
        order_by="DisputePhaseHistory.seq",
        cascade="all, delete-orphan",
    )


class DisputePhaseHistory(Base):
    """Append-only record of every accepted webhook event for a dispute id.

    A new row is written for the initial event and for every subsequent event
    that we did not treat as a pure redelivery no-op. The dispute's *current*
    state always lives on :class:`DisputeCase`; this table is the trail behind it.
    """

    __tablename__ = "dispute_phase_history"

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dispute_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("dispute_cases.id"), nullable=False, index=True
    )

    phase: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    phase_rank: Mapped[int] = mapped_column(Integer, nullable=False)
    reason_code: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_description: Mapped[str | None] = mapped_column(String(512), nullable=True)
    respond_by: Mapped[int] = mapped_column(BigInteger, nullable=False)

    transition_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # True when this event advanced a dispute out of a terminal status
    # (won / lost / closed) into a later phase — i.e. a genuine reopen.
    is_reopen: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # True when the event arrived describing a phase/time earlier than one we had
    # already recorded (Razorpay does not guarantee ordered delivery).
    out_of_order: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    prev_phase: Mapped[str | None] = mapped_column(String(32), nullable=True)
    prev_status: Mapped[str | None] = mapped_column(String(32), nullable=True)

    webhook_event_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_created_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    recorded_at: Mapped[int] = mapped_column(BigInteger, nullable=False)

    dispute: Mapped[DisputeCase] = relationship(back_populates="phase_history")


Index("ix_phase_history_dispute_seq", DisputePhaseHistory.dispute_id, DisputePhaseHistory.seq)
