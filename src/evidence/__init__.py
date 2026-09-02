"""evidence-assembler — turns a classified ``DisputeCase`` into an ``EvidenceBundle``.

Public surface (import from here):

    from src.evidence import (
        init_system_tables,
        assemble_evidence,
        get_evidence_bundle,
        AssemblyResult,
        DisputeNotFound,
        EvidenceBundle,
    )

This module reads ``DisputeCase`` rows and the read-only external
order/shipping/comms store; it writes only its own ``EvidenceBundle`` rows to the
system store. It never fabricates a missing slot — ``missing`` is a first-class,
honestly-reported state.

``init_system_tables()`` is explicit and side-effect-free until called; importing
this package does not touch the database.
"""

from __future__ import annotations


def init_system_tables() -> None:
    """Create this module's table in the system store. Idempotent."""
    from src.common.db import system_engine
    from src.common.models_base import Base
    from src.evidence import models  # noqa: F401  (registers the table on Base)

    Base.metadata.create_all(
        system_engine(),
        tables=[models.EvidenceBundle.__table__],
    )


from src.evidence.assembler import (  # noqa: E402
    AssemblyResult,
    DisputeNotFound,
    assemble_evidence,
    get_evidence_bundle,
)
from src.evidence.models import EvidenceBundle  # noqa: E402
from src.evidence.store import EvidenceRecords, fetch_records  # noqa: E402

__all__ = [
    "init_system_tables",
    "assemble_evidence",
    "get_evidence_bundle",
    "AssemblyResult",
    "DisputeNotFound",
    "EvidenceBundle",
    "EvidenceRecords",
    "fetch_records",
]
