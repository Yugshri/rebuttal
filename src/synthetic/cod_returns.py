"""COD / returns-fraud fields, per account.

Produces ``customer_return_history`` for every account in the transaction graph
so ``evidence-assembler`` (COD/returns checks) and ``risk-graph-service`` have
full coverage. A subset of accounts is a deliberate returns-abuse cohort with
high return rate, delivery-refusal history, and multi-account / refund-to-other-
account flags.

Calibration — e-commerce return-abuse dataset (Kaggle
``sarveshchhetri/e-commerce-return-abuse-detection-dataset``, opened via its
croissant metadata):
* Published class balance: Legitimate 70.1%, Policy Abuser 11.9%, Fraudulent
  Return 10.2%, Wardrobing 7.7%  -> ~30% combined abuse cohort. We use ~22%
  here (abuse is rarer among *account holders* than among flagged *returns*),
  and name this as a deliberate, non-measured adjustment in the failure
  taxonomy.
* The dataset's fields ``return_rate_pct``, ``total_orders_lifetime``,
  ``total_returns_lifetime``, ``account_age_days``, ``customer_segment``,
  ``multiple_accounts_flag``, ``refund_to_different_account``,
  ``previous_dispute_count`` are mirrored directly by name.
* Legit return-rate centre ~9% follows widely-cited India e-commerce return
  rates (industry reports put overall online returns ~8-12%, fashion higher);
  abuse cohort centred ~42%.
"""

from __future__ import annotations

import numpy as np

from .transaction_graph import BURSTY_CONTROLS, PLANTED_MULES

_SEGMENTS = ("new", "bronze", "silver", "gold")


def build_return_history(rng: np.random.Generator, nodes: list[dict]) -> list[dict]:
    account_ids = [n["account_id"] for n in nodes]
    node_by_id = {n["account_id"]: n for n in nodes}
    n = len(account_ids)

    # ~22% abuse cohort. Planted mules are always in it; fringe accounts are
    # over-represented; bursty controls are deliberately NOT (they are legit).
    abuse_target = round(n * 0.22)
    forced_in = set(PLANTED_MULES)
    forced_out = set(BURSTY_CONTROLS)
    pool = [a for a in account_ids if a not in forced_in and a not in forced_out]
    weights = np.array([
        3.0 if node_by_id[a]["account_type"] == "fringe" else 1.0 for a in pool
    ])
    weights = weights / weights.sum()
    extra = max(0, abuse_target - len(forced_in))
    chosen = set(rng.choice(pool, size=min(extra, len(pool)), replace=False,
                            p=weights).tolist())
    abusers = forced_in | chosen

    rows = []
    for acc in account_ids:
        node = node_by_id[acc]
        is_abuser = acc in abusers
        age_days = int(rng.integers(20, 90) if node["account_type"] == "fringe"
                       else rng.integers(120, 2200))
        segment = ("new" if age_days < 120
                   else _SEGMENTS[min(3, 1 + int(rng.integers(0, 3)))])
        orders_lifetime = int(max(1, rng.poisson(6 if node["account_type"] == "fringe"
                                                 else 34)))
        if is_abuser:
            return_rate = float(np.clip(rng.normal(42, 12), 15, 92))
        else:
            return_rate = float(np.clip(rng.normal(9, 5), 0, 30))
        returns_lifetime = int(round(orders_lifetime * return_rate / 100.0))
        refusals = int(rng.integers(1, 6)) if (is_abuser and rng.random() < 0.6) else \
            (1 if (not is_abuser and rng.random() < 0.05) else 0)
        prev_disputes = int(rng.integers(1, 5)) if (is_abuser and rng.random() < 0.5) \
            else int(rng.random() < 0.12)
        rows.append({
            "account_id": acc,
            "account_age_days": age_days,
            "segment": segment,
            "total_orders_lifetime": orders_lifetime,
            "total_returns_lifetime": returns_lifetime,
            "return_rate_pct": round(return_rate, 1),
            "delivery_refusals": refusals,
            "previous_dispute_count": prev_disputes,
            "multiple_accounts_flag": int(is_abuser and rng.random() < 0.45),
            "refund_to_different_account": int(is_abuser and rng.random() < 0.35),
        })
    rows.sort(key=lambda r: r["account_id"])
    return rows, sorted(abusers)
