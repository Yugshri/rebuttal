"""Transaction-graph loading and time-windowing helpers.

The transaction graph lives in the **read-only** external store
(``data/external.db`` -> ``transaction_edges`` / ``account_nodes``). This module
never writes anything: it loads the timestamped edge log, and slices it into
rolling time windows so ``batch.py`` can watch each account's centrality move
against *its own* history rather than against a cross-account percentile.

Nothing here is on the API request path — see ``batch.py`` for why graph
computation is a scheduled job, not a per-request call.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import networkx as nx
import numpy as np
from sqlalchemy import text

from src.common.db import read_only_session

_ISO = "%Y-%m-%dT%H:%M:%S"


@dataclass(frozen=True)
class Edge:
    """One directed, timestamped transfer."""

    txn_id: str
    src: str
    dst: str
    amount: float
    ts: datetime
    kind: str


@dataclass(frozen=True)
class AccountNode:
    account_id: str
    account_type: str
    cluster: str
    opened_at: datetime
    home_city: str


def _parse(value: str) -> datetime:
    return datetime.strptime(value, _ISO)


def _iso(value: datetime) -> str:
    return value.strftime(_ISO)


def load_edges() -> list[Edge]:
    """Every transaction edge, ascending by timestamp. Read-only."""
    with read_only_session() as session:
        rows = session.execute(
            text(
                "SELECT txn_id, src_account, dst_account, amount, timestamp, edge_kind "
                "FROM transaction_edges ORDER BY timestamp, txn_id"
            )
        ).all()
    return [
        Edge(r[0], r[1], r[2], float(r[3]), _parse(r[4]), r[5]) for r in rows
    ]


def load_nodes() -> dict[str, AccountNode]:
    """Account metadata keyed by ``account_id``. Read-only."""
    with read_only_session() as session:
        rows = session.execute(
            text(
                "SELECT account_id, account_type, cluster, opened_at, home_city "
                "FROM account_nodes"
            )
        ).all()
    return {
        r[0]: AccountNode(r[0], r[1], r[2], _parse(r[3]), r[4]) for r in rows
    }


def load_return_history() -> dict[str, dict]:
    """Per-account COD / returns history (``customer_return_history``). Read-only."""
    with read_only_session() as session:
        rows = session.execute(
            text(
                "SELECT account_id, account_age_days, segment, "
                "total_orders_lifetime, total_returns_lifetime, return_rate_pct, "
                "delivery_refusals, previous_dispute_count, multiple_accounts_flag, "
                "refund_to_different_account FROM customer_return_history"
            )
        ).all()
    out: dict[str, dict] = {}
    for r in rows:
        out[r[0]] = {
            "account_age_days": int(r[1]),
            "segment": r[2],
            "total_orders_lifetime": int(r[3]),
            "total_returns_lifetime": int(r[4]),
            "return_rate_pct": float(r[5]),
            "delivery_refusals": int(r[6]),
            "previous_dispute_count": int(r[7]),
            "multiple_accounts_flag": bool(r[8]),
            "refund_to_different_account": bool(r[9]),
        }
    return out


def load_high_return_density_accounts() -> set[str]:
    """Accounts with at least one address flagged as a return/reship hotspot."""
    with read_only_session() as session:
        rows = session.execute(
            text(
                "SELECT DISTINCT account_id FROM addresses "
                "WHERE high_return_density = 1"
            )
        ).all()
    return {r[0] for r in rows}


@dataclass(frozen=True)
class Window:
    index: int
    start: datetime
    end: datetime  # exclusive

    def contains(self, ts: datetime) -> bool:
        return self.start <= ts < self.end


def rolling_windows(
    edges: list[Edge], *, window_days: int, step_days: int
) -> list[Window]:
    """Overlapping fixed-width windows spanning the edge log.

    ``window_days`` wide, advanced by ``step_days``. Overlapping (step < width)
    windows give a smoother per-account centrality series to build a baseline
    from, which matters at demo scale where any single window is sparse.
    """
    if not edges:
        return []
    first = edges[0].ts
    last = edges[-1].ts
    width = timedelta(days=window_days)
    step = timedelta(days=step_days)
    windows: list[Window] = []
    idx = 0
    start = first
    while start < last:
        windows.append(Window(idx, start, start + width))
        idx += 1
        start = start + step
    # Guarantee the final window's end covers the last edge.
    if windows and windows[-1].end <= last:
        windows.append(
            Window(len(windows), windows[-1].start + step, last + timedelta(seconds=1))
        )
    return windows


def edges_in_window(edges: list[Edge], window: Window) -> list[Edge]:
    return [e for e in edges if window.contains(e.ts)]


def build_digraph(edges: list[Edge]) -> nx.DiGraph:
    """Directed multigraph collapsed to a weighted DiGraph.

    Edge ``weight`` is total rupee value src->dst in the slice; ``count`` is the
    number of transfers. PageRank uses ``weight``; betweenness uses hop distance
    (structural bridging, not value).
    """
    g = nx.DiGraph()
    for e in edges:
        if g.has_edge(e.src, e.dst):
            g[e.src][e.dst]["weight"] += e.amount
            g[e.src][e.dst]["count"] += 1
        else:
            g.add_edge(e.src, e.dst, weight=e.amount, count=1)
    return g


def pagerank(
    g: nx.DiGraph, *, alpha: float = 0.85, max_iter: int = 200, tol: float = 1e-9
) -> dict[str, float]:
    """Weighted PageRank by numpy power iteration.

    Implemented directly (not via ``nx.pagerank``) because networkx 3.x delegates
    to SciPy, which is not a dependency of this project. Dangling nodes (no
    out-edges) redistribute their mass uniformly, matching networkx semantics.
    A mule that stops receiving from high-trust payroll/merchant nodes loses
    inbound weight and its score falls toward the teleport floor.
    """
    nodes = list(g.nodes())
    n = len(nodes)
    if n == 0:
        return {}
    idx = {node: i for i, node in enumerate(nodes)}
    m = np.zeros((n, n), dtype=float)
    for src, dst, data in g.edges(data=True):
        m[idx[src], idx[dst]] += float(data.get("weight", 1.0))
    row_sums = m.sum(axis=1)
    dangling = row_sums == 0
    row_sums_safe = np.where(dangling, 1.0, row_sums)
    m = m / row_sums_safe[:, None]
    m[dangling, :] = 1.0 / n

    rank = np.full(n, 1.0 / n)
    teleport = np.full(n, 1.0 / n)
    for _ in range(max_iter):
        new = alpha * (m.T @ rank) + (1.0 - alpha) * teleport
        if np.abs(new - rank).sum() < tol:
            rank = new
            break
        rank = new
    rank = rank / rank.sum()
    return {node: float(rank[idx[node]]) for node in nodes}


def betweenness(g: nx.DiGraph) -> dict[str, float]:
    if g.number_of_nodes() == 0:
        return {}
    return nx.betweenness_centrality(g, normalized=True)


__all__ = [
    "Edge",
    "AccountNode",
    "Window",
    "load_edges",
    "load_nodes",
    "load_return_history",
    "load_high_return_density_accounts",
    "rolling_windows",
    "edges_in_window",
    "build_digraph",
    "pagerank",
    "betweenness",
]
