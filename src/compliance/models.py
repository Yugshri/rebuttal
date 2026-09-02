"""Return-shape Pydantic models for the compliance knowledge graph.

These are the objects ``evidence-assembler`` and ``confidence-scorer-review``
embed in their API responses, so the shape is deliberately small, flat and
self-describing.

Explicit non-claim (mirrored in ``docs/failure-taxonomy.md``): this module is
decision-support grounding and explainability. A returned :class:`Citation` says
"this is the named regulatory anchor that bears on this decision" — it is **not**
a statement that the system, or any recommendation it produces, is legally
compliant. That determination needs qualified legal/compliance review.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# Attached to every match so the non-claim travels with the data into whatever
# API response embeds it, instead of living only in a doc a reader may not open.
GROUNDING_DISCLAIMER = (
    "Regulatory grounding for explainability only. Identifies the applicable "
    "framework; not a legal compliance certification or legal advice."
)


class Citation(BaseModel):
    """The parent regulation a requirement hangs off."""

    regulation_id: str = Field(description="Stable node id, e.g. 'rbi_pa_pg_md'.")
    title: str = Field(description="Full name of the regulation / initiative.")
    authority: str = Field(description="Body that issues or administers it.")
    provision_ref: str | None = Field(
        default=None,
        description=(
            "A specific section/rule reference ONLY where verified against a "
            "primary source (indiacode.nic.in, rbi.org.in, meity.gov.in, "
            "prsindia.org). None means the citation is deliberately kept at "
            "whole-regulation level rather than risk a fabricated pinpoint."
        ),
    )


class RequirementMatch(BaseModel):
    """One curated requirement that applies to the queried entity."""

    requirement_id: str
    summary: str = Field(
        description="Plain-language description of the requirement, in our words."
    )
    matched_entity: str = Field(
        description="The exact entity string that was looked up."
    )
    applies_to: list[str] = Field(
        description="Every entity string this requirement is linked to."
    )
    citation: Citation
    disclaimer: str = GROUNDING_DISCLAIMER
