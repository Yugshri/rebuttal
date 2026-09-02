"""Compliance knowledge graph — curated India regulatory grounding.

Public surface (all other modules should import only from here):

    from src.compliance import lookup, RequirementMatch, Citation

``lookup(entity: str) -> list[RequirementMatch]`` is the single query function.
See ``src.compliance.lookup`` for the entity vocabulary and contract.

Non-claim: decision-support grounding and explainability only. Not a legal
compliance certification; does not replace legal/compliance review.
"""

from __future__ import annotations

from src.compliance.graph import (
    REGULATIONS,
    REQUIREMENTS,
    build_compliance_graph,
    regulation_ids,
    requirement_ids,
)
from src.compliance.lookup import citations_for, lookup
from src.compliance.models import GROUNDING_DISCLAIMER, Citation, RequirementMatch

__all__ = [
    "lookup",
    "citations_for",
    "Citation",
    "RequirementMatch",
    "GROUNDING_DISCLAIMER",
    "build_compliance_graph",
    "REGULATIONS",
    "REQUIREMENTS",
    "regulation_ids",
    "requirement_ids",
]
