"""Read-only lookups against the external evidence store, keyed on ``payment_id``.

Every function here opens the external store through
``src.common.db.read_only_session()`` — a driver-level read-only handle. There is
no code path in this module that can write ``external.db``.

The authoritative "does a real record exist to back this slot" signal is the
``evidence_availability`` row for the payment (its schema comment says exactly
this). Where a dedicated backing table also exists (``shipments``,
``communications``, ``orders``, ``addresses``) we pull the concrete content and
attach it, so a `present` slot carries real data, not just a boolean. Where no
dedicated table exists (``cancellation_proof``, ``refund_confirmation``) the
availability flag is the record, and we say so in the slot's provenance.

Nothing here decides `present` / `missing` / `not_applicable` — that is
``assembler.py``'s job. This module only answers "what records exist for this
payment".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text

from src.common import reason_codes as rc
from src.common.db import read_only_session

# evidence_availability column per slot (explanation_letter has none — generated).
_AVAILABILITY_COLUMN: dict[str, str] = {
    rc.SHIPPING_PROOF: "has_shipping_proof",
    rc.BILLING_PROOF: "has_billing_proof",
    rc.CANCELLATION_PROOF: "has_cancellation_proof",
    rc.CUSTOMER_COMMUNICATION: "has_customer_communication",
    rc.PROOF_OF_SERVICE: "has_proof_of_service",
    rc.REFUND_CONFIRMATION: "has_refund_confirmation",
    rc.ACTIVITY_LOG: "has_activity_log",
}


@dataclass(frozen=True)
class EvidenceRecords:
    """Everything the external store holds for one payment id.

    ``availability`` is the per-slot "record exists" map (from
    ``evidence_availability``); the other fields are the concrete backing rows,
    each ``None`` / empty when absent.
    """

    payment_id: str
    availability: dict[str, bool] = field(default_factory=dict)
    match_flags: dict[str, Any] = field(default_factory=dict)
    completeness_bucket: str | None = None
    order: dict[str, Any] | None = None
    shipment: dict[str, Any] | None = None
    communications: list[dict[str, Any]] = field(default_factory=list)
    billing_address: dict[str, Any] | None = None
    shipping_address: dict[str, Any] | None = None
    payment: dict[str, Any] | None = None

    def has_availability_row(self) -> bool:
        return bool(self.availability) or self.completeness_bucket is not None


def _row_to_dict(row: Any) -> dict[str, Any]:
    return dict(row._mapping)


def fetch_records(payment_id: str) -> EvidenceRecords:
    """Load every external record backing an evidence bundle for ``payment_id``."""
    with read_only_session() as s:
        avail_row = s.execute(
            text("SELECT * FROM evidence_availability WHERE payment_id = :pid"),
            {"pid": payment_id},
        ).fetchone()

        availability: dict[str, bool] = {}
        match_flags: dict[str, Any] = {}
        completeness_bucket: str | None = None
        if avail_row is not None:
            a = _row_to_dict(avail_row)
            for slot, col in _AVAILABILITY_COLUMN.items():
                availability[slot] = bool(a.get(col, 0))
            match_flags = {
                "billing_shipping_address_match": a.get(
                    "billing_shipping_address_match"
                ),
                "email_domain_consistent": a.get("email_domain_consistent"),
                "avs_match": a.get("avs_match"),
            }
            completeness_bucket = a.get("completeness_bucket")

        order_row = s.execute(
            text("SELECT * FROM orders WHERE payment_id = :pid"),
            {"pid": payment_id},
        ).fetchone()
        order = _row_to_dict(order_row) if order_row is not None else None

        payment_row = s.execute(
            text("SELECT * FROM payments WHERE payment_id = :pid"),
            {"pid": payment_id},
        ).fetchone()
        payment = _row_to_dict(payment_row) if payment_row is not None else None

        shipment = None
        if order is not None:
            ship_row = s.execute(
                text("SELECT * FROM shipments WHERE order_id = :oid"),
                {"oid": order["order_id"]},
            ).fetchone()
            shipment = _row_to_dict(ship_row) if ship_row is not None else None

        comm_rows = s.execute(
            text(
                "SELECT * FROM communications WHERE payment_id = :pid "
                "ORDER BY timestamp, comm_id"
            ),
            {"pid": payment_id},
        ).fetchall()
        communications = [_row_to_dict(r) for r in comm_rows]

        account_id = None
        if order is not None:
            account_id = order.get("account_id")
        elif payment is not None:
            account_id = payment.get("account_id")

        billing_address = shipping_address = None
        if account_id is not None:
            addr_rows = s.execute(
                text("SELECT * FROM addresses WHERE account_id = :aid"),
                {"aid": account_id},
            ).fetchall()
            for r in addr_rows:
                d = _row_to_dict(r)
                if d.get("kind") == "billing" and billing_address is None:
                    billing_address = d
                elif d.get("kind") == "shipping" and shipping_address is None:
                    shipping_address = d

    return EvidenceRecords(
        payment_id=payment_id,
        availability=availability,
        match_flags=match_flags,
        completeness_bucket=completeness_bucket,
        order=order,
        shipment=shipment,
        communications=communications,
        billing_address=billing_address,
        shipping_address=shipping_address,
        payment=payment,
    )


__all__ = ["EvidenceRecords", "fetch_records"]
