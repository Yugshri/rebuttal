"""Synthetic order / shipping / comms / address store, keyed on ``payment_id``.

Feeds ``evidence-assembler``. Deliberately leaves a documented fraction of
records incomplete so the "never fabricate, report missing honestly" path is
actually exercised — this is a designed test condition, not an oversight.

Incompleteness rates (over the dispute corpus):
* ``full``    ~58%  — every required evidence slot has a real backing record.
* ``partial`` ~30%  — exactly one non-critical required slot has no record.
* ``severe``  ~12%  — a critical slot (or 2+ slots) has no record.

Rationale for deliberately injecting missingness: real fraud/dispute feature
stores are far from complete. IEEE-CIS Fraud Detection (Vesta) ships with many
``D*``/``V*``/``id_*`` columns >50% null; a chargeback evidence system built for
production must handle "the record simply isn't there". The e-commerce
return-abuse dataset (Kaggle ``sarveshchhetri/e-commerce-return-abuse-detection-
dataset``, opened via croissant metadata: 60,000 rows x 35 cols, 0 missing) is
the *opposite* extreme — clean enough to be unrealistic for this purpose.

Match / mismatch flags mirror IEEE-CIS's ``M1..M9`` match family and the
address/email-consistency signals a real evidence assessor would compute:
* ``billing_shipping_address_match`` — billing vs. ship-to address.
* ``email_domain_consistent``        — order email domain vs. the account's
                                       historical pattern.
* ``avs_match``                       — Address Verification System result.
"""

from __future__ import annotations

import numpy as np

from src.common import reason_codes as rc

from .rng import parse_iso, to_iso
from datetime import timedelta

_CATEGORIES_ITEMS = {
    "Electronics": ("Wireless earbuds", "Power bank 20000mAh", "Smart watch", "USB-C hub"),
    "Apparel": ("Cotton kurta set", "Running shoes", "Denim jacket", "Silk saree"),
    "Home": ("Non-stick cookware set", "Bedsheet queen", "Table lamp", "Wall clock"),
    "Beauty": ("Skincare gift box", "Hair dryer", "Perfume 100ml", "Makeup kit"),
    "Grocery": ("Dry fruits 1kg", "Cold-pressed oil 2L", "Filter coffee 500g", "Honey 1kg"),
}
_CARRIERS = ("Delhivery", "Blue Dart", "Ekart", "XpressBees", "India Post")
_CRITICAL_SLOTS = {rc.SHIPPING_PROOF, rc.ACTIVITY_LOG, rc.PROOF_OF_SERVICE}


def _address(account_id, kind, city, rng, high_density=False):
    return {
        "address_id": f"addr_{account_id}_{kind}",
        "account_id": account_id,
        "kind": kind,
        "line1": f"{int(rng.integers(1, 400))}, "
                 f"{rng.choice(('MG Road','Sector 17','Link Rd','Church St','Ring Rd'))}",
        "city": city,
        "pincode": f"{int(rng.integers(110001, 700100))}",
        "country": "IN",
        "high_return_density": int(high_density),
    }


def build_evidence_store(rng: np.random.Generator, disputes, node_by_id: dict):
    """Return dict of row-lists for orders, shipments, communications,
    addresses, evidence_availability."""
    orders, shipments, comms, addresses, availability = [], [], [], [], []
    seen_addr: set[str] = set()

    # Bucket assignment up front so the documented rates hold exactly.
    n = len(disputes)
    n_severe = round(n * 0.12)
    n_partial = round(n * 0.30)
    buckets = (["severe"] * n_severe + ["partial"] * n_partial
               + ["full"] * (n - n_severe - n_partial))
    perm = rng.permutation(n)
    bucket_for = {}
    for slot_i, d_i in enumerate(perm):
        bucket_for[disputes[int(d_i)].dispute_id] = buckets[slot_i]

    for d in disputes:
        acc = d.account_id
        node = node_by_id.get(acc, {"home_city": "Mumbai", "account_type": "consumer"})
        city = node["home_city"]
        is_risky = d.meta.get("account_role") in ("mule", "fringe")

        cat_name = rng.choice(list(_CATEGORIES_ITEMS))
        item = _CATEGORIES_ITEMS[cat_name][int(rng.integers(0, 4))]
        price_point = round(d.amount / max(1, int(rng.integers(1, 3))), 2)
        is_cod = int(d.payment["method"] != "card" or rng.random() < 0.18)
        order_date = parse_iso(d.payment["created_at"])
        orders.append({
            "order_id": d.payment["order_id"],
            "payment_id": d.payment_id,
            "account_id": acc,
            "item_category": cat_name,
            "item_description": item,
            "price_point": price_point,
            "quantity": int(rng.integers(1, 4)),
            "is_cod": is_cod,
            "is_high_value": int(d.amount >= 25_000),
            "discount_used": int(rng.random() < 0.4),
            "order_date": to_iso(order_date),
        })

        # Addresses: billing always, shipping for physical-goods categories.
        bill_city = city
        ship_city = city
        # Mismatch is a risk signal — force it more often on risky accounts.
        mismatch_p = 0.55 if is_risky else 0.16
        address_mismatch = rng.random() < mismatch_p
        if address_mismatch:
            ship_city = rng.choice([c for c in
                                    ("Mumbai", "Delhi", "Kolkata", "Chennai", "Indore")
                                    if c != bill_city])
        bill = _address(acc, "billing", bill_city, rng)
        high_density = is_risky and rng.random() < 0.5
        ship = _address(acc, "shipping", ship_city, rng, high_density=high_density)
        for a in (bill, ship):
            if a["address_id"] not in seen_addr:
                addresses.append(a)
                seen_addr.add(a["address_id"])

        bucket = bucket_for[d.dispute_id]
        required = set(rc.required_slots(d.category_hint)) or {
            rc.SHIPPING_PROOF, rc.ACTIVITY_LOG, rc.CUSTOMER_COMMUNICATION,
            rc.EXPLANATION_LETTER,
        }
        # explanation_letter is LLM-generated downstream — always "available".
        present = {s: True for s in rc.EVIDENCE_SLOTS}
        droppable = sorted(required - {rc.EXPLANATION_LETTER})
        non_critical = [s for s in droppable if s not in _CRITICAL_SLOTS]
        critical = [s for s in droppable if s in _CRITICAL_SLOTS]
        if bucket == "partial" and non_critical:
            present[non_critical[int(rng.integers(0, len(non_critical)))]] = False
        elif bucket == "severe":
            drop = []
            if critical:
                drop.append(critical[int(rng.integers(0, len(critical)))])
            pool = [s for s in droppable if s not in drop]
            rng.shuffle(pool)
            drop += pool[: int(rng.integers(1, 3))]
            for s in drop:
                present[s] = False

        # Slots that just don't apply to this category are recorded as absent
        # too (assembler decides missing-vs-not_applicable; we only say "no
        # record exists").
        for s in rc.EVIDENCE_SLOTS:
            if s not in required and s != rc.EXPLANATION_LETTER and rng.random() < 0.5:
                present[s] = False

        # Shipment row — present iff we "have" shipping proof.
        has_shipment = present[rc.SHIPPING_PROOF] and cat_name != "Grocery" or \
            (present[rc.SHIPPING_PROOF] and rng.random() < 0.7)
        if has_shipment:
            shipped = order_date + timedelta(days=float(rng.uniform(0.5, 3)))
            delivered_status = "delivered"
            roll = rng.random()
            if d.meta.get("account_role") == "mule" and roll < 0.4:
                delivered_status = "refused"
            elif roll < 0.08:
                delivered_status = "in_transit"
            elif roll < 0.12:
                delivered_status = "lost"
            delivered_at = (to_iso(shipped + timedelta(days=float(rng.uniform(1, 6))))
                            if delivered_status == "delivered" else None)
            shipments.append({
                "order_id": d.payment["order_id"],
                "carrier": _CARRIERS[int(rng.integers(0, len(_CARRIERS)))],
                "tracking_number": f"TRK{int(rng.integers(10**9, 10**10))}",
                "tracking_valid": int(rng.random() < (0.6 if is_risky else 0.93)),
                "shipped_at": to_iso(shipped),
                "delivered_at": delivered_at,
                "delivery_status": delivered_status,
                "signature_captured": int(delivered_status == "delivered"
                                          and rng.random() < 0.55),
                "ship_to_address_id": ship["address_id"],
            })
            addr_match_flag = 0 if address_mismatch else 1
        else:
            addr_match_flag = None  # no shipment -> n/a

        # Communications — present iff we "have" customer_communication.
        if present[rc.CUSTOMER_COMMUNICATION]:
            n_comms = int(rng.integers(1, 5))
            for k in range(n_comms):
                t = order_date + timedelta(days=float(rng.uniform(0, 20)))
                direction = "inbound" if k % 2 == 0 else "outbound"
                comms.append({
                    "comm_id": f"comm_{d.payment_id}_{k}",
                    "payment_id": d.payment_id,
                    "channel": rng.choice(("email", "sms", "chat", "call")),
                    "direction": direction,
                    "timestamp": to_iso(t),
                    "summary": rng.choice((
                        "Customer asked about delivery status",
                        "Merchant shared tracking link",
                        "Customer requested return/refund",
                        "Merchant explained refund policy",
                        "Customer confirmed receipt of item",
                        "Merchant offered replacement",
                    )),
                })

        email_consistent = int(rng.random() < (0.5 if is_risky else 0.9))
        if d.category_hint == rc.FRAUD:
            avs = rng.choice(("no_match", "partial", "not_checked", "full"),
                             p=[0.4, 0.25, 0.2, 0.15])
        else:
            avs = rng.choice(("full", "partial", "no_match", "not_checked"),
                             p=[0.62, 0.2, 0.08, 0.1])

        availability.append({
            "payment_id": d.payment_id,
            "has_shipping_proof": int(present[rc.SHIPPING_PROOF] and has_shipment),
            "has_billing_proof": int(present[rc.BILLING_PROOF]),
            "has_cancellation_proof": int(present[rc.CANCELLATION_PROOF]),
            "has_customer_communication": int(present[rc.CUSTOMER_COMMUNICATION]),
            "has_proof_of_service": int(present[rc.PROOF_OF_SERVICE]),
            "has_refund_confirmation": int(present[rc.REFUND_CONFIRMATION]),
            "has_activity_log": int(present[rc.ACTIVITY_LOG]),
            "billing_shipping_address_match": addr_match_flag,
            "email_domain_consistent": email_consistent,
            "avs_match": str(avs),
            "completeness_bucket": bucket,
        })

    return {
        "orders": orders,
        "shipments": shipments,
        "communications": comms,
        "addresses": addresses,
        "evidence_availability": availability,
        "_bucket_for": bucket_for,
    }
