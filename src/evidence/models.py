"""``EvidenceBundle`` — the assembled-evidence row owned by evidence-assembler.

One row per dispute id. Mirrors Razorpay's evidence object slot-for-slot
(``shipping_proof``, ``billing_proof``, ``cancellation_proof``,
``customer_communication``, ``proof_of_service``, ``explanation_letter``,
``refund_confirmation``, ``activity_log``) and adds this system's own
completeness bookkeeping.

Written to the **system** store only (``system.db``) via ``system_session()``.
This module never writes the read-only external store, and it never writes the
``DisputeCase`` row it reads (the ``assembled_evidence`` field on ``DisputeCase``
is filled by the scorer/integration layer, not here).

The rule that shapes this schema: **a slot with no backing record is
``missing``, never a fabricated placeholder.** Three per-slot states, kept
distinct:

* ``present``        — a real record was found; its content is in the slot column.
* ``missing``        — the slot is required for this dispute's category, we looked,
                       and no record exists. An honestly-reported gap.
* ``not_applicable`` — this reason-code category does not need this slot at all.

Types are deliberately generic (``String``, ``Integer``, ``Float``, ``Boolean``,
``JSON``, ``Text``) so the schema is identical on SQLite and Postgres — no native
DB enums, no SQLite-only features.
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from src.common.models_base import Base

# Per-slot status vocabulary. Validated in Python before it reaches a column.
SLOT_PRESENT = "present"
SLOT_MISSING = "missing"
SLOT_NOT_APPLICABLE = "not_applicable"
SLOT_STATUSES: tuple[str, ...] = (SLOT_PRESENT, SLOT_MISSING, SLOT_NOT_APPLICABLE)

# Bundle-level assembly status.
ASSEMBLY_COMPLETE = "complete"
ASSEMBLY_PARTIAL = "partial"
ASSEMBLY_PENDING = "pending"
ASSEMBLY_STATUSES: tuple[str, ...] = (
    ASSEMBLY_COMPLETE,
    ASSEMBLY_PARTIAL,
    ASSEMBLY_PENDING,
)

# Where the explanation letter's text came from.
LETTER_SOURCE_LLM = "llm"
LETTER_SOURCE_TEMPLATE = "template"
LETTER_SOURCE_NONE = "none"


class EvidenceBundle(Base):
    """Assembled evidence for one dispute id, plus the completeness signal.

    ``confidence-scorer-review`` reads :attr:`completeness`, :attr:`slot_status`
    and :attr:`needs_manual_classification` off this row; it does not re-derive
    them. This module does not interpret them into a confidence score — that is
    not its call to make.
    """

    __tablename__ = "evidence_bundles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dispute_id: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    payment_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # --- classification context (read from DisputeCase, not decided here) ---
    category: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    needs_manual_classification: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, index=True
    )
    required_slots: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    # --- Razorpay evidence object slots (assembled content, or NULL) ---
    # Each is the real record content pulled from the external store, or NULL
    # when the slot is `missing` / `not_applicable`. explanation_letter is text
    # (narrative over the other present slots); the rest are structured JSON.
    shipping_proof: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    billing_proof: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    cancellation_proof: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    customer_communication: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    proof_of_service: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    explanation_letter: Mapped[str | None] = mapped_column(Text, nullable=True)
    refund_confirmation: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    activity_log: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # --- per-slot status map: slot name -> present | missing | not_applicable ---
    slot_status: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # slot name -> which external table (or "generated") backed it. Provenance.
    slot_sources: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    # --- completeness measure -------------------------------------------------
    # present / (present + missing) over the category's *required source* slots
    # (explanation_letter excluded — it is generated, not retrieved). NULL when
    # there is nothing to measure (needs_manual_classification).
    completeness: Mapped[float | None] = mapped_column(Float, nullable=True)
    present_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    missing_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    not_applicable_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    assembly_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ASSEMBLY_PENDING, index=True
    )

    # --- compliance grounding for PII-bearing slots (DPDP data-minimisation) ---
    # slot name -> list of RequirementMatch dicts from src.compliance.lookup().
    compliance_citations: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict
    )

    # --- explanation-letter provenance --------------------------------------
    explanation_letter_source: Mapped[str] = mapped_column(
        String(16), nullable=False, default=LETTER_SOURCE_NONE
    )

    # --- timing awareness (read from DisputeCase.respond_by; not owned here) ---
    respond_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    hours_to_deadline: Mapped[float | None] = mapped_column(Float, nullable=True)
    deadline_pressure: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    # --- bookkeeping -------------------------------------------------------
    assembled_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    assembly_passes: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    def as_dict(self) -> dict:
        return {
            "dispute_id": self.dispute_id,
            "payment_id": self.payment_id,
            "category": self.category,
            "needs_manual_classification": self.needs_manual_classification,
            "required_slots": list(self.required_slots or []),
            "slots": {
                "shipping_proof": self.shipping_proof,
                "billing_proof": self.billing_proof,
                "cancellation_proof": self.cancellation_proof,
                "customer_communication": self.customer_communication,
                "proof_of_service": self.proof_of_service,
                "explanation_letter": self.explanation_letter,
                "refund_confirmation": self.refund_confirmation,
                "activity_log": self.activity_log,
            },
            "slot_status": dict(self.slot_status or {}),
            "slot_sources": dict(self.slot_sources or {}),
            "completeness": self.completeness,
            "present_count": self.present_count,
            "missing_count": self.missing_count,
            "not_applicable_count": self.not_applicable_count,
            "assembly_status": self.assembly_status,
            "compliance_citations": dict(self.compliance_citations or {}),
            "explanation_letter_source": self.explanation_letter_source,
            "respond_by": self.respond_by,
            "hours_to_deadline": self.hours_to_deadline,
            "deadline_pressure": self.deadline_pressure,
            "assembled_at": self.assembled_at,
            "updated_at": self.updated_at,
            "assembly_passes": self.assembly_passes,
            "notes": self.notes,
        }


__all__ = [
    "EvidenceBundle",
    "SLOT_PRESENT",
    "SLOT_MISSING",
    "SLOT_NOT_APPLICABLE",
    "SLOT_STATUSES",
    "ASSEMBLY_COMPLETE",
    "ASSEMBLY_PARTIAL",
    "ASSEMBLY_PENDING",
    "ASSEMBLY_STATUSES",
    "LETTER_SOURCE_LLM",
    "LETTER_SOURCE_TEMPLATE",
    "LETTER_SOURCE_NONE",
]
