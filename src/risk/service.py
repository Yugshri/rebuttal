"""Profile lookup — the read path behind ``GET /accounts/{id}/risk-profile``.

This module does **one** thing: fetch the latest precomputed
``AccountRiskProfile`` row from the system store. It never imports
``src.risk.batch`` or ``src.risk.graph`` and never touches the transaction graph.
Graph recomputation is the nightly batch's job (see ``batch.py``); if the API
could trigger it, a single lookup could spawn a whole-graph PageRank pass.
"""

from __future__ import annotations

from src.common.db import system_session
from src.risk.models import AccountRiskProfile

# RBI DPIP framing, cited at initiative level via compliance-knowledge-graph
# (requirement node: dpip_shared_intelligence_alignment). Surfaced on every
# response so the "shared mule-intelligence alignment" claim travels with the data.
try:  # compliance graph is a sibling module; degrade gracefully if absent
    from src.compliance import lookup as _compliance_lookup

    _DPIP_MATCHES = [m.model_dump() for m in _compliance_lookup("AccountRiskProfile")]
except Exception:  # pragma: no cover - defensive only
    _DPIP_MATCHES = []


class ProfileNotFound(Exception):
    """No precomputed profile exists for this account id yet."""


def get_risk_profile(account_id: str) -> dict:
    """Return the latest precomputed profile for ``account_id`` as a plain dict.

    Raises :class:`ProfileNotFound` if the batch has never scored this account.
    Read-only: a single indexed primary-key SELECT, no graph work.
    """
    with system_session() as session:
        row = session.get(AccountRiskProfile, account_id)
        if row is None:
            raise ProfileNotFound(account_id)
        payload = row.as_dict()
    payload["regulatory_grounding"] = _DPIP_MATCHES
    return payload


def profile_exists(account_id: str) -> bool:
    with system_session() as session:
        return session.get(AccountRiskProfile, account_id) is not None


__all__ = ["get_risk_profile", "profile_exists", "ProfileNotFound"]
