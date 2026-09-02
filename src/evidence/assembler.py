"""Assembly logic: a classified ``DisputeCase`` -> a persisted ``EvidenceBundle``.

Flow for one dispute id:

1. Read the ``DisputeCase`` from the **system** store (read only — this module
   never writes that row; ``DisputeCase.assembled_evidence`` is the
   scorer/integration layer's field, not ours).
2. Resolve the category -> required slots via
   ``src.common.reason_codes.required_slots``.
3. For each of the 8 Razorpay evidence slots decide exactly one of:
   ``present`` (a real record was found) / ``missing`` (required, looked, absent)
   / ``not_applicable`` (this category does not need it).
4. Build ``explanation_letter`` from the *present* slots only (LLM with
   deterministic template fallback — see ``letter.py``).
5. Compute the completeness measure + ``assembly_status``.
6. Attach DPDP data-minimisation citations for the PII-bearing slots.
7. Persist the ``EvidenceBundle`` (upsert on dispute id).

A ``needs_manual_classification`` dispute (category unresolved, no required
slots) is **not** assembled — it gets a bundle flagged ``pending`` /
``needs_manual_classification=True`` for the scorer to route to a human. Nothing
is guessed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from src.common import reason_codes as rc
from src.common.db import system_session
from src.evidence import letter as letter_mod
from src.evidence.models import (
    ASSEMBLY_COMPLETE,
    ASSEMBLY_PARTIAL,
    ASSEMBLY_PENDING,
    LETTER_SOURCE_NONE,
    SLOT_MISSING,
    SLOT_NOT_APPLICABLE,
    SLOT_PRESENT,
    EvidenceBundle,
)
from src.evidence.store import EvidenceRecords, fetch_records

# PII-bearing slots get a DPDP data-minimisation citation attached when they are
# required for the category (present or missing — the concern is about holding
# the data category at all, not just when a record was found).
try:  # compliance graph is a sibling module; degrade gracefully if absent
    from src.compliance import lookup as _compliance_lookup
except Exception:  # pragma: no cover - defensive only
    _compliance_lookup = None

_DEADLINE_PRESSURE_HOURS = 48.0
_SLOT_ORDER = rc.EVIDENCE_SLOTS


class DisputeNotFound(Exception):
    """No ``DisputeCase`` row exists for this dispute id."""


@dataclass
class AssemblyResult:
    """What ``assemble_evidence`` produced — for the API response and for tests."""

    dispute_id: str
    payment_id: str
    category: str
    needs_manual_classification: bool
    required_slots: list[str]
    slot_status: dict[str, str]
    completeness: float | None
    present_count: int
    missing_count: int
    not_applicable_count: int
    assembly_status: str
    explanation_letter_source: str
    compliance_citations: dict[str, list]
    respond_by: int | None
    hours_to_deadline: float | None
    deadline_pressure: bool
    notes: str | None = None
    guard_note: str | None = None
    slot_content: dict[str, Any] = field(default_factory=dict)
    slot_sources: dict[str, str] = field(default_factory=dict)

    # --- the completeness signal confidence-scorer-review consumes ---
    def completeness_signal(self) -> dict[str, Any]:
        return {
            "dispute_id": self.dispute_id,
            "category": self.category,
            "needs_manual_classification": self.needs_manual_classification,
            "assembly_status": self.assembly_status,
            "completeness": self.completeness,
            "present_count": self.present_count,
            "missing_count": self.missing_count,
            "slot_status": dict(self.slot_status),
            "deadline_pressure": self.deadline_pressure,
            "hours_to_deadline": self.hours_to_deadline,
        }


# --------------------------------------------------------------------------- #
# per-slot extraction — decide present/missing and pull real content
# --------------------------------------------------------------------------- #
def _extract_slot(
    slot: str, rec: EvidenceRecords
) -> tuple[dict[str, Any] | None, str, dict[str, Any]]:
    """Return ``(content, source, letter_fact)`` for a slot, or ``(None, reason, {})``.

    ``content is None`` means "no record" -> the caller marks the slot `missing`.
    """
    available = rec.availability.get(slot, False)
    if not available:
        return None, "evidence_availability: no record for this slot", {}

    if slot == rc.SHIPPING_PROOF:
        if not rec.shipment:
            return None, "availability flag set but no shipments row", {}
        s = rec.shipment
        content = {
            "carrier": s.get("carrier"),
            "tracking_number": s.get("tracking_number"),
            "tracking_valid": s.get("tracking_valid"),
            "shipped_at": s.get("shipped_at"),
            "delivered_at": s.get("delivered_at"),
            "delivery_status": s.get("delivery_status"),
            "signature_captured": s.get("signature_captured"),
            "ship_to_address": rec.shipping_address,
            "billing_shipping_address_match": rec.match_flags.get(
                "billing_shipping_address_match"
            ),
        }
        fact = {
            "carrier": s.get("carrier"),
            "tracking_number": s.get("tracking_number"),
            "delivery_status": s.get("delivery_status"),
            "delivered_at": s.get("delivered_at"),
            "signature_captured": bool(s.get("signature_captured")),
        }
        return content, "shipments", fact

    if slot == rc.PROOF_OF_SERVICE:
        if not rec.order:
            return None, "availability flag set but no orders row", {}
        o = rec.order
        content = {
            "order_id": o.get("order_id"),
            "item_category": o.get("item_category"),
            "item_description": o.get("item_description"),
            "quantity": o.get("quantity"),
            "price_point": o.get("price_point"),
            "order_date": o.get("order_date"),
            "delivery_status": (rec.shipment or {}).get("delivery_status"),
        }
        fact = {
            "item_description": o.get("item_description"),
            "item_category": o.get("item_category"),
            "quantity": o.get("quantity"),
            "order_date": o.get("order_date"),
        }
        return content, "orders", fact

    if slot == rc.CUSTOMER_COMMUNICATION:
        if not rec.communications:
            return None, "availability flag set but no communications rows", {}
        msgs = rec.communications
        channels = sorted({m.get("channel") for m in msgs if m.get("channel")})
        content = {
            "message_count": len(msgs),
            "channels": channels,
            "messages": [
                {
                    "channel": m.get("channel"),
                    "direction": m.get("direction"),
                    "timestamp": m.get("timestamp"),
                    "summary": m.get("summary"),
                }
                for m in msgs
            ],
            "email_domain_consistent": rec.match_flags.get("email_domain_consistent"),
        }
        fact = {
            "message_count": len(msgs),
            "channels": channels,
            "first_timestamp": msgs[0].get("timestamp"),
            "last_timestamp": msgs[-1].get("timestamp"),
        }
        return content, "communications", fact

    if slot == rc.BILLING_PROOF:
        if not rec.billing_address:
            return None, "availability flag set but no billing address row", {}
        pay = rec.payment or {}
        content = {
            "billing_address": rec.billing_address,
            "card_network": pay.get("card_network"),
            "card_last4": pay.get("card_last4"),
            "method": pay.get("method"),
            "avs_match": rec.match_flags.get("avs_match"),
            "billing_shipping_address_match": rec.match_flags.get(
                "billing_shipping_address_match"
            ),
        }
        fact = {
            "billing_city": rec.billing_address.get("city"),
            "billing_shipping_address_match": rec.match_flags.get(
                "billing_shipping_address_match"
            ),
        }
        return content, "addresses + payments", fact

    if slot == rc.ACTIVITY_LOG:
        if not (rec.order or rec.payment):
            return None, "availability flag set but no order/payment row", {}
        o = rec.order or {}
        pay = rec.payment or {}
        content = {
            "order_id": o.get("order_id"),
            "order_date": o.get("order_date"),
            "payment_id": rec.payment_id,
            "payment_created_at": pay.get("created_at"),
            "payment_status": pay.get("status"),
            "three_ds": pay.get("three_ds"),
        }
        fact = {
            "order_id": o.get("order_id"),
            "payment_created_at": pay.get("created_at"),
        }
        return content, "orders + payments", fact

    if slot == rc.REFUND_CONFIRMATION:
        # No dedicated mirror table in the synthetic store; the availability row
        # IS the record. Represented honestly (see failure taxonomy).
        pay = rec.payment or {}
        content = {
            "backed_by": "evidence_availability record",
            "detail_records_mirrored": False,
            "payment_status": pay.get("status"),
        }
        fact = {"payment_status": pay.get("status")}
        return content, "evidence_availability (no mirror table)", fact

    if slot == rc.CANCELLATION_PROOF:
        cancel_comms = [
            m
            for m in rec.communications
            if "cancel" in (m.get("summary") or "").lower()
            or "refund" in (m.get("summary") or "").lower()
        ]
        content = {
            "backed_by": "evidence_availability record",
            "detail_records_mirrored": False,
            "related_communications": [m.get("summary") for m in cancel_comms],
        }
        return content, "evidence_availability (no mirror table)", {}

    return None, "unknown slot", {}


# --------------------------------------------------------------------------- #
# main entry point
# --------------------------------------------------------------------------- #
def _read_case(dispute_id: str) -> dict[str, Any]:
    from src.ingestion.models import DisputeCase

    with system_session() as s:
        case = s.get(DisputeCase, dispute_id)
        if case is None:
            raise DisputeNotFound(dispute_id)
        return {
            "id": case.id,
            "payment_id": case.payment_id,
            "amount": case.amount,
            "reason_code": case.reason_code,
            "reason_description": case.reason_description,
            "respond_by": case.respond_by,
            "phase": case.phase,
            "category": case.category,
            "needs_manual_classification": bool(case.needs_manual_classification),
        }


def _citations_for(slot: str) -> list[dict]:
    if _compliance_lookup is None:
        return []
    try:
        return [m.model_dump() for m in _compliance_lookup(slot)]
    except Exception:  # pragma: no cover - defensive only
        return []


def assemble_evidence(
    dispute_id: str,
    *,
    now_epoch: int | None = None,
    allow_llm: bool = True,
) -> AssemblyResult:
    """Assemble (or re-assemble) the evidence bundle for one dispute id."""
    case = _read_case(dispute_id)
    now = int(now_epoch if now_epoch is not None else time.time())
    payment_id = case["payment_id"]
    category = case["category"]
    needs_manual = case["needs_manual_classification"]
    respond_by = case["respond_by"]
    required = rc.required_slots(category)

    hours_to_deadline = (
        (respond_by - now) / 3600.0 if respond_by else None
    )

    # ---- needs_manual_classification: do NOT assemble ----
    if needs_manual or not required:
        slot_status = {s: SLOT_NOT_APPLICABLE for s in _SLOT_ORDER}
        result = AssemblyResult(
            dispute_id=dispute_id,
            payment_id=payment_id,
            category=category,
            needs_manual_classification=True,
            required_slots=[],
            slot_status=slot_status,
            completeness=None,
            present_count=0,
            missing_count=0,
            not_applicable_count=len(_SLOT_ORDER),
            assembly_status=ASSEMBLY_PENDING,
            explanation_letter_source=LETTER_SOURCE_NONE,
            compliance_citations={},
            respond_by=respond_by,
            hours_to_deadline=hours_to_deadline,
            deadline_pressure=bool(
                hours_to_deadline is not None
                and hours_to_deadline <= _DEADLINE_PRESSURE_HOURS
            ),
            notes=(
                "Dispute has no resolved reason-code category "
                "(needs_manual_classification). Evidence was NOT assembled; the "
                "bundle is flagged pending for human classification. Scorer "
                "contract: defer_to_human."
            ),
        )
        _persist(result, now)
        return result

    # ---- normal assembly ----
    records = fetch_records(payment_id)
    required_set = set(required)
    required_source = required_set - {rc.EXPLANATION_LETTER}

    slot_status: dict[str, str] = {}
    slot_content: dict[str, Any] = {}
    slot_sources: dict[str, str] = {}
    present_slot_facts: dict[str, dict] = {}

    for slot in _SLOT_ORDER:
        if slot == rc.EXPLANATION_LETTER:
            continue
        if slot not in required_source:
            slot_status[slot] = SLOT_NOT_APPLICABLE
            slot_sources[slot] = "not required for category " + category
            continue
        content, source, fact = _extract_slot(slot, records)
        if content is not None:
            slot_status[slot] = SLOT_PRESENT
            slot_content[slot] = content
            slot_sources[slot] = source
            present_slot_facts[slot] = fact
        else:
            slot_status[slot] = SLOT_MISSING
            slot_sources[slot] = source

    # ---- explanation letter (from present slots only) ----
    case_facts = {
        "dispute_id": dispute_id,
        "payment_id": payment_id,
        "amount_rupees": (case["amount"] / 100.0) if case.get("amount") else None,
        "reason_code": case["reason_code"],
        "reason_description": case["reason_description"],
        "phase": case["phase"],
    }
    letter_text, letter_source, guard_note = letter_mod.build_explanation_letter(
        case_facts, present_slot_facts, allow_llm=allow_llm
    )
    slot_status[rc.EXPLANATION_LETTER] = SLOT_PRESENT
    slot_content[rc.EXPLANATION_LETTER] = letter_text
    slot_sources[rc.EXPLANATION_LETTER] = f"generated:{letter_source}"

    # ---- completeness over required *source* slots ----
    present_count = sum(
        1 for s in required_source if slot_status[s] == SLOT_PRESENT
    )
    missing_count = sum(
        1 for s in required_source if slot_status[s] == SLOT_MISSING
    )
    denom = present_count + missing_count
    completeness = (present_count / denom) if denom else None
    not_applicable_count = sum(
        1 for s in _SLOT_ORDER if slot_status[s] == SLOT_NOT_APPLICABLE
    )

    if completeness is None:
        assembly_status = ASSEMBLY_PENDING
    elif missing_count == 0:
        assembly_status = ASSEMBLY_COMPLETE
    else:
        assembly_status = ASSEMBLY_PARTIAL

    deadline_pressure = bool(
        hours_to_deadline is not None
        and hours_to_deadline <= _DEADLINE_PRESSURE_HOURS
        and assembly_status != ASSEMBLY_COMPLETE
    )

    # ---- DPDP citations for PII-bearing slots that were sought ----
    compliance_citations: dict[str, list] = {}
    for slot in rc.PII_BEARING_SLOTS:
        if slot_status.get(slot) in (SLOT_PRESENT, SLOT_MISSING):
            cites = _citations_for(slot)
            if cites:
                compliance_citations[slot] = cites

    notes_parts = []
    if guard_note:
        notes_parts.append(f"explanation_letter: {guard_note}")
    if missing_count:
        missing_slots = [s for s in required_source if slot_status[s] == SLOT_MISSING]
        notes_parts.append("missing required slots: " + ", ".join(sorted(missing_slots)))
    if records.completeness_bucket:
        notes_parts.append(
            f"external store completeness bucket: {records.completeness_bucket}"
        )

    result = AssemblyResult(
        dispute_id=dispute_id,
        payment_id=payment_id,
        category=category,
        needs_manual_classification=False,
        required_slots=list(required),
        slot_status=slot_status,
        completeness=completeness,
        present_count=present_count,
        missing_count=missing_count,
        not_applicable_count=not_applicable_count,
        assembly_status=assembly_status,
        explanation_letter_source=letter_source,
        compliance_citations=compliance_citations,
        respond_by=respond_by,
        hours_to_deadline=hours_to_deadline,
        deadline_pressure=deadline_pressure,
        notes=" | ".join(notes_parts) or None,
        guard_note=guard_note,
        slot_content=slot_content,
        slot_sources=slot_sources,
    )
    _persist(result, now)
    return result


# --------------------------------------------------------------------------- #
# persistence (system store only; upsert on dispute id)
# --------------------------------------------------------------------------- #
def _persist(result: AssemblyResult, now: int) -> None:
    from sqlalchemy import select

    with system_session() as s:
        row = s.execute(
            select(EvidenceBundle).where(EvidenceBundle.dispute_id == result.dispute_id)
        ).scalar_one_or_none()
        if row is None:
            row = EvidenceBundle(
                dispute_id=result.dispute_id,
                assembled_at=now,
                assembly_passes=1,
            )
            s.add(row)
        else:
            row.assembly_passes = (row.assembly_passes or 1) + 1

        row.payment_id = result.payment_id
        row.category = result.category
        row.needs_manual_classification = result.needs_manual_classification
        row.required_slots = list(result.required_slots)
        row.shipping_proof = result.slot_content.get(rc.SHIPPING_PROOF)
        row.billing_proof = result.slot_content.get(rc.BILLING_PROOF)
        row.cancellation_proof = result.slot_content.get(rc.CANCELLATION_PROOF)
        row.customer_communication = result.slot_content.get(rc.CUSTOMER_COMMUNICATION)
        row.proof_of_service = result.slot_content.get(rc.PROOF_OF_SERVICE)
        row.explanation_letter = result.slot_content.get(rc.EXPLANATION_LETTER)
        row.refund_confirmation = result.slot_content.get(rc.REFUND_CONFIRMATION)
        row.activity_log = result.slot_content.get(rc.ACTIVITY_LOG)
        row.slot_status = dict(result.slot_status)
        row.slot_sources = dict(result.slot_sources)
        row.completeness = result.completeness
        row.present_count = result.present_count
        row.missing_count = result.missing_count
        row.not_applicable_count = result.not_applicable_count
        row.assembly_status = result.assembly_status
        row.compliance_citations = dict(result.compliance_citations)
        row.explanation_letter_source = result.explanation_letter_source
        row.respond_by = result.respond_by
        row.hours_to_deadline = result.hours_to_deadline
        row.deadline_pressure = result.deadline_pressure
        row.updated_at = now
        row.notes = result.notes


def get_evidence_bundle(dispute_id: str) -> dict[str, Any] | None:
    """The persisted bundle for a dispute id as a plain dict (or ``None``)."""
    from sqlalchemy import select

    with system_session() as s:
        row = s.execute(
            select(EvidenceBundle).where(EvidenceBundle.dispute_id == dispute_id)
        ).scalar_one_or_none()
        return row.as_dict() if row is not None else None


__all__ = [
    "assemble_evidence",
    "get_evidence_bundle",
    "AssemblyResult",
    "DisputeNotFound",
]
