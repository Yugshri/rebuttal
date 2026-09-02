"""Unit tests for the compliance knowledge graph and its lookup contract.

Cross-module integration (that citations actually surface in evidence-assembler
and confidence-scorer-review output) is qa-evaluator's job, not this file's.
"""

from __future__ import annotations

import networkx as nx
import pytest

from src.common import reason_codes as rc
from src.compliance import (
    REGULATIONS,
    REQUIREMENTS,
    Citation,
    RequirementMatch,
    build_compliance_graph,
    citations_for,
    lookup,
)
from src.compliance.graph import (
    ENTITY_ACCOUNT_RISK_PROFILE,
    ENTITY_EVIDENCE_BUNDLE,
    NODE_TYPE_REGULATION,
    NODE_TYPE_REQUIREMENT,
    RESPOND_BY,
)

FIVE_REGULATIONS = {
    "pss_act_2007",
    "rbi_pa_pg_md",
    "dpip",
    "dpdp_act_2023",
    "consumer_protection_ecommerce_rules_2020",
}


# --- graph shape ----------------------------------------------------------
def test_exactly_the_five_regulation_nodes():
    assert set(REGULATIONS) == FIVE_REGULATIONS


def test_at_least_eight_requirement_nodes():
    assert len(REQUIREMENTS) >= 8


def test_graph_is_a_digraph_with_typed_nodes():
    g = build_compliance_graph()
    assert isinstance(g, nx.DiGraph)
    regs = [n for n, d in g.nodes(data=True) if d.get("node_type") == NODE_TYPE_REGULATION]
    reqs = [n for n, d in g.nodes(data=True) if d.get("node_type") == NODE_TYPE_REQUIREMENT]
    assert set(regs) == FIVE_REGULATIONS
    assert len(reqs) == len(REQUIREMENTS)


def test_every_requirement_has_a_governing_regulation():
    g = build_compliance_graph()
    for req_id in REQUIREMENTS:
        parents = [
            p
            for p in g.predecessors(req_id)
            if g.nodes[p].get("node_type") == NODE_TYPE_REGULATION
        ]
        assert len(parents) == 1, req_id


def test_regulation_summaries_are_present_and_paraphrased():
    g = build_compliance_graph()
    for reg_id in REGULATIONS:
        node = g.nodes[reg_id]
        assert node["summary"].strip()
        assert node["authority"].strip()
        assert node["relevance"].strip()
        # guard against pasted verbatim legal text markers
        assert "hereby" not in node["summary"].lower()


def test_applies_to_edges_only_use_fixed_vocabulary():
    known = (
        set(rc.CATEGORIES)
        | set(rc.EVIDENCE_SLOTS)
        | {RESPOND_BY, ENTITY_EVIDENCE_BUNDLE, ENTITY_ACCOUNT_RISK_PROFILE}
    )
    for spec in REQUIREMENTS.values():
        assert set(spec["applies_to"]) <= known


def test_no_fabricated_pinpoint_citations():
    """Provision refs exist only for the regs/requirements verified vs. primary
    sources; the RBI Master Direction and DPIP stay at regulation level."""
    assert REGULATIONS["rbi_pa_pg_md"]["provision_ref"] is None
    assert REGULATIONS["dpip"]["provision_ref"] is None
    for req_id, spec in REQUIREMENTS.items():
        ref = spec["provision_ref"]
        if spec["regulation"] in {"rbi_pa_pg_md", "dpip"}:
            assert ref is None, req_id
        if ref is not None:
            assert any(ch.isdigit() for ch in ref)


# --- lookup: one example per reason-code category ------------------------
@pytest.mark.parametrize(
    "category",
    [rc.FRAUD, rc.AUTHORIZATION, rc.PROCESSING_ERROR, rc.CONSUMER_DISPUTE],
)
def test_lookup_returns_citations_for_every_reason_code_category(category):
    matches = lookup(category)
    assert matches, f"no compliance grounding for category {category!r}"
    for m in matches:
        assert isinstance(m, RequirementMatch)
        assert m.matched_entity == category
        assert category in m.applies_to
        assert isinstance(m.citation, Citation)
        assert m.citation.regulation_id in FIVE_REGULATIONS
        assert m.citation.title and m.citation.authority
        assert m.disclaimer  # non-claim travels with the data


def test_consumer_dispute_pulls_ecommerce_rules():
    reg_ids = {m.citation.regulation_id for m in lookup(rc.CONSUMER_DISPUTE)}
    assert "consumer_protection_ecommerce_rules_2020" in reg_ids
    assert "rbi_pa_pg_md" in reg_ids


def test_fraud_pulls_dpip_alignment():
    req_ids = {m.requirement_id for m in lookup(rc.FRAUD)}
    assert "dpip_shared_intelligence_alignment" in req_ids


# --- lookup: evidence slots (one PII-bearing, one not) ------------------
def test_pii_bearing_slot_gets_dpdp_data_minimisation():
    assert rc.SHIPPING_PROOF in rc.PII_BEARING_SLOTS
    req_ids = {m.requirement_id for m in lookup(rc.SHIPPING_PROOF)}
    assert "dpdp_data_minimisation" in req_ids
    assert "dpdp_storage_limitation" in req_ids


def test_customer_communication_gets_dpdp_grounding():
    reg_ids = {m.citation.regulation_id for m in lookup(rc.CUSTOMER_COMMUNICATION)}
    assert "dpdp_act_2023" in reg_ids


def test_non_pii_slot_gets_grounding_without_dpdp_minimisation():
    # refund_confirmation is not a PII-bearing slot
    assert rc.REFUND_CONFIRMATION not in rc.PII_BEARING_SLOTS
    matches = lookup(rc.REFUND_CONFIRMATION)
    assert matches
    req_ids = {m.requirement_id for m in matches}
    assert "dpdp_data_minimisation" not in req_ids
    assert {"papg_refund_original_method", "ecom_policy_disclosure"} <= req_ids


def test_explanation_letter_grounded_for_consumer_disclosure():
    req_ids = {m.requirement_id for m in lookup(rc.EXPLANATION_LETTER)}
    assert "ecom_policy_disclosure" in req_ids


# --- lookup: respond_by -------------------------------------------------
def test_respond_by_maps_to_turnaround_time_expectation():
    matches = lookup("respond_by")
    assert matches
    req_ids = {m.requirement_id for m in matches}
    assert "papg_turnaround_time" in req_ids
    assert all(m.citation.regulation_id in FIVE_REGULATIONS for m in matches)


# --- lookup: model-name entities --------------------------------------
def test_account_risk_profile_maps_to_dpip():
    reg_ids = {m.citation.regulation_id for m in lookup("AccountRiskProfile")}
    assert "dpip" in reg_ids


def test_evidence_bundle_maps_to_papg_and_dpdp():
    reg_ids = {m.citation.regulation_id for m in lookup("EvidenceBundle")}
    assert {"rbi_pa_pg_md", "dpdp_act_2023"} <= reg_ids


# --- lookup: contract edge cases -------------------------------------
def test_unknown_entity_returns_empty_list_not_error():
    assert lookup("not_a_real_entity") == []
    assert lookup("") == []
    assert lookup("  ") == []


def test_lookup_is_whitespace_tolerant_but_exact_match():
    assert lookup("  fraud  ") == lookup("fraud")
    # exact-match, not fuzzy: a near-miss string returns nothing
    assert lookup("frauds") == []
    assert lookup("FRAUD") == []


def test_lookup_results_are_deterministically_sorted():
    a = [m.requirement_id for m in lookup(rc.CONSUMER_DISPUTE)]
    b = [m.requirement_id for m in lookup(rc.CONSUMER_DISPUTE)]
    assert a == b
    keys = [(m.citation.regulation_id, m.requirement_id) for m in lookup(rc.CONSUMER_DISPUTE)]
    assert keys == sorted(keys)


def test_lookup_rejects_non_string():
    with pytest.raises(TypeError):
        lookup(None)  # type: ignore[arg-type]


def test_citations_for_dedupes_by_regulation():
    cites = citations_for(rc.CONSUMER_DISPUTE)
    ids = [c.regulation_id for c in cites]
    assert len(ids) == len(set(ids))


def test_requirement_match_is_json_serialisable():
    # evidence-assembler / confidence-scorer-review embed this in API responses
    payload = [m.model_dump() for m in lookup(rc.CONSUMER_DISPUTE)]
    assert payload
    assert payload[0]["citation"]["regulation_id"]
    assert payload[0]["disclaimer"]
