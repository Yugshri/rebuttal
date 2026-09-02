"""Risk Graph Service — counterparty-risk enrichment.

Builds a per-account ``AccountRiskProfile`` from the synthetic transaction graph
(PageRank + betweenness, compared against each account's *own* rolling baseline)
and from COD/returns history. Exposed read-only via
``GET /accounts/{id}/risk-profile``; all graph computation happens in the
schedulable ``src.risk.batch.run_nightly_batch`` job, never on the request path.

This module is an enrichment signal for ``confidence-scorer-review`` — it does
not classify disputes, assemble evidence, or make the routing decision.
"""
