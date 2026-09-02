"""The nightly graph-risk batch job.

Why this is a batch, not a request-path computation (architecture decision, not a
detail): PageRank and betweenness are whole-graph algorithms over every account
and every edge. That is a fundamentally different computational shape from a
single ``GET /accounts/{id}/risk-profile`` call. So the graph work runs here, on
a schedule, and writes a flat ``AccountRiskProfile`` row per account; the API
only ever reads the latest row. ``run_nightly_batch()`` is a plain callable with
no scheduler bound to it — a cron/APScheduler/Airflow trigger *could* call it,
but nothing in this repo starts a daemon.

Detection technique (see the module spec): ``baseline_deviation`` is a real
rolling comparison of an account's *current* graph position against *its own*
earlier history — never a cross-account percentile at a single instant. Concretely:

* Slice the timestamped edge log into overlapping 14-day windows (7-day step).
* Per window, compute weighted PageRank and (undirected) betweenness centrality.
* For each account, take its first ~40% of active windows as an "establishment
  baseline", then score every later window for how far its centrality has moved
  from that baseline (robust/MAD z-score, capped), **gated by** how much of that
  movement is toward structurally risky counterparties (thin-file "fringe"
  accounts, or a different account cluster than the one the account was
  established in). A pure volume spike among the *same* counterparties barely
  moves the score; a pivot to new fringe counterparties or a new cluster bridge
  moves it a lot. That gate is what separates a planted mule from a
  legitimately-bursty merchant.
* Thin-history accounts that barely exist until late in the window and then
  surface as high-betweenness pass-through hubs are caught by a separate
  "emergence" term (they have no establishment baseline to deviate from).
* Velocity/recency features (recent-window txn count, rolling velocity,
  time-since-last-txn, first-time-counterparty rate, fan-out ratio) are computed
  alongside and stored on the same row.

COD/returns signals come from a different source entirely — ``customer_return_history``
and ``addresses`` in the external store — but land on the same profile row.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

import networkx as nx
import numpy as np

from src.common.db import system_engine, system_session
from src.common.models_base import Base
from src.risk import graph as gm
from src.risk.models import AccountRiskProfile

# --- Tunables (documented in docs/failure-taxonomy.md) ---------------------
WINDOW_DAYS = 14
STEP_DAYS = 7
BASELINE_FRACTION = 0.40           # first 40% of an account's active windows
MIN_WINDOWS_FOR_DEVIATION = 3
BETWEENNESS_Z_CAP = 15.0
PAGERANK_Z_CAP = 8.0
HIGH_DEVIATION_THRESHOLD = 8.0     # sits between the bursty controls (~2) and mules (>14)
ELEVATED_DEVIATION_THRESHOLD = 4.0
RETURNS_HIGH_THRESHOLD = 0.60
RETURNS_ELEVATED_THRESHOLD = 0.35


@dataclass
class BatchResult:
    run_id: str
    computed_through: str | None
    n_accounts: int
    n_windows: int
    high_deviation_accounts: list[str] = field(default_factory=list)
    elevated_deviation_accounts: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Graph-signal computation
# --------------------------------------------------------------------------
@dataclass
class _WindowSignals:
    pagerank: dict[str, float]
    betweenness: dict[str, float]
    counterparties: dict[str, set[str]]
    txn_count: dict[str, int]
    out_count: dict[str, int]


def _window_signals(edges: list[gm.Edge], window: gm.Window) -> _WindowSignals:
    we = gm.edges_in_window(edges, window)
    g = gm.build_digraph(we)
    pr = gm.pagerank(g)
    btw = gm.betweenness(g.to_undirected())
    cps: dict[str, set[str]] = {}
    tc: dict[str, int] = {}
    oc: dict[str, int] = {}
    for e in we:
        cps.setdefault(e.src, set()).add(e.dst)
        cps.setdefault(e.dst, set()).add(e.src)
        tc[e.src] = tc.get(e.src, 0) + 1
        tc[e.dst] = tc.get(e.dst, 0) + 1
        oc[e.src] = oc.get(e.src, 0) + 1
    return _WindowSignals(pr, btw, cps, tc, oc)


def _robust_z(value: float, history: list[float], floor: float, cap: float) -> float:
    hist = np.asarray(history, dtype=float)
    if hist.size < 2:
        return 0.0
    median = float(np.median(hist))
    mad = float(np.median(np.abs(hist - median)))
    scale = max(1.4826 * mad, 0.5 * floor, float(np.std(hist)))
    if scale <= 0:
        return 0.0
    return float(np.clip((value - median) / scale, -cap, cap))


def _risky_counterparty(home_cluster: str, cp: str, nodes: dict[str, gm.AccountNode]) -> bool:
    node = nodes.get(cp)
    if node is None:
        return False
    if node.account_type == "fringe" or node.cluster == "fringe":
        return True
    if home_cluster == "core" or node.cluster == "core":
        return False
    return node.cluster != home_cluster


def _illicit_fraction(
    home_cluster: str, counterparties: set[str], nodes: dict[str, gm.AccountNode]
) -> float:
    if not counterparties:
        return 0.0
    n = sum(
        1 for cp in counterparties if _risky_counterparty(home_cluster, cp, nodes)
    )
    return n / len(counterparties)


@dataclass
class _AccountGraphProfile:
    account_id: str
    pagerank_score: float = 0.0
    betweenness_score: float = 0.0
    baseline_deviation: float = 0.0
    pagerank_baseline_mean: float = 0.0
    betweenness_baseline_mean: float = 0.0
    pagerank_deviation_z: float = 0.0
    betweenness_deviation_z: float = 0.0
    illicit_counterparty_fraction: float = 0.0
    peak_window_start: str | None = None
    peak_window_end: str | None = None
    windows_observed: int = 0
    baseline_windows: int = 0
    insufficient_history: bool = True
    txn_count_recent_window: int = 0
    rolling_txn_velocity: float = 0.0
    days_since_last_txn: float = 0.0
    first_time_counterparty_rate: float = 0.0
    fan_out_ratio: float = 0.0


def compute_graph_profiles(
    edges: list[gm.Edge], nodes: dict[str, gm.AccountNode]
) -> dict[str, _AccountGraphProfile]:
    """Pure function: timestamped edges + node metadata -> per-account graph profile.

    No DB access. ``batch.run_nightly_batch`` calls this; the API layer must not.
    """
    profiles: dict[str, _AccountGraphProfile] = {
        acc: _AccountGraphProfile(account_id=acc) for acc in nodes
    }
    if not edges:
        return profiles

    windows = gm.rolling_windows(edges, window_days=WINDOW_DAYS, step_days=STEP_DAYS)
    if not windows:
        return profiles
    signals = [_window_signals(edges, w) for w in windows]
    midpoint = windows[len(windows) // 2].start
    last_edge_ts = edges[-1].ts

    btw_floor = float(
        np.median([v for s in signals for v in s.betweenness.values() if v > 0] or [1e-6])
    )
    pr_floor = float(
        np.median([v for s in signals for v in s.pagerank.values() if v > 0] or [1e-6])
    )

    for acc in nodes:
        home = nodes[acc].cluster
        active = [
            i for i, s in enumerate(signals) if len(s.counterparties.get(acc, ())) > 0
        ]
        prof = profiles[acc]
        prof.windows_observed = len(active)
        if not active:
            continue

        last_active = active[-1]
        prof.pagerank_score = signals[last_active].pagerank.get(acc, 0.0)
        prof.betweenness_score = signals[last_active].betweenness.get(acc, 0.0)
        prof.txn_count_recent_window = signals[last_active].txn_count.get(acc, 0)
        out_c = signals[last_active].out_count.get(acc, 0)
        prof.fan_out_ratio = out_c / max(1, prof.txn_count_recent_window)
        prof.rolling_txn_velocity = float(
            np.mean([signals[i].txn_count.get(acc, 0) for i in active])
        )
        prof.days_since_last_txn = _days_since_last_edge(edges, acc, last_edge_ts)

        # -- Emergence term: thin history then a late high-betweenness hub --
        early_active = [i for i in active if windows[i].start < midpoint]
        late_active = [i for i in active if windows[i].start >= midpoint]
        emergence = 0.0
        if len(early_active) <= 1 and late_active:
            pk = max(late_active, key=lambda i: signals[i].betweenness.get(acc, 0.0))
            pk_btw = signals[pk].betweenness.get(acc, 0.0)
            il = _illicit_fraction(home, signals[pk].counterparties[acc], nodes)
            emergence = min(pk_btw / btw_floor, 20.0) * (0.15 + 0.85 * il) + 0.3 * min(
                signals[pk].txn_count.get(acc, 0), 20
            ) * il
            if emergence > 0:
                prof.peak_window_start = gm._iso(windows[pk].start)
                prof.peak_window_end = gm._iso(windows[pk].end)
                prof.illicit_counterparty_fraction = il

        # -- Windowed deviation vs establishment baseline --
        windowed = 0.0
        if len(active) >= MIN_WINDOWS_FOR_DEVIATION:
            n_base = max(2, math.ceil(len(active) * BASELINE_FRACTION))
            base_idx = active[:n_base]
            cand_idx = active[n_base:]
            prof.baseline_windows = len(base_idx)
            if cand_idx:
                prof.insufficient_history = False
                pr_base = [signals[i].pagerank.get(acc, 0.0) for i in base_idx]
                btw_base = [signals[i].betweenness.get(acc, 0.0) for i in base_idx]
                tc_base = [signals[i].txn_count.get(acc, 0) for i in base_idx]
                base_cps: set[str] = set().union(
                    *(signals[i].counterparties[acc] for i in base_idx)
                )
                prof.pagerank_baseline_mean = float(np.mean(pr_base))
                prof.betweenness_baseline_mean = float(np.mean(btw_base))

                for w in cand_idx:
                    pr_z = _robust_z(
                        signals[w].pagerank.get(acc, 0.0), pr_base, pr_floor,
                        PAGERANK_Z_CAP,
                    )
                    btw_z = _robust_z(
                        signals[w].betweenness.get(acc, 0.0), btw_base, btw_floor,
                        BETWEENNESS_Z_CAP,
                    )
                    cps = signals[w].counterparties[acc]
                    il = _illicit_fraction(home, cps, nodes)
                    turnover = len(cps - base_cps) / max(1, len(cps))
                    ramp = signals[w].txn_count.get(acc, 0) / (
                        float(np.mean(tc_base)) + 1.0
                    )
                    score = (
                        max(0.0, btw_z) * (0.15 + 0.85 * il)
                        + abs(pr_z) * il
                        + 3.0 * turnover * il
                        + 0.6 * min(ramp, 10.0) * il
                    )
                    if score > windowed:
                        windowed = score
                        prof.pagerank_deviation_z = pr_z
                        prof.betweenness_deviation_z = btw_z
                        prof.illicit_counterparty_fraction = il
                        prof.first_time_counterparty_rate = turnover
                        prof.peak_window_start = gm._iso(windows[w].start)
                        prof.peak_window_end = gm._iso(windows[w].end)
            elif len(active) < MIN_WINDOWS_FOR_DEVIATION:
                prof.insufficient_history = True
        prof.baseline_deviation = round(max(windowed, emergence), 4)
        if emergence > windowed:
            prof.insufficient_history = len(early_active) <= 1

    return profiles


def _days_since_last_edge(
    edges: list[gm.Edge], acc: str, reference: datetime
) -> float:
    last: datetime | None = None
    for e in edges:
        if e.src == acc or e.dst == acc:
            if last is None or e.ts > last:
                last = e.ts
    if last is None:
        return -1.0
    return max(0.0, (reference - last).total_seconds() / 86400.0)


def _deviation_band(value: float) -> str:
    if value >= HIGH_DEVIATION_THRESHOLD:
        return "high"
    if value >= ELEVATED_DEVIATION_THRESHOLD:
        return "elevated"
    return "low"


# --------------------------------------------------------------------------
# COD / returns signal
# --------------------------------------------------------------------------
def compute_returns_signal(
    history: dict, high_return_density: bool
) -> tuple[float, str, dict]:
    """Composite returns-abuse signal from ``customer_return_history`` + addresses.

    Returns ``(score in 0..1, band, feature dict)``. This is an enrichment
    signal, not a routing decision — ``confidence-scorer-review`` decides what to
    do with it.
    """
    rr = history["return_rate_pct"] / 100.0
    orders = max(1, history["total_orders_lifetime"])
    lifetime_ratio = history["total_returns_lifetime"] / orders
    refusals = history["delivery_refusals"]
    prev_disputes = history["previous_dispute_count"]
    multi = history["multiple_accounts_flag"]
    refund_other = history["refund_to_different_account"]
    young = history["account_age_days"] < 120

    score = (
        0.45 * min(rr / 0.5, 1.0)
        + 0.15 * min(lifetime_ratio / 0.5, 1.0)
        + 0.12 * min(refusals / 3.0, 1.0)
        + 0.10 * min(prev_disputes / 3.0, 1.0)
        + 0.08 * (1.0 if multi else 0.0)
        + 0.06 * (1.0 if refund_other else 0.0)
        + 0.04 * (1.0 if high_return_density else 0.0)
    )
    if young and rr > 0.25:
        score = min(1.0, score + 0.05)
    score = round(min(1.0, score), 4)

    if score >= RETURNS_HIGH_THRESHOLD:
        band = "high"
    elif score >= RETURNS_ELEVATED_THRESHOLD:
        band = "elevated"
    else:
        band = "low"
    return score, band, {"lifetime_return_ratio": round(lifetime_ratio, 4)}


# --------------------------------------------------------------------------
# Orchestration + upsert
# --------------------------------------------------------------------------
def run_nightly_batch() -> BatchResult:
    """Recompute every ``AccountRiskProfile`` from the current external graph.

    Read-only against the external store (``transaction_edges``, ``account_nodes``,
    ``customer_return_history``, ``addresses``); read/write only against the
    system store's ``account_risk_profile`` table. Idempotent: a second run over
    unchanged data produces identical rows (bar ``last_updated`` / ``batch_run_id``).
    """
    run_id = uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc)

    edges = gm.load_edges()
    nodes = gm.load_nodes()
    return_history = gm.load_return_history()
    hrd_accounts = gm.load_high_return_density_accounts()

    graph_profiles = compute_graph_profiles(edges, nodes)
    computed_through = gm._iso(edges[-1].ts) if edges else None

    Base.metadata.create_all(
        bind=system_engine(), tables=[AccountRiskProfile.__table__]
    )

    high: list[str] = []
    elevated: list[str] = []
    all_accounts = set(nodes) | set(return_history)

    with system_session() as session:
        existing = {
            row.account_id: row
            for row in session.query(AccountRiskProfile).all()
        }
        for acc in sorted(all_accounts):
            gp = graph_profiles.get(acc, _AccountGraphProfile(account_id=acc))
            hist = return_history.get(acc)
            if hist is not None:
                r_score, r_band, r_extra = compute_returns_signal(
                    hist, acc in hrd_accounts
                )
            else:
                r_score, r_band, r_extra = 0.0, "low", {"lifetime_return_ratio": 0.0}

            band = _deviation_band(gp.baseline_deviation)
            if band == "high":
                high.append(acc)
            elif band == "elevated":
                elevated.append(acc)

            row = existing.get(acc)
            if row is None:
                row = AccountRiskProfile(account_id=acc)
                session.add(row)

            row.pagerank_score = gp.pagerank_score
            row.betweenness_score = gp.betweenness_score
            row.baseline_deviation = gp.baseline_deviation
            row.deviation_band = band
            row.last_updated = now
            row.pagerank_baseline_mean = gp.pagerank_baseline_mean
            row.betweenness_baseline_mean = gp.betweenness_baseline_mean
            row.pagerank_deviation_z = gp.pagerank_deviation_z
            row.betweenness_deviation_z = gp.betweenness_deviation_z
            row.illicit_counterparty_fraction = gp.illicit_counterparty_fraction
            row.peak_window_start = gp.peak_window_start
            row.peak_window_end = gp.peak_window_end
            row.windows_observed = gp.windows_observed
            row.baseline_windows = gp.baseline_windows
            row.insufficient_history = int(gp.insufficient_history)
            row.txn_count_recent_window = gp.txn_count_recent_window
            row.rolling_txn_velocity = gp.rolling_txn_velocity
            row.days_since_last_txn = gp.days_since_last_txn
            row.first_time_counterparty_rate = gp.first_time_counterparty_rate
            row.fan_out_ratio = gp.fan_out_ratio

            if hist is not None:
                row.return_rate_pct = hist["return_rate_pct"]
                row.lifetime_return_ratio = r_extra["lifetime_return_ratio"]
                row.delivery_refusals = hist["delivery_refusals"]
                row.previous_dispute_count = hist["previous_dispute_count"]
                row.multiple_accounts_flag = int(hist["multiple_accounts_flag"])
                row.refund_to_different_account = int(
                    hist["refund_to_different_account"]
                )
                row.account_age_days = hist["account_age_days"]
                row.customer_segment = hist["segment"]
            row.high_return_density_address = int(acc in hrd_accounts)
            row.returns_risk_score = r_score
            row.returns_risk_band = r_band
            row.computed_through = computed_through
            row.batch_run_id = run_id

    return BatchResult(
        run_id=run_id,
        computed_through=computed_through,
        n_accounts=len(all_accounts),
        n_windows=len(gm.rolling_windows(edges, window_days=WINDOW_DAYS, step_days=STEP_DAYS)),
        high_deviation_accounts=sorted(high),
        elevated_deviation_accounts=sorted(elevated),
    )


__all__ = [
    "run_nightly_batch",
    "compute_graph_profiles",
    "compute_returns_signal",
    "BatchResult",
    "WINDOW_DAYS",
    "STEP_DAYS",
    "HIGH_DEVIATION_THRESHOLD",
]


if __name__ == "__main__":  # `python -m src.risk.batch` — the schedulable entry
    import json

    r = run_nightly_batch()
    print(
        json.dumps(
            {
                "run_id": r.run_id,
                "computed_through": r.computed_through,
                "n_accounts": r.n_accounts,
                "n_windows": r.n_windows,
                "high_deviation_accounts": r.high_deviation_accounts,
                "elevated_deviation_accounts": r.elevated_deviation_accounts,
            },
            indent=2,
        )
    )
