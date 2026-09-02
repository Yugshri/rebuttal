"""Ingestion logic: webhook payload -> classified, phase-tracked ``DisputeCase``.

Guarantees:

* **Idempotent on the dispute id.** Replaying a webhook (same ``event_id``, or the
  same phase+status+deadline) never creates a second row and never mutates the
  case.
* **Phase advances update in place, with history.** When an existing dispute id
  arrives at a later phase, the ``DisputeCase`` row is updated and a
  :class:`DisputePhaseHistory` row is appended. If the previous status was
  terminal (``won`` / ``lost`` / ``closed``) the advance is flagged as a reopen.
* **Out-of-order delivery never rolls a case backwards.** An event describing an
  earlier phase (or carrying an older timestamp) than what we already recorded is
  written to history as ``out_of_order`` and otherwise ignored — the case keeps
  its furthest-advanced state.

Writes go to the **system** store only (``system_session``). This module never
opens the external store.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from src.common.db import system_session
from src.common.models_base import (
    DisputePhase,
    DisputeStatus,
    phase_rank,
)
from src.ingestion.classification import ClassificationResult, classify_reason_code
from src.ingestion.models import (
    TRANSITION_INITIAL,
    TRANSITION_OUT_OF_ORDER,
    TRANSITION_PHASE_ADVANCE,
    TRANSITION_REDELIVERY,
    TRANSITION_STATUS_CHANGE,
    DisputeCase,
    DisputePhaseHistory,
)

_TERMINAL_STATUSES = {DisputeStatus.WON.value, DisputeStatus.LOST.value, DisputeStatus.CLOSED.value}


class WebhookPayloadError(ValueError):
    """The webhook payload is missing required fields or carries bad enum values."""


@dataclass(frozen=True)
class DisputeEventFields:
    """The flat set of fields we care about, pulled out of the webhook envelope."""

    id: str
    payment_id: str
    amount: int
    amount_deducted: int
    reason_code: str
    reason_description: str | None
    respond_by: int
    status: str
    phase: str
    network: str | None
    currency: str | None
    dispute_created_at: int | None
    webhook_event_id: str | None
    event_created_at: int | None


@dataclass(frozen=True)
class IngestOutcome:
    """What ``ingest_dispute_event`` did, for the API response and for tests."""

    dispute_id: str
    outcome: str  # created | phase_advance | status_change | out_of_order | redelivery_noop
    transition_type: str
    is_reopen: bool
    out_of_order: bool
    prev_phase: str | None
    prev_status: str | None
    phase: str
    status: str
    category: str
    needs_manual_classification: bool
    reopen_count: int
    event_count: int


# --------------------------------------------------------------------------- #
# payload parsing
# --------------------------------------------------------------------------- #
def _dig(payload: dict[str, Any]) -> dict[str, Any]:
    """Locate the dispute entity dict inside whatever envelope shape we're given."""
    node: Any = payload
    if isinstance(node, dict) and "payload" in node:
        node = node["payload"]
    if isinstance(node, dict) and "dispute" in node:
        node = node["dispute"]
    if isinstance(node, dict) and "entity" in node:
        node = node["entity"]
    if not isinstance(node, dict) or "id" not in node:
        raise WebhookPayloadError(
            "could not locate a dispute entity in the webhook payload"
        )
    return node


def _require(entity: dict[str, Any], key: str) -> Any:
    if key not in entity or entity[key] in (None, ""):
        raise WebhookPayloadError(f"missing required dispute field: {key!r}")
    return entity[key]


def _int(value: Any, key: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
        raise WebhookPayloadError(f"dispute field {key!r} is not an integer: {value!r}") from exc


def extract_dispute_fields(payload: dict[str, Any]) -> DisputeEventFields:
    """Parse + validate the webhook payload. Raises :class:`WebhookPayloadError`.

    ``network`` and ``reason_code`` are read as separate fields off the dispute
    entity (``payload.dispute.entity.network`` / ``.reason_code``); the classifier
    lower-cases and strips them and joins them as ``"<network>:<code>"``.
    """
    if not isinstance(payload, dict):
        raise WebhookPayloadError("webhook payload must be a JSON object")

    envelope_event_id = payload.get("event_id") if isinstance(payload, dict) else None
    envelope_created_at = payload.get("created_at") if isinstance(payload, dict) else None

    entity = _dig(payload)

    status = str(_require(entity, "status")).strip().lower()
    phase = str(_require(entity, "phase")).strip().lower()
    try:
        DisputeStatus(status)
    except ValueError as exc:
        raise WebhookPayloadError(f"unknown dispute status: {status!r}") from exc
    try:
        DisputePhase(phase)
    except ValueError as exc:
        raise WebhookPayloadError(f"unknown dispute phase: {phase!r}") from exc

    amount = _int(_require(entity, "amount"), "amount")
    amount_deducted = _int(entity.get("amount_deducted", amount), "amount_deducted")
    event_created_at = entity.get("created_at", envelope_created_at)

    return DisputeEventFields(
        id=str(_require(entity, "id")).strip(),
        payment_id=str(_require(entity, "payment_id")).strip(),
        amount=amount,
        amount_deducted=amount_deducted,
        reason_code=str(_require(entity, "reason_code")).strip(),
        reason_description=(
            str(entity["reason_description"]).strip()
            if entity.get("reason_description")
            else None
        ),
        respond_by=_int(_require(entity, "respond_by"), "respond_by"),
        status=status,
        phase=phase,
        network=(str(entity["network"]).strip().lower() if entity.get("network") else None),
        currency=(str(entity["currency"]).strip() if entity.get("currency") else None),
        dispute_created_at=(_int(entity["created_at"], "created_at") if entity.get("created_at") else None),
        webhook_event_id=(str(envelope_event_id).strip() if envelope_event_id else None),
        event_created_at=(_int(event_created_at, "created_at") if event_created_at else None),
    )


# --------------------------------------------------------------------------- #
# ingestion
# --------------------------------------------------------------------------- #
def _new_case(f: DisputeEventFields, cls: ClassificationResult, now: int) -> DisputeCase:
    return DisputeCase(
        id=f.id,
        payment_id=f.payment_id,
        amount=f.amount,
        amount_deducted=f.amount_deducted,
        reason_code=f.reason_code,
        reason_description=f.reason_description,
        respond_by=f.respond_by,
        status=f.status,
        phase=f.phase,
        network=cls.network or (f.network or ""),
        category=cls.category,
        needs_manual_classification=cls.needs_manual_classification,
        phase_rank=phase_rank(f.phase),
        reopen_count=0,
        event_count=1,
        currency=f.currency,
        dispute_created_at=f.dispute_created_at,
        first_seen_at=now,
        last_updated_at=now,
    )


def _history_row(
    f: DisputeEventFields,
    *,
    transition_type: str,
    is_reopen: bool,
    out_of_order: bool,
    prev_phase: str | None,
    prev_status: str | None,
    now: int,
) -> DisputePhaseHistory:
    return DisputePhaseHistory(
        dispute_id=f.id,
        phase=f.phase,
        status=f.status,
        phase_rank=phase_rank(f.phase),
        reason_code=f.reason_code,
        reason_description=f.reason_description,
        respond_by=f.respond_by,
        transition_type=transition_type,
        is_reopen=is_reopen,
        out_of_order=out_of_order,
        prev_phase=prev_phase,
        prev_status=prev_status,
        webhook_event_id=f.webhook_event_id,
        event_created_at=f.event_created_at,
        recorded_at=now,
    )


def ingest_dispute_event(payload: dict[str, Any]) -> IngestOutcome:
    """Idempotent upsert of one ``dispute.created`` webhook event."""
    f = extract_dispute_fields(payload)
    cls = classify_reason_code(f.network, f.reason_code)
    now = int(time.time())

    with system_session() as s:
        existing = s.get(DisputeCase, f.id)

        if existing is None:
            case = _new_case(f, cls, now)
            s.add(case)
            s.flush()
            s.add(
                _history_row(
                    f,
                    transition_type=TRANSITION_INITIAL,
                    is_reopen=False,
                    out_of_order=False,
                    prev_phase=None,
                    prev_status=None,
                    now=now,
                )
            )
            return IngestOutcome(
                dispute_id=f.id,
                outcome="created",
                transition_type=TRANSITION_INITIAL,
                is_reopen=False,
                out_of_order=False,
                prev_phase=None,
                prev_status=None,
                phase=case.phase,
                status=case.status,
                category=case.category,
                needs_manual_classification=case.needs_manual_classification,
                reopen_count=case.reopen_count,
                event_count=case.event_count,
            )

        history = list(existing.phase_history)
        prev_phase, prev_status = existing.phase, existing.status
        incoming_rank = phase_rank(f.phase)

        # 1. exact redelivery: same webhook event id already recorded.
        if f.webhook_event_id and any(
            h.webhook_event_id == f.webhook_event_id for h in history
        ):
            return _noop_outcome(existing, prev_phase, prev_status)

        # 2. redelivery no-op: same phase, same status, same deadline.
        if (
            f.phase == existing.phase
            and f.status == existing.status
            and f.respond_by == existing.respond_by
        ):
            return _noop_outcome(existing, prev_phase, prev_status)

        latest_event_ts = max((h.event_created_at or 0) for h in history) if history else 0
        looks_older = (
            f.event_created_at is not None
            and latest_event_ts
            and f.event_created_at < latest_event_ts
        )

        # 3. genuine phase advance -> update in place, preserve history.
        if incoming_rank > existing.phase_rank:
            is_reopen = existing.status in _TERMINAL_STATUSES
            existing.phase = f.phase
            existing.phase_rank = incoming_rank
            existing.status = f.status
            existing.reason_code = f.reason_code
            existing.reason_description = f.reason_description
            existing.respond_by = f.respond_by
            existing.amount = f.amount
            existing.amount_deducted = f.amount_deducted
            existing.category = cls.category
            existing.needs_manual_classification = cls.needs_manual_classification
            if cls.network:
                existing.network = cls.network
            if is_reopen:
                existing.reopen_count += 1
            existing.event_count += 1
            existing.last_updated_at = now
            s.add(
                _history_row(
                    f,
                    transition_type=TRANSITION_PHASE_ADVANCE,
                    is_reopen=is_reopen,
                    out_of_order=False,
                    prev_phase=prev_phase,
                    prev_status=prev_status,
                    now=now,
                )
            )
            return _outcome(existing, "phase_advance", TRANSITION_PHASE_ADVANCE, is_reopen, False, prev_phase, prev_status)

        # 4. out-of-order / stale: earlier phase, or an older timestamp. Record,
        #    do not roll the case back.
        if incoming_rank < existing.phase_rank or looks_older:
            existing.event_count += 1
            existing.last_updated_at = now
            s.add(
                _history_row(
                    f,
                    transition_type=TRANSITION_OUT_OF_ORDER,
                    is_reopen=False,
                    out_of_order=True,
                    prev_phase=prev_phase,
                    prev_status=prev_status,
                    now=now,
                )
            )
            return _outcome(existing, "out_of_order", TRANSITION_OUT_OF_ORDER, False, True, prev_phase, prev_status)

        # 5. same phase, status or deadline changed -> amend in place.
        existing.status = f.status
        existing.respond_by = f.respond_by
        existing.reason_description = f.reason_description
        existing.event_count += 1
        existing.last_updated_at = now
        s.add(
            _history_row(
                f,
                transition_type=TRANSITION_STATUS_CHANGE,
                is_reopen=False,
                out_of_order=False,
                prev_phase=prev_phase,
                prev_status=prev_status,
                now=now,
            )
        )
        return _outcome(existing, "status_change", TRANSITION_STATUS_CHANGE, False, False, prev_phase, prev_status)


def _outcome(
    case: DisputeCase,
    outcome: str,
    transition_type: str,
    is_reopen: bool,
    out_of_order: bool,
    prev_phase: str | None,
    prev_status: str | None,
) -> IngestOutcome:
    return IngestOutcome(
        dispute_id=case.id,
        outcome=outcome,
        transition_type=transition_type,
        is_reopen=is_reopen,
        out_of_order=out_of_order,
        prev_phase=prev_phase,
        prev_status=prev_status,
        phase=case.phase,
        status=case.status,
        category=case.category,
        needs_manual_classification=case.needs_manual_classification,
        reopen_count=case.reopen_count,
        event_count=case.event_count,
    )


def _noop_outcome(case: DisputeCase, prev_phase: str, prev_status: str) -> IngestOutcome:
    return IngestOutcome(
        dispute_id=case.id,
        outcome="redelivery_noop",
        transition_type=TRANSITION_REDELIVERY,
        is_reopen=False,
        out_of_order=False,
        prev_phase=prev_phase,
        prev_status=prev_status,
        phase=case.phase,
        status=case.status,
        category=case.category,
        needs_manual_classification=case.needs_manual_classification,
        reopen_count=case.reopen_count,
        event_count=case.event_count,
    )


# --------------------------------------------------------------------------- #
# read helpers (phase history is queryable after the fact)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PhaseHistoryEntry:
    seq: int
    phase: str
    status: str
    phase_rank: int
    transition_type: str
    is_reopen: bool
    out_of_order: bool
    prev_phase: str | None
    prev_status: str | None
    reason_code: str
    respond_by: int
    webhook_event_id: str | None
    event_created_at: int | None
    recorded_at: int


def get_dispute_case(dispute_id: str) -> dict[str, Any] | None:
    """Current state of a dispute case as a plain dict (or ``None``)."""
    with system_session() as s:
        case = s.get(DisputeCase, dispute_id)
        if case is None:
            return None
        return {
            "id": case.id,
            "payment_id": case.payment_id,
            "amount": case.amount,
            "amount_deducted": case.amount_deducted,
            "reason_code": case.reason_code,
            "reason_description": case.reason_description,
            "respond_by": case.respond_by,
            "status": case.status,
            "phase": case.phase,
            "network": case.network,
            "category": case.category,
            "needs_manual_classification": case.needs_manual_classification,
            "phase_rank": case.phase_rank,
            "reopen_count": case.reopen_count,
            "event_count": case.event_count,
            "currency": case.currency,
            "dispute_created_at": case.dispute_created_at,
            "first_seen_at": case.first_seen_at,
            "last_updated_at": case.last_updated_at,
            "assembled_evidence": case.assembled_evidence,
            "confidence_score": case.confidence_score,
            "recommended_action": case.recommended_action,
            "reviewed_by": case.reviewed_by,
        }


def get_phase_history(dispute_id: str) -> list[PhaseHistoryEntry]:
    """Full ordered phase/status trail for a dispute id (empty if unknown)."""
    with system_session() as s:
        case = s.get(DisputeCase, dispute_id)
        if case is None:
            return []
        return [
            PhaseHistoryEntry(
                seq=h.seq,
                phase=h.phase,
                status=h.status,
                phase_rank=h.phase_rank,
                transition_type=h.transition_type,
                is_reopen=h.is_reopen,
                out_of_order=h.out_of_order,
                prev_phase=h.prev_phase,
                prev_status=h.prev_status,
                reason_code=h.reason_code,
                respond_by=h.respond_by,
                webhook_event_id=h.webhook_event_id,
                event_created_at=h.event_created_at,
                recorded_at=h.recorded_at,
            )
            for h in sorted(case.phase_history, key=lambda h: h.seq)
        ]


def was_reopened(dispute_id: str) -> bool:
    """True if this dispute ever advanced out of a terminal status into a later phase."""
    with system_session() as s:
        case = s.get(DisputeCase, dispute_id)
        if case is None:
            return False
        if case.reopen_count > 0:
            return True
        return any(h.is_reopen for h in case.phase_history)
