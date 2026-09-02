"""One-command deterministic build of the entire synthetic corpus.

    .venv/Scripts/python.exe -m src.synthetic.build

Regenerates, from the single seed in ``src.synthetic.SEED``:
* ``data/external.db``      (read-only issuer/Razorpay-side store)
* ``data/webhooks/*.json``  (dispute.created payloads for the router to replay)
* ``data/heldout/*.json``   (labelled ground truth; pipeline never reads this)
* ``data/README.md``        (calibration provenance)

Nothing here makes a pipeline decision. Import-safe: ``main()`` returns a summary
dict and is used by the determinism test.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from . import (
    GRAPH_WINDOW_END,
    GRAPH_WINDOW_START,
    MULE_SHIFT_DAY,
    NOW,
    SEED,
)
from .cod_returns import build_return_history
from .disputes import build_disputes
from .evidence_store import build_evidence_store
from .heldout import build_account_labels, build_dispute_dispositions
from .rng import streams
from .schema import create_external_db
from .transaction_graph import (
    BURSTY_CONTROLS,
    PLANTED_MULES,
    build_graph,
)

_STREAM_NAMES = ("graph", "returns", "disputes", "evidence", "heldout")


def _data_dir(explicit: str | Path | None) -> Path:
    if explicit is not None:
        return Path(explicit).resolve()
    return Path(os.environ.get("TRACK02_DATA_DIR", "data")).resolve()


def _insert(conn: sqlite3.Connection, table: str, rows: list[dict]) -> None:
    if not rows:
        return
    cols = list(rows[0].keys())
    placeholders = ", ".join("?" for _ in cols)
    sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})"
    conn.executemany(sql, [tuple(r[c] for c in cols) for r in rows])


def main(data_dir: str | Path | None = None) -> dict:
    out = _data_dir(data_dir)
    out.mkdir(parents=True, exist_ok=True)
    external_db = Path(
        os.environ.get("TRACK02_EXTERNAL_DB", str(out / "external.db"))
    ).resolve()
    webhooks_dir = out / "webhooks"
    heldout_dir = out / "heldout"
    for d in (webhooks_dir, heldout_dir):
        d.mkdir(parents=True, exist_ok=True)
        for stale in d.glob("*.json"):
            stale.unlink()

    rng = streams(SEED, _STREAM_NAMES)

    # 1. Transaction graph (timestamped event log).
    graph = build_graph(rng["graph"])
    node_by_id = {n["account_id"]: n for n in graph.nodes}

    # 2. Per-account COD/returns history (needs the node list).
    return_rows, returns_abusers = build_return_history(rng["returns"], graph.nodes)

    # 3. Dispute corpus + backing payments. Steer some disputes onto risky
    #    counterparties so risk enrichment has signal AND a false-positive target.
    account_pool = {
        "mule": list(PLANTED_MULES),
        "bursty": list(BURSTY_CONTROLS),
        "fringe": [n["account_id"] for n in graph.nodes
                   if n["account_type"] == "fringe"],
        "normal": [n["account_id"] for n in graph.nodes
                   if n["account_type"] in ("consumer", "merchant")
                   and n["account_id"] not in PLANTED_MULES
                   and n["account_id"] not in BURSTY_CONTROLS],
    }
    disputes = build_disputes(rng["disputes"], account_pool)

    # 4. Evidence store (orders/shipping/comms/addresses/availability).
    ev = build_evidence_store(rng["evidence"], disputes, node_by_id)

    # 5. Held-out ground truth (separate directory, pipeline never reads it).
    account_labels = build_account_labels(graph.nodes, returns_abusers)
    dispositions = build_dispute_dispositions(
        rng["heldout"], disputes, ev["evidence_availability"]
    )

    # --- Write external.db --------------------------------------------------
    conn = create_external_db(external_db)
    try:
        _insert(conn, "account_nodes", graph.nodes)
        _insert(conn, "transaction_edges", graph.edges)
        _insert(conn, "payments", [d.payment for d in disputes])
        _insert(conn, "orders", ev["orders"])
        _insert(conn, "shipments", ev["shipments"])
        _insert(conn, "communications", ev["communications"])
        _insert(conn, "addresses", ev["addresses"])
        _insert(conn, "customer_return_history", return_rows)
        _insert(conn, "evidence_availability", ev["evidence_availability"])
        conn.commit()
    finally:
        conn.close()

    # --- Write webhook payloads ------------------------------------------
    webhook_count = 0
    for d in disputes:
        for i, payload in enumerate(d.followups):
            fname = f"{d.dispute_id}__{i:02d}__{payload['payload']['dispute']['entity']['phase']}.json"
            (webhooks_dir / fname).write_text(
                json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
            )
            webhook_count += 1

    # --- Write held-out ground truth -----------------------------------
    (heldout_dir / "account_labels.json").write_text(
        json.dumps(account_labels, indent=2, sort_keys=True), encoding="utf-8"
    )
    (heldout_dir / "dispute_dispositions.json").write_text(
        json.dumps(dispositions, indent=2, sort_keys=True), encoding="utf-8"
    )
    (heldout_dir / "README.md").write_text(_HELDOUT_README, encoding="utf-8")

    summary = {
        "seed": SEED,
        "external_db": str(external_db),
        "accounts": len(graph.nodes),
        "edges": len(graph.edges),
        "disputes": len(disputes),
        "webhook_events": webhook_count,
        "reopen_chains": sum(1 for d in disputes if d.meta.get("reopened")),
        "planted_mules": list(PLANTED_MULES),
        "bursty_controls": list(BURSTY_CONTROLS),
        "returns_abusers": len(returns_abusers),
        "incompleteness": _bucket_rates(ev["evidence_availability"]),
        "defer_fraction": dispositions["summary"]["defer_fraction"],
        "category_coverage": _category_coverage(disputes),
        "phase_coverage": sorted({d.phase for d in disputes}),
    }
    _write_data_readme(out, summary)
    return summary


def _bucket_rates(rows: list[dict]) -> dict:
    n = len(rows)
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["completeness_bucket"]] = counts.get(r["completeness_bucket"], 0) + 1
    return {k: {"count": v, "fraction": round(v / n, 3)} for k, v in sorted(counts.items())}


def _category_coverage(disputes) -> dict:
    counts: dict[str, int] = {}
    for d in disputes:
        counts[d.category_hint] = counts.get(d.category_hint, 0) + 1
    return dict(sorted(counts.items()))


_HELDOUT_README = """# Held-out ground truth — do not wire into the pipeline

`qa-evaluator` is the ONLY consumer of this directory. The ingestion / evidence /
risk / scoring paths must never read these files. No label here is duplicated
into any column the pipeline computes from.

* `account_labels.json`      — planted_mule / bursty_control / normal per account,
                               plus `returns_abuser` auxiliary flag.
* `dispute_dispositions.json`— expected assemble_clean vs. defer_to_human per
                               dispute, with the contributing factors and the
                               hand-flagged borderline cases.
"""


def _write_data_readme(out: Path, s: dict) -> None:
    from .transaction_graph import (
        BURSTY_PAYDAY,
        BURSTY_SEASONAL,
        MULE_BRIDGE,
        MULE_FANOUT,
        MULE_PASSTHRU,
    )

    text = f"""# Synthetic data corpus (Track 02 AI Risk Manager)

Regenerate everything deterministically:

    .venv/Scripts/python.exe -m src.synthetic.build

Single seed: **{s['seed']}** (defined in `src/synthetic/__init__.py`, nowhere else).

## Artifacts

| Path | What it is |
|---|---|
| `data/external.db` | Read-only "issuer / Razorpay-side" store. `src/common/db.py::external_engine()` opens this `mode=ro`. Tables: `account_nodes`, `transaction_edges`, `payments`, `orders`, `shipments`, `communications`, `addresses`, `customer_return_history`, `evidence_availability`. |
| `data/webhooks/*.json` | {s['webhook_events']} synthetic `dispute.created` payloads (Razorpay-shaped) across {s['disputes']} disputes. Reopen chains emit multiple events for one dispute id. |
| `data/heldout/*.json` | Labelled ground truth. **The pipeline never reads this** — only `qa-evaluator`. |

## Corpus shape

* Accounts (graph nodes): **{s['accounts']}**  ·  transaction edges: **{s['edges']}** (every edge carries a real ISO-8601 timestamp)
* Disputes: **{s['disputes']}**  ·  webhook events: **{s['webhook_events']}**  ·  genuine reopen chains: **{s['reopen_chains']}**
* Reason-code category coverage: `{s['category_coverage']}`
* Phase coverage: `{s['phase_coverage']}`
* Graph window: {GRAPH_WINDOW_START} .. {GRAPH_WINDOW_END} ; mule behavioural shift begins ~day {MULE_SHIFT_DAY}; dispute "now" = {NOW}

## Evidence incompleteness (deliberate, documented)

| bucket | meaning | fraction |
|---|---|---|
| full | every required evidence slot has a real backing record | {s['incompleteness'].get('full', {}).get('fraction', 0)} |
| partial | exactly one non-critical required slot has no record | {s['incompleteness'].get('partial', {}).get('fraction', 0)} |
| severe | a critical slot (or 2+ slots) has no record | {s['incompleteness'].get('severe', {}).get('fraction', 0)} |

This is a designed test condition for the "never fabricate, report missing
honestly" path — not an oversight. Rationale: real fraud/dispute feature stores
(IEEE-CIS Fraud Detection) ship with many columns >50% null.

Held-out expected `defer_to_human` fraction: **{s['defer_fraction']}**

## Planted accounts (ground truth — full rationale in `data/heldout/account_labels.json`)

| account_id | role | pattern |
|---|---|---|
| `{MULE_FANOUT}` | planted mule | high-trust inflow for ~7 weeks, then late rapid fan-out to ~25 fringe accounts |
| `{MULE_BRIDGE}` | planted mule | operates in one community, then late small irregular transfers bridging two communities (betweenness spike) |
| `{MULE_PASSTHRU}` | planted mule | thin history then bursts: receives and forwards ~85-95% onward within hours (velocity + short time-to-forward) |
| `{BURSTY_SEASONAL}` | bursty control (legit) | ~5x festival-season volume spike, **same** customers and suppliers — no fringe, no bridge. The false-positive target. |
| `{BURSTY_PAYDAY}` | bursty control (legit) | monthly payday out-degree spike to the **same** ~40 recipients |

Returns-abuse cohort: **{s['returns_abusers']}** accounts (high `return_rate_pct`,
delivery refusals, multi-account / refund-to-other-account flags).

## Calibration provenance (which real dataset backs which parameter)

| Parameter | Calibrated against | Notes |
|---|---|---|
| Transaction-graph structure (directed, timestamped event log, small illicit minority) | **IBM AML** `ealtman2019/ibm-transactions-for-anti-money-laundering-aml` (opened via croissant metadata: HI-Small ~515K accounts / ~5M txns / 10-97 days 2022, `Timestamp` + hex account ids, laundering ~1 per 981 ≈ 0.1%) | We mirror the *shape* at demo scale. Planted-mule share here (~1% of accounts) is deliberately inflated above IBM's ~0.1% txn rate for visible demo signal. |
| Behavioural-feature families (counting, velocity/time-delta, match/mismatch flags) | **IEEE-CIS Fraud Detection** (Vesta) — `C*` counting, `D*` time-delta, `M1..M9` match families | `evidence_availability` carries `billing_shipping_address_match`, `email_domain_consistent`, `avs_match`; graph edges carry real timestamps for velocity/recency. |
| Deliberate evidence missingness | **IEEE-CIS** (many `D*`/`V*`/`id_*` columns >50% null) | Motivates the full/partial/severe buckets. |
| Dispute amount distribution (right-skewed lognormal, median ~₹1,900, tail to ~₹92K) | **ULB Credit Card Fraud** `mlg-ulb/creditcardfraud` (published: 284,807 txns, mean amount ≈ 88, strong right skew) for tail *shape*; **RBI DBIE** (avg UPI ticket ≈ ₹1,300-1,600 through 2024-25) for scale sanity | ULB is EUR card-present; only the shape transfers, not the absolute scale. |
| India-specific transaction features (night/weekend flags, retry `attempt_count`, `amount_slab`, Razorpay payment-id shape) | **Pattern-Based UPI Transaction Risk Dataset** `kalpitlabs/upi-fraud-detection-dataset-india-synthetic` (opened via croissant: columns incl. `razorpay_payment_id`, `timestamp`, `amount`, `upi_app`, `device_fingerprint`, `ip_address`, `attempt_count`, `is_night_transaction`, `is_weekend`; class balance 55/45) | India-specific dataset that **held up on inspection** — used for feature *vocabulary*, not for fraud base rate (its 45% is synthetic oversampling, not a real rate). |
| COD / returns fields (`return_rate_pct`, `total_orders_lifetime`, `total_returns_lifetime`, `account_age_days`, `customer_segment`, `multiple_accounts_flag`, `refund_to_different_account`, `previous_dispute_count`) | **E-commerce Return Abuse Detection** `sarveshchhetri/e-commerce-return-abuse-detection-dataset` (opened via croissant: 60,000 rows × 35 cols, 0 missing; published class balance Legitimate 70.1% / Policy Abuser 11.9% / Fraudulent Return 10.2% / Wardrobing 7.7%) | Field names mirrored directly. Our account-level abuse cohort (~22%) is below the dataset's ~30% *return-level* rate — deliberate, non-measured adjustment. |
| Reason codes (Visa 10.4, MC 4863, Amex F24, …) and Razorpay Dispute/evidence schema fields | Card-network rulebooks + Razorpay docs (already real, from the research docs) | Not from Kaggle. `src/common/reason_codes.py` is the shared source of truth. |

India-specific datasets checked: `kalpitlabs/upi-fraud-detection-dataset-india-synthetic`
(**used** — feature vocabulary) and `kumarperiya/comprehensive-indian-online-fraud-dataset`
(opened via croissant: ~1,200 rows, columns `transaction_id, customer_id, merchant_id,
amount, transaction_time, is_fraudulent, card_type, location, purchase_category,
customer_age, fraud_type`; **ruled out for calibration** — too small, no published
class balance or distribution stats, no graph/velocity structure). See
`docs/failure-taxonomy.md` for the full mapping-gap discussion.
"""
    (out / "README.md").write_text(text, encoding="utf-8")


if __name__ == "__main__":
    result = main()
    print(json.dumps(result, indent=2))
