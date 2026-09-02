"""confidence-scorer-review definition-of-done tests.

Covered:
* the transparent weighted confidence score + its per-factor breakdown;
* NO code path from high confidence -> dispatched/submitted status that skips the
  human review record (behavioural + a source-level check that only the review
  endpoint writes the transition);
* priority queue ordering across several amount/deadline combinations;
* the 48h deadline monitor flags an unresolved case inside the window, not one
  outside it, and retires a flag once the case resolves;
* a reopened (won -> pre_arbitration) case re-enters review;
* the outcome log pairs decision-time confidence with the actual outcome;
* a missing risk profile is treated as UNKNOWN -> human review (never low risk).
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.common import db
from src.common.models_base import RecommendedAction
from src.ingestion import init_system_tables as ingestion_init
from src.ingestion.models import DisputeCase, DisputePhaseHistory
from src.evidence import init_system_tables as evidence_init
from src.evidence.models import (
    ASSEMBLY_COMPLETE,
    ASSEMBLY_PARTIAL,
    ASSEMBLY_PENDING,
    EvidenceBundle,
)
from src.risk.models import AccountRiskProfile
from src.scoring import init_system_tables as scoring_init
from src.scoring.api import router as scoring_router
from src.scoring.deadline import run_deadline_scan
from src.scoring.models import (
    QUEUE_DRAFT_FOR_SUBMIT,
    QUEUE_HUMAN_REVIEW,
    DeadlineFlag,
    HumanReviewDecision,
    OutcomeLogEntry,
    ReviewQueueEntry,
)
from src.scoring.outcome import get_outcome_log, record_outcome
from src.scoring.routing import (
    compute_priority,
    get_review_queue,
    score_and_route,
)
from src.scoring.scorer import (
    CONFIDENCE_THRESHOLD,
    WEIGHT_EVIDENCE_COMPLETENESS,
    WEIGHT_RISK,
    gather_inputs,
    score_dispute,
)
from src.synthetic.schema import create_external_db

_NOW = 1_788_000_000
_FAR = _NOW + 30 * 24 * 3600      # 30 days out — no deadline pressure
_SOON = _NOW + 20 * 3600          # 20h out — inside the 48h window


# --------------------------------------------------------------------------- #
# fixtures / seeding helpers
# --------------------------------------------------------------------------- #
@pytest.fixture()
def env(isolated_dbs):
    ingestion_init()
    evidence_init()
    scoring_init()
    from src.common.models_base import Base

    Base.metadata.create_all(
        db.system_engine(), tables=[AccountRiskProfile.__table__]
    )
    # a minimal external store so payment_id -> account_id resolves
    conn = create_external_db(isolated_dbs["external"])
    conn.close()
    db.reset_engines_for_tests()
    return isolated_dbs


def _add_payment(external: Path, payment_id: str, account_id: str) -> None:
    conn = sqlite3.connect(str(external))
    try:
        conn.execute(
            "INSERT OR REPLACE INTO payments VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                payment_id, f"order_{payment_id}", account_id, 1000.0, "INR",
                "card", "visa", "4242", "A1", "captured",
                "2026-08-01T00:00:00", "x@y.com", "1.1.1.1", "dev", 1,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_case(
    dispute_id: str,
    *,
    payment_id: str,
    amount: int = 500_00,
    respond_by: int = _FAR,
    status: str = "open",
    phase: str = "chargeback",
    category: str = "consumer_dispute",
    needs_manual: bool = False,
) -> None:
    from src.common.models_base import phase_rank

    with db.system_session() as s:
        s.add(
            DisputeCase(
                id=dispute_id,
                payment_id=payment_id,
                amount=amount,
                amount_deducted=amount,
                reason_code="visa:13.1",
                reason_description="Merchandise not received",
                respond_by=respond_by,
                status=status,
                phase=phase,
                network="visa",
                category=category,
                needs_manual_classification=needs_manual,
                phase_rank=phase_rank(phase),
                reopen_count=0,
                event_count=1,
                first_seen_at=_NOW,
                last_updated_at=_NOW,
            )
        )


def _seed_bundle(
    dispute_id: str,
    *,
    payment_id: str,
    completeness: float | None = 1.0,
    assembly_status: str = ASSEMBLY_COMPLETE,
    needs_manual: bool = False,
    category: str = "consumer_dispute",
) -> None:
    with db.system_session() as s:
        s.add(
            EvidenceBundle(
                dispute_id=dispute_id,
                payment_id=payment_id,
                category=category,
                needs_manual_classification=needs_manual,
                required_slots=[],
                slot_status={},
                slot_sources={},
                completeness=completeness,
                present_count=int((completeness or 0) * 4),
                missing_count=0 if (completeness or 0) == 1.0 else 2,
                not_applicable_count=0,
                assembly_status=assembly_status,
                compliance_citations={},
                assembled_at=_NOW,
                updated_at=_NOW,
            )
        )


def _seed_profile(account_id: str, *, baseline_deviation: float = 0.0) -> None:
    with db.system_session() as s:
        s.add(
            AccountRiskProfile(
                account_id=account_id,
                pagerank_score=0.01,
                betweenness_score=0.0,
                baseline_deviation=baseline_deviation,
                deviation_band="high" if baseline_deviation >= 8 else "low",
                last_updated=datetime.now(timezone.utc),
                returns_risk_score=0.1,
                returns_risk_band="low",
            )
        )


def _full_case(
    env,
    dispute_id="D1",
    *,
    account_id="ACC_1",
    completeness=1.0,
    assembly_status=ASSEMBLY_COMPLETE,
    baseline_deviation=0.0,
    respond_by=_FAR,
    phase="chargeback",
    status="open",
    amount=500_00,
    with_profile=True,
    needs_manual=False,
):
    pid = f"pay_{dispute_id}"
    _add_payment(env["external"], pid, account_id)
    db.reset_engines_for_tests()
    _seed_case(
        dispute_id, payment_id=pid, amount=amount, respond_by=respond_by,
        phase=phase, status=status, needs_manual=needs_manual,
    )
    _seed_bundle(
        dispute_id, payment_id=pid, completeness=completeness,
        assembly_status=assembly_status, needs_manual=needs_manual,
    )
    if with_profile:
        _seed_profile(account_id, baseline_deviation=baseline_deviation)
    return dispute_id


# --------------------------------------------------------------------------- #
# 1. the transparent score
# --------------------------------------------------------------------------- #
def test_confidence_score_is_the_documented_weighted_rule(env):
    _full_case(env, "D1", completeness=0.5, baseline_deviation=4.0)
    res = score_dispute("D1", now_epoch=_NOW)
    # risk factor = 1 - min(4/8, 1) = 0.5
    expected = round(
        WEIGHT_EVIDENCE_COMPLETENESS * 0.5 + WEIGHT_RISK * 0.5, 4
    )
    assert res.confidence_score == expected
    f = res.breakdown["factors"]
    assert f["evidence_completeness"]["value"] == 0.5
    assert f["evidence_completeness"]["contribution"] == round(
        WEIGHT_EVIDENCE_COMPLETENESS * 0.5, 4
    )
    assert f["risk"]["baseline_deviation"] == 4.0
    assert f["risk"]["factor"] == 0.5
    assert res.breakdown["threshold"] == CONFIDENCE_THRESHOLD


def test_high_deviation_lowers_confidence(env):
    _full_case(env, "LOWRISK", account_id="ACC_LOW", completeness=1.0, baseline_deviation=0.0)
    _full_case(env, "HIGHRISK", account_id="ACC_HIGH", completeness=1.0, baseline_deviation=12.0)
    low = score_dispute("LOWRISK", now_epoch=_NOW).confidence_score
    high = score_dispute("HIGHRISK", now_epoch=_NOW).confidence_score
    assert low > high
    assert low == 1.0  # 0.65*1 + 0.35*1


def test_needs_manual_classification_skips_score_and_defers(env):
    _full_case(
        env, "MANUAL", completeness=None, assembly_status=ASSEMBLY_PENDING,
        needs_manual=True,
    )
    dec = score_and_route("MANUAL", now_epoch=_NOW)
    assert dec.queue == QUEUE_HUMAN_REVIEW
    assert dec.recommended_action == RecommendedAction.NEEDS_MANUAL_CLASSIFICATION.value
    assert dec.confidence_score is None


def test_assembly_pending_forces_human_review(env):
    _full_case(env, "PENDING", completeness=None, assembly_status=ASSEMBLY_PENDING)
    dec = score_and_route("PENDING", now_epoch=_NOW)
    assert dec.queue == QUEUE_HUMAN_REVIEW
    assert "assembly_status_pending" in dec.hard_gates


# --------------------------------------------------------------------------- #
# 2. NO auto-submit — the non-negotiable
# --------------------------------------------------------------------------- #
def test_recommended_action_enum_has_no_submitted_state():
    values = {e.value for e in RecommendedAction}
    assert not any("submit" in v and v != "draft_for_submit" for v in values)
    assert "submitted" not in values
    assert not hasattr(RecommendedAction, "SUBMITTED")


def test_no_scoring_path_moves_a_max_confidence_case_to_submitted(env):
    """Construct the single most confident case possible and prove every
    scoring-path entry point leaves DisputeCase.status untouched and writes no
    human review record. Only POST /disputes/{id}/review may advance it."""
    did = _full_case(
        env, "MAXCONF", completeness=1.0, baseline_deviation=0.0,
        respond_by=_FAR, phase="chargeback", status="open",
    )

    # every public scoring-path callable, run against the max-confidence case
    dec = score_and_route(did, now_epoch=_NOW)
    assert dec.queue == QUEUE_DRAFT_FOR_SUBMIT
    assert dec.confidence_score == 1.0
    score_dispute(did, now_epoch=_NOW)
    gather_inputs(did)
    get_review_queue()
    run_deadline_scan(now_epoch=_NOW)
    compute_priority(999_999_00, 1.0)

    with db.system_session() as s:
        case = s.get(DisputeCase, did)
        assert case.status == "open", "scoring path advanced the dispute status"
        assert case.reviewed_by is None
        entry = (
            s.query(ReviewQueueEntry)
            .filter(ReviewQueueEntry.dispute_id == did)
            .one()
        )
        assert entry.dispatched is False
        assert entry.dispatched_at is None
        assert s.query(HumanReviewDecision).count() == 0

    # the ONLY path that advances it: a human posting a decision
    app = FastAPI()
    app.include_router(scoring_router)
    client = TestClient(app)
    r = client.post(
        f"/disputes/{did}/review",
        json={"reviewed_by": "analyst_7", "decision": "submit"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["dispatched"] is True
    assert body["dispute_status_before"] == "open"
    assert body["dispute_status_after"] == "under_review"

    with db.system_session() as s:
        case = s.get(DisputeCase, did)
        assert case.status == "under_review"
        assert case.reviewed_by == "analyst_7"
        assert s.query(HumanReviewDecision).count() == 1
        entry = (
            s.query(ReviewQueueEntry)
            .filter(ReviewQueueEntry.dispute_id == did)
            .one()
        )
        assert entry.dispatched is True


def test_only_the_review_endpoint_writes_the_dispatch_transition():
    """Source-level: the status / dispatched / reviewed_by writes that move a
    dispute toward submitted live ONLY in api.py's review handler — not in
    scorer / routing / deadline / outcome."""
    scoring_dir = Path(__file__).resolve().parents[1] / "src" / "scoring"
    forbidden = ("dispatched = True", ".status = DisputeStatus", "case.reviewed_by =")
    for pyfile in scoring_dir.glob("*.py"):
        text = pyfile.read_text(encoding="utf-8")
        for needle in forbidden:
            if needle in text:
                assert pyfile.name == "api.py", (
                    f"{pyfile.name} contains dispatch-transition write {needle!r}"
                )


# --------------------------------------------------------------------------- #
# 3. priority queue ordering
# --------------------------------------------------------------------------- #
def test_priority_formula_monotonic_in_amount_and_deadline():
    # higher amount -> higher priority (same deadline)
    assert compute_priority(10_000_00, 100.0) > compute_priority(1_000_00, 100.0)
    # closer deadline -> higher priority (same amount)
    assert compute_priority(5_000_00, 5.0) > compute_priority(5_000_00, 500.0)
    # overdue clamps to max urgency, not negative / zero
    assert compute_priority(5_000_00, -50.0) >= compute_priority(5_000_00, 1.0)
    assert compute_priority(5_000_00, -50.0) > 0


def test_review_queue_is_priority_sorted(env):
    # four cases: (amount paise, hours to deadline)
    combos = {
        "BIG_SOON": (50_000_00, _NOW + 3 * 3600),
        "BIG_LATE": (50_000_00, _NOW + 20 * 24 * 3600),
        "SMALL_SOON": (500_00, _NOW + 3 * 3600),
        "SMALL_LATE": (500_00, _NOW + 20 * 24 * 3600),
    }
    for i, (name, (amt, rb)) in enumerate(combos.items()):
        # force human review via high deviation so all four land in one queue
        _full_case(
            env, name, account_id=f"ACC_{i}", completeness=1.0,
            baseline_deviation=20.0, amount=amt, respond_by=rb,
        )
        score_and_route(name, now_epoch=_NOW)

    q = get_review_queue()
    order = [e["dispute_id"] for e in q]
    prios = [e["priority"] for e in q]
    assert prios == sorted(prios, reverse=True), "queue not sorted by priority desc"
    assert order[0] == "BIG_SOON"
    assert order[-1] == "SMALL_LATE"
    assert order.index("BIG_LATE") < order.index("SMALL_LATE")
    assert order.index("SMALL_SOON") < order.index("SMALL_LATE")


# --------------------------------------------------------------------------- #
# 4. deadline monitor
# --------------------------------------------------------------------------- #
def test_deadline_scan_flags_inside_window_not_outside(env):
    _full_case(env, "URGENT", account_id="ACC_U", respond_by=_SOON, status="open")
    _full_case(env, "RELAXED", account_id="ACC_R", respond_by=_FAR, status="open")

    result = run_deadline_scan(now_epoch=_NOW)
    assert "URGENT" in result.flagged
    assert "RELAXED" not in result.flagged

    with db.system_session() as s:
        flags = {f.dispute_id: f for f in s.query(DeadlineFlag).all()}
    assert set(flags) == {"URGENT"}
    assert flags["URGENT"].resolved is False
    assert flags["URGENT"].compliance_citations  # respond_by citations attached


def test_deadline_scan_flags_overdue_and_retires_on_resolution(env):
    _full_case(env, "OVERDUE", account_id="ACC_O", respond_by=_NOW - 5 * 3600, status="open")
    r1 = run_deadline_scan(now_epoch=_NOW)
    assert "OVERDUE" in r1.overdue

    # dispute resolves -> next scan retires the flag
    with db.system_session() as s:
        s.get(DisputeCase, "OVERDUE").status = "won"
    r2 = run_deadline_scan(now_epoch=_NOW)
    assert "OVERDUE" in r2.resolved_since_last_scan
    with db.system_session() as s:
        assert s.get(DeadlineFlag, 1).resolved is True


# --------------------------------------------------------------------------- #
# 5. reopen re-enters review
# --------------------------------------------------------------------------- #
def test_reopened_won_dispute_re_enters_review(env):
    did = _full_case(
        env, "REOPEN", account_id="ACC_RE", completeness=1.0,
        baseline_deviation=0.0, phase="chargeback", status="won",
    )
    # first pass: it was won, dispatched by a human, outcome logged
    score_and_route(did, now_epoch=_NOW)
    with db.system_session() as s:
        entry = s.query(ReviewQueueEntry).filter_by(dispute_id=did).one()
        entry.dispatched = True
    record_outcome(did, "won", now_epoch=_NOW)

    # now ingestion advances it to pre_arbitration and marks the reopen
    from src.common.models_base import phase_rank

    with db.system_session() as s:
        case = s.get(DisputeCase, did)
        case.phase = "pre_arbitration"
        case.phase_rank = phase_rank("pre_arbitration")
        case.status = "under_review"
        case.reopen_count = 1
        s.add(
            DisputePhaseHistory(
                dispute_id=did, phase="pre_arbitration", status="under_review",
                phase_rank=phase_rank("pre_arbitration"), reason_code="visa:13.1",
                respond_by=_FAR, transition_type="phase_advance", is_reopen=True,
                out_of_order=False, prev_phase="chargeback", prev_status="won",
                recorded_at=_NOW,
            )
        )

    dec = score_and_route(did, now_epoch=_NOW)
    assert dec.re_entered is True
    assert dec.queue_generation == 2
    assert dec.queue == QUEUE_HUMAN_REVIEW
    assert "reopened_dispute_re_entered_review" in dec.hard_gates

    with db.system_session() as s:
        entries = (
            s.query(ReviewQueueEntry)
            .filter_by(dispute_id=did)
            .order_by(ReviewQueueEntry.queue_generation)
            .all()
        )
        assert len(entries) == 2
        assert entries[0].superseded is True
        assert entries[1].superseded is False
        assert entries[1].dispatched is False
    # the reopened dispute is back in the human queue, not resting on its win
    assert did in [e["dispute_id"] for e in get_review_queue()]


# --------------------------------------------------------------------------- #
# 6. outcome log pairs decision-time confidence with the actual outcome
# --------------------------------------------------------------------------- #
def test_outcome_log_pairs_decision_confidence_with_outcome(env):
    did = _full_case(env, "OUT1", account_id="ACC_OUT", completeness=1.0, baseline_deviation=0.0)
    dec = score_and_route(did, now_epoch=_NOW)
    assert dec.queue == QUEUE_DRAFT_FOR_SUBMIT

    app = FastAPI()
    app.include_router(scoring_router)
    client = TestClient(app)
    client.post(
        f"/disputes/{did}/review",
        json={"reviewed_by": "analyst_2", "decision": "submit"},
    )
    rec = record_outcome(did, "won", now_epoch=_NOW + 5 * 24 * 3600)
    assert rec.decision_confidence_score == dec.confidence_score
    assert rec.actual_outcome == "won"
    assert rec.score_vs_outcome_agrees is True  # high-conf draft that won

    log = get_outcome_log(did)
    assert len(log) == 1
    row = log[0]
    assert row["decision_confidence_score"] == dec.confidence_score
    assert row["actual_outcome"] == "won"
    assert row["decision_queue"] == QUEUE_DRAFT_FOR_SUBMIT
    assert row["features"]["completeness"] == 1.0
    assert row["features"]["baseline_deviation"] == 0.0


def test_outcome_log_records_disagreement(env):
    """A high-confidence draft that then LOST — the log captures the miss for
    retraining (score_vs_outcome_agrees is False)."""
    did = _full_case(env, "MISS", account_id="ACC_MISS", completeness=1.0, baseline_deviation=0.0)
    dec = score_and_route(did, now_epoch=_NOW)
    assert dec.queue == QUEUE_DRAFT_FOR_SUBMIT
    rec = record_outcome(did, "lost", now_epoch=_NOW)
    assert rec.score_vs_outcome_agrees is False
    assert get_outcome_log(did)[0]["actual_outcome"] == "lost"


# --------------------------------------------------------------------------- #
# 7. missing risk profile -> UNKNOWN -> human review (never low risk)
# --------------------------------------------------------------------------- #
def test_missing_risk_profile_is_unknown_and_defers(env):
    _full_case(
        env, "NORISK", account_id="ACC_NONE", completeness=1.0,
        with_profile=False,
    )
    res = score_dispute("NORISK", now_epoch=_NOW)
    assert res.breakdown["factors"]["risk"]["source"] == "unknown_no_profile"
    assert res.breakdown["factors"]["risk"]["factor"] == 0.0  # worst value, not low risk
    dec = score_and_route("NORISK", now_epoch=_NOW)
    assert dec.queue == QUEUE_HUMAN_REVIEW
    assert "risk_profile_unknown" in dec.hard_gates
    # even with perfect evidence, an unmeasured counterparty never auto-drafts
    assert dec.recommended_action != RecommendedAction.DRAFT_FOR_SUBMIT.value


def test_recommendation_endpoint_returns_cited_rationale(env):
    _full_case(env, "REC1", account_id="ACC_REC", completeness=0.6, baseline_deviation=2.0)
    app = FastAPI()
    app.include_router(scoring_router)
    client = TestClient(app)
    body = client.get("/disputes/REC1/recommendation").json()
    assert body["dispute_id"] == "REC1"
    assert body["confidence_score"] is not None
    assert "consumer_dispute" in body["rationale"]
    assert body["compliance_citations"], "recommendation carries no citations"
    assert body["priority"] > 0
