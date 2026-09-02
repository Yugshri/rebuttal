"""evidence-assembler definition-of-done tests.

Covers:
* consumer-dispute case with matching records -> every required slot assembled;
* no matching shipping record -> `shipping_proof` is `missing` (visible in the
  status map), never fabricated, never silently skipped;
* slots not required by the category -> `not_applicable`, distinct from `missing`;
* the completeness measure, hand-checked against fixed cases;
* the `explanation_letter` deterministic template path works with no API key AND
  carries no content from a slot that is not `present`;
* `needs_manual_classification` -> not assembled, flagged pending for a human;
* DPDP data-minimisation citations attach to the PII-bearing slots;
* corpus-level: what fraction of real synthetic disputes end up genuinely
  incomplete (expected, fine).
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

from src.common import db
from src.common import reason_codes as rc
from src.evidence import (
    assemble_evidence,
    get_evidence_bundle,
    init_system_tables as evidence_init,
)
from src.evidence.assembler import DisputeNotFound
from src.evidence.models import (
    ASSEMBLY_COMPLETE,
    ASSEMBLY_PARTIAL,
    ASSEMBLY_PENDING,
    LETTER_SOURCE_TEMPLATE,
    SLOT_MISSING,
    SLOT_NOT_APPLICABLE,
    SLOT_PRESENT,
)
from src.ingestion import init_system_tables as ingestion_init
from src.synthetic.schema import create_external_db

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_EXTERNAL_DB = REPO_ROOT / "data" / "external.db"
WEBHOOK_DIR = REPO_ROOT / "data" / "webhooks"

_NOW = 1_756_900_000  # fixed "now" for deterministic deadline math


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #
@pytest.fixture()
def env(isolated_dbs):
    """Isolated system store with ingestion + evidence tables created."""
    ingestion_init()
    evidence_init()
    return isolated_dbs


def _seed_external(
    path: Path,
    *,
    payment_id: str = "pay_T1",
    account_id: str = "ACC_T1",
    order: bool = True,
    shipment: bool = True,
    communications: int = 3,
    billing_address: bool = True,
    availability: dict | None = None,
) -> None:
    conn = create_external_db(path)
    try:
        if order:
            conn.execute(
                "INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "order_T1", payment_id, account_id, "Electronics",
                    "USB-C hub", 1500.0, 1, 0, 0, 0, "2026-08-01T10:00:00",
                ),
            )
        conn.execute(
            "INSERT INTO payments VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                payment_id, "order_T1", account_id, 1500.0, "INR", "card",
                "visa", "4242", "AUTH01", "captured", "2026-08-01T10:00:00",
                "u@example.com", "1.2.3.4", "df_1", 1,
            ),
        )
        if billing_address:
            conn.execute(
                "INSERT INTO addresses VALUES (?,?,?,?,?,?,?,?)",
                ("addr_b", account_id, "billing", "1 A St", "Mumbai", "400001", "IN", 0),
            )
            conn.execute(
                "INSERT INTO addresses VALUES (?,?,?,?,?,?,?,?)",
                ("addr_s", account_id, "shipping", "1 A St", "Mumbai", "400001", "IN", 0),
            )
        if shipment:
            conn.execute(
                "INSERT INTO shipments VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    "order_T1", "Delhivery", "TRK999", 1, "2026-08-02T09:00:00",
                    "2026-08-05T14:00:00", "delivered", 1, "addr_s",
                ),
            )
        for k in range(communications):
            conn.execute(
                "INSERT INTO communications VALUES (?,?,?,?,?,?)",
                (
                    f"comm_{k}", payment_id, "email", "inbound" if k % 2 else "outbound",
                    f"2026-08-0{k + 1}T12:00:00", "Customer asked about delivery status",
                ),
            )
        av = {
            "has_shipping_proof": 1,
            "has_billing_proof": 1,
            "has_cancellation_proof": 1,
            "has_customer_communication": 1,
            "has_proof_of_service": 1,
            "has_refund_confirmation": 1,
            "has_activity_log": 1,
        }
        if availability:
            av.update(availability)
        conn.execute(
            "INSERT INTO evidence_availability VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                payment_id, av["has_shipping_proof"], av["has_billing_proof"],
                av["has_cancellation_proof"], av["has_customer_communication"],
                av["has_proof_of_service"], av["has_refund_confirmation"],
                av["has_activity_log"], 1, 1, "full", "full",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _make_case(
    dispute_id: str,
    *,
    payment_id: str = "pay_T1",
    network: str = "visa",
    reason_code: str = "13.1",
    category: str = rc.CONSUMER_DISPUTE,
    needs_manual: bool = False,
    respond_by: int = _NOW + 30 * 24 * 3600,
    phase: str = "chargeback",
    amount: int = 150000,
) -> None:
    from src.common.db import system_session
    from src.ingestion.models import DisputeCase

    with system_session() as s:
        s.add(
            DisputeCase(
                id=dispute_id,
                payment_id=payment_id,
                amount=amount,
                amount_deducted=amount,
                reason_code=reason_code,
                reason_description="test",
                respond_by=respond_by,
                status="open",
                phase=phase,
                network=network,
                category=category,
                needs_manual_classification=needs_manual,
                phase_rank=2,
                reopen_count=0,
                event_count=1,
                first_seen_at=_NOW,
                last_updated_at=_NOW,
            )
        )


# --------------------------------------------------------------------------- #
# 1. consumer dispute, all records present
# --------------------------------------------------------------------------- #
def test_consumer_dispute_full_records_assembles_every_required_slot(env):
    _seed_external(env["external"])
    _make_case("disp_full")

    result = assemble_evidence("disp_full", now_epoch=_NOW, allow_llm=False)

    required_source = set(rc.required_slots(rc.CONSUMER_DISPUTE)) - {rc.EXPLANATION_LETTER}
    for slot in required_source:
        assert result.slot_status[slot] == SLOT_PRESENT, slot
    assert result.slot_status[rc.EXPLANATION_LETTER] == SLOT_PRESENT
    assert result.completeness == 1.0
    assert result.assembly_status == ASSEMBLY_COMPLETE
    assert result.missing_count == 0

    bundle = get_evidence_bundle("disp_full")
    assert bundle["slots"]["shipping_proof"]["tracking_number"] == "TRK999"
    assert bundle["slots"]["proof_of_service"]["item_description"] == "USB-C hub"
    assert bundle["slots"]["customer_communication"]["message_count"] == 3
    assert isinstance(bundle["slots"]["explanation_letter"], str)


# --------------------------------------------------------------------------- #
# 2. no shipping record -> missing, not fabricated
# --------------------------------------------------------------------------- #
def test_missing_shipping_record_is_missing_never_fabricated(env):
    _seed_external(
        env["external"],
        shipment=False,
        availability={"has_shipping_proof": 0},
    )
    _make_case("disp_noship")

    result = assemble_evidence("disp_noship", now_epoch=_NOW, allow_llm=False)

    assert result.slot_status[rc.SHIPPING_PROOF] == SLOT_MISSING
    assert result.slot_status[rc.SHIPPING_PROOF] != SLOT_NOT_APPLICABLE
    bundle = get_evidence_bundle("disp_noship")
    assert bundle["slots"]["shipping_proof"] is None  # not a placeholder
    assert bundle["slot_status"]["shipping_proof"] == SLOT_MISSING
    assert result.assembly_status == ASSEMBLY_PARTIAL
    assert result.completeness < 1.0
    assert "shipping_proof" in (result.notes or "")


# --------------------------------------------------------------------------- #
# 3. slots not required by the category -> not_applicable (distinct from missing)
# --------------------------------------------------------------------------- #
def test_slots_not_required_are_not_applicable(env):
    _seed_external(env["external"])
    _make_case(
        "disp_fraud",
        network="visa",
        reason_code="10.4",
        category=rc.FRAUD,
    )

    result = assemble_evidence("disp_fraud", now_epoch=_NOW, allow_llm=False)

    fraud_required = set(rc.required_slots(rc.FRAUD))
    for slot in (rc.SHIPPING_PROOF, rc.PROOF_OF_SERVICE, rc.CANCELLATION_PROOF, rc.REFUND_CONFIRMATION):
        assert slot not in fraud_required
        assert result.slot_status[slot] == SLOT_NOT_APPLICABLE
    for slot in (rc.BILLING_PROOF, rc.ACTIVITY_LOG, rc.CUSTOMER_COMMUNICATION):
        assert result.slot_status[slot] == SLOT_PRESENT
    # not_applicable and missing are genuinely different states
    assert SLOT_NOT_APPLICABLE != SLOT_MISSING
    assert result.not_applicable_count == 4


# --------------------------------------------------------------------------- #
# 4. completeness measure — hand-checked
# --------------------------------------------------------------------------- #
def test_completeness_measure_hand_checked_partial(env):
    # consumer_dispute required source slots: shipping_proof, proof_of_service,
    # customer_communication, refund_confirmation, cancellation_proof  (5 slots).
    # Drop shipping + cancellation -> 3 present / 2 missing -> 0.6 exactly.
    _seed_external(
        env["external"],
        shipment=False,
        availability={"has_shipping_proof": 0, "has_cancellation_proof": 0},
    )
    _make_case("disp_060")

    result = assemble_evidence("disp_060", now_epoch=_NOW, allow_llm=False)

    assert result.present_count == 3
    assert result.missing_count == 2
    assert result.completeness == pytest.approx(0.6)
    assert result.assembly_status == ASSEMBLY_PARTIAL


def test_completeness_all_missing_is_zero_not_one(env):
    _seed_external(
        env["external"],
        shipment=False,
        communications=0,
        availability={
            "has_shipping_proof": 0,
            "has_proof_of_service": 0,
            "has_customer_communication": 0,
            "has_refund_confirmation": 0,
            "has_cancellation_proof": 0,
        },
    )
    _make_case("disp_empty")
    result = assemble_evidence("disp_empty", now_epoch=_NOW, allow_llm=False)
    assert result.completeness == 0.0
    assert result.present_count == 0
    assert result.missing_count == 5


# --------------------------------------------------------------------------- #
# 5. explanation letter — template path, and no missing-slot content
# --------------------------------------------------------------------------- #
def test_explanation_letter_template_path_without_api_key(env):
    assert not os.environ.get("SARVAM_API_KEY")
    _seed_external(env["external"])
    _make_case("disp_letter")

    result = assemble_evidence("disp_letter", now_epoch=_NOW)  # allow_llm defaults True

    assert result.explanation_letter_source == LETTER_SOURCE_TEMPLATE
    bundle = get_evidence_bundle("disp_letter")
    letter = bundle["slots"]["explanation_letter"]
    assert isinstance(letter, str) and len(letter) > 50
    assert "disp_letter" in letter


def test_explanation_letter_carries_no_content_from_a_non_present_slot(env):
    # shipping_proof missing -> the letter must not claim delivery / tracking.
    _seed_external(
        env["external"],
        shipment=False,
        availability={"has_shipping_proof": 0},
    )
    _make_case("disp_noship_letter")
    result = assemble_evidence("disp_noship_letter", now_epoch=_NOW, allow_llm=False)
    assert result.slot_status[rc.SHIPPING_PROOF] == SLOT_MISSING

    letter = get_evidence_bundle("disp_noship_letter")["slots"]["explanation_letter"].lower()
    for banned in ("tracking", "trk999", "delivered on", "carrier", "delhivery", "signature"):
        assert banned not in letter, banned

    # sanity: when shipping IS present the letter DOES narrate it
    db.reset_engines_for_tests()  # release the read-only handle before reseeding
    _seed_external(env["external"], payment_id="pay_T2")
    _make_case("disp_ship_letter", payment_id="pay_T2")
    r2 = assemble_evidence("disp_ship_letter", now_epoch=_NOW, allow_llm=False)
    assert r2.slot_status[rc.SHIPPING_PROOF] == SLOT_PRESENT
    letter2 = get_evidence_bundle("disp_ship_letter")["slots"]["explanation_letter"].lower()
    assert "trk999" in letter2 and "delivered" in letter2


# --------------------------------------------------------------------------- #
# 6. needs_manual_classification -> not assembled
# --------------------------------------------------------------------------- #
def test_needs_manual_classification_produces_pending_bundle_not_assembly(env):
    _seed_external(env["external"])
    _make_case(
        "disp_manual",
        network="discover",
        reason_code="4752",
        category=rc.NEEDS_MANUAL_CLASSIFICATION,
        needs_manual=True,
    )

    result = assemble_evidence("disp_manual", now_epoch=_NOW, allow_llm=False)

    assert result.needs_manual_classification is True
    assert result.assembly_status == ASSEMBLY_PENDING
    assert result.completeness is None
    assert all(v == SLOT_NOT_APPLICABLE for v in result.slot_status.values())
    bundle = get_evidence_bundle("disp_manual")
    assert bundle["slots"]["explanation_letter"] is None
    assert bundle["slots"]["shipping_proof"] is None
    assert "defer_to_human" in bundle["notes"]


# --------------------------------------------------------------------------- #
# 7. DPDP citations on PII-bearing slots
# --------------------------------------------------------------------------- #
def test_pii_slots_get_dpdp_data_minimisation_citation(env):
    _seed_external(env["external"])
    _make_case("disp_pii")

    result = assemble_evidence("disp_pii", now_epoch=_NOW, allow_llm=False)

    # customer_communication + shipping_proof are PII-bearing and required here.
    assert rc.CUSTOMER_COMMUNICATION in result.compliance_citations
    assert rc.SHIPPING_PROOF in result.compliance_citations
    cc = result.compliance_citations[rc.CUSTOMER_COMMUNICATION]
    reg_ids = {m["citation"]["regulation_id"] for m in cc}
    assert "dpdp_act_2023" in reg_ids
    req_ids = {m["requirement_id"] for m in cc}
    assert "dpdp_data_minimisation" in req_ids
    # the non-claim disclaimer travels with each match
    assert all(m.get("disclaimer") for m in cc)


def test_pii_citation_attached_even_when_slot_missing(env):
    _seed_external(
        env["external"], communications=0,
        availability={"has_customer_communication": 0},
    )
    _make_case("disp_pii_missing")
    result = assemble_evidence("disp_pii_missing", now_epoch=_NOW, allow_llm=False)
    assert result.slot_status[rc.CUSTOMER_COMMUNICATION] == SLOT_MISSING
    assert rc.CUSTOMER_COMMUNICATION in result.compliance_citations


# --------------------------------------------------------------------------- #
# 8. timing awareness
# --------------------------------------------------------------------------- #
def test_deadline_pressure_flag_when_close_and_incomplete(env):
    _seed_external(
        env["external"], shipment=False, availability={"has_shipping_proof": 0}
    )
    _make_case("disp_urgent", respond_by=_NOW + 12 * 3600)  # 12h out
    result = assemble_evidence("disp_urgent", now_epoch=_NOW, allow_llm=False)
    assert result.hours_to_deadline == pytest.approx(12.0)
    assert result.deadline_pressure is True


def test_no_deadline_pressure_when_complete(env):
    _seed_external(env["external"])
    _make_case("disp_urgent_ok", respond_by=_NOW + 12 * 3600)
    result = assemble_evidence("disp_urgent_ok", now_epoch=_NOW, allow_llm=False)
    assert result.assembly_status == ASSEMBLY_COMPLETE
    assert result.deadline_pressure is False


# --------------------------------------------------------------------------- #
# 9. misc contract
# --------------------------------------------------------------------------- #
def test_unknown_dispute_id_raises(env):
    with pytest.raises(DisputeNotFound):
        assemble_evidence("nope", now_epoch=_NOW)


def test_reassembly_is_idempotent_and_bumps_pass_count(env):
    _seed_external(env["external"])
    _make_case("disp_re")
    r1 = assemble_evidence("disp_re", now_epoch=_NOW, allow_llm=False)
    r2 = assemble_evidence("disp_re", now_epoch=_NOW, allow_llm=False)
    assert r1.slot_status == r2.slot_status
    assert r1.completeness == r2.completeness
    assert get_evidence_bundle("disp_re")["assembly_passes"] == 2


# --------------------------------------------------------------------------- #
# 10. corpus-level: genuine-incompleteness fraction on the real synthetic data
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(
    not REAL_EXTERNAL_DB.exists() or not WEBHOOK_DIR.exists(),
    reason="run `python -m src.synthetic.build` first",
)
def test_corpus_incomplete_bundle_fraction(tmp_path_factory, capsys):
    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    from src.ingestion.api import router as ingestion_router

    tmp = tmp_path_factory.mktemp("evidence_corpus")
    system_db = tmp / "system.db"
    saved = (db.EXTERNAL_DB_PATH, db.EXTERNAL_DB_URL, db.SYSTEM_DB_PATH, db.SYSTEM_DB_URL)
    db.EXTERNAL_DB_PATH = REAL_EXTERNAL_DB.resolve()
    db.EXTERNAL_DB_URL = f"sqlite:///file:{db.EXTERNAL_DB_PATH.as_posix()}?mode=ro&uri=true"
    db.SYSTEM_DB_PATH = system_db.resolve()
    db.SYSTEM_DB_URL = f"sqlite:///{db.SYSTEM_DB_PATH.as_posix()}"
    db.reset_engines_for_tests()
    try:
        ingestion_init()
        evidence_init()
        app = FastAPI()
        app.include_router(ingestion_router)
        client = TestClient(app)
        dispute_ids: set[str] = set()
        for wh in sorted(WEBHOOK_DIR.glob("*.json")):
            payload = json.loads(wh.read_text())
            resp = client.post("/webhook/dispute-created", json=payload)
            assert resp.status_code == 200
            dispute_ids.add(resp.json()["dispute_id"])

        statuses: dict[str, int] = {}
        manual = 0
        incomplete = 0
        resolved = 0
        any_missing = 0
        for did in sorted(dispute_ids):
            r = assemble_evidence(did, now_epoch=int(time.time()), allow_llm=False)
            statuses[r.assembly_status] = statuses.get(r.assembly_status, 0) + 1
            if r.needs_manual_classification:
                manual += 1
                continue
            resolved += 1
            if r.assembly_status != ASSEMBLY_COMPLETE:
                incomplete += 1
            if r.missing_count > 0:
                any_missing += 1

        frac = incomplete / resolved if resolved else 0.0
        with capsys.disabled():
            print(
                f"\n[corpus] disputes={len(dispute_ids)} resolved={resolved} "
                f"manual={manual} incomplete={incomplete} "
                f"incomplete_fraction={frac:.3f} statuses={statuses}"
            )
        # deliberate data design: ~30% partial + ~12% severe -> expect a real,
        # non-trivial minority of genuinely incomplete bundles, honestly reported.
        assert 0.15 <= frac <= 0.75
        assert any_missing > 0  # `missing` is actually being reported
    finally:
        (db.EXTERNAL_DB_PATH, db.EXTERNAL_DB_URL, db.SYSTEM_DB_PATH, db.SYSTEM_DB_URL) = saved
        db.reset_engines_for_tests()
