"""The 48-hour deadline monitor.

``run_deadline_scan()`` is a plain callable with no scheduler bound to it — the
same "could be scheduled, nothing starts a daemon" pattern as
``src.risk.batch.run_nightly_batch``. A cron / APScheduler / Airflow trigger
could call it every few minutes; this repo just exposes the function.

It flags every ``DisputeCase`` that is:
* within 48 hours of ``respond_by`` (including already overdue — negative hours), and
* still unresolved (status is ``open`` or ``under_review``, not ``won`` / ``lost``
  / ``closed``).

A dispute silently aging past its deadline unreviewed is a real production
failure mode; this is the designed-against control for it. Flags carry the
``respond_by`` compliance citations so the row reads as a cited obligation, not a
bare timer.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select

from src.common.db import system_session
from src.common.models_base import DisputeStatus
from src.ingestion.models import DisputeCase
from src.scoring.models import DeadlineFlag

try:
    from src.compliance import lookup as _compliance_lookup
except Exception:  # pragma: no cover - defensive only
    _compliance_lookup = None

DEADLINE_WINDOW_HOURS = 48.0

_UNRESOLVED_STATUSES = {DisputeStatus.OPEN.value, DisputeStatus.UNDER_REVIEW.value}


@dataclass
class DeadlineScanResult:
    scan_run_id: str
    scanned_at: int
    n_cases: int
    flagged: list[str] = field(default_factory=list)
    overdue: list[str] = field(default_factory=list)
    resolved_since_last_scan: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "scan_run_id": self.scan_run_id,
            "scanned_at": self.scanned_at,
            "n_cases": self.n_cases,
            "flagged": list(self.flagged),
            "overdue": list(self.overdue),
            "resolved_since_last_scan": list(self.resolved_since_last_scan),
        }


def _respond_by_citations() -> list[dict]:
    if _compliance_lookup is None:
        return []
    try:
        return [m.model_dump() for m in _compliance_lookup("respond_by")]
    except Exception:  # pragma: no cover - defensive
        return []


def run_deadline_scan(*, now_epoch: int | None = None) -> DeadlineScanResult:
    """Flag unresolved disputes within 48h of ``respond_by``. Idempotent per run."""
    now = int(now_epoch if now_epoch is not None else time.time())
    run_id = uuid.uuid4().hex[:12]
    citations = _respond_by_citations()

    flagged: list[str] = []
    overdue: list[str] = []
    resolved_since: list[str] = []

    with system_session() as s:
        cases = s.execute(select(DisputeCase)).scalars().all()
        existing = {
            f.dispute_id: f
            for f in s.execute(select(DeadlineFlag)).scalars().all()
        }

        for case in cases:
            hours = (
                (case.respond_by - now) / 3600.0 if case.respond_by else None
            )
            unresolved = case.status in _UNRESOLVED_STATUSES
            within_window = hours is not None and hours <= DEADLINE_WINDOW_HOURS

            flag = existing.get(case.id)

            if not unresolved:
                # dispute reached a terminal status — retire any active flag.
                if flag is not None and not flag.resolved:
                    flag.resolved = True
                    flag.last_scanned_at = now
                    flag.status = case.status
                    resolved_since.append(case.id)
                continue

            if not within_window:
                continue

            is_overdue = hours is not None and hours < 0
            rationale = (
                f"dispute {case.id} is "
                + (
                    f"{abs(hours):.1f}h OVERDUE"
                    if is_overdue
                    else f"{hours:.1f}h from respond_by"
                )
                + f" and still {case.status} (phase {case.phase}) — must be "
                "reviewed/dispatched before the window closes"
            )

            if flag is None:
                flag = DeadlineFlag(
                    dispute_id=case.id,
                    first_flagged_at=now,
                )
                s.add(flag)
            flag.last_scanned_at = now
            flag.respond_by = case.respond_by
            flag.hours_to_deadline = round(hours, 4)
            flag.overdue = bool(is_overdue)
            flag.phase = case.phase
            flag.status = case.status
            flag.amount = case.amount
            flag.resolved = False
            flag.scan_run_id = run_id
            flag.compliance_citations = list(citations)
            flag.rationale = rationale

            flagged.append(case.id)
            if is_overdue:
                overdue.append(case.id)

        n_cases = len(cases)

    return DeadlineScanResult(
        scan_run_id=run_id,
        scanned_at=now,
        n_cases=n_cases,
        flagged=sorted(flagged),
        overdue=sorted(overdue),
        resolved_since_last_scan=sorted(resolved_since),
    )


def get_active_deadline_flags() -> list[dict]:
    """All unresolved deadline flags, most urgent (smallest hours) first."""
    with system_session() as s:
        rows = (
            s.execute(
                select(DeadlineFlag)
                .where(DeadlineFlag.resolved.is_(False))
                .order_by(DeadlineFlag.hours_to_deadline.asc())
            )
            .scalars()
            .all()
        )
        return [r.as_dict() for r in rows]


__all__ = [
    "DEADLINE_WINDOW_HOURS",
    "DeadlineScanResult",
    "run_deadline_scan",
    "get_active_deadline_flags",
]


if __name__ == "__main__":  # `python -m src.scoring.deadline` — the schedulable entry
    import json

    print(json.dumps(run_deadline_scan().as_dict(), indent=2))
