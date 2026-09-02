"""``AccountRiskProfile`` — the system store's precomputed counterparty-risk row.

This is the **only** thing ``risk-graph-service`` writes, and it writes it only
to the system store (``system.db``) via ``system_session()`` — never to the
read-only external store. It is a cache of the last nightly batch's output; the
API layer reads it and nothing else.

Framing (RBI Digital Payments Intelligence Platform / DPIP): DPIP points toward
network-level, shared mule-account intelligence rather than isolated internal
scores. This row's shape — a per-account profile with named graph signals and an
explicit baseline it was measured against — is deliberately structured so it
could be published into, or enriched from, such a shared layer later. See
``compliance-knowledge-graph`` requirement node ``dpip_shared_intelligence_alignment``
(RBI DPIP, cited at initiative level) — surfaced on the API response, not left as
an uncited comment.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from src.common.models_base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AccountRiskProfile(Base):
    """Precomputed graph + COD/returns risk signals for one account.

    Written by :func:`src.risk.batch.run_nightly_batch`, read by
    :func:`src.risk.service.get_risk_profile`. Postgres-compatible column types
    only (no SQLite-specific features).
    """

    __tablename__ = "account_risk_profile"

    account_id: Mapped[str] = mapped_column(String, primary_key=True)

    # --- Core graph signals (spec-named) ---------------------------------
    pagerank_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    betweenness_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    baseline_deviation: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    last_updated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    # --- Deviation explainability (how baseline_deviation was reached) ---
    deviation_band: Mapped[str] = mapped_column(String, nullable=False, default="low")
    pagerank_baseline_mean: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    betweenness_baseline_mean: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0
    )
    pagerank_deviation_z: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    betweenness_deviation_z: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0
    )
    illicit_counterparty_fraction: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0
    )
    peak_window_start: Mapped[str | None] = mapped_column(String, nullable=True)
    peak_window_end: Mapped[str | None] = mapped_column(String, nullable=True)
    windows_observed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    baseline_windows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    insufficient_history: Mapped[bool] = mapped_column(
        Integer, nullable=False, default=0
    )

    # --- Velocity / recency features (used alongside PageRank/betweenness) ---
    txn_count_recent_window: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    rolling_txn_velocity: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    days_since_last_txn: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    first_time_counterparty_rate: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0
    )
    fan_out_ratio: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    # --- COD / returns fraud signals (source: customer_return_history + addresses) ---
    return_rate_pct: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    lifetime_return_ratio: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0
    )
    delivery_refusals: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    previous_dispute_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    multiple_accounts_flag: Mapped[bool] = mapped_column(
        Integer, nullable=False, default=0
    )
    refund_to_different_account: Mapped[bool] = mapped_column(
        Integer, nullable=False, default=0
    )
    high_return_density_address: Mapped[bool] = mapped_column(
        Integer, nullable=False, default=0
    )
    account_age_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    customer_segment: Mapped[str] = mapped_column(String, nullable=False, default="unknown")
    returns_risk_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    returns_risk_band: Mapped[str] = mapped_column(
        String, nullable=False, default="low"
    )

    # --- Provenance ----------------------------------------------------
    computed_through: Mapped[str | None] = mapped_column(String, nullable=True)
    batch_run_id: Mapped[str | None] = mapped_column(String, nullable=True)

    def as_dict(self) -> dict:
        return {
            "account_id": self.account_id,
            "pagerank_score": self.pagerank_score,
            "betweenness_score": self.betweenness_score,
            "baseline_deviation": self.baseline_deviation,
            "deviation_band": self.deviation_band,
            "last_updated": self.last_updated.isoformat()
            if self.last_updated
            else None,
            "explain": {
                "pagerank_baseline_mean": self.pagerank_baseline_mean,
                "betweenness_baseline_mean": self.betweenness_baseline_mean,
                "pagerank_deviation_z": self.pagerank_deviation_z,
                "betweenness_deviation_z": self.betweenness_deviation_z,
                "illicit_counterparty_fraction": self.illicit_counterparty_fraction,
                "peak_window_start": self.peak_window_start,
                "peak_window_end": self.peak_window_end,
                "windows_observed": self.windows_observed,
                "baseline_windows": self.baseline_windows,
                "insufficient_history": bool(self.insufficient_history),
            },
            "velocity": {
                "txn_count_recent_window": self.txn_count_recent_window,
                "rolling_txn_velocity": self.rolling_txn_velocity,
                "days_since_last_txn": self.days_since_last_txn,
                "first_time_counterparty_rate": self.first_time_counterparty_rate,
                "fan_out_ratio": self.fan_out_ratio,
            },
            "cod_returns": {
                "return_rate_pct": self.return_rate_pct,
                "lifetime_return_ratio": self.lifetime_return_ratio,
                "delivery_refusals": self.delivery_refusals,
                "previous_dispute_count": self.previous_dispute_count,
                "multiple_accounts_flag": bool(self.multiple_accounts_flag),
                "refund_to_different_account": bool(self.refund_to_different_account),
                "high_return_density_address": bool(self.high_return_density_address),
                "account_age_days": self.account_age_days,
                "customer_segment": self.customer_segment,
                "returns_risk_score": self.returns_risk_score,
                "returns_risk_band": self.returns_risk_band,
            },
            "provenance": {
                "computed_through": self.computed_through,
                "batch_run_id": self.batch_run_id,
            },
        }


__all__ = ["AccountRiskProfile"]
