"""Section 8 — Entity Relationship Graph Contracts."""

from datetime import date

from pydantic import Field

from .common import EntityId, EvidenceId, StrictModel


class RevenueSignificance(StrictModel):
    """Revenue significance of a relationship between two entities."""

    a_revenue_from_b_pct: float | None = Field(default=None, ge=0, le=1)
    b_cost_from_a_pct: float | None = Field(default=None, ge=0, le=1)


class EntityRelationship(StrictModel):
    """A directed, evidence-backed relationship between two entities."""

    relationship_id: str = Field(min_length=1)
    entity_a: EntityId
    entity_b: EntityId
    relationship_type: str = Field(
        min_length=1,
        pattern=r"^(supply_chain|competition|ownership|capital_flow|macro_transmission)\..+$",
    )
    direction: str = Field(pattern=r"^(a_to_b|b_to_a|bidirectional)$")
    strength: str = Field(pattern=r"^(critical|strong|moderate|weak)$")
    revenue_significance: RevenueSignificance | None = None
    evidence_ids: list[EvidenceId] = Field(min_length=1)
    effective_from: date
    effective_to: date | None = None
    last_verified: date
    verification_source: str = Field(min_length=1)
