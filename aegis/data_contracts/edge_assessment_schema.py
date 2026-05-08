"""Section 15.2 — Edge Assessment Contract."""

from pydantic import Field

from .common import EdgeDurability, EdgeType, StrictModel, ThesisId


class EdgeAssessment(StrictModel):
    """Assessment of the information edge behind a thesis.

    Every published thesis must have an edge assessment.
    "Liking a good company" is not an edge.
    """

    edge_assessment_id: str = Field(min_length=1)
    thesis_id: ThesisId
    primary_edge_type: EdgeType
    secondary_edge_type: EdgeType | None = None
    edge_source: str = Field(min_length=1)
    edge_durability: EdgeDurability
    edge_decay_trigger: str = Field(min_length=1)
    edge_confidence: str = Field(pattern=r"^(low|medium|high)$")
    why_market_is_wrong: str = Field(min_length=1)
    what_would_change_my_mind: str = Field(min_length=1)
    edge_uniqueness: str = Field(min_length=1)
    historical_edge_hit_rate: float | None = None  # null until calibrated
