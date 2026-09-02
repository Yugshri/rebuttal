"""Tests for dispute-ingestion-router: classification, idempotency, phase transitions.

Cross-module integration (that evidence-assembler reads the classification table,
that the scorer consumes phase history) is qa-evaluator's job, not this file's.
"""

from __future__ import annotations

import copy
import glob
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.common import reason_codes as rc
from src.ingestion import (
    classification_table,
    classify_reason_code,
    get_dispute_case,
    get_phase_history,
    ingest_dispute_event,
    init_system_tables,
    was_reopened,
)

WEBHOOK_DIR = Path(__file__).resolve().parents[1] / "data" / "webhooks"


@pytest.fixture()
def ingest_db(isolated_dbs):
    """Isolated system store with this module's tables created."""
    init_system_tables()
    return isolated_dbs


def _load(name: str) -> dict:
    return json.loads((WEBHOOK_DIR / name).read_text())


def _all_webhooks() -> list[Path]:
    return sorted(WEBHOOK_DIR.glob("*.json"))


# --------------------------------------------------------------------------- #
# classification
# --------------------------------------------------------------------------- #
def test_classification_table_is_a_view_over_shared_source_of_truth():
    rows = classification_table()
    assert {r.key for r in rows} == set(rc.REASON_CODE_TO_CATEGORY)
    for r in rows:
        assert r.category == rc.REASON_CODE_TO_CATEGORY[r.key]
        assert r.evidence_types == tuple(rc.CATEGORY_EVIDENCE_TYPES.get(r.category, ()))
        assert r.required_slots == tuple(rc.CATEGORY_REQUIRED_SLOTS.get(r.category, ()))


@pytest.mark.parametrize(
    ("network", "code", "expected"),
    [
        ("amex", "F24", rc.FRAUD),
        ("visa", "11.2", rc.AUTHORIZATION),
        ("visa", "12.2", rc.PROCESSING_ERROR),
        ("mastercard", "4853", rc.CONSUMER_DISPUTE),
    ],
)
def test_one_real_code_per_category(network, code, expected):
    result = classify_reason_code(network, code)
    assert result.category == expected
    assert result.needs_manual_classification is False
    assert result.required_slots  # non-empty mapping exposed for evidence-assembler


@pytest.mark.parametrize(
    ("network", "code"),
    [("mastercard", "4846"), ("amex", "C14"), ("rupay", "121"), ("visa", "10.1"), ("discover", "4752")],
)
def test_unrecognised_real_code_routes_to_manual_bucket_not_a_guess(network, code):
    result = classify_reason_code(network, code)
    assert result.category == rc.NEEDS_MANUAL_CLASSIFICATION
    assert result.needs_manual_classification is True
    assert "manual classification" in result.reason


def test_missing_network_is_manual_not_crash():
    result = classify_reason_code(None, "4853")
    assert result.needs_manual_classification is True


def test_classifier_is_case_and_whitespace_tolerant():
    assert classify_reason_code(" AMEX ", " F24 ").category == rc.FRAUD


# --------------------------------------------------------------------------- #
# ingestion + idempotency
# --------------------------------------------------------------------------- #
def test_ingest_creates_one_row(ingest_db):
    payload = _load("disp_0001__00__fraud.json")
    out = ingest_dispute_event(payload)
    assert out.outcome == "created"
    case = get_dispute_case("disp_0001")
    assert case["category"] == rc.FRAUD
    assert case["phase"] == "fraud"
    assert case["status"] == "open"
    assert case["amount"] == 119364
    # downstream-owned fields untouched
    assert case["assembled_evidence"] is None
    assert case["confidence_score"] is None
    assert case["recommended_action"] is None
    assert case["reviewed_by"] is None


def test_replaying_same_payload_is_idempotent(ingest_db):
    payload = _load("disp_0001__00__fraud.json")
    ingest_dispute_event(payload)
    out2 = ingest_dispute_event(copy.deepcopy(payload))
    assert out2.outcome == "redelivery_noop"
    assert len(get_phase_history("disp_0001")) == 1
    assert get_dispute_case("disp_0001")["event_count"] == 1


def test_redelivery_with_new_event_id_same_state_still_noop(ingest_db):
    payload = _load("disp_0001__00__fraud.json")
    ingest_dispute_event(payload)
    dup = copy.deepcopy(payload)
    dup["event_id"] = "evt_replay_999"
    out = ingest_dispute_event(dup)
    assert out.outcome == "redelivery_noop"
    assert len(get_phase_history("disp_0001")) == 1


def test_every_webhook_ingests_without_error_and_dedupes_to_129(ingest_db):
    for path in _all_webhooks():
        ingest_dispute_event(json.loads(path.read_text()))
    # replay the whole corpus – still 129 distinct disputes, no dup rows
    for path in _all_webhooks():
        ingest_dispute_event(json.loads(path.read_text()))

    from src.common.db import system_session
    from src.ingestion.models import DisputeCase

    with system_session() as s:
        assert s.query(DisputeCase).count() == 129


def test_category_distribution_across_corpus(ingest_db):
    for path in _all_webhooks():
        ingest_dispute_event(json.loads(path.read_text()))

    from src.common.db import system_session
    from src.ingestion.models import DisputeCase

    counts: dict[str, int] = {}
    with system_session() as s:
        for (cat,) in s.query(DisputeCase.category).all():
            counts[cat] = counts.get(cat, 0) + 1

    assert counts == {
        "fraud": 34,
        "authorization": 24,
        "processing_error": 20,
        "consumer_dispute": 40,
        "needs_manual_classification": 11,
    }


# --------------------------------------------------------------------------- #
# phase transitions
# --------------------------------------------------------------------------- #
def test_open_fraud_to_won_fraud_to_pre_arbitration_is_one_evolving_case(ingest_db):
    base = _load("disp_0001__00__fraud.json")

    # event 0: open / fraud
    ingest_dispute_event(base)

    # event 1: same phase, status now won
    e1 = copy.deepcopy(base)
    e1["event_id"] = "evt_won"
    e1["payload"]["dispute"]["entity"]["status"] = "won"
    e1["payload"]["dispute"]["entity"]["created_at"] += 1000
    out1 = ingest_dispute_event(e1)
    assert out1.outcome == "status_change"

    # event 2: reopened at pre_arbitration, under_review
    e2 = copy.deepcopy(base)
    e2["event_id"] = "evt_reopen"
    ent = e2["payload"]["dispute"]["entity"]
    ent["status"] = "under_review"
    ent["phase"] = "pre_arbitration"
    ent["created_at"] += 2000
    out2 = ingest_dispute_event(e2)
    assert out2.outcome == "phase_advance"
    assert out2.is_reopen is True

    case = get_dispute_case("disp_0001")
    assert case["phase"] == "pre_arbitration"
    assert case["status"] == "under_review"
    assert case["reopen_count"] == 1

    history = get_phase_history("disp_0001")
    assert [h.phase for h in history] == ["fraud", "fraud", "pre_arbitration"]
    assert [h.status for h in history] == ["open", "won", "under_review"]
    assert history[-1].is_reopen is True
    assert was_reopened("disp_0001") is True


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("disp_0049__00__pre_arbitration.json", "disp_0049__01__arbitration.json"),
        ("disp_0091__00__chargeback.json", "disp_0091__01__pre_arbitration.json"),
        ("disp_0205__00__chargeback.json", "disp_0205__01__pre_arbitration.json"),
    ],
)
def test_real_reopen_chains_are_queryable(ingest_db, first, second):
    ingest_dispute_event(_load(first))
    dispute_id = _load(first)["payload"]["dispute"]["entity"]["id"]
    assert was_reopened(dispute_id) is False  # won, but not yet reopened

    out = ingest_dispute_event(_load(second))
    assert out.outcome == "phase_advance"
    assert out.is_reopen is True
    assert was_reopened(dispute_id) is True

    history = get_phase_history(dispute_id)
    assert len(history) == 2
    assert history[0].phase_rank < history[1].phase_rank
    assert history[1].prev_status == "won"


def test_out_of_order_event_does_not_roll_the_case_back(ingest_db):
    first = _load("disp_0049__00__pre_arbitration.json")
    second = _load("disp_0049__01__arbitration.json")
    ingest_dispute_event(first)
    ingest_dispute_event(second)  # now at arbitration

    # the pre_arbitration event arrives again, late (older timestamp, earlier phase)
    stale = copy.deepcopy(first)
    stale["event_id"] = "evt_stale_dupe"
    out = ingest_dispute_event(stale)
    assert out.outcome == "out_of_order"
    assert out.out_of_order is True

    case = get_dispute_case("disp_0049")
    assert case["phase"] == "arbitration"  # not rolled back
    assert case["status"] == "under_review"

    history = get_phase_history("disp_0049")
    assert history[-1].transition_type == "out_of_order"
    assert history[-1].out_of_order is True


def test_bad_payload_raises_webhook_error(ingest_db):
    from src.ingestion import WebhookPayloadError

    with pytest.raises(WebhookPayloadError):
        ingest_dispute_event({"nonsense": True})


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
def test_api_end_to_end(ingest_db):
    from src.main import app

    client = TestClient(app)

    r = client.post("/webhook/dispute-created", json=_load("disp_0049__00__pre_arbitration.json"))
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "created"

    r = client.post("/webhook/dispute-created", json=_load("disp_0049__01__arbitration.json"))
    assert r.json()["outcome"] == "phase_advance"
    assert r.json()["is_reopen"] is True

    r = client.get("/disputes/disp_0049/phase-history")
    assert r.status_code == 200
    body = r.json()
    assert body["was_reopened"] is True
    assert len(body["history"]) == 2

    r = client.get("/classification/table")
    assert len(r.json()["rows"]) == len(rc.REASON_CODE_TO_CATEGORY)

    r = client.get("/classification/mastercard/4846")
    assert r.json()["needs_manual_classification"] is True

    r = client.post("/webhook/dispute-created", json={"nope": 1})
    assert r.status_code == 422
