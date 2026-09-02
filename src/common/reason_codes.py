"""Canonical reason-code -> category -> required-evidence mapping.

This is the single source of truth shared across modules that would otherwise
drift apart:

- ``synthetic-data-generator`` builds first and needs the real network codes to
  emit realistic ``DisputeCase`` records.
- ``dispute-ingestion-router`` owns the *queryable* classification table exposed
  through the API; it builds that table from the constants here so its output can
  never disagree with what the data was generated against.
- ``evidence-assembler`` reads :data:`CATEGORY_REQUIRED_SLOTS` to know which
  ``EvidenceBundle`` slots actually matter for a given dispute.
- ``compliance-knowledge-graph`` keys its ``applies_to`` edges on the category
  names and slot names defined here, so citation lookups are exact-match.

Sources for the codes themselves: Visa Core Rules / Visa Claims Resolution,
Mastercard Chargeback Guide, Amex dispute reason codes, and Razorpay's Dispute
entity documentation (reason-code categories: ``fraud``, ``authorization``,
``processing_error``, ``consumer_dispute``). These are real published rulebook
values, not synthetic.
"""

from __future__ import annotations

# --- Category identifiers -------------------------------------------------------
# These four strings are the fixed vocabulary. Do not rename without updating
# compliance-knowledge-graph's edges and every module that switches on category.
FRAUD = "fraud"
AUTHORIZATION = "authorization"
PROCESSING_ERROR = "processing_error"
CONSUMER_DISPUTE = "consumer_dispute"

CATEGORIES: tuple[str, ...] = (FRAUD, AUTHORIZATION, PROCESSING_ERROR, CONSUMER_DISPUTE)

# Bucket for a real network code we don't recognise. An unrecognised code must be
# routed here explicitly, never guessed into the nearest category.
NEEDS_MANUAL_CLASSIFICATION = "needs_manual_classification"


# --- Reason code -> category ---------------------------------------------------
# Network-specific codes. Key is "<network>:<code>" to disambiguate overlapping
# numeric codes across networks.
REASON_CODE_TO_CATEGORY: dict[str, str] = {
    # Fraud — cardholder claims they did not authorise / recognise the charge.
    "visa:10.4": FRAUD,          # Other Fraud - Card Absent Environment
    "mastercard:4863": FRAUD,    # Cardholder Does Not Recognise - Potential Fraud
    "amex:F24": FRAUD,           # No Cardholder Authorisation
    "amex:F29": FRAUD,           # Card Not Present
    # Authorization — auth was declined / absent / expired.
    "visa:11.1": AUTHORIZATION,  # Card Recovery Bulletin
    "visa:11.2": AUTHORIZATION,  # Declined Authorisation
    "visa:11.3": AUTHORIZATION,  # No Authorisation
    "mastercard:4837": AUTHORIZATION,  # No Cardholder Authorisation
    "mastercard:4808": AUTHORIZATION,  # Authorisation-Related Chargeback
    # Processing error — the transaction was processed incorrectly.
    "visa:12.2": PROCESSING_ERROR,   # Incorrect Transaction Code
    "visa:12.5": PROCESSING_ERROR,   # Incorrect Amount
    "visa:12.6": PROCESSING_ERROR,   # Duplicate Processing / Paid by Other Means
    "mastercard:4834": PROCESSING_ERROR,  # Point-of-Interaction Error
    # Consumer dispute — goods/services not received, not as described, or
    # cancelled/credit not processed.
    "visa:13.1": CONSUMER_DISPUTE,   # Merchandise/Services Not Received
    "visa:13.2": CONSUMER_DISPUTE,   # Cancelled Recurring
    "visa:13.3": CONSUMER_DISPUTE,   # Not as Described or Defective
    "visa:13.6": CONSUMER_DISPUTE,   # Credit Not Processed
    "mastercard:4853": CONSUMER_DISPUTE,  # Cardholder Dispute
    "mastercard:4855": CONSUMER_DISPUTE,  # Goods or Services Not Provided
    "amex:C08": CONSUMER_DISPUTE,   # Goods/Services Not Received or Only Partially
    "amex:C31": CONSUMER_DISPUTE,   # Goods/Services Not As Described
    "amex:C05": CONSUMER_DISPUTE,   # Goods/Services Cancelled
}


# --- Category -> human-readable evidence types --------------------------------
# The descriptive column from dispute-ingestion-router's classification table.
# evidence-assembler maps these onto concrete EvidenceBundle slots below.
CATEGORY_EVIDENCE_TYPES: dict[str, tuple[str, ...]] = {
    FRAUD: (
        "address / CVV verification match (AVS result)",
        "velocity checks on the account/card",
        "3-D Secure authentication proof",
        "device / IP consistency with prior legitimate activity",
    ),
    AUTHORIZATION: (
        "valid authorization code for the settled amount",
        "transaction / settlement records",
    ),
    PROCESSING_ERROR: (
        "batch reconciliation records",
        "audit trail proving no duplicate charge / correct amount",
    ),
    CONSUMER_DISPUTE: (
        "delivery proof (carrier tracking, delivery confirmation)",
        "signed receipt / proof of collection",
        "product description / images shown at purchase",
        "refund and cancellation policy the cardholder accepted",
        "customer communication history",
    ),
}


# --- EvidenceBundle slot names (mirrors Razorpay's evidence object) ------------
SHIPPING_PROOF = "shipping_proof"
BILLING_PROOF = "billing_proof"
CANCELLATION_PROOF = "cancellation_proof"
CUSTOMER_COMMUNICATION = "customer_communication"
PROOF_OF_SERVICE = "proof_of_service"
EXPLANATION_LETTER = "explanation_letter"
REFUND_CONFIRMATION = "refund_confirmation"
ACTIVITY_LOG = "activity_log"

EVIDENCE_SLOTS: tuple[str, ...] = (
    SHIPPING_PROOF,
    BILLING_PROOF,
    CANCELLATION_PROOF,
    CUSTOMER_COMMUNICATION,
    PROOF_OF_SERVICE,
    EXPLANATION_LETTER,
    REFUND_CONFIRMATION,
    ACTIVITY_LOG,
)

# Slots that carry customer PII -> compliance-knowledge-graph attaches the DPDP
# Act data-minimisation note to these.
PII_BEARING_SLOTS: frozenset[str] = frozenset(
    {CUSTOMER_COMMUNICATION, SHIPPING_PROOF, BILLING_PROOF}
)


# --- Category -> required EvidenceBundle slots --------------------------------
# "Required" = the assembler should look for this slot and report it `missing` if
# absent (as opposed to `not_applicable`). Slot -> status logic lives in
# evidence-assembler; this is only the which-slots-matter mapping.
CATEGORY_REQUIRED_SLOTS: dict[str, tuple[str, ...]] = {
    FRAUD: (BILLING_PROOF, ACTIVITY_LOG, CUSTOMER_COMMUNICATION, EXPLANATION_LETTER),
    AUTHORIZATION: (ACTIVITY_LOG, EXPLANATION_LETTER),
    PROCESSING_ERROR: (ACTIVITY_LOG, REFUND_CONFIRMATION, EXPLANATION_LETTER),
    CONSUMER_DISPUTE: (
        SHIPPING_PROOF,
        PROOF_OF_SERVICE,
        CUSTOMER_COMMUNICATION,
        REFUND_CONFIRMATION,
        CANCELLATION_PROOF,
        EXPLANATION_LETTER,
    ),
}


def classify(network: str, code: str) -> str:
    """Return the category for a network reason code.

    ``network`` is e.g. ``"visa"``/``"mastercard"``/``"amex"`` (case-insensitive),
    ``code`` the raw network code (e.g. ``"10.4"``, ``"4863"``, ``"C08"``).
    Returns :data:`NEEDS_MANUAL_CLASSIFICATION` for an unrecognised code rather
    than guessing.
    """
    key = f"{network.strip().lower()}:{code.strip()}"
    return REASON_CODE_TO_CATEGORY.get(key, NEEDS_MANUAL_CLASSIFICATION)


def required_slots(category: str) -> tuple[str, ...]:
    """EvidenceBundle slots that matter for ``category`` (empty for unknown)."""
    return CATEGORY_REQUIRED_SLOTS.get(category, ())
