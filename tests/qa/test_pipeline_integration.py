"""Cross-pipeline integration — the modules working *together*, not each alone.

* a full case: webhook -> classified -> assembled -> risk-enriched -> scored ->
  routed, with an end state coherent with the inputs;
* a reopen case: a `won` dispute that comes back at a later phase re-enters
  human review through the whole pipeline;
* compliance citations actually surface in BOTH the EvidenceBundle output and the
  scorer's recommendation output — not just that `lookup()` works in isolation.
"""

from __future__ import annotations

import pytest

from src.qa.harness import DISPUTE_NOW_EPOCH


# --------------------------------------------------------------------------- #
# 1. a full case flows end to end with a coherent end state
# --------------------------------------------------------------------------- #
def test_clean_fraud_case_flows_to_draft_for_submit(corpus):
    """disp_0010: Amex F24, chargeback phase, full evidence, low-risk account.
    Every module should agree: classify -> assemble complete -> score high ->
    draft-for-submit (still needs a human to dispatch)."""
    r = corpus.results["disp_0010"]
    assert r["category"] == "fraud"
    assert r["needs_manual_classification"] is False
    assert r["assembly_status"] == "complete"
    assert r["completeness"] == 1.0
    assert r["confidence_score"] is not None and r["confidence_score"] >= 0.72
    assert r["hard_gates"] == []
    assert r["queue"] == "draft_for_submit"
    assert r["recommended_action"] == "draft_for_submit"
    assert r["priority"] and r["priority"] > 0
    assert "draft-for-submit" in r["rationale"]
    # end-state coherence: a draft is NOT dispatched by the pipeline
    assert r["status"] in ("open", "under_review")


def test_every_dispute_has_a_coherent_terminal_routing(corpus):
    """No dispute falls through the pipeline: each ends in exactly one queue with
    a rationale, and manual-classification cases never carry a numeric score."""
    for did, r in corpus.results.items():
        assert r["queue"] in ("draft_for_submit", "human_review"), did
        assert r["rationale"], did
        if r["needs_manual_classification"]:
            assert r["queue"] == "human_review", did
            assert r["confidence_score"] is None, did
            assert r["recommended_action"] == "needs_manual_classification", did
        if r["queue"] == "draft_for_submit":
            assert r["hard_gates"] == [], (did, r["hard_gates"])
            assert r["confidence_score"] >= 0.72, did


# --------------------------------------------------------------------------- #
# 2. reopen case: won -> later phase re-enters review, through the whole pipeline
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("dispute_id", ["disp_0049", "disp_0091"])
def test_reopened_won_dispute_re_enters_review(corpus, dispute_id):
    """These are genuine reopen chains in the corpus: a terminal (`won`) dispute
    that ingestion advanced to a later phase. After the full pipeline runs, it
    must be back in human review, not resting on its old win."""
    r = corpus.results[dispute_id]
    assert r["reopen_count"] >= 1, r
    assert r["phase"] in ("pre_arbitration", "arbitration")
    assert r["queue"] == "human_review"
    assert "reopened_dispute_re_entered_review" in r["hard_gates"]
    # the late-phase clock gate also fires — a reopen at pre_arb+ is doubly gated
    assert "late_phase_escalation" in r["hard_gates"]


def test_corpus_has_the_expected_number_of_reopen_chains(corpus):
    reopened = [d for d, r in corpus.results.items() if r["reopen_count"] >= 1]
    assert len(reopened) == 6, reopened
    for d in reopened:
        assert corpus.results[d]["queue"] == "human_review"


# --------------------------------------------------------------------------- #
# 3. compliance citations surface in BOTH bundle and recommendation output
# --------------------------------------------------------------------------- #
def test_citations_surface_in_bundle_and_in_recommendation(corpus):
    """A consumer_dispute case with PII-bearing slots: the EvidenceBundle must
    carry DPDP citations per slot, AND the scorer's recommendation must carry the
    reason-category frameworks in its citation list and its rationale text."""
    picked = None
    for did, r in corpus.results.items():
        if (
            r["category"] == "consumer_dispute"
            and r["assembly_status"] in ("complete", "partial")
            and r["bundle_compliance_citations"]
        ):
            picked = did
            break
    assert picked, "no consumer_dispute case with citations in the corpus"
    r = corpus.results[picked]

    # (a) bundle side — DPDP data-minimisation / storage-limitation on PII slots
    bundle_cites = r["bundle_compliance_citations"]
    assert set(bundle_cites) & {"customer_communication", "shipping_proof", "billing_proof"}
    flat = [c for slot in bundle_cites.values() for c in slot]
    assert any("dpdp" in c["requirement_id"] for c in flat)
    assert all(c.get("disclaimer") for c in flat), "citation ships without its non-claim caveat"

    # (b) scorer side — the recommendation carries the category's frameworks
    entry_cites = r["entry_compliance_citations"]
    assert entry_cites, "recommendation carries no citations"
    titles = {c["citation"]["title"] for c in entry_cites if c.get("citation")}
    assert titles
    assert "applicable frameworks:" in r["rationale"]
    for t in titles:
        assert t in r["rationale"], t


def test_manual_classification_case_carries_no_fabricated_citations(corpus):
    for did, r in corpus.results.items():
        if r["needs_manual_classification"]:
            assert r["bundle_compliance_citations"] == {}, did
            # scorer still cites nothing category-specific (category is the
            # manual bucket, not in rc.CATEGORIES)
            assert r["entry_compliance_citations"] == [], did


# --------------------------------------------------------------------------- #
# 4. the run itself used the corpus's frozen clock (deadline gates are real)
# --------------------------------------------------------------------------- #
def test_run_used_the_frozen_corpus_clock(corpus):
    assert DISPUTE_NOW_EPOCH == 1_788_436_800  # 2026-09-03T12:00:00Z
    # at that clock, the urgent-deadline gate fires for at least a few cases
    urgent = [
        d for d, r in corpus.results.items()
        if "urgent_deadline_with_imperfect_evidence" in r["hard_gates"]
    ]
    assert urgent, "no case tripped the urgent-deadline gate — clock likely wrong"
