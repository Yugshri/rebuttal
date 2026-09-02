"""Defense-only boundary — real assertions against the real code.

CLAUDE.md's non-negotiable: the system must be *structurally* incapable of moving
money, freezing an account, or contacting a customer — "the credentials it holds
cannot do that", not "policy says no". Four checks:

1. A write through the external engine RAISES (it is opened read-only).
2. `RecommendedAction` has no submitted/dispatched member — the scoring path
   literally cannot emit one.
3. No payments/accounts/messaging HTTP client exists anywhere in `src/` except
   `src/common/llm.py` (which only talks to Sarvam for the explanation letter).
4. The dispatch transition (`dispatched = True` / status advance / `reviewed_by`)
   is written in exactly one place: `src/scoring/api.py`'s review handler.

Plus: the pipeline source never reads `data/heldout/`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from src.common import db
from src.common.models_base import RecommendedAction
from src.qa.harness import EXTERNAL_DB, corpus_env

SRC = Path(__file__).resolve().parents[2] / "src"

# The defense-only boundary constrains the *running system*. Two trees are
# deliberately outside it and excluded from the source scans:
#   * src/synthetic/  — the build-time data generator. It populates external.db
#     directly as a build step ("the data-seeding equivalent of receiving a read
#     replica", per src/common/db.py) and never runs in the request path.
#   * src/qa/         — this evaluator. Not shipped, not on the pipeline path.
_RUNTIME_PY = [
    py for py in SRC.rglob("*.py")
    if not py.relative_to(SRC).as_posix().startswith(("synthetic/", "qa/"))
]


# --------------------------------------------------------------------------- #
# 1. the external store cannot be written
# --------------------------------------------------------------------------- #
@pytest.fixture()
def external_engine(tmp_path):
    """The real, read-only external engine via the actual `src.common.db` code."""
    if not EXTERNAL_DB.exists():
        pytest.skip("data/external.db missing — run `python -m src.synthetic.build`")
    with corpus_env(tmp_path / "throwaway_system.db"):
        yield db.external_engine()


def test_external_engine_write_raises(external_engine):
    """Attempt an UPDATE against the read-only external store; assert it raises
    at the driver, not because of an app-level check."""
    with external_engine.connect() as conn:
        # a read works ...
        assert conn.execute(text("SELECT COUNT(*) FROM payments")).scalar() > 0
        # ... a write does not.
        with pytest.raises(OperationalError) as exc:
            conn.execute(text("UPDATE payments SET amount = amount + 1"))
            conn.commit()
    msg = str(exc.value).lower()
    assert "readonly" in msg or "read-only" in msg or "attempt to write" in msg


def test_external_engine_insert_and_delete_also_raise(external_engine):
    with external_engine.connect() as conn:
        for stmt in (
            "INSERT INTO payments (payment_id) VALUES ('x')",
            "DELETE FROM payments",
            "DROP TABLE payments",
        ):
            with pytest.raises(OperationalError):
                conn.execute(text(stmt))


# --------------------------------------------------------------------------- #
# 2. no submitted state in the recommendation enum
# --------------------------------------------------------------------------- #
def test_recommended_action_has_no_submitted_state():
    values = {e.value for e in RecommendedAction}
    assert values == {
        "draft_for_submit",
        "human_review",
        "needs_manual_classification",
    }
    assert not hasattr(RecommendedAction, "SUBMITTED")
    assert not hasattr(RecommendedAction, "DISPATCHED")
    for v in values:
        assert v in ("draft_for_submit",) or "submit" not in v
        assert "auto" not in v


# --------------------------------------------------------------------------- #
# 3. no money/account/messaging client anywhere in src/ (except the LLM wrapper)
# --------------------------------------------------------------------------- #
_HTTP_CLIENT_TOKENS = (
    "httpx", "requests.", "aiohttp", "urllib.request", "http.client",
    "boto3", "razorpay", "stripe", "twilio", "smtplib", "sendgrid",
)


def test_only_the_llm_wrapper_makes_outbound_http_calls():
    offenders: dict[str, list[str]] = {}
    for py in _RUNTIME_PY:
        rel = py.relative_to(SRC).as_posix()
        if rel == "common/llm.py":
            continue
        body = py.read_text(encoding="utf-8")
        hits = [
            tok for tok in _HTTP_CLIENT_TOKENS
            if re.search(rf"(?:import|from)\s+{re.escape(tok.rstrip('.'))}\b", body)
            or f"{tok}(" in body
            or (tok.endswith(".") and tok in body)
        ]
        if hits:
            offenders[rel] = hits
    assert not offenders, (
        f"outbound-HTTP / SDK usage outside src/common/llm.py: {offenders}"
    )


def test_llm_wrapper_only_targets_sarvam():
    body = (SRC / "common" / "llm.py").read_text(encoding="utf-8")
    # the only URL it can hit is the Sarvam chat-completions endpoint
    urls = re.findall(r"https?://[^\s\"']+", body)
    for u in urls:
        assert "sarvam.ai" in u, u
    # and it is used for exactly one thing: the explanation letter
    letter = (SRC / "evidence" / "letter.py").read_text(encoding="utf-8")
    assert "llm" in letter.lower()
    # nothing in scoring / risk / ingestion / compliance imports the llm wrapper
    for mod in ("scoring", "risk", "ingestion", "compliance"):
        for py in (SRC / mod).rglob("*.py"):
            assert "common.llm" not in py.read_text(encoding="utf-8"), py


# --------------------------------------------------------------------------- #
# 4. exactly one place writes the dispatch transition
# --------------------------------------------------------------------------- #
def test_dispatch_transition_is_written_only_in_scoring_api():
    needles = ("dispatched = True", "case.status = DisputeStatus", "case.reviewed_by =")
    writers: dict[str, list[str]] = {}
    for py in _RUNTIME_PY:
        body = py.read_text(encoding="utf-8")
        hits = [n for n in needles if n in body]
        if hits:
            writers[py.relative_to(SRC).as_posix()] = hits
    assert set(writers) == {"scoring/api.py"}, writers


def test_pipeline_module_stops_at_scored_and_queued():
    body = (SRC / "pipeline.py").read_text(encoding="utf-8")
    assert "dispatched = True" not in body
    assert "post_review" not in body
    assert ".status = " not in body


# --------------------------------------------------------------------------- #
# 5. the pipeline source never reads data/heldout/
# --------------------------------------------------------------------------- #
def test_no_pipeline_source_reads_heldout():
    tokens = ("data/heldout", "heldout/", "account_labels.json",
              "dispute_dispositions.json")
    offenders: dict[str, list[str]] = {}
    for py in SRC.rglob("*.py"):
        rel = py.relative_to(SRC).as_posix()
        # src/qa/ is the sanctioned reader; src/synthetic/ WRITES the files at
        # build time and is not on the request/pipeline path.
        if rel.startswith("qa/") or rel.startswith("synthetic/"):
            continue
        body = py.read_text(encoding="utf-8")
        hits = [t for t in tokens if t in body]
        if hits:
            offenders[rel] = hits
    assert not offenders, f"pipeline source references held-out data: {offenders}"
