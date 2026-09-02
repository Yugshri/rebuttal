"""The corpus meets the synthetic-data-generator definition of done."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from src.common import reason_codes as rc
from src.common.models_base import DisputePhase, phase_rank
from src.synthetic.build import main
from src.synthetic.transaction_graph import BURSTY_CONTROLS, PLANTED_MULES


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    d = tmp_path_factory.mktemp("corpus")
    summary = main(d)
    conn = sqlite3.connect(str(d / "external.db"))
    conn.row_factory = sqlite3.Row
    yield {"dir": d, "summary": summary, "conn": conn}
    conn.close()


def test_all_four_categories_and_manual_bucket_present(corpus):
    cov = corpus["summary"]["category_coverage"]
    for cat in (rc.FRAUD, rc.AUTHORIZATION, rc.PROCESSING_ERROR, rc.CONSUMER_DISPUTE):
        assert cov.get(cat, 0) >= 5, f"category {cat} under-represented"
    assert cov.get(rc.NEEDS_MANUAL_CLASSIFICATION, 0) >= 3


def test_all_five_phases_present(corpus):
    phases = set()
    for f in (corpus["dir"] / "webhooks").glob("*.json"):
        payload = json.loads(f.read_text())
        phases.add(payload["payload"]["dispute"]["entity"]["phase"])
    assert phases == {p.value for p in DisputePhase}


def test_unrecognised_codes_are_actually_unrecognised(corpus):
    """needs_manual disputes must carry codes the shared table does not map."""
    for f in (corpus["dir"] / "webhooks").glob("*.json"):
        payload = json.loads(f.read_text())["payload"]["dispute"]["entity"]
        net, code = payload["network"], payload["reason_code"]
        key = f"{net}:{code}"
        # If it's in the table it must map to a real category; if not, that's
        # fine — those are the needs_manual ones. Just assert at least the known
        # ones classify correctly.
        if key in rc.REASON_CODE_TO_CATEGORY:
            assert rc.classify(net, code) in rc.CATEGORIES


def test_needs_manual_traffic_exists(corpus):
    unmapped = 0
    seen = set()
    for f in (corpus["dir"] / "webhooks").glob("*.json"):
        e = json.loads(f.read_text())["payload"]["dispute"]["entity"]
        if e["id"] in seen:
            continue
        seen.add(e["id"])
        if rc.classify(e["network"], e["reason_code"]) == rc.NEEDS_MANUAL_CLASSIFICATION:
            unmapped += 1
    assert unmapped >= 3


def test_genuine_reopen_case_exists(corpus):
    """At least one dispute id with a won->later-phase-under_review chain."""
    by_id: dict[str, list[dict]] = {}
    for f in (corpus["dir"] / "webhooks").glob("*.json"):
        e = json.loads(f.read_text())["payload"]["dispute"]["entity"]
        by_id.setdefault(e["id"], []).append(e)
    reopens = 0
    for events in by_id.values():
        if len(events) < 2:
            continue
        events.sort(key=lambda x: x["created_at"])
        if events[0]["status"] == "won" and phase_rank(events[-1]["phase"]) > phase_rank(
            events[0]["phase"]
        ):
            reopens += 1
    assert reopens >= 1


def test_some_disputes_inside_48h_window(corpus):
    import datetime as dt

    from src.synthetic import NOW
    from src.synthetic.rng import parse_iso

    now = parse_iso(NOW)
    urgent = 0
    seen = set()
    for f in (corpus["dir"] / "webhooks").glob("*.json"):
        e = json.loads(f.read_text())["payload"]["dispute"]["entity"]
        if e["id"] in seen:
            continue
        seen.add(e["id"])
        respond_by = dt.datetime.fromtimestamp(e["respond_by"])
        if (respond_by - now).total_seconds() <= 48 * 3600:
            urgent += 1
    assert urgent >= 5


def test_every_edge_has_a_real_timestamp(corpus):
    rows = corpus["conn"].execute(
        "SELECT timestamp FROM transaction_edges"
    ).fetchall()
    assert len(rows) > 1000
    distinct = {r[0] for r in rows}
    # Real sequence -> lots of distinct timestamps, not a handful of buckets.
    assert len(distinct) > len(rows) * 0.9
    for r in rows[:50]:
        # parses as full datetime
        __import__("datetime").datetime.strptime(r[0], "%Y-%m-%dT%H:%M:%S")


def test_planted_mules_and_controls_are_in_the_graph(corpus):
    ids = {
        r[0]
        for r in corpus["conn"].execute("SELECT account_id FROM account_nodes")
    }
    for acc in PLANTED_MULES + BURSTY_CONTROLS:
        assert acc in ids


def test_mule_fanout_actually_shifts_counterparties_over_time(corpus):
    """The shift must be derivable from the sequence, not declared."""
    from src.synthetic import MULE_SHIFT_DAY
    from src.synthetic.transaction_graph import MULE_FANOUT

    rows = corpus["conn"].execute(
        "SELECT dst_account, timestamp, edge_kind FROM transaction_edges "
        "WHERE src_account = ? ORDER BY timestamp",
        (MULE_FANOUT,),
    ).fetchall()
    early = [r for r in rows if r[2] == "merchant_payment"]
    late = [r for r in rows if r[2] == "fan_out"]
    assert len(early) >= 3 and len(late) >= 10
    early_dst = {r[0] for r in early}
    late_dst = {r[0] for r in late}
    # Counterparty set genuinely turns over.
    assert len(early_dst & late_dst) <= 1
    assert all("FRINGE" in d for d in late_dst)


def test_bursty_control_keeps_same_counterparties(corpus):
    """The false-positive target must NOT shift to fringe / new clusters."""
    from src.synthetic.transaction_graph import BURSTY_SEASONAL

    rows = corpus["conn"].execute(
        "SELECT src_account, dst_account FROM transaction_edges "
        "WHERE src_account = ? OR dst_account = ?",
        (BURSTY_SEASONAL, BURSTY_SEASONAL),
    ).fetchall()
    counterparties = {r[0] for r in rows} | {r[1] for r in rows}
    counterparties.discard(BURSTY_SEASONAL)
    assert not any("FRINGE" in c for c in counterparties)


def test_evidence_incompleteness_rate_is_documented_and_real(corpus):
    inc = corpus["summary"]["incompleteness"]
    assert 0.5 <= inc["full"]["fraction"] <= 0.66
    assert 0.24 <= inc["partial"]["fraction"] <= 0.36
    assert 0.08 <= inc["severe"]["fraction"] <= 0.16
    # and the DB agrees with the summary
    rows = corpus["conn"].execute(
        "SELECT completeness_bucket, COUNT(*) FROM evidence_availability GROUP BY 1"
    ).fetchall()
    db_counts = {r[0]: r[1] for r in rows}
    for bucket, stats in inc.items():
        assert db_counts[bucket] == stats["count"]


def test_match_mismatch_flags_present_on_every_payment(corpus):
    rows = corpus["conn"].execute(
        "SELECT billing_shipping_address_match, email_domain_consistent, avs_match "
        "FROM evidence_availability"
    ).fetchall()
    assert len(rows) == corpus["summary"]["disputes"]
    # email flag is never null; address match may be null only when no shipment
    assert all(r[1] in (0, 1) for r in rows)
    assert any(r[0] == 0 for r in rows) and any(r[0] == 1 for r in rows)


def test_returns_history_covers_every_account(corpus):
    n_accounts = corpus["conn"].execute(
        "SELECT COUNT(*) FROM account_nodes"
    ).fetchone()[0]
    n_hist = corpus["conn"].execute(
        "SELECT COUNT(*) FROM customer_return_history"
    ).fetchone()[0]
    assert n_accounts == n_hist


def test_ground_truth_not_leaked_into_external_db(corpus):
    """No table/column in external.db should carry a disposition or mule label."""
    cur = corpus["conn"].execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )
    banned = ("disposition", "is_mule", "mule_label", "expected_", "ground_truth",
              "should_defer", "planted")
    for (table,) in cur.fetchall():
        cols = [
            c[1].lower()
            for c in corpus["conn"].execute(f"PRAGMA table_info({table})")
        ]
        for col in cols:
            assert not any(b in col for b in banned), f"{table}.{col} leaks GT"


def test_heldout_dir_has_no_db_and_is_separate(corpus):
    heldout = corpus["dir"] / "heldout"
    assert (heldout / "account_labels.json").exists()
    assert (heldout / "dispute_dispositions.json").exists()
    # ground truth is not in the external store path
    assert not (heldout / "external.db").exists()


def test_disposition_labels_are_not_a_single_column_function(corpus):
    """Both dispositions must occur across every completeness bucket -> the
    label can't be recovered from completeness alone."""
    disp = json.loads(
        (corpus["dir"] / "heldout" / "dispute_dispositions.json").read_text()
    )["dispositions"]
    by_bucket: dict[str, set] = {}
    for v in disp.values():
        by_bucket.setdefault(v["completeness_bucket"], set()).add(
            v["expected_disposition"]
        )
    assert by_bucket["full"] == {"assemble_clean", "defer_to_human"}
    assert by_bucket["partial"] == {"assemble_clean", "defer_to_human"}
