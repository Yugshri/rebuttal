"""The curated India regulatory / policy knowledge graph.

Small, hand-curated, static, in-memory. Two node types in a ``networkx.DiGraph``:

* **regulation** nodes  (5) — the named authority anchors.
* **requirement** nodes (11) — specific, load-bearing provisions, each linked
  ``governs`` from its parent regulation and ``applies_to`` one or more entity
  strings drawn *verbatim* from ``src/common/reason_codes.py`` and
  ``src/common/models_base.py`` (reason-code category names, evidence-slot names,
  the ``respond_by`` timing key, and the model names ``EvidenceBundle`` /
  ``AccountRiskProfile``). Exact-match, never fuzzy.

Sourcing rule (see the agent spec and ``docs/failure-taxonomy.md``): summaries are
paraphrased in our own words, never copied legal text. A ``provision_ref`` is set
only where the section/rule was verified against a primary/official source;
otherwise the citation stays at whole-regulation level rather than invent a
pinpoint.

Primary sources consulted for the provision refs used below:
* Payment and Settlement Systems Act, 2007 — indiacode.nic.in (Act 51 of 2007),
  s. 4 (no payment system may operate without RBI authorisation).
* Digital Personal Data Protection Act, 2023 — meity.gov.in (Act 22 of 2023),
  s. 6(1) (consent — and the data collected under it — limited to what is
  necessary for the specified purpose),
  s. 8(7) (erase personal data on consent withdrawal or once the specified
  purpose is no longer being served, whichever is earlier).
* Consumer Protection (E-Commerce) Rules, 2020 — Department of Consumer Affairs;
  r. 4 (grievance officer, 48-hour acknowledgement, ~1-month redressal),
  rr. 5-6 (disclosure of return / refund / exchange / cancellation / warranty
  terms by e-commerce entities and sellers).
The RBI PA-PG Master Direction and the DPIP initiative are cited at
regulation/initiative level only.
"""

from __future__ import annotations

from functools import lru_cache

import networkx as nx

from src.common import reason_codes as rc

NODE_TYPE_REGULATION = "regulation"
NODE_TYPE_REQUIREMENT = "requirement"

EDGE_GOVERNS = "governs"
EDGE_APPLIES_TO = "applies_to"

# Entity string used by confidence-scorer-review's deadline logic. Not defined as
# a constant in common/ (it is a DisputeCase column name), so pin it here so the
# edge keys on the exact string other modules pass in.
RESPOND_BY = "respond_by"

# Model names other modules ask about by name.
ENTITY_EVIDENCE_BUNDLE = "EvidenceBundle"
ENTITY_ACCOUNT_RISK_PROFILE = "AccountRiskProfile"


# --- Regulation nodes --------------------------------------------------------
REGULATIONS: dict[str, dict[str, str | None]] = {
    "pss_act_2007": {
        "title": "Payment and Settlement Systems Act, 2007",
        "authority": "Parliament of India; administered by the Reserve Bank of India",
        "provision_ref": "s. 4",
        "summary": (
            "The statute that makes the RBI the designated authority for the "
            "regulation and supervision of payment systems in India and bars any "
            "entity from operating a payment system without RBI authorisation. It "
            "is the root legal power under which the RBI payment-system "
            "directions referenced here are issued."
        ),
        "relevance": (
            "Establishes why the RBI can impose any dispute-handling, "
            "settlement, or data obligation on an entity like a payment "
            "aggregator at all — the foundation the other RBI items in this "
            "graph stand on."
        ),
    },
    "rbi_pa_pg_md": {
        "title": (
            "RBI Master Direction on Regulation of Payment Aggregators "
            "(consolidating the earlier Guidelines on Regulation of Payment "
            "Aggregators and Payment Gateways)"
        ),
        "authority": "Reserve Bank of India",
        "provision_ref": None,
        "summary": (
            "RBI's directions for payment aggregators covering dispute and "
            "chargeback handling, correct assignment of reason codes, turnaround "
            "times for failed transactions and refunds, the prohibition on "
            "storing card data, and escrow-based settlement. Consolidated into a "
            "Master Direction issued 15 September 2025 that supersedes the 2020 "
            "and 2021 guidelines."
        ),
        "relevance": (
            "Directly governs how an entity like Razorpay must handle disputes "
            "and chargebacks, the reason-code and evidence discipline expected, "
            "and the timelines the deadline logic tracks."
        ),
    },
    "dpip": {
        "title": "RBI Digital Payments Intelligence Platform (DPIP)",
        "authority": (
            "Reserve Bank of India (developed via the Reserve Bank Innovation "
            "Hub with participating banks)"
        ),
        "provision_ref": None,
        "summary": (
            "An RBI-led digital-public-infrastructure initiative for "
            "network-level payment-fraud intelligence sharing, announced in 2024 "
            "on the recommendations of an RBI committee and being operationalised "
            "with a set of participating banks. It aims to pool fraud and "
            "mule-account signals across the ecosystem."
        ),
        "relevance": (
            "The named public-policy anchor for this system's counterparty risk "
            "enrichment: a shared fraud-graph direction that a per-account risk "
            "profile is meant to align with rather than stand alone as an "
            "isolated internal score."
        ),
    },
    "dpdp_act_2023": {
        "title": "Digital Personal Data Protection Act, 2023 (Act 22 of 2023)",
        "authority": (
            "Parliament of India; Ministry of Electronics and Information "
            "Technology"
        ),
        "provision_ref": None,
        "summary": (
            "India's general personal-data-protection statute. It limits "
            "processing of personal data to the specific purpose the person's "
            "consent covers, expects collection of only the data necessary for "
            "that purpose, and requires erasing personal data once the purpose "
            "is served and no law requires its retention."
        ),
        "relevance": (
            "Governs how this system may hold and use customer personal data "
            "inside evidence bundles — communication logs, names, addresses — "
            "and pushes evidence to be scoped to the specific dispute rather "
            "than a broad customer dossier."
        ),
    },
    "consumer_protection_ecommerce_rules_2020": {
        "title": "Consumer Protection (E-Commerce) Rules, 2020",
        "authority": (
            "Department of Consumer Affairs, Ministry of Consumer Affairs, Food "
            "and Public Distribution (under the Consumer Protection Act, 2019)"
        ),
        "provision_ref": None,
        "summary": (
            "Rules requiring e-commerce entities to disclose — clearly and "
            "before purchase — their return, refund, exchange, warranty and "
            "cancellation terms, and to run a grievance-redressal mechanism with "
            "a named officer, acknowledgement within 48 hours and redressal "
            "within about a month."
        ),
        "relevance": (
            "Sets what counts as legitimate merchant-side disclosure and record "
            "for consumer-dispute chargebacks — the refund/cancellation policy "
            "the cardholder accepted and the grievance history."
        ),
    },
}


# --- Requirement nodes -----------------------------------------------------
# Each: parent regulation id, plain-language summary, verified provision_ref (or
# None), and the exact entity strings it applies_to.
REQUIREMENTS: dict[str, dict] = {
    "pss_rbi_authorisation": {
        "regulation": "pss_act_2007",
        "provision_ref": "s. 4",
        "summary": (
            "No entity may operate a payment system in India without RBI "
            "authorisation, and RBI is the authority that supervises it. Every "
            "payment-aggregator dispute, settlement and data obligation this "
            "system leans on derives from this authorisation regime."
        ),
        "applies_to": [
            ENTITY_EVIDENCE_BUNDLE,
            ENTITY_ACCOUNT_RISK_PROFILE,
            RESPOND_BY,
        ],
    },
    "papg_dispute_evidence_handling": {
        "regulation": "rbi_pa_pg_md",
        "provision_ref": None,
        "summary": (
            "Payment aggregators must operate a dispute-resolution mechanism, "
            "assign correct reason codes, and respond to chargebacks raised "
            "against onboarded merchants. This system's evidence assembly exists "
            "to support that obligation; it does not discharge it (a human still "
            "dispatches every response)."
        ),
        "applies_to": [
            ENTITY_EVIDENCE_BUNDLE,
            rc.FRAUD,
            rc.AUTHORIZATION,
            rc.PROCESSING_ERROR,
            rc.CONSUMER_DISPUTE,
            rc.ACTIVITY_LOG,
            rc.EXPLANATION_LETTER,
        ],
    },
    "papg_turnaround_time": {
        "regulation": "rbi_pa_pg_md",
        "provision_ref": None,
        "summary": (
            "RBI prescribes turnaround times for failed-transaction reversals "
            "and expects disputes and refunds to be handled within defined "
            "timelines. The dispute-response deadline this system tracks is the "
            "chargeback-side analogue of that timeline discipline."
        ),
        "applies_to": [RESPOND_BY],
    },
    "papg_card_data_storage_limit": {
        "regulation": "rbi_pa_pg_md",
        "provision_ref": None,
        "summary": (
            "Payment aggregators and merchants must not store full card data. "
            "Evidence built for a dispute must rely on masked or tokenised "
            "identifiers, not retained card numbers."
        ),
        "applies_to": [rc.BILLING_PROOF, ENTITY_EVIDENCE_BUNDLE],
    },
    "papg_refund_original_method": {
        "regulation": "rbi_pa_pg_md",
        "provision_ref": None,
        "summary": (
            "Refunds must be routed back to the original payment instrument. A "
            "refund-confirmation record should evidence that the refund followed "
            "that path."
        ),
        "applies_to": [rc.REFUND_CONFIRMATION],
    },
    "dpdp_purpose_limitation": {
        "regulation": "dpdp_act_2023",
        "provision_ref": "s. 6(1)",
        "summary": (
            "Personal data may be processed only for the specific purpose the "
            "data principal consented to. Customer data pulled into a dispute "
            "file must be used to defend that dispute and not repurposed for "
            "unrelated profiling."
        ),
        "applies_to": [rc.CUSTOMER_COMMUNICATION, ENTITY_EVIDENCE_BUNDLE],
    },
    "dpdp_data_minimisation": {
        "regulation": "dpdp_act_2023",
        "provision_ref": "s. 6(1)",
        "summary": (
            "Collect only the personal data necessary for the stated purpose. An "
            "evidence bundle should carry the minimum customer communication, "
            "name and address needed for this dispute, not a broader customer "
            "profile."
        ),
        # PII-bearing slots per rc.PII_BEARING_SLOTS.
        "applies_to": [
            rc.CUSTOMER_COMMUNICATION,
            rc.SHIPPING_PROOF,
            rc.BILLING_PROOF,
        ],
    },
    "dpdp_storage_limitation": {
        "regulation": "dpdp_act_2023",
        "provision_ref": "s. 8(7)",
        "summary": (
            "Personal data should be erased once its purpose is served and no "
            "law requires retention. Dispute evidence containing PII needs a "
            "retention limit tied to the dispute lifecycle and any audit "
            "obligation, not indefinite storage."
        ),
        "applies_to": [
            rc.CUSTOMER_COMMUNICATION,
            rc.SHIPPING_PROOF,
            rc.BILLING_PROOF,
            ENTITY_EVIDENCE_BUNDLE,
        ],
    },
    "ecom_policy_disclosure": {
        "regulation": "consumer_protection_ecommerce_rules_2020",
        "provision_ref": "rr. 5-6",
        "summary": (
            "E-commerce entities and sellers must disclose return, refund, "
            "exchange, warranty and cancellation terms clearly before purchase. "
            "For a consumer-dispute chargeback the accepted policy and an "
            "explanation letter that references it are core merchant-side "
            "evidence."
        ),
        "applies_to": [
            rc.REFUND_CONFIRMATION,
            rc.EXPLANATION_LETTER,
            rc.CONSUMER_DISPUTE,
        ],
    },
    "ecom_grievance_redressal": {
        "regulation": "consumer_protection_ecommerce_rules_2020",
        "provision_ref": "r. 4",
        "summary": (
            "E-commerce entities must run a grievance-redressal mechanism with a "
            "named officer, acknowledge complaints within 48 hours and resolve "
            "them within about a month. That grievance trail is legitimate "
            "evidence of the merchant engaging the customer before a chargeback."
        ),
        "applies_to": [
            rc.CUSTOMER_COMMUNICATION,
            rc.CANCELLATION_PROOF,
            rc.CONSUMER_DISPUTE,
        ],
    },
    "dpip_shared_intelligence_alignment": {
        "regulation": "dpip",
        "provision_ref": None,
        "summary": (
            "The DPIP initiative points toward network-level, shared fraud and "
            "mule-account intelligence. This system's per-account risk profile "
            "and its interface shape are designed to align with that direction "
            "rather than stand as an isolated internal score."
        ),
        "applies_to": [ENTITY_ACCOUNT_RISK_PROFILE, rc.FRAUD],
    },
}


def _validate_entities() -> None:
    """Fail fast at import if an edge keys on a string not in the fixed vocab."""
    known = (
        set(rc.CATEGORIES)
        | set(rc.EVIDENCE_SLOTS)
        | {RESPOND_BY, ENTITY_EVIDENCE_BUNDLE, ENTITY_ACCOUNT_RISK_PROFILE}
    )
    for req_id, spec in REQUIREMENTS.items():
        if spec["regulation"] not in REGULATIONS:
            raise ValueError(f"{req_id}: unknown parent regulation {spec['regulation']}")
        unknown = set(spec["applies_to"]) - known
        if unknown:
            raise ValueError(f"{req_id}: applies_to has non-vocabulary entities {unknown}")


_validate_entities()


@lru_cache(maxsize=1)
def build_compliance_graph() -> nx.DiGraph:
    """Build (once) and return the directed regulation/requirement graph."""
    g = nx.DiGraph()

    for reg_id, spec in REGULATIONS.items():
        g.add_node(
            reg_id,
            node_type=NODE_TYPE_REGULATION,
            title=spec["title"],
            authority=spec["authority"],
            provision_ref=spec["provision_ref"],
            summary=spec["summary"],
            relevance=spec["relevance"],
        )

    for req_id, spec in REQUIREMENTS.items():
        g.add_node(
            req_id,
            node_type=NODE_TYPE_REQUIREMENT,
            provision_ref=spec["provision_ref"],
            summary=spec["summary"],
            applies_to=list(spec["applies_to"]),
        )
        g.add_edge(spec["regulation"], req_id, edge_type=EDGE_GOVERNS)
        for entity in spec["applies_to"]:
            g.add_edge(req_id, entity, edge_type=EDGE_APPLIES_TO)

    return g


def regulation_ids() -> list[str]:
    return list(REGULATIONS)


def requirement_ids() -> list[str]:
    return list(REQUIREMENTS)
