"""Shared pytest fixtures.

Individual modules add their own fixtures in ``tests/``; this file only holds the
cross-cutting ones (isolated databases, deterministic seed) so module test files
don't each re-invent them.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# Deterministic seed for anything that generates data inside a test. The
# synthetic-data-generator owns the real seed used for the demo datasets; this is
# just a stable default for ad-hoc test fixtures.
TEST_SEED = 20260905


@pytest.fixture()
def isolated_dbs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point external.db / system.db at a tmp dir for the duration of a test."""
    external = tmp_path / "external.db"
    system = tmp_path / "system.db"
    monkeypatch.setenv("TRACK02_EXTERNAL_DB", str(external))
    monkeypatch.setenv("TRACK02_SYSTEM_DB", str(system))

    from src.common import db

    # Re-resolve module-level paths from the patched env, then drop cached engines.
    db.EXTERNAL_DB_PATH = external.resolve()
    db.SYSTEM_DB_PATH = system.resolve()
    db.EXTERNAL_DB_URL = f"sqlite:///file:{db.EXTERNAL_DB_PATH.as_posix()}?mode=ro&uri=true"
    db.SYSTEM_DB_URL = f"sqlite:///{db.SYSTEM_DB_PATH.as_posix()}"
    db.reset_engines_for_tests()

    yield {"external": external, "system": system}

    db.reset_engines_for_tests()


@pytest.fixture()
def seed() -> int:
    return TEST_SEED
