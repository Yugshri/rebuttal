"""End-to-end pipeline wiring — the cross-module glue the orchestrator owns.

Each module is independently testable; this is the thin layer that runs a dispute
through all of them in order:

    webhook payload
      -> ingest_dispute_event         (dispute-ingestion-router)
      -> assemble_evidence            (evidence-assembler)
      -> [risk profiles precomputed]  (risk-graph-service, batch)
      -> score_and_route             (confidence-scorer-review)

The risk profile is deliberately NOT computed here per-request — that is the
scheduled batch job's role (:func:`ensure_risk_profiles` runs it once if the
table is empty; in a real deployment it runs nightly). ``score_and_route`` reads
the latest precomputed profile and treats a missing one as UNKNOWN → human
review.

Nothing in this module can submit a dispute. It stops at "scored and queued";
dispatch is only ``POST /disputes/{id}/review``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.evidence import assemble_evidence
from src.ingestion import get_dispute_case, ingest_dispute_event
from src.init_db import init_all_tables


@dataclass
class PipelineResult:
    dispute_id: str
    ingest_status: str          # created / redelivery / phase_advance / ...
    category: str
    needs_manual_classification: bool
    evidence_completeness: float | None
    evidence_assembly_status: str
    confidence_score: float | None
    recommended_action: str
    queue: str                  # draft_for_submit / human_review
    priority: float | None
    rationale: str


def ensure_risk_profiles(*, force: bool = False) -> int:
    """Make sure ``account_risk_profile`` is populated. Returns row count.

    The graph batch computes every account's profile in one pass. We run it once
    lazily so a fresh system store is usable; a real deployment schedules it.
    """
    from src.common.db import system_session
    from src.risk.batch import run_nightly_batch
    from src.risk.models import AccountRiskProfile

    with system_session() as s:
        count = s.query(AccountRiskProfile).count()
    if count and not force:
        return count

    run_nightly_batch()

    with system_session() as s:
        return s.query(AccountRiskProfile).count()


def run_pipeline(dispute_id: str, *, now_epoch: int | None = None) -> PipelineResult:
    """Assemble evidence, then score and route an already-ingested dispute."""
    case = get_dispute_case(dispute_id)
    if case is None:
        raise ValueError(f"dispute {dispute_id!r} not ingested")

    assembly = assemble_evidence(dispute_id, now_epoch=now_epoch)

    # Local import: scoring pulls in the risk service; keep the dependency lazy.
    from src.scoring import score_and_route

    routing = score_and_route(dispute_id, now_epoch=now_epoch)

    _write_assembled_evidence_summary(dispute_id, assembly)

    return PipelineResult(
        dispute_id=dispute_id,
        ingest_status="already_ingested",
        category=(
            getattr(case, "category", None)
            or getattr(assembly, "category", None)
            or ""
        ),
        needs_manual_classification=bool(
            getattr(case, "needs_manual_classification", None)
            if getattr(case, "needs_manual_classification", None) is not None
            else getattr(assembly, "needs_manual_classification", False)
        ),
        evidence_completeness=getattr(assembly, "completeness", None),
        evidence_assembly_status=getattr(assembly, "assembly_status", "") or "",
        confidence_score=getattr(routing, "confidence_score", None),
        recommended_action=getattr(routing, "recommended_action", "") or "",
        queue=getattr(routing, "queue", "") or "",
        priority=getattr(routing, "priority", None),
        rationale=getattr(routing, "rationale", "") or "",
    )


def ingest_and_run(
    payload: dict[str, Any], *, now_epoch: int | None = None
) -> PipelineResult:
    """Full path from a raw webhook payload to a scored, queued dispute."""
    outcome = ingest_dispute_event(payload)
    dispute_id = outcome.dispute_id
    result = run_pipeline(dispute_id, now_epoch=now_epoch)
    result.ingest_status = getattr(outcome, "transition_type", None) or getattr(
        outcome, "status", "ingested"
    )
    return result


def _write_assembled_evidence_summary(dispute_id: str, assembly: Any) -> None:
    """Store a compact evidence summary on ``DisputeCase.assembled_evidence``.

    The full ``EvidenceBundle`` lives in its own table; this is the at-a-glance
    copy the Dispute entity carries (mirrors Razorpay's ``evidence`` sub-object
    being attached to the dispute).
    """
    from src.common.db import system_session
    from src.ingestion.models import DisputeCase

    summary = {
        "assembly_status": getattr(assembly, "assembly_status", None),
        "completeness": getattr(assembly, "completeness", None),
        "slot_status": getattr(assembly, "slot_status", None),
        "explanation_letter_source": getattr(
            assembly, "explanation_letter_source", None
        ),
    }
    with system_session() as s:
        case = s.get(DisputeCase, dispute_id)
        if case is not None:
            case.assembled_evidence = summary


__all__ = [
    "PipelineResult",
    "ensure_risk_profiles",
    "run_pipeline",
    "ingest_and_run",
    "init_all_tables",
]
