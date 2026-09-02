"""Session-scoped corpus run for the qa-evaluator suite.

The full pipeline is replayed over the committed webhook corpus exactly once
(the risk batch alone is ~2s); every qa test scores against that single run.
"""

from __future__ import annotations

import pytest

from src.qa.harness import EXTERNAL_DB, WEBHOOK_DIR, Heldout, evaluate, run_corpus


@pytest.fixture(scope="session")
def corpus(tmp_path_factory):
    if not EXTERNAL_DB.exists() or not WEBHOOK_DIR.exists():
        pytest.skip("run `.venv/Scripts/python.exe -m src.synthetic.build` first")
    system_db = tmp_path_factory.mktemp("qa_corpus") / "system.db"
    return run_corpus(system_db)


@pytest.fixture(scope="session")
def heldout() -> Heldout:
    return Heldout.load()


@pytest.fixture(scope="session")
def evaluation(corpus, heldout):
    return evaluate(corpus, heldout)
