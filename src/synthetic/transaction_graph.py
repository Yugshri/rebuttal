"""Synthetic counterparty transaction graph.

Highest-value artifact in this package. It is a **real timestamped event log**,
not a two-snapshot graph: every edge in ``transaction_edges`` carries a real
ISO-8601 ``timestamp`` inside the 90-day window, so ``risk-graph-service`` can
derive genuine velocity / recency / first-seen-counterparty features from the
*sequence* and compute ``baseline_deviation`` as a real calculation rather than
reading a label.

What is deliberately planted (ground truth lives in ``data/heldout/``, never
here):

* 3 mule-like accounts, each with a *different* behavioural shift that only
  becomes visible by reading the timestamp sequence:
    - ``ACC_MULE_FANOUT``   high-trust inflows early, then a late pivot to
                            fan-out into many low-rank fringe accounts.
    - ``ACC_MULE_BRIDGE``   sits inside one community, then late in the window
                            starts small irregular transfers that bridge to a
                            second community (a betweenness spike).
    - ``ACC_MULE_PASSTHRU`` late in the window starts receiving bursts and
                            forwarding ~90% onward within hours (a short
                            time-to-forward + high velocity signature).
* 2 legitimately-bursty control accounts that a naive volume/velocity detector
  should false-positive on, but a real behavioural-shift detector should not:
    - ``ACC_BURSTY_SEASONAL``  a festival-season retailer: ~5x transaction
                               volume spike, but counterparties stay the same
                               established merchant/consumer set — no fringe,
                               no new bridge.
    - ``ACC_BURSTY_PAYDAY``    a payroll disburser whose out-degree spikes on
                               month boundaries, every month, to the same
                               recipients.

Calibration:
* Graph scale (few hundred accounts, sparse) and the timestamped-event-log shape
  follow IBM's AML dataset (Kaggle ``ealtman2019/ibm-transactions-for-anti-
  money-laundering-aml``, opened via its croissant metadata): HI-Small is ~515K
  accounts / ~5M txns over ~10-97 days in 2022 with a ``Timestamp`` column and
  hex account ids — we mirror the *structure* (directed, timestamped, currency
  amounts, a small illicit minority) at demo scale.
* Planted-mule share here is ~1% of accounts, deliberately inflated above IBM
  AML's ~0.1% laundering-transaction rate so the demo has visible signal. Named
  in the failure taxonomy as a known divergence.
* Amounts: right-skewed lognormal. RBI's Database on Indian Economy (DBIE)
  macro figure — average UPI transaction ~INR 1,300-1,600 through 2024-25 — is
  the scale sanity check; the heavy right tail follows the amount distribution
  shape in ULB's credit-card fraud dataset (``mlg-ulb/creditcardfraud``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import numpy as np

from . import GRAPH_WINDOW_DAYS, GRAPH_WINDOW_START, MULE_SHIFT_DAY
from .rng import lognormal_amount, parse_iso, to_iso

# -- Fixed identifiers for the planted accounts. The held-out labeller imports
# these; risk-graph-service must NOT (it has to find them, not be told).
MULE_FANOUT = "ACC_MULE_FANOUT"
MULE_BRIDGE = "ACC_MULE_BRIDGE"
MULE_PASSTHRU = "ACC_MULE_PASSTHRU"
BURSTY_SEASONAL = "ACC_BURSTY_SEASONAL"
BURSTY_PAYDAY = "ACC_BURSTY_PAYDAY"

PLANTED_MULES: tuple[str, ...] = (MULE_FANOUT, MULE_BRIDGE, MULE_PASSTHRU)
BURSTY_CONTROLS: tuple[str, ...] = (BURSTY_SEASONAL, BURSTY_PAYDAY)

_CITIES = (
    "Mumbai",
    "Bengaluru",
    "Delhi",
    "Hyderabad",
    "Pune",
    "Chennai",
    "Kolkata",
    "Ahmedabad",
    "Jaipur",
    "Surat",
)


@dataclass
class Graph:
    nodes: list[dict]
    edges: list[dict]

    @property
    def account_ids(self) -> list[str]:
        return [n["account_id"] for n in self.nodes]

    def consumers(self) -> list[str]:
        return [n["account_id"] for n in self.nodes if n["account_type"] == "consumer"]


def _window_start():
    return parse_iso(GRAPH_WINDOW_START)


def _ts(rng: np.random.Generator, day: float) -> str:
    """ISO timestamp ``day`` days into the window, at a random second-of-day."""
    base = _window_start() + timedelta(days=float(day))
    base = base + timedelta(seconds=int(rng.integers(0, 86_400)))
    return to_iso(base)


def build_graph(rng: np.random.Generator) -> Graph:
    """Construct the full node + timestamped-edge set deterministically."""
    nodes: list[dict] = []
    edges: list[dict] = []
    counter = {"txn": 0}

    def add_edge(src: str, dst: str, amount: float, day: float, kind: str) -> None:
        counter["txn"] += 1
        edges.append(
            {
                "txn_id": f"TXN{counter['txn']:06d}",
                "src_account": src,
                "dst_account": dst,
                "amount": amount,
                "timestamp": _ts(rng, day),
                "edge_kind": kind,
            }
        )

    # -- Node population --------------------------------------------------
    # ~300 accounts: 5 payroll, 16 merchant, 232 consumer, ~47 fringe.
    payroll = [f"ACC_PAYROLL_{i:02d}" for i in range(5)]
    merchants = [f"ACC_MERCHANT_{i:02d}" for i in range(16)]
    consumers = [f"ACC_CONS_{i:03d}" for i in range(232)]
    fringe = [f"ACC_FRINGE_{i:03d}" for i in range(47)]

    def city(i: int) -> str:
        return _CITIES[i % len(_CITIES)]

    for i, acc in enumerate(payroll):
        nodes.append(_node(acc, "payroll", "core", -400 - i * 5, city(i), rng,
                           "salary/vendor disburser"))
    for i, acc in enumerate(merchants):
        # Split merchants into two communities so a bridge is meaningful.
        cluster = "market_a" if i < 8 else "market_b"
        nodes.append(_node(acc, "merchant", cluster, -300 - i * 3, city(i), rng,
                           "established merchant"))
    for i, acc in enumerate(consumers):
        cluster = "market_a" if i % 2 == 0 else "market_b"
        nodes.append(_node(acc, "consumer", cluster, int(rng.integers(-500, -20)),
                           city(i), rng, ""))
    for i, acc in enumerate(fringe):
        nodes.append(_node(acc, "fringe", "fringe",
                           int(rng.integers(-40, -2)), city(i), rng,
                           "low-tenure, thin history"))

    # The planted accounts are consumers/merchants on the surface.
    nodes.append(_node(MULE_FANOUT, "consumer", "market_a", -220, "Mumbai", rng, ""))
    nodes.append(_node(MULE_BRIDGE, "consumer", "market_a", -260, "Delhi", rng, ""))
    nodes.append(_node(MULE_PASSTHRU, "consumer", "market_b", -35, "Hyderabad", rng, ""))
    nodes.append(_node(BURSTY_SEASONAL, "merchant", "market_a", -900, "Jaipur", rng,
                       "festival-season apparel retailer"))
    nodes.append(_node(BURSTY_PAYDAY, "payroll", "core", -700, "Pune", rng,
                       "SME payroll disburser"))

    merchants_a = merchants[:8]
    merchants_b = merchants[8:]
    consumers_a = [c["account_id"] for c in nodes
                   if c["account_type"] == "consumer" and c["cluster"] == "market_a"
                   and c["account_id"] not in PLANTED_MULES]
    consumers_b = [c["account_id"] for c in nodes
                   if c["account_type"] == "consumer" and c["cluster"] == "market_b"
                   and c["account_id"] not in PLANTED_MULES]

    # -- Background: legitimate salaried-consumer behaviour --------------
    # Each consumer: monthly payroll credit + irregular merchant payments to
    # their own community. Velocity is bounded and counterparties are stable.
    all_consumers = consumers_a + consumers_b
    for acc in all_consumers:
        src_payroll = payroll[int(rng.integers(0, len(payroll)))]
        home = merchants_a if acc in consumers_a else merchants_b
        for month_start in (2, 32, 62):  # ~3 monthly cycles inside the window
            credit = lognormal_amount(rng, median=42_000, sigma=0.35,
                                      lo=12_000, hi=180_000)
            add_edge(src_payroll, acc, credit, month_start + rng.uniform(-1, 1),
                     "payroll_credit")
            n_pay = int(rng.integers(2, 6))
            picks = rng.choice(len(home), size=min(n_pay, len(home)), replace=False)
            for p in picks:
                amt = lognormal_amount(rng, median=1_500, sigma=0.9,
                                       lo=80, hi=45_000)
                day = month_start + rng.uniform(1, 27)
                add_edge(acc, home[int(p)], amt, day, "merchant_payment")

    # Sparse merchant<->merchant settlement inside each community.
    for grp in (merchants_a, merchants_b):
        for _ in range(24):
            a, b = rng.choice(len(grp), size=2, replace=False)
            add_edge(grp[int(a)], grp[int(b)],
                     lognormal_amount(rng, median=8_000, sigma=0.7, lo=500, hi=90_000),
                     rng.uniform(1, GRAPH_WINDOW_DAYS - 1), "p2p_transfer")

    # A few fringe accounts have thin, legitimate-looking histories so fringe
    # isn't a synonym for "mule counterparty".
    for acc in fringe[:20]:
        for _ in range(int(rng.integers(1, 4))):
            other = merchants_b[int(rng.integers(0, len(merchants_b)))]
            add_edge(acc, other,
                     lognormal_amount(rng, median=600, sigma=0.8, lo=50, hi=6_000),
                     rng.uniform(1, GRAPH_WINDOW_DAYS - 1), "merchant_payment")

    # -- Planted mule 1: high-trust inflow -> late fan-out --------------
    _plant_fanout(add_edge, rng, payroll, merchants_a, fringe)
    # -- Planted mule 2: late cluster bridge ---------------------------
    _plant_bridge(add_edge, rng, merchants_a, consumers_a, consumers_b, merchants_b)
    # -- Planted mule 3: rapid pass-through ---------------------------
    _plant_passthru(add_edge, rng, consumers_a + consumers_b, fringe)
    # -- Bursty control 1: seasonal spike, same counterparties --------
    _plant_bursty_seasonal(add_edge, rng, consumers_a, merchants_a)
    # -- Bursty control 2: monthly payday spike ----------------------
    _plant_bursty_payday(add_edge, rng, consumers_a + consumers_b)

    edges.sort(key=lambda e: (e["timestamp"], e["txn_id"]))
    return Graph(nodes=nodes, edges=edges)


def _node(account_id, account_type, cluster, opened_offset_days, home_city, rng, notes):
    opened = _window_start() + timedelta(days=float(opened_offset_days))
    return {
        "account_id": account_id,
        "account_type": account_type,
        "cluster": cluster,
        "opened_at": to_iso(opened),
        "home_city": home_city,
        "notes": notes,
    }


def _plant_fanout(add_edge, rng, payroll, merchants_a, fringe):
    acc = MULE_FANOUT
    # Phase 1 (day 0 .. MULE_SHIFT_DAY): looks like a normal salaried consumer.
    for month_start in (3, 33):
        add_edge(payroll[int(rng.integers(0, len(payroll)))], acc,
                 lognormal_amount(rng, median=55_000, sigma=0.3, lo=30_000, hi=120_000),
                 month_start, "payroll_credit")
        for _ in range(3):
            add_edge(acc, merchants_a[int(rng.integers(0, len(merchants_a)))],
                     lognormal_amount(rng, median=2_000, sigma=0.7, lo=200, hi=20_000),
                     month_start + rng.uniform(1, 25), "merchant_payment")
    # Trigger: a large inbound near the shift day (illicit funds arriving).
    add_edge(payroll[0], acc, 240_000.0, MULE_SHIFT_DAY - 1, "payroll_credit")
    # Phase 2: rapid fan-out to many fringe accounts, small irregular amounts,
    # accelerating cadence. This is the mule cash-out.
    targets = list(fringe[15:40])
    day = float(MULE_SHIFT_DAY)
    for i, dst in enumerate(targets):
        day += float(rng.uniform(0.05, 0.9))
        add_edge(acc, dst,
                 lognormal_amount(rng, median=7_500, sigma=0.5, lo=1_500, hi=25_000),
                 day, "fan_out")
        if i % 3 == 0 and i > 0:
            add_edge(acc, targets[int(rng.integers(0, len(targets)))],
                     lognormal_amount(rng, median=5_000, sigma=0.5, lo=1_000, hi=18_000),
                     day + rng.uniform(0.01, 0.2), "fan_out")


def _plant_bridge(add_edge, rng, merchants_a, consumers_a, consumers_b, merchants_b):
    acc = MULE_BRIDGE
    # Phase 1: entirely inside market_a.
    for _ in range(10):
        add_edge(acc, merchants_a[int(rng.integers(0, len(merchants_a)))],
                 lognormal_amount(rng, median=1_800, sigma=0.8, lo=150, hi=15_000),
                 rng.uniform(1, MULE_SHIFT_DAY), "merchant_payment")
    for _ in range(4):
        add_edge(consumers_a[int(rng.integers(0, len(consumers_a)))], acc,
                 lognormal_amount(rng, median=3_000, sigma=0.6, lo=400, hi=20_000),
                 rng.uniform(1, MULE_SHIFT_DAY), "p2p_transfer")
    # Phase 2: small, irregular transfers that connect market_a <-> market_b.
    # Individually unremarkable; collectively a betweenness bridge.
    for _ in range(14):
        src = consumers_a[int(rng.integers(0, len(consumers_a)))]
        dst = consumers_b[int(rng.integers(0, len(consumers_b)))]
        day = rng.uniform(MULE_SHIFT_DAY + 2, GRAPH_WINDOW_DAYS - 1)
        add_edge(src, acc,
                 lognormal_amount(rng, median=900, sigma=0.5, lo=200, hi=4_000),
                 day, "p2p_transfer")
        add_edge(acc, dst,
                 lognormal_amount(rng, median=850, sigma=0.5, lo=180, hi=3_800),
                 day + rng.uniform(0.02, 0.6), "p2p_transfer")
    # A couple of direct market_b merchant touches to cement the bridge.
    for _ in range(3):
        add_edge(acc, merchants_b[int(rng.integers(0, len(merchants_b)))],
                 lognormal_amount(rng, median=1_200, sigma=0.6, lo=200, hi=6_000),
                 rng.uniform(MULE_SHIFT_DAY + 5, GRAPH_WINDOW_DAYS - 1),
                 "merchant_payment")


def _plant_passthru(add_edge, rng, consumers, fringe):
    acc = MULE_PASSTHRU
    # Almost no history until late; then bursts in, ~90% straight back out
    # within hours to fringe accounts.
    start = float(MULE_SHIFT_DAY + 8)
    for burst in range(6):
        day = start + burst * float(rng.uniform(1.5, 4.0))
        inflow_total = 0.0
        for _ in range(int(rng.integers(2, 5))):
            amt = lognormal_amount(rng, median=18_000, sigma=0.4, lo=6_000, hi=60_000)
            inflow_total += amt
            add_edge(consumers[int(rng.integers(0, len(consumers)))], acc, amt,
                     day + rng.uniform(0, 0.3), "p2p_transfer")
        forward = round(inflow_total * float(rng.uniform(0.85, 0.95)), 2)
        n_out = int(rng.integers(2, 4))
        for k in range(n_out):
            add_edge(acc, fringe[int(rng.integers(0, len(fringe)))],
                     round(forward / n_out, 2),
                     day + rng.uniform(0.05, 0.35), "fan_out")


def _plant_bursty_seasonal(add_edge, rng, consumers_a, merchants_a):
    acc = BURSTY_SEASONAL
    regulars = list(rng.choice(consumers_a, size=30, replace=False))
    suppliers = list(merchants_a[:4])
    # Baseline: steady trickle of customer payments in, supplier payments out.
    for _ in range(40):
        add_edge(str(regulars[int(rng.integers(0, len(regulars)))]), acc,
                 lognormal_amount(rng, median=1_600, sigma=0.7, lo=200, hi=12_000),
                 rng.uniform(1, GRAPH_WINDOW_DAYS - 1), "merchant_payment")
    # Festival spike: ~5x volume in a two-week window, SAME regular customers and
    # SAME suppliers — no fringe, no bridge. A volume detector should fire here;
    # a behavioural-shift detector should not.
    for _ in range(210):
        day = rng.uniform(MULE_SHIFT_DAY + 4, MULE_SHIFT_DAY + 18)
        add_edge(str(regulars[int(rng.integers(0, len(regulars)))]), acc,
                 lognormal_amount(rng, median=1_900, sigma=0.7, lo=200, hi=14_000),
                 day, "merchant_payment")
    for _ in range(60):
        day = rng.uniform(MULE_SHIFT_DAY + 6, MULE_SHIFT_DAY + 20)
        add_edge(acc, suppliers[int(rng.integers(0, len(suppliers)))],
                 lognormal_amount(rng, median=9_000, sigma=0.5, lo=2_000, hi=60_000),
                 day, "p2p_transfer")


def _plant_bursty_payday(add_edge, rng, consumers):
    acc = BURSTY_PAYDAY
    recipients = list(rng.choice(consumers, size=40, replace=False))
    # Every month boundary: a big fan-out to the SAME 40 recipients. Regular,
    # predictable, same counterparties — a velocity spike that is not a shift.
    for month_start in (1, 31, 61):
        for r in recipients:
            add_edge(acc, str(r),
                     lognormal_amount(rng, median=28_000, sigma=0.3, lo=12_000, hi=90_000),
                     month_start + rng.uniform(-0.4, 0.8), "payroll_credit")
