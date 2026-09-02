"""Deadline-miss monitor — fires inside 48h of respond_by, silent outside it.

A dispute silently aging past `respond_by` unreviewed is a named production
failure mode in the design doc. This tests `run_deadline_scan` directly, both
directions, rather than trusting the implementation's own claim.
"""

from __future__ import annotations

import pytest

from src.common import db
from src.ingestion import init_system_tables as ingestion_init
from src.ingestion.models import DisputeCase
from src.scoring import init_system_tables as scoring_init
from src.scoring.deadline import (
    DEADLINE_WINDOW_HOURS,
    get_active_deadline_flags,
    run_deadline_scan,
)
from src.scoring.models import DeadlineFlag

_NOW = 1_788_000_000
_H = 3600


@pytest.fixture()
def env(isolated_dbs):
    ingestion_init()
    scoring_init()
    return isolated_dbs


def _seed(dispute_id: str, *, hours_from_now: float, status: str = "open") -> None:
    from src.common.models_base import phase_rank

    with db.system_session() as s:
        s.add(
            DisputeCase(
                id=dispute_id,
                payment_id=f"pay_{dispute_id}",
                amount=250_00,
                amount_deducted=250_00,
                reason_code="visa:13.1",
                reason_description="Merchandise not received",
                respond_by=int(_NOW + hours_from_now * _H),
                status=status,
                phase="chargeback",
                network="visa",
                category="consumer_dispute",
                needs_manual_classification=False,
                phase_rank=phase_rank("chargeback"),
                reopen_count=0,
                event_count=1,
                first_seen_at=_NOW,
                last_updated_at=_NOW,
            )
        )


def test_flags_inside_window_not_outside(env):
    _seed("INSIDE", hours_from_now=20)          # < 48h
    _seed("EDGE", hours_from_now=DEADLINE_WINDOW_HOURS - 0.5)
    _seed("OUTSIDE", hours_from_now=72)          # > 48h

    result = run_deadline_scan(now_epoch=_NOW)

    assert "INSIDE" in result.flagged
    assert "EDGE" in result.flagged
    assert "OUTSIDE" not in result.flagged
    assert result.overdue == []

    with db.system_session() as s:
        flagged = {f.dispute_id for f in s.query(DeadlineFlag).all()}
    assert flagged == {"INSIDE", "EDGE"}


def test_flags_overdue_case(env):
    _seed("OVERDUE", hours_from_now=-10)         # respond_by already passed
    result = run_deadline_scan(now_epoch=_NOW)
    assert "OVERDUE" in result.flagged
    assert "OVERDUE" in result.overdue
    flags = get_active_deadline_flags()
    assert flags and flags[0]["dispute_id"] == "OVERDUE"
    assert flags[0]["overdue"] is True
    assert flags[0]["hours_to_deadline"] < 0


def test_resolved_case_is_not_flagged_and_flag_retires(env):
    _seed("RESOLVED", hours_from_now=5, status="won")   # inside window BUT terminal
    r1 = run_deadline_scan(now_epoch=_NOW)
    assert "RESOLVED" not in r1.flagged

    # now a case that was flagged, then resolves -> next scan retires the flag
    _seed("LIVE", hours_from_now=5, status="open")
    run_deadline_scan(now_epoch=_NOW)
    with db.system_session() as s:
        s.get(DisputeCase, "LIVE").status = "lost"
    r3 = run_deadline_scan(now_epoch=_NOW)
    assert "LIVE" in r3.resolved_since_last_scan
    assert all(f["dispute_id"] != "LIVE" for f in get_active_deadline_flags())


def test_deadline_scan_does_not_dispatch_anything(env):
    """The monitor only flags — it never advances a dispute toward submitted."""
    _seed("URGENT", hours_from_now=1)
    run_deadline_scan(now_epoch=_NOW)
    from src.scoring.models import HumanReviewDecision, ReviewQueueEntry

    with db.system_session() as s:
        assert s.query(HumanReviewDecision).count() == 0
        assert s.query(ReviewQueueEntry).count() == 0
        assert s.get(DisputeCase, "URGENT").status == "open"
        assert s.get(DisputeCase, "URGENT").reviewed_by is None
