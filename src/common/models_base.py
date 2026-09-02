"""Shared SQLAlchemy declarative base and cross-module enums.

Schema is written to be Postgres-compatible: no SQLite-only column types or
pragmas. Enums are stored as plain ``String`` columns (not native DB enums) so a
new ``phase`` value never requires a migration on either backend.
"""

from __future__ import annotations

import enum

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base shared by every module's ORM models."""


class DisputeStatus(str, enum.Enum):
    OPEN = "open"
    UNDER_REVIEW = "under_review"
    WON = "won"
    LOST = "lost"
    CLOSED = "closed"


class DisputePhase(str, enum.Enum):
    FRAUD = "fraud"
    RETRIEVAL = "retrieval"
    CHARGEBACK = "chargeback"
    PRE_ARBITRATION = "pre_arbitration"
    ARBITRATION = "arbitration"


# Ordered for "did this dispute escalate?" checks. A move to a later index on an
# existing dispute id is a genuine phase advance (a reopen), not a redelivery.
PHASE_ORDER: tuple[DisputePhase, ...] = (
    DisputePhase.FRAUD,
    DisputePhase.RETRIEVAL,
    DisputePhase.CHARGEBACK,
    DisputePhase.PRE_ARBITRATION,
    DisputePhase.ARBITRATION,
)


def phase_rank(phase: DisputePhase | str) -> int:
    """Index of ``phase`` in :data:`PHASE_ORDER` (-1 if unknown)."""
    value = DisputePhase(phase) if not isinstance(phase, DisputePhase) else phase
    try:
        return PHASE_ORDER.index(value)
    except ValueError:
        return -1


# Terminal recommendation actions the confidence scorer may produce. Note there
# is deliberately no "submitted" / "auto_submitted" action here — moving a
# dispute to a submitted state is a human action recorded via POST
# /disputes/{id}/review, never something the scoring path can emit.
class RecommendedAction(str, enum.Enum):
    DRAFT_FOR_SUBMIT = "draft_for_submit"
    HUMAN_REVIEW = "human_review"
    NEEDS_MANUAL_CLASSIFICATION = "needs_manual_classification"
