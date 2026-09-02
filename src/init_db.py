"""Canonical system-store table creation.

Each module ships its own ``init_system_tables()`` for isolated unit tests. For
the wired-together app and the end-to-end pipeline we want a single call that
creates every module's tables at once, so import order and per-module bookkeeping
can't leave a gap. Every model hangs off the one shared
``src.common.models_base.Base``, so this is just "import all model modules, then
create_all".

Only touches the system store (read/write). The external store is never created
here — it is a build artifact of ``src.synthetic.build`` and is opened read-only.
"""

from __future__ import annotations


def init_all_tables() -> None:
    """Create every module's tables in the system store. Idempotent."""
    from src.common.db import system_engine
    from src.common.models_base import Base

    # Import for side effect: each registers its tables on the shared Base.
    from src.ingestion import models as _ingestion_models  # noqa: F401
    from src.evidence import models as _evidence_models  # noqa: F401
    from src.risk import models as _risk_models  # noqa: F401
    from src.scoring import models as _scoring_models  # noqa: F401

    Base.metadata.create_all(system_engine())
