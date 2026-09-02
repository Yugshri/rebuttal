"""Synthetic data generation for the Track 02 AI Risk Manager.

This package produces *every* dataset the rest of the system builds against and is
evaluated against. It never makes a pipeline decision (no routing, assembly,
scoring or classification logic lives here) — it only emits data.

Artifacts produced by ``python -m src.synthetic.build``:

* ``data/external.db``        — the read-only "issuer/Razorpay-side" store
                                (``src/common/db.py::external_engine`` opens this).
* ``data/webhooks/*.json``    — synthetic ``dispute.created`` webhook payloads the
                                router replays.
* ``data/heldout/*.json``     — labelled ground truth. The normal pipeline path
                                NEVER reads this directory; only ``qa-evaluator``.
* ``data/README.md``          — written by ``build.py`` (calibration provenance).

Everything is reproducible from :data:`SEED`, defined here and nowhere else.
"""

from __future__ import annotations

# --- The one and only seed ---------------------------------------------------
# Every generator derives an independent child stream from this via
# ``src.synthetic.rng.streams``. Change this in exactly one place and the whole
# corpus regenerates deterministically. Value = the buildathon start date.
SEED: int = 20260902

# --- Simulated calendar ----------------------------------------------------
# The transaction graph is a real timestamped event log over this window, so
# risk-graph-service can derive velocity/recency features from the *sequence*
# rather than from two hand-placed snapshots.
GRAPH_WINDOW_START = "2026-05-01T00:00:00"
GRAPH_WINDOW_END = "2026-07-30T00:00:00"
GRAPH_WINDOW_DAYS = 90

# Day index (from GRAPH_WINDOW_START) at which planted mule accounts begin their
# behavioural shift. Kept as a single constant so the held-out labeller and the
# generator can't disagree about it.
MULE_SHIFT_DAY = 52

# "Now" for the dispute corpus. Disputes are created in the weeks before this and
# their ``respond_by`` clocks are measured against it. Matches the buildathon
# working window so "inside the 48h deadline" cases are genuinely urgent.
NOW = "2026-09-03T12:00:00"

__all__ = [
    "SEED",
    "GRAPH_WINDOW_START",
    "GRAPH_WINDOW_END",
    "GRAPH_WINDOW_DAYS",
    "MULE_SHIFT_DAY",
    "NOW",
]
