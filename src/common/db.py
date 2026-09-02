"""Database access — and the defense-only boundary, enforced structurally.

The non-negotiable constraint (see CLAUDE.md): this system must be *structurally
incapable* of moving money, freezing an account, or contacting a customer. Not
"policy says no" — the credentials it holds cannot do it.

How that is enforced here, concretely:

* **Two separate databases.**
  - ``external.db`` holds the data that, in a real deployment, would live in
    Razorpay/issuer systems: payments, orders, shipping, communications, the
    transaction graph, returns history, addresses. The application only ever
    opens this file **read-only** (SQLite ``mode=ro`` URI + ``PRAGMA
    query_only=ON`` on every connection). Any INSERT/UPDATE/DELETE against it
    raises ``OperationalError`` at the driver level — there is no code path that
    can obtain a writable handle to it.
  - ``system.db`` holds only the system's *own* working state: dispute cases,
    assembled evidence, computed risk profiles, recommendations, human review
    decisions, deadline flags, the outcome log, and the audit log. The
    application has full read/write here.

* **There is no payments/accounts/messaging client anywhere in ``src/``.** No
  SDK, no base URL, no API key is loaded for anything that could move funds,
  change account status, or send a message to a customer. ``qa-evaluator``
  asserts this against the actual code, not against a comment.

* **Nothing auto-submits.** A dispute only reaches a submitted state through
  ``POST /disputes/{id}/review`` recording a human decision. The scoring path
  cannot write that transition (see ``src/common/models_base.py`` —
  ``RecommendedAction`` has no submitted state).

``synthetic-data-generator`` populates ``external.db`` directly as a build-time
step; that is the data-seeding equivalent of receiving a read replica, and it
runs outside the application's credential model on purpose.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

_DATA_DIR = Path(os.environ.get("TRACK02_DATA_DIR", "data")).resolve()

EXTERNAL_DB_PATH = Path(
    os.environ.get("TRACK02_EXTERNAL_DB", str(_DATA_DIR / "external.db"))
).resolve()
SYSTEM_DB_PATH = Path(
    os.environ.get("TRACK02_SYSTEM_DB", str(_DATA_DIR / "system.db"))
).resolve()

# read-only URI form: the OS/driver refuses writes before SQLAlchemy is involved.
EXTERNAL_DB_URL = f"sqlite:///file:{EXTERNAL_DB_PATH.as_posix()}?mode=ro&uri=true"
SYSTEM_DB_URL = f"sqlite:///{SYSTEM_DB_PATH.as_posix()}"

_external_engine: Engine | None = None
_system_engine: Engine | None = None


def _install_query_only_guard(engine: Engine) -> None:
    """Defense in depth: force ``PRAGMA query_only=ON`` on every connection.

    Even if ``external.db`` were somehow opened without ``mode=ro`` (e.g. a future
    backend swap to Postgres), this keeps the connection non-writable.
    """

    @event.listens_for(engine, "connect")
    def _set_query_only(dbapi_connection, _connection_record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA query_only=ON")
        finally:
            cursor.close()


def external_engine() -> Engine:
    """Lazily-created **read-only** engine for payment/order/dispute source data."""
    global _external_engine
    if _external_engine is None:
        if not EXTERNAL_DB_PATH.exists():
            raise FileNotFoundError(
                f"external data store not found at {EXTERNAL_DB_PATH}. Run the "
                "synthetic-data-generator build step first."
            )
        _external_engine = create_engine(EXTERNAL_DB_URL, future=True)
        _install_query_only_guard(_external_engine)
    return _external_engine


def system_engine() -> Engine:
    """Lazily-created read/write engine for the system's own tables."""
    global _system_engine
    if _system_engine is None:
        SYSTEM_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _system_engine = create_engine(SYSTEM_DB_URL, future=True)
    return _system_engine


# Session factories. The read-only one disables autoflush so ORM bookkeeping
# never attempts an implicit write against the read-only connection.
ExternalSession = sessionmaker(
    bind=None, autoflush=False, autocommit=False, expire_on_commit=False, future=True
)
SystemSession = sessionmaker(
    bind=None, autoflush=True, autocommit=False, expire_on_commit=False, future=True
)


@contextmanager
def read_only_session() -> Iterator[Session]:
    """A session over the read-only external store. Writes raise at the driver."""
    session = ExternalSession(bind=external_engine())
    try:
        yield session
    finally:
        session.close()


@contextmanager
def system_session() -> Iterator[Session]:
    """A read/write session over the system's own tables."""
    session = SystemSession(bind=system_engine())
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engines_for_tests() -> None:
    """Drop cached engines so a test can point the paths somewhere else."""
    global _external_engine, _system_engine
    for eng in (_external_engine, _system_engine):
        if eng is not None:
            eng.dispose()
    _external_engine = None
    _system_engine = None
