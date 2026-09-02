"""confidence-scorer-review — routing, the review queue, deadlines, the feedback log.

This module decides what happens to a dispute once it is classified,
evidence-assembled and risk-enriched. It **consumes** ``EvidenceBundle`` and
``AccountRiskProfile``; it does not recompute evidence or risk.

The human-in-the-loop guarantee, structurally: there is no code path — at any
confidence level — where a recommendation reaches a dispatched / submitted state
without a human posting ``POST /disputes/{id}/review`` first.
``src.common.models_base.RecommendedAction`` has no submitted member; only
``src.scoring.api.post_review`` writes ``DisputeCase.status`` /
``ReviewQueueEntry.dispatched``.

Public surface (import from here):

    from src.scoring import (
        init_system_tables,
        score_and_route,
        score_dispute,
        run_deadline_scan,
        record_outcome,
    )

``init_system_tables()`` is explicit and side-effect-free until called.
"""

from __future__ import annotations


def init_system_tables() -> None:
    """Create this module's tables in the system store. Idempotent."""
    from src.common.db import system_engine
    from src.common.models_base import Base
    from src.scoring import models  # noqa: F401  (registers tables on Base)

    Base.metadata.create_all(system_engine(), tables=list(models.ALL_TABLES))


from src.scoring.deadline import (  # noqa: E402
    DEADLINE_WINDOW_HOURS,
    DeadlineScanResult,
    get_active_deadline_flags,
    run_deadline_scan,
)
from src.scoring.outcome import (  # noqa: E402
    get_outcome_log,
    record_outcome,
    training_pairs,
)
from src.scoring.routing import (  # noqa: E402
    RoutingDecision,
    compute_priority,
    get_active_entry,
    get_draft_queue,
    get_review_queue,
    score_and_route,
)
from src.scoring.scorer import (  # noqa: E402
    CONFIDENCE_THRESHOLD,
    WEIGHT_EVIDENCE_COMPLETENESS,
    WEIGHT_RISK,
    DisputeNotFound,
    ScoreResult,
    gather_inputs,
    score_dispute,
)

__all__ = [
    "init_system_tables",
    "score_dispute",
    "gather_inputs",
    "score_and_route",
    "compute_priority",
    "get_active_entry",
    "get_review_queue",
    "get_draft_queue",
    "run_deadline_scan",
    "get_active_deadline_flags",
    "record_outcome",
    "get_outcome_log",
    "training_pairs",
    "RoutingDecision",
    "ScoreResult",
    "DeadlineScanResult",
    "DisputeNotFound",
    "CONFIDENCE_THRESHOLD",
    "WEIGHT_EVIDENCE_COMPLETENESS",
    "WEIGHT_RISK",
    "DEADLINE_WINDOW_HOURS",
]
