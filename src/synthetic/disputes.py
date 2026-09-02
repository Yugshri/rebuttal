"""Synthetic dispute corpus + the payments that back each dispute.

Emits, per dispute:
* a row in ``payments`` (the transaction the dispute is about), and
* one or more ``dispute.created`` webhook payloads mirroring Razorpay's Dispute
  entity (``build.py`` writes these to ``data/webhooks/``).

Reason codes come from ``src/common/reason_codes.py`` (real network rulebook
values). A handful of deliberately *unrecognised-but-real* codes are included so
``dispute-ingestion-router`` has genuine ``needs_manual_classification`` traffic,
not a theoretical branch.

This module labels nothing about how a dispute *should* be handled — that ground
truth is produced separately in ``heldout.py`` and never stored where the
pipeline reads it.

Calibration:
* Dispute ``amount`` — right-skewed lognormal, median ~INR 1,900, long tail to
  ~INR 90,000. Scale sanity-checked against RBI DBIE average UPI ticket size
  (~INR 1,300-1,600 through 2024-25); heavy-tail shape from ULB
  ``mlg-ulb/creditcardfraud`` (published: 284,807 txns, mean amount ~88, strong
  right skew). India card-not-present dispute tickets skew higher than the UPI
  average, hence the lifted median.
* Category mix leans to consumer-dispute + fraud, the two biggest real
  chargeback buckets for e-commerce (Visa/Mastercard chargeback guides); exact
  proportions are a demo-coverage choice, not a measured base rate.
* ``needs_manual_classification`` share ~8% — a plausible "codes our table
  doesn't cover yet" rate for a young system; not calibrated to a public number.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

import numpy as np

from src.common import reason_codes as rc
from src.common.models_base import DisputePhase, DisputeStatus

from . import NOW
from .rng import lognormal_amount, parse_iso, to_iso

# -- Recognised codes, grouped so we can guarantee category coverage --------
_RECOGNISED_BY_CATEGORY: dict[str, list[tuple[str, str]]] = {}
for _key, _cat in rc.REASON_CODE_TO_CATEGORY.items():
    _net, _code = _key.split(":", 1)
    _RECOGNISED_BY_CATEGORY.setdefault(_cat, []).append((_net, _code))

# -- Real network codes intentionally absent from REASON_CODE_TO_CATEGORY ---
# All are genuine published reason codes; the router's table just doesn't map
# them yet, so they must route to needs_manual_classification (never guessed).
_UNRECOGNISED_CODES: tuple[tuple[str, str, str], ...] = (
    ("visa", "13.7", "Cancelled Merchandise/Services"),
    ("visa", "10.1", "EMV Liability Shift Counterfeit Fraud"),
    ("mastercard", "4841", "Cancelled Recurring Transaction"),
    ("mastercard", "4846", "Correct Transaction Currency Code Not Provided"),
    ("amex", "C14", "Paid by Other Means"),
    ("rupay", "121", "Goods/Services Not Received (RuPay)"),
    ("discover", "4752", "Does Not Recognize"),
)

_REASON_DESCRIPTIONS: dict[str, str] = {
    "visa:10.4": "Other Fraud - Card Absent Environment",
    "visa:11.1": "Card Recovery Bulletin",
    "visa:11.2": "Declined Authorisation",
    "visa:11.3": "No Authorisation",
    "visa:12.2": "Incorrect Transaction Code",
    "visa:12.5": "Incorrect Amount",
    "visa:12.6": "Duplicate Processing / Paid by Other Means",
    "visa:13.1": "Merchandise/Services Not Received",
    "visa:13.2": "Cancelled Recurring",
    "visa:13.3": "Not as Described or Defective",
    "visa:13.6": "Credit Not Processed",
    "mastercard:4863": "Cardholder Does Not Recognise - Potential Fraud",
    "mastercard:4837": "No Cardholder Authorisation",
    "mastercard:4808": "Authorisation-Related Chargeback",
    "mastercard:4834": "Point-of-Interaction Error",
    "mastercard:4853": "Cardholder Dispute",
    "mastercard:4855": "Goods or Services Not Provided",
    "amex:F24": "No Cardholder Authorisation",
    "amex:F29": "Card Not Present",
    "amex:C08": "Goods/Services Not Received or Only Partially Received",
    "amex:C31": "Goods/Services Not As Described",
    "amex:C05": "Goods/Services Cancelled",
}

_CARD_NETWORKS = ("visa", "mastercard", "amex", "rupay")
_EMAIL_DOMAINS = ("gmail.com", "yahoo.in", "outlook.com", "rediffmail.com", "hotmail.com")


@dataclass
class Dispute:
    dispute_id: str
    payment_id: str
    account_id: str
    amount: float
    reason_key: str          # "<network>:<code>"  (may be unrecognised)
    reason_code: str
    network: str
    reason_description: str
    category_hint: str        # the true category, or "needs_manual_classification"
    phase: str
    status: str
    created_at: str
    respond_by: str
    payment: dict
    # extra webhook events for reopen chains (each already a full payload dict)
    followups: list[dict] = field(default_factory=list)
    # bookkeeping the held-out labeller reads (NOT written to external.db)
    meta: dict = field(default_factory=dict)


def _payment_row(rng, payment_id, order_id, account_id, amount, network, authorised,
                 created_at, email, three_ds):
    method = "card"
    return {
        "payment_id": payment_id,
        "order_id": order_id,
        "account_id": account_id,
        "amount": amount,
        "currency": "INR",
        "method": method,
        "card_network": network,
        "card_last4": f"{int(rng.integers(0, 10000)):04d}",
        "auth_code": (f"A{int(rng.integers(100000, 999999))}" if authorised else None),
        "status": "captured",
        "created_at": created_at,
        "customer_email": email,
        "customer_ip": f"{int(rng.integers(1,224))}.{int(rng.integers(0,256))}."
                       f"{int(rng.integers(0,256))}.{int(rng.integers(1,255))}",
        "device_fingerprint": f"df_{int(rng.integers(0, 1_000_000)):06d}",
        "three_ds": int(three_ds),
    }


def _mk_webhook(d: "Dispute", *, status: str, phase: str, respond_by: str,
                created_at: str, event_id: str) -> dict:
    """A Razorpay-shaped ``dispute.created`` payload."""
    return {
        "event": "dispute.created",
        "event_id": event_id,
        "created_at": int(parse_iso(created_at).timestamp()),
        "payload": {
            "dispute": {
                "entity": {
                    "id": d.dispute_id,
                    "entity": "dispute",
                    "payment_id": d.payment_id,
                    "amount": int(round(d.amount * 100)),   # paise, like Razorpay
                    "currency": "INR",
                    "amount_deducted": int(round(d.amount * 100)),
                    "reason_code": d.reason_code,
                    "reason_description": d.reason_description,
                    "network": d.network,
                    "respond_by": int(parse_iso(respond_by).timestamp()),
                    "status": status,
                    "phase": phase,
                    "created_at": int(parse_iso(created_at).timestamp()),
                }
            },
            "payment": {
                "entity": {
                    "id": d.payment_id,
                    "entity": "payment",
                    "amount": int(round(d.payment["amount"] * 100)),
                    "currency": "INR",
                    "status": d.payment["status"],
                    "method": d.payment["method"],
                    "captured": True,
                    "email": d.payment["customer_email"],
                    "created_at": int(parse_iso(d.payment["created_at"]).timestamp()),
                }
            },
        },
    }


def build_disputes(rng: np.random.Generator, account_pool: dict) -> list[Dispute]:
    """Generate the dispute corpus.

    ``account_pool`` = {"mule": [...], "bursty": [...], "fringe": [...],
    "normal": [...]} — used to steer some disputes onto risky counterparties so
    risk enrichment has real signal AND a real false-positive target.
    """
    now = parse_iso(NOW)
    disputes: list[Dispute] = []
    n = {"seq": 0}

    # Target: ~130 disputes. Category quotas guarantee coverage.
    quota = {
        rc.FRAUD: 34,
        rc.AUTHORIZATION: 24,
        rc.PROCESSING_ERROR: 20,
        rc.CONSUMER_DISPUTE: 40,
        rc.NEEDS_MANUAL_CLASSIFICATION: 11,
    }
    # Phase mix: chargeback dominates (that's where evidence response happens),
    # but every phase is represented, incl. arbitration.
    phase_choices = (
        [DisputePhase.FRAUD.value] * 2
        + [DisputePhase.RETRIEVAL.value] * 2
        + [DisputePhase.CHARGEBACK.value] * 10
        + [DisputePhase.PRE_ARBITRATION.value] * 3
        + [DisputePhase.ARBITRATION.value] * 1
    )

    def next_id(prefix: str) -> str:
        n["seq"] += 1
        return f"{prefix}{n['seq']:04d}"

    def pick_account(bucket_roll: float) -> tuple[str, str]:
        if bucket_roll < 0.12:
            return str(rng.choice(account_pool["mule"])), "mule"
        if bucket_roll < 0.24:
            return str(rng.choice(account_pool["bursty"])), "bursty"
        if bucket_roll < 0.32:
            return str(rng.choice(account_pool["fringe"])), "fringe"
        return str(rng.choice(account_pool["normal"])), "normal"

    for category, count in quota.items():
        for _ in range(count):
            dispute_id = next_id("disp_")
            payment_id = next_id("pay_")
            order_id = payment_id.replace("pay_", "order_")

            acc, acc_role = pick_account(float(rng.random()))

            if category == rc.NEEDS_MANUAL_CLASSIFICATION:
                network, code, desc = _UNRECOGNISED_CODES[
                    int(rng.integers(0, len(_UNRECOGNISED_CODES)))
                ]
                reason_key = f"{network}:{code}"
                category_hint = rc.NEEDS_MANUAL_CLASSIFICATION
            else:
                network, code = _RECOGNISED_BY_CATEGORY[category][
                    int(rng.integers(0, len(_RECOGNISED_BY_CATEGORY[category])))
                ]
                reason_key = f"{network}:{code}"
                desc = _REASON_DESCRIPTIONS.get(reason_key, code)
                category_hint = category

            # Amount: lognormal, median ~1,900 INR, tail to ~90k. See module doc.
            amount = lognormal_amount(rng, median=1_900, sigma=1.05, lo=120, hi=92_000)
            # Fraud/authorization tickets skew a bit higher.
            if category in (rc.FRAUD, rc.AUTHORIZATION):
                amount = round(min(amount * float(rng.uniform(1.2, 2.4)), 150_000), 2)

            created_at = now - timedelta(days=float(rng.uniform(2, 29)),
                                         hours=float(rng.uniform(0, 24)))

            # respond_by: ~16% already inside 48h, ~9% overdue, rest 3-25d out.
            roll = float(rng.random())
            if roll < 0.09:
                respond_by = now - timedelta(hours=float(rng.uniform(2, 40)))
            elif roll < 0.25:
                respond_by = now + timedelta(hours=float(rng.uniform(3, 47)))
            else:
                respond_by = now + timedelta(days=float(rng.uniform(3, 25)))

            phase = phase_choices[int(rng.integers(0, len(phase_choices)))]
            # Authorization disputes are frequently still in fraud/retrieval.
            if category == rc.AUTHORIZATION and rng.random() < 0.4:
                phase = DisputePhase.RETRIEVAL.value

            status = DisputeStatus.OPEN.value
            if rng.random() < 0.18:
                status = DisputeStatus.UNDER_REVIEW.value

            authorised = not (category == rc.AUTHORIZATION and rng.random() < 0.7)
            three_ds = category != rc.FRAUD and rng.random() < 0.6
            email = (f"user{int(rng.integers(1000, 9999))}@"
                     f"{_EMAIL_DOMAINS[int(rng.integers(0, len(_EMAIL_DOMAINS)))]}")

            payment = _payment_row(rng, payment_id, order_id, acc, amount, network,
                                   authorised, to_iso(created_at), email, three_ds)

            d = Dispute(
                dispute_id=dispute_id,
                payment_id=payment_id,
                account_id=acc,
                amount=amount,
                reason_key=reason_key,
                reason_code=code,
                network=network,
                reason_description=desc,
                category_hint=category_hint,
                phase=phase,
                status=status,
                created_at=to_iso(created_at),
                respond_by=to_iso(respond_by),
                payment=payment,
                meta={
                    "account_role": acc_role,
                    "authorised": authorised,
                    "three_ds": bool(three_ds),
                },
            )
            d.followups.append(
                _mk_webhook(d, status=status, phase=phase,
                            respond_by=to_iso(respond_by), created_at=to_iso(created_at),
                            event_id=next_id("evt_"))
            )
            disputes.append(d)

    _plant_reopens(disputes, rng, next_id)
    disputes.sort(key=lambda x: x.dispute_id)
    return disputes


def _plant_reopens(disputes, rng, next_id):
    """Turn ~6 disputes into genuine reopen chains.

    A dispute that resolved (``won`` at an earlier phase) comes back as a later
    phase under ``under_review`` — the case ``dispute-ingestion-router`` and
    ``confidence-scorer-review`` must not silently drop.
    """
    from src.common.models_base import PHASE_ORDER

    candidates = [d for d in disputes
                  if d.phase in (DisputePhase.CHARGEBACK.value,
                                 DisputePhase.PRE_ARBITRATION.value)]
    picks = rng.choice(len(candidates), size=min(6, len(candidates)), replace=False)
    for idx in picks:
        d = candidates[int(idx)]
        # First event: resolved in the merchant's favour at the original phase.
        d.followups.clear()
        won_at = parse_iso(d.created_at)
        d.followups.append(
            _mk_webhook(d, status=DisputeStatus.WON.value, phase=d.phase,
                        respond_by=d.respond_by, created_at=to_iso(won_at),
                        event_id=next_id("evt_"))
        )
        # Escalation: same dispute id, next phase, back under review, new clock.
        cur = PHASE_ORDER.index(next(p for p in PHASE_ORDER if p.value == d.phase))
        new_phase = PHASE_ORDER[min(cur + 1, len(PHASE_ORDER) - 1)].value
        reopened_at = won_at + timedelta(days=float(rng.uniform(6, 20)))
        new_respond_by = reopened_at + timedelta(days=float(rng.uniform(2, 12)))
        d.phase = new_phase
        d.status = DisputeStatus.UNDER_REVIEW.value
        d.respond_by = to_iso(new_respond_by)
        d.meta["reopened"] = True
        d.followups.append(
            _mk_webhook(d, status=DisputeStatus.UNDER_REVIEW.value, phase=new_phase,
                        respond_by=to_iso(new_respond_by), created_at=to_iso(reopened_at),
                        event_id=next_id("evt_"))
        )
