"""Labelled held-out ground truth.

Written to ``data/heldout/`` — a directory the normal pipeline path NEVER reads.
Only ``qa-evaluator`` opens these files, to score the pipeline's independently
computed output against them. No label here is stored in any column the pipeline
computes from; leaking one would make every downstream metric vacuous.

Two ground-truth tasks:

1. ``dispute_dispositions.json`` — per dispute, whether it *should* assemble
   cleanly or *should* defer to human review. This is a deliberate combination
   of several factors (never a single stored field), plus a small set of
   hand-flagged borderline cases, so a pipeline can't recover it by reading one
   column.

2. ``account_labels.json`` — per account, ``planted_mule`` /
   ``bursty_control`` / ``normal``, with the specific behavioural pattern for
   each planted account, plus an auxiliary ``returns_abuser`` flag for the
   COD/returns evaluation.
"""

from __future__ import annotations

import numpy as np

from src.common import reason_codes as rc

from . import NOW
from .rng import parse_iso
from .transaction_graph import (
    BURSTY_PAYDAY,
    BURSTY_SEASONAL,
    MULE_BRIDGE,
    MULE_FANOUT,
    MULE_PASSTHRU,
)

_MULE_PATTERNS = {
    MULE_FANOUT: {
        "pattern": "high_trust_inflow_then_late_fanout",
        "rationale": "Normal salaried-consumer profile for the first ~7 weeks "
                     "(payroll credits in, established-merchant payments out), "
                     "then a large inbound near day 52 followed by rapid "
                     "fan-out into ~25 low-tenure fringe accounts with small "
                     "irregular amounts. Classic mule cash-out; visible only "
                     "from the timestamp sequence.",
    },
    MULE_BRIDGE: {
        "pattern": "late_cross_cluster_bridge",
        "rationale": "Operates entirely inside community market_a for the first "
                     "half of the window, then begins small, irregular pass-"
                     "through transfers that connect market_a and market_b. "
                     "Each transfer is unremarkable; together they create a "
                     "betweenness bridge that did not exist before day ~54.",
    },
    MULE_PASSTHRU: {
        "pattern": "rapid_passthrough_high_velocity",
        "rationale": "Thin history until ~day 60, then repeated bursts: "
                     "receives from several consumers and forwards ~85-95% "
                     "onward to fringe accounts within hours. Short "
                     "time-to-forward and high in/out velocity are the tell.",
    },
}
_BURSTY_PATTERNS = {
    BURSTY_SEASONAL: {
        "pattern": "seasonal_volume_spike_same_counterparties",
        "rationale": "Festival-season retailer: ~5x transaction volume in a "
                     "two-week window, but counterparties stay the same "
                     "regular-customer and supplier set — no fringe, no new "
                     "bridge. A naive volume/velocity detector should "
                     "false-positive here; a behavioural-shift detector "
                     "should not. This is the account that makes the "
                     "false-positive-cost metric meaningful.",
    },
    BURSTY_PAYDAY: {
        "pattern": "monthly_payday_fanout_same_recipients",
        "rationale": "SME payroll disburser: large out-degree spike on every "
                     "month boundary to the SAME ~40 recipients. Regular, "
                     "predictable, stable counterparties — a recurring "
                     "velocity spike that is not a behavioural shift.",
    },
}


def build_account_labels(nodes: list[dict], returns_abusers: list[str]) -> dict:
    abusers = set(returns_abusers)
    labels = {}
    for node in sorted(nodes, key=lambda n: n["account_id"]):
        acc = node["account_id"]
        if acc in _MULE_PATTERNS:
            entry = {"label": "planted_mule", **_MULE_PATTERNS[acc]}
        elif acc in _BURSTY_PATTERNS:
            entry = {"label": "bursty_control", **_BURSTY_PATTERNS[acc]}
        else:
            entry = {"label": "normal", "pattern": None, "rationale": ""}
        entry["returns_abuser"] = acc in abusers
        labels[acc] = entry
    return {
        "task": "Identify planted mule-like accounts vs. legitimately-bursty "
                "controls vs. normal accounts, from the transaction graph "
                "sequence alone.",
        "planted_mules": sorted(_MULE_PATTERNS),
        "bursty_controls": sorted(_BURSTY_PATTERNS),
        "labels": labels,
    }


def build_dispute_dispositions(rng: np.random.Generator, disputes, availability_rows):
    now = parse_iso(NOW)
    avail_by_pay = {r["payment_id"]: r for r in availability_rows}
    results = {}
    clean_ids = []
    for d in disputes:
        av = avail_by_pay[d.payment_id]
        bucket = av["completeness_bucket"]
        hrs_to_deadline = (parse_iso(d.respond_by) - now).total_seconds() / 3600.0
        within_48h = hrs_to_deadline <= 48.0
        factors = []
        if d.category_hint == rc.NEEDS_MANUAL_CLASSIFICATION:
            factors.append("unrecognised_reason_code")
        if bucket == "severe":
            factors.append("evidence_severely_incomplete")
        if d.phase in ("pre_arbitration", "arbitration"):
            factors.append(f"late_phase:{d.phase}")
        if d.meta.get("reopened"):
            factors.append("reopened_case")
        if d.meta.get("account_role") == "mule" and d.amount >= 8_000:
            factors.append("high_amount_on_mule_linked_account")
        if within_48h and bucket == "partial":
            factors.append("urgent_deadline_with_imperfect_evidence")

        disposition = "defer_to_human" if factors else "assemble_clean"
        if disposition == "assemble_clean":
            clean_ids.append(d.dispute_id)
        results[d.dispute_id] = {
            "expected_disposition": disposition,
            "factors": factors,
            "amount": d.amount,
            "phase": d.phase,
            "category_hint": d.category_hint,
            "completeness_bucket": bucket,
            "hours_to_deadline": round(hrs_to_deadline, 1),
            "borderline_flip": False,
        }

    # Designed borderline cases: flip ~4 otherwise-clean disputes to
    # defer_to_human. These are the "a careful human would want a look" cases —
    # documented so a reviewer sees them as intentional, not noise.
    flips = rng.choice(len(clean_ids), size=min(4, len(clean_ids)), replace=False)
    for idx in flips:
        did = clean_ids[int(idx)]
        results[did]["expected_disposition"] = "defer_to_human"
        results[did]["factors"] = ["borderline_designer_judgment"]
        results[did]["borderline_flip"] = True

    n = len(results)
    n_defer = sum(1 for v in results.values()
                  if v["expected_disposition"] == "defer_to_human")
    return {
        "task": "For each dispute, decide whether the pipeline should assemble a "
                "draft-for-submit bundle cleanly, or route to human review. "
                "Ground truth is a combination of several factors plus "
                "hand-flagged borderline cases — not a single stored column.",
        "summary": {
            "total": n,
            "assemble_clean": n - n_defer,
            "defer_to_human": n_defer,
            "defer_fraction": round(n_defer / n, 3),
        },
        "dispositions": dict(sorted(results.items())),
    }
