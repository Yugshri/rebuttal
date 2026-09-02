"""The ``explanation_letter`` slot — the one LLM-touched surface in the pipeline.

Hard constraint: the letter narrates **only facts that are already present in
other assembled slots** (plus the dispute's own identifying fields, which come
from the ``DisputeCase``, not from an evidence lookup). It never introduces a
fact — no invented tracking numbers, dates, names, amounts, or claims about
evidence the bundle does not hold.

Two paths, same constraint:

* **Deterministic template** (always available, no network). Builds the letter by
  concatenating one short, factual paragraph per *present* slot. Safe by
  construction: a slot that is `missing` / `not_applicable` contributes no text.
* **LLM** (``src.common.llm.generate``, Sarvam ``sarvam-m``). Given the same
  structured facts and a strict system prompt. The output is then **guarded**:
  if it mentions a claim tied to a slot that is not `present`, it is rejected and
  the template is used instead. The guard is keyword-based and therefore
  best-effort — see ``docs/failure-taxonomy.md`` for why NL grounding cannot be
  fully verified.

If ``SARVAM_API_KEY`` is unset (the default, and the test configuration),
``generate`` returns ``None`` and the template path runs. The pipeline never
depends on the LLM being reachable.
"""

from __future__ import annotations

from src.common import llm
from src.common import reason_codes as rc
from src.evidence.models import (
    LETTER_SOURCE_LLM,
    LETTER_SOURCE_TEMPLATE,
)

# Claim keywords that must NOT appear in a letter when the matching slot is not
# `present`. Best-effort guard for the LLM path (the template path never trips
# it). Deliberately conservative — see failure taxonomy.
_SLOT_CLAIM_KEYWORDS: dict[str, tuple[str, ...]] = {
    rc.SHIPPING_PROOF: (
        "tracking number",
        "was delivered",
        "delivery confirmation",
        "carrier",
        "courier",
        "signed for",
        "proof of delivery",
    ),
    rc.PROOF_OF_SERVICE: ("proof of service", "service was rendered", "access log"),
    rc.CUSTOMER_COMMUNICATION: (
        "email exchange",
        "chat transcript",
        "correspondence shows",
        "the customer wrote",
        "call recording",
    ),
    rc.REFUND_CONFIRMATION: (
        "refund was processed",
        "refund confirmation",
        "we refunded",
        "credit was issued",
    ),
    rc.CANCELLATION_PROOF: ("cancellation request", "cancellation record", "was cancelled"),
    rc.BILLING_PROOF: ("billing address on file", "avs match", "card verification"),
    rc.ACTIVITY_LOG: ("activity log shows", "account activity record"),
}

_SYSTEM_PROMPT = (
    "You draft a merchant's chargeback rebuttal letter for a payment dispute. "
    "You will be given a JSON object of FACTS that have been verified against "
    "real records. Write a short, professional letter (120-220 words) that "
    "argues the charge is valid using ONLY those facts. Absolute rules: do not "
    "invent any detail not in the FACTS - no tracking numbers, dates, names, "
    "amounts, or claims about evidence not listed. If a category of evidence is "
    "not in the FACTS, do not mention it or imply it exists. Do not speculate. "
    "Plain prose, no markdown, no placeholders."
)


def _rupees(amount_paise_or_rupees: float) -> str:
    # DisputeCase.amount is in paise (Razorpay convention); the disposition file
    # and orders table carry rupees. Caller passes rupees already.
    return f"INR {amount_paise_or_rupees:,.2f}"


def _slot_paragraph(slot: str, fact: dict) -> str | None:
    """One factual sentence for a present slot. Returns None if nothing to say."""
    if slot == rc.SHIPPING_PROOF:
        bits = []
        if fact.get("carrier"):
            bits.append(f"carrier {fact['carrier']}")
        if fact.get("tracking_number"):
            bits.append(f"tracking {fact['tracking_number']}")
        status = fact.get("delivery_status")
        when = fact.get("delivered_at")
        lead = "Shipping records show the order was handled"
        if status == "delivered" and when:
            lead = f"Shipping records show the order was delivered on {when}"
        elif status:
            lead = f"Shipping records show the shipment status is '{status}'"
        tail = (" via " + ", ".join(bits)) if bits else ""
        sig = (
            " A delivery signature was captured."
            if fact.get("signature_captured")
            else ""
        )
        return f"{lead}{tail}.{sig}"
    if slot == rc.PROOF_OF_SERVICE:
        desc = fact.get("item_description")
        cat = fact.get("item_category")
        qty = fact.get("quantity")
        when = fact.get("order_date")
        parts = ["The order record describes"]
        parts.append(
            f" {qty} x '{desc}'" if qty and desc else f" '{desc}'" if desc else " the purchased item"
        )
        if cat:
            parts.append(f" ({cat})")
        if when:
            parts.append(f", ordered {when}")
        return "".join(parts) + "."
    if slot == rc.CUSTOMER_COMMUNICATION:
        n = fact.get("message_count", 0)
        chans = fact.get("channels") or []
        chan_txt = (" across " + ", ".join(chans)) if chans else ""
        first = fact.get("first_timestamp")
        last = fact.get("last_timestamp")
        span = f" between {first} and {last}" if first and last else ""
        return (
            f"There are {n} logged customer-service interaction(s){chan_txt}{span}; "
            "the merchant engaged with the customer about this order."
        )
    if slot == rc.BILLING_PROOF:
        city = fact.get("billing_city")
        match = fact.get("billing_shipping_address_match")
        base = "A billing address record is on file"
        if city:
            base += f" ({city})"
        if match == 1:
            base += "; it matches the shipping address"
        elif match == 0:
            base += "; it differs from the shipping address"
        return base + "."
    if slot == rc.REFUND_CONFIRMATION:
        status = fact.get("payment_status")
        extra = f" The payment status is '{status}'." if status else ""
        return (
            "A refund-handling record exists for this payment, evidencing how any "
            "credit was routed." + extra
        )
    if slot == rc.CANCELLATION_PROOF:
        return (
            "A cancellation-handling record exists for this order, evidencing "
            "whether and when a cancellation was requested and processed."
        )
    if slot == rc.ACTIVITY_LOG:
        oid = fact.get("order_id")
        created = fact.get("payment_created_at")
        base = "An order/payment activity record is available"
        if oid:
            base += f" for {oid}"
        if created:
            base += f", with the payment captured {created}"
        return base + "."
    return None


def _template_letter(case_facts: dict, present_slot_facts: dict[str, dict]) -> str:
    did = case_facts.get("dispute_id", "this dispute")
    pid = case_facts.get("payment_id", "the payment")
    amount = case_facts.get("amount_rupees")
    reason = case_facts.get("reason_description") or case_facts.get("reason_code")
    phase = case_facts.get("phase")

    head = (
        f"Re: Dispute {did} on payment {pid}"
        + (f" for {_rupees(amount)}" if amount is not None else "")
        + (f", raised under reason '{reason}'" if reason else "")
        + (f" (current phase: {phase})" if phase else "")
        + "."
    )
    intro = (
        "The merchant submits that this charge is valid and asks that the dispute "
        "be resolved in the merchant's favour. The following facts are drawn from "
        "records held for this transaction:"
    )

    body: list[str] = []
    for slot in rc.EVIDENCE_SLOTS:
        if slot == rc.EXPLANATION_LETTER:
            continue
        fact = present_slot_facts.get(slot)
        if not fact:
            continue
        para = _slot_paragraph(slot, fact)
        if para:
            body.append("- " + para)

    if not body:
        body.append(
            "- No corroborating evidence records were located for this dispute; "
            "this letter therefore raises no factual claim and the matter is "
            "referred for manual handling."
        )

    close = (
        "No facts beyond those listed above are asserted. Where an evidence "
        "category is not mentioned, the merchant is not relying on it."
    )
    return "\n".join([head, "", intro, "", *body, "", close])


def _violates_grounding(letter: str, present_slots: set[str]) -> str | None:
    """Return the first slot-claim the letter makes that it should not, or None."""
    low = letter.lower()
    for slot, keywords in _SLOT_CLAIM_KEYWORDS.items():
        if slot in present_slots:
            continue
        for kw in keywords:
            if kw in low:
                return f"{slot}:{kw}"
    return None


def build_explanation_letter(
    case_facts: dict,
    present_slot_facts: dict[str, dict],
    *,
    allow_llm: bool = True,
) -> tuple[str, str, str | None]:
    """Build the explanation letter.

    Returns ``(text, source, guard_note)`` where ``source`` is ``"llm"`` or
    ``"template"`` and ``guard_note`` explains any LLM rejection (else ``None``).
    """
    template = _template_letter(case_facts, present_slot_facts)
    present_slots = set(present_slot_facts)

    if not allow_llm or not llm.is_configured():
        return template, LETTER_SOURCE_TEMPLATE, None

    import json

    user = json.dumps(
        {
            "dispute_id": case_facts.get("dispute_id"),
            "payment_id": case_facts.get("payment_id"),
            "amount": (
                _rupees(case_facts["amount_rupees"])
                if case_facts.get("amount_rupees") is not None
                else None
            ),
            "reason": case_facts.get("reason_description")
            or case_facts.get("reason_code"),
            "phase": case_facts.get("phase"),
            "verified_facts_by_slot": present_slot_facts,
        },
        default=str,
        indent=2,
    )
    out = llm.generate(system=_SYSTEM_PROMPT, user=user)
    if not out:
        return template, LETTER_SOURCE_TEMPLATE, "llm unavailable or empty response"

    violation = _violates_grounding(out, present_slots)
    if violation is not None:
        return (
            template,
            LETTER_SOURCE_TEMPLATE,
            f"llm output rejected: referenced non-present evidence ({violation})",
        )
    return out.strip(), LETTER_SOURCE_LLM, None


__all__ = ["build_explanation_letter"]
