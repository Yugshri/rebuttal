"""DDL for ``data/external.db`` and a plain build-time writer.

``external.db`` is what, in a real deployment, would be Razorpay / issuer systems:
payments, orders, shipping, communications, addresses, per-account return
history, the counterparty transaction graph, and a per-payment evidence
availability map. The application only ever opens this file **read-only** (see
``src/common/db.py``). This module is the *build-time* seeder — it runs outside
the application's credential model on purpose, using a bare ``sqlite3`` writable
handle, exactly as the module docstring in ``src/common/db.py`` permits.

Schema is written Postgres-compatible: TEXT/INTEGER/REAL only, ISO-8601 strings
for timestamps, integer 0/1 for booleans, no SQLite-only features.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

# Ordered so foreign-key-ish references (payments <- orders <- shipments) read
# top-down. There are no hard FK constraints — the read side joins defensively
# and some rows are deliberately absent (see evidence incompleteness).
DDL: tuple[str, ...] = (
    # -- Counterparty transaction graph --------------------------------------
    """
    CREATE TABLE account_nodes (
        account_id        TEXT PRIMARY KEY,
        account_type      TEXT NOT NULL,   -- payroll | merchant | consumer | fringe
        cluster           TEXT NOT NULL,   -- coarse community label
        opened_at         TEXT NOT NULL,   -- ISO-8601
        home_city         TEXT NOT NULL,
        notes             TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE transaction_edges (
        txn_id            TEXT PRIMARY KEY,
        src_account       TEXT NOT NULL,
        dst_account       TEXT NOT NULL,
        amount            REAL NOT NULL,
        timestamp         TEXT NOT NULL,   -- ISO-8601, real per-edge time
        edge_kind         TEXT NOT NULL    -- payroll_credit | merchant_payment | p2p_transfer | fan_out
    )
    """,
    # -- Payments / orders / fulfilment ------------------------------------
    """
    CREATE TABLE payments (
        payment_id        TEXT PRIMARY KEY,
        order_id          TEXT NOT NULL,
        account_id        TEXT NOT NULL,   -- joins account_nodes.account_id
        amount            REAL NOT NULL,
        currency          TEXT NOT NULL DEFAULT 'INR',
        method            TEXT NOT NULL,   -- card | upi | netbanking
        card_network      TEXT,            -- visa | mastercard | amex | rupay | NULL for non-card
        card_last4        TEXT,
        auth_code         TEXT,            -- present iff authorised
        status            TEXT NOT NULL,   -- captured | refunded | failed
        created_at        TEXT NOT NULL,
        customer_email    TEXT NOT NULL,
        customer_ip       TEXT NOT NULL,
        device_fingerprint TEXT NOT NULL,
        three_ds          INTEGER NOT NULL DEFAULT 0   -- 3-D Secure completed
    )
    """,
    """
    CREATE TABLE orders (
        order_id          TEXT PRIMARY KEY,
        payment_id        TEXT NOT NULL,
        account_id        TEXT NOT NULL,
        item_category     TEXT NOT NULL,
        item_description  TEXT NOT NULL,
        price_point       REAL NOT NULL,
        quantity          INTEGER NOT NULL,
        is_cod            INTEGER NOT NULL DEFAULT 0,
        is_high_value     INTEGER NOT NULL DEFAULT 0,
        discount_used     INTEGER NOT NULL DEFAULT 0,
        order_date        TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE shipments (
        order_id          TEXT PRIMARY KEY,
        carrier           TEXT,
        tracking_number   TEXT,
        tracking_valid    INTEGER,          -- 1 valid, 0 invalid/unverifiable, NULL unknown
        shipped_at        TEXT,
        delivered_at      TEXT,
        delivery_status   TEXT,             -- delivered | refused | in_transit | lost | none
        signature_captured INTEGER,
        ship_to_address_id TEXT
    )
    """,
    """
    CREATE TABLE communications (
        comm_id           TEXT PRIMARY KEY,
        payment_id        TEXT NOT NULL,
        channel           TEXT NOT NULL,    -- email | sms | call | chat
        direction         TEXT NOT NULL,    -- inbound | outbound
        timestamp         TEXT NOT NULL,
        summary           TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE addresses (
        address_id        TEXT PRIMARY KEY,
        account_id        TEXT NOT NULL,
        kind              TEXT NOT NULL,    -- billing | shipping
        line1             TEXT NOT NULL,
        city              TEXT NOT NULL,
        pincode           TEXT NOT NULL,
        country           TEXT NOT NULL DEFAULT 'IN',
        high_return_density INTEGER NOT NULL DEFAULT 0  -- address is a known reship/return hotspot
    )
    """,
    # -- COD / returns fraud fields (per account) --------------------------
    """
    CREATE TABLE customer_return_history (
        account_id           TEXT PRIMARY KEY,
        account_age_days      INTEGER NOT NULL,
        segment              TEXT NOT NULL,   -- new | bronze | silver | gold
        total_orders_lifetime INTEGER NOT NULL,
        total_returns_lifetime INTEGER NOT NULL,
        return_rate_pct      REAL NOT NULL,
        delivery_refusals    INTEGER NOT NULL,
        previous_dispute_count INTEGER NOT NULL,
        multiple_accounts_flag INTEGER NOT NULL DEFAULT 0,
        refund_to_different_account INTEGER NOT NULL DEFAULT 0
    )
    """,
    # -- Per-payment evidence availability + match/mismatch flags ----------
    # This is NOT the assembled EvidenceBundle (evidence-assembler owns that).
    # It is the ground-level "does a real record exist to back this slot"
    # map plus the cheap match flags a real evidence system would compute.
    """
    CREATE TABLE evidence_availability (
        payment_id                  TEXT PRIMARY KEY,
        has_shipping_proof          INTEGER NOT NULL,
        has_billing_proof           INTEGER NOT NULL,
        has_cancellation_proof      INTEGER NOT NULL,
        has_customer_communication  INTEGER NOT NULL,
        has_proof_of_service        INTEGER NOT NULL,
        has_refund_confirmation     INTEGER NOT NULL,
        has_activity_log            INTEGER NOT NULL,
        billing_shipping_address_match INTEGER,   -- 1 match, 0 mismatch, NULL n/a (no shipment)
        email_domain_consistent     INTEGER NOT NULL,  -- email domain vs. historical pattern
        avs_match                   TEXT,        -- full | partial | no_match | not_checked
        completeness_bucket         TEXT NOT NULL  -- full | partial | severe  (documented rates)
    )
    """,
)


def create_external_db(path: Path) -> sqlite3.Connection:
    """Create a fresh ``external.db`` at ``path`` and return an open writable conn.

    Any existing file is removed first so a rebuild is a clean regeneration, not
    an append.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=DELETE")  # avoid -wal/-shm side files
    for statement in DDL:
        conn.execute(statement)
    conn.commit()
    return conn


def dump_all(path: Path) -> dict[str, list[tuple]]:
    """Every table as sorted row tuples — used by the determinism test."""
    conn = sqlite3.connect(str(path))
    try:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
        out: dict[str, list[tuple]] = {}
        for table in tables:
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()
            out[table] = sorted(rows, key=lambda r: tuple(str(x) for x in r))
        return out
    finally:
        conn.close()
