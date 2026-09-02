"""The one synchronous, in-memory query other modules call.

Contract
--------
``lookup(entity: str) -> list[RequirementMatch]``

* ``entity`` is matched **exactly** (after a surrounding-whitespace strip) against
  the ``applies_to`` edges in the graph. Pass one of:
    - a reason-code category:  ``"fraud"``, ``"authorization"``,
      ``"processing_error"``, ``"consumer_dispute"``
      (the constants in ``src.common.reason_codes``)
    - an evidence-slot name:   ``"shipping_proof"``, ``"customer_communication"``,
      ``"refund_confirmation"``, ... (``src.common.reason_codes.EVIDENCE_SLOTS``)
    - the deadline key:        ``"respond_by"``
    - a model name:            ``"EvidenceBundle"``, ``"AccountRiskProfile"``
* An unknown / unmapped entity returns ``[]`` — never an error, never a guess.
* Results are sorted deterministically (regulation id, then requirement id).
* Each :class:`RequirementMatch` carries its parent :class:`Citation` and the
  standing non-claim disclaimer, so a caller that embeds it in an API response
  ships the grounding *and* the caveat together.

This is decision-support grounding, not a legal compliance certification.
"""

from __future__ import annotations

from src.compliance.graph import (
    NODE_TYPE_REQUIREMENT,
    REGULATIONS,
    REQUIREMENTS,
    build_compliance_graph,
)
from src.compliance.models import Citation, RequirementMatch


def _citation_for_requirement(requirement_id: str) -> Citation:
    parent_id = REQUIREMENTS[requirement_id]["regulation"]
    reg = REGULATIONS[parent_id]
    return Citation(
        regulation_id=parent_id,
        title=reg["title"],
        authority=reg["authority"],
        provision_ref=REQUIREMENTS[requirement_id]["provision_ref"],
    )


def lookup(entity: str) -> list[RequirementMatch]:
    """Return the curated requirements that apply to ``entity`` (see module docs)."""
    if not isinstance(entity, str):
        raise TypeError(f"entity must be a str, got {type(entity).__name__}")
    key = entity.strip()
    if not key:
        return []

    graph = build_compliance_graph()
    if key not in graph:
        return []

    matches: list[RequirementMatch] = []
    for predecessor in graph.predecessors(key):
        node = graph.nodes[predecessor]
        if node.get("node_type") != NODE_TYPE_REQUIREMENT:
            continue
        matches.append(
            RequirementMatch(
                requirement_id=predecessor,
                summary=node["summary"],
                matched_entity=key,
                applies_to=list(node["applies_to"]),
                citation=_citation_for_requirement(predecessor),
            )
        )

    matches.sort(key=lambda m: (m.citation.regulation_id, m.requirement_id))
    return matches


def citations_for(entity: str) -> list[Citation]:
    """Just the distinct parent citations for ``entity`` (convenience wrapper)."""
    seen: dict[str, Citation] = {}
    for match in lookup(entity):
        seen.setdefault(match.citation.regulation_id, match.citation)
    return list(seen.values())
