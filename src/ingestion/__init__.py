"""dispute-ingestion-router — the pipeline entry point.

Turns a raw Razorpay ``dispute.created`` webhook into a classified, phase-tracked
:class:`~src.ingestion.models.DisputeCase` in the system store.

Public surface (import from here):

    from src.ingestion import (
        init_system_tables,
        ingest_dispute_event,
        classify_reason_code,
        classification_table,
        get_dispute_case,
        get_phase_history,
        was_reopened,
    )

``init_system_tables()`` is an explicit, side-effect-free-until-called table
creator. Importing this package does not touch the database.
"""

from __future__ import annotations


def init_system_tables() -> None:
    """Create this module's tables in the system store. Idempotent."""
    from src.common.db import system_engine
    from src.common.models_base import Base
    from src.ingestion import models  # noqa: F401  (registers tables on Base)

    Base.metadata.create_all(
        system_engine(),
        tables=[
            models.DisputeCase.__table__,
            models.DisputePhaseHistory.__table__,
        ],
    )


from src.ingestion.classification import (  # noqa: E402
    ClassificationResult,
    ClassificationRow,
    classification_table,
    classify_reason_code,
)
from src.ingestion.service import (  # noqa: E402
    DisputeEventFields,
    IngestOutcome,
    PhaseHistoryEntry,
    WebhookPayloadError,
    extract_dispute_fields,
    get_dispute_case,
    get_phase_history,
    ingest_dispute_event,
    was_reopened,
)

__all__ = [
    "init_system_tables",
    "ingest_dispute_event",
    "extract_dispute_fields",
    "classify_reason_code",
    "classification_table",
    "get_dispute_case",
    "get_phase_history",
    "was_reopened",
    "ClassificationResult",
    "ClassificationRow",
    "DisputeEventFields",
    "IngestOutcome",
    "PhaseHistoryEntry",
    "WebhookPayloadError",
]
