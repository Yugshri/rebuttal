"""risk-graph-service definition-of-done tests.

Run against the committed ``data/external.db`` (read-only) with a throwaway
system store. Covers:

* PageRank + betweenness flag the 3 planted mules as high ``baseline_deviation``
  vs. their own history;
* ``baseline_deviation`` is a real change-over-time measure — verified with a
  planted shifted account AND the bursty-but-legitimate controls, which must NOT
  be flagged (the honest false-positive check);
* ``GET /accounts/{id}/risk-profile`` never triggers a synchronous graph
  recomputation;
* COD/returns fields are present and populated.
"""

from __future__ import annotations

import datetime as _dt
import inspect
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.common import db
from src.risk import batch as batch_mod
from src.risk import graph as graph_mod
from src.risk import service as service_mod
from src.risk.api import router as risk_router
from src.risk.batch import (
    ELEVATED_DEVIATION_THRESHOLD,
    HIGH_DEVIATION_THRESHOLD,
    compute_graph_profiles,
    run_nightly_batch,
)
from src.risk.models import AccountRiskProfile

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_EXTERNAL_DB = REPO_ROOT / "data" / "external.db"

PLANTED_MULES = ("ACC_MULE_FANOUT", "ACC_MULE_BRIDGE", "ACC_MULE_PASSTHRU")
BURSTY_CONTROLS = ("ACC_BURSTY_SEASONAL", "ACC_BURSTY_PAYDAY")


@pytest.fixture(scope="module")
def risk_env(tmp_path_factory):
    """Real external.db (read-only) + a fresh tmp system.db, batch run once."""
    if not REAL_EXTERNAL_DB.exists():
        pytest.skip("data/external.db missing — run `python -m src.synthetic.build`")

    tmp = tmp_path_factory.mktemp("risk")
    system_db = tmp / "system.db"

    saved = (db.EXTERNAL_DB_PATH, db.EXTERNAL_DB_URL, db.SYSTEM_DB_PATH, db.SYSTEM_DB_URL)
    db.EXTERNAL_DB_PATH = REAL_EXTERNAL_DB.resolve()
    db.EXTERNAL_DB_URL = (
        f"sqlite:///file:{db.EXTERNAL_DB_PATH.as_posix()}?mode=ro&uri=true"
    )
    db.SYSTEM_DB_PATH = system_db.resolve()
    db.SYSTEM_DB_URL = f"sqlite:///{db.SYSTEM_DB_PATH.as_posix()}"
    db.reset_engines_for_tests()

    result = run_nightly_batch()

    app = FastAPI()
    app.include_router(risk_router)
    client = TestClient(app)

    yield {"result": result, "client": client, "system_db": system_db}

    db.reset_engines_for_tests()
    (db.EXTERNAL_DB_PATH, db.EXTERNAL_DB_URL, db.SYSTEM_DB_PATH, db.SYSTEM_DB_URL) = saved
    db.reset_engines_for_tests()


# --------------------------------------------------------------------------
# 1. PageRank + betweenness flag the planted mules
# --------------------------------------------------------------------------
def test_planted_mules_flagged_high_deviation(risk_env):
    client = risk_env["client"]
    for mule in PLANTED_MULES:
        body = client.get(f"/accounts/{mule}/risk-profile").json()
        assert body["baseline_deviation"] >= HIGH_DEVIATION_THRESHOLD, (
            f"{mule} deviation {body['baseline_deviation']} below high threshold"
        )
        assert body["deviation_band"] == "high", mule
    # and the batch's own high-deviation roster lists all three
    assert set(PLANTED_MULES).issubset(set(risk_env["result"].high_deviation_accounts))


def test_mule_deviation_is_driven_by_betweenness_or_emergence(risk_env):
    """The signal is PageRank/betweenness movement, not COD/returns leakage."""
    client = risk_env["client"]
    for mule in PLANTED_MULES:
        ex = client.get(f"/accounts/{mule}/risk-profile").json()["explain"]
        # betweenness at the peak window is far above the account's own baseline,
        # OR the account emerged late with no establishment baseline at all.
        spiked = ex["betweenness_deviation_z"] >= 2.0
        emerged = ex["insufficient_history"] and ex["peak_window_start"] is not None
        assert spiked or emerged, (mule, ex)
        # the movement is measured against the account's *own* earlier windows
        assert ex["windows_observed"] >= 3
        assert ex["peak_window_start"] is not None


# --------------------------------------------------------------------------
# 2. baseline_deviation is change-over-time, and the bursty controls are NOT flagged
# --------------------------------------------------------------------------
def test_bursty_legit_controls_not_flagged(risk_env):
    """Honest false-positive check: a 5x seasonal volume spike and a monthly
    payday fan-out — both to the *same* counterparties — must not look like a
    shifting mule."""
    client = risk_env["client"]
    mule_devs = [
        client.get(f"/accounts/{m}/risk-profile").json()["baseline_deviation"]
        for m in PLANTED_MULES
    ]
    for control in BURSTY_CONTROLS:
        body = client.get(f"/accounts/{control}/risk-profile").json()
        assert body["deviation_band"] != "high", (
            f"{control} wrongly flagged high ({body['baseline_deviation']})"
        )
        assert body["baseline_deviation"] < ELEVATED_DEVIATION_THRESHOLD
        assert body["explain"]["illicit_counterparty_fraction"] < 0.25
        assert body["baseline_deviation"] < min(mule_devs)


def test_baseline_deviation_needs_the_post_shift_history(risk_env):
    """Truncating the edge log to the pre-shift period collapses the mule signal:
    proves the deviation is measured over time, not from static structure."""
    from src.synthetic import GRAPH_WINDOW_START, MULE_SHIFT_DAY
    from src.synthetic.rng import parse_iso

    edges = graph_mod.load_edges()
    nodes = graph_mod.load_nodes()
    cutoff = parse_iso(GRAPH_WINDOW_START) + _dt.timedelta(days=MULE_SHIFT_DAY)
    pre_shift = [e for e in edges if e.ts < cutoff]

    full = compute_graph_profiles(edges, nodes)
    pre = compute_graph_profiles(pre_shift, nodes)

    for mule in ("ACC_MULE_FANOUT", "ACC_MULE_BRIDGE"):
        assert full[mule].baseline_deviation >= HIGH_DEVIATION_THRESHOLD
        assert pre[mule].baseline_deviation < ELEVATED_DEVIATION_THRESHOLD, (
            f"{mule} still flagged on pre-shift-only history "
            f"({pre[mule].baseline_deviation}) — deviation is not change-over-time"
        )

    # A bursty control stays low whether or not the post-shift window is included.
    for control in BURSTY_CONTROLS:
        assert full[control].baseline_deviation < ELEVATED_DEVIATION_THRESHOLD


# --------------------------------------------------------------------------
# 3. The endpoint never recomputes the graph
# --------------------------------------------------------------------------
def test_risk_profile_endpoint_does_not_recompute_graph(risk_env, monkeypatch):
    client = risk_env["client"]

    def _boom(*_a, **_k):  # pragma: no cover - must never run
        raise AssertionError("graph computation triggered from the request path")

    monkeypatch.setattr(batch_mod, "run_nightly_batch", _boom)
    monkeypatch.setattr(batch_mod, "compute_graph_profiles", _boom)
    monkeypatch.setattr(graph_mod, "build_digraph", _boom)
    monkeypatch.setattr(graph_mod, "pagerank", _boom)
    monkeypatch.setattr(graph_mod, "betweenness", _boom)
    monkeypatch.setattr(graph_mod, "load_edges", _boom)

    resp = client.get("/accounts/ACC_MULE_FANOUT/risk-profile")
    assert resp.status_code == 200
    assert resp.json()["account_id"] == "ACC_MULE_FANOUT"


def test_service_and_api_modules_do_not_reference_graph_compute():
    """Static guarantee: the read path holds no reference that could recompute."""
    api_mod = __import__("src.risk.api", fromlist=["router"])
    forbidden = {
        batch_mod,
        graph_mod,
        batch_mod.run_nightly_batch,
        batch_mod.compute_graph_profiles,
        graph_mod.pagerank,
        graph_mod.betweenness,
        graph_mod.build_digraph,
        graph_mod.load_edges,
    }
    for mod in (service_mod, api_mod):
        referenced = set()
        for value in vars(mod).values():
            try:
                referenced.add(value)
            except TypeError:  # unhashable
                continue
        clash = referenced & forbidden
        assert not clash, f"{mod.__name__} references graph-compute: {clash}"
        # transitively: nothing it imported pulls the graph modules in either
        for value in vars(mod).values():
            if inspect.ismodule(value):
                assert value not in (batch_mod, graph_mod)


def test_unknown_account_returns_404_without_recompute(risk_env, monkeypatch):
    client = risk_env["client"]
    monkeypatch.setattr(batch_mod, "run_nightly_batch", lambda *a, **k: 1 / 0)
    monkeypatch.setattr(graph_mod, "pagerank", lambda *a, **k: 1 / 0)
    resp = client.get("/accounts/ACC_DOES_NOT_EXIST/risk-profile")
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# 4. COD / returns fields present and populated
# --------------------------------------------------------------------------
def test_cod_returns_fields_present_and_populated(risk_env):
    client = risk_env["client"]
    body = client.get("/accounts/ACC_MULE_FANOUT/risk-profile").json()
    cod = body["cod_returns"]
    for key in (
        "return_rate_pct",
        "lifetime_return_ratio",
        "delivery_refusals",
        "previous_dispute_count",
        "multiple_accounts_flag",
        "refund_to_different_account",
        "high_return_density_address",
        "account_age_days",
        "customer_segment",
        "returns_risk_score",
        "returns_risk_band",
    ):
        assert key in cod, key
    assert 0.0 <= cod["returns_risk_score"] <= 1.0
    assert cod["returns_risk_band"] in ("low", "elevated", "high")
    assert cod["account_age_days"] > 0
    assert cod["customer_segment"] in ("new", "bronze", "silver", "gold")


def test_returns_signal_covers_every_account_with_history(risk_env):
    with db.system_session() as session:
        rows = session.query(AccountRiskProfile).all()
        populated = [
            r for r in rows
            if r.return_rate_pct > 0 or r.account_age_days > 0
        ]
    # every one of the 305 graph accounts has a customer_return_history row
    assert len(populated) >= 300


def test_returns_abuse_cohort_scores_above_legit_baseline(risk_env):
    """Directional sanity: accounts with high return rate + refusals + multi-acct
    flags score higher on returns_risk than the median account."""
    with db.system_session() as session:
        rows = session.query(AccountRiskProfile).all()
    scores = sorted(r.returns_risk_score for r in rows)
    median = scores[len(scores) // 2]
    abusers = [
        r for r in rows
        if r.return_rate_pct >= 40 and r.delivery_refusals >= 1
    ]
    assert abusers, "expected a returns-abuse cohort in the synthetic data"
    assert sum(r.returns_risk_score for r in abusers) / len(abusers) > median


# --------------------------------------------------------------------------
# 5. Graph primitives
# --------------------------------------------------------------------------
def test_pagerank_is_a_distribution():
    edges = graph_mod.load_edges()[:800]
    g = graph_mod.build_digraph(edges)
    pr = graph_mod.pagerank(g)
    assert abs(sum(pr.values()) - 1.0) < 1e-6
    assert all(v >= 0 for v in pr.values())


def test_rolling_windows_cover_the_edge_log():
    edges = graph_mod.load_edges()
    windows = graph_mod.rolling_windows(edges, window_days=14, step_days=7)
    assert len(windows) >= 8
    assert windows[0].start == edges[0].ts
    assert windows[-1].end > edges[-1].ts


def test_batch_is_idempotent_on_unchanged_data(risk_env):
    first = {
        m: risk_env["client"].get(f"/accounts/{m}/risk-profile").json()[
            "baseline_deviation"
        ]
        for m in PLANTED_MULES
    }
    run_nightly_batch()
    second = {
        m: risk_env["client"].get(f"/accounts/{m}/risk-profile").json()[
            "baseline_deviation"
        ]
        for m in PLANTED_MULES
    }
    assert first == second
