"""Section 16.3 — Comparison Matrix Contract."""

from datetime import datetime

from pydantic import Field

from .common import EntityId, StrictModel


class ComparisonDimension(StrictModel):
    """One dimension of cross-entity comparison."""

    dimension: str = Field(min_length=1)
    rankings: dict[str, int] = Field(min_length=2)  # entity_id -> rank
    values: dict[str, float] = Field(min_length=2)  # entity_id -> value


class RelativeValuation(StrictModel):
    """Relative valuation snapshot across entities."""

    metric: str = Field(min_length=1)
    values: dict[str, float] = Field(min_length=2)
    sector_median: float


class ComparisonMatrix(StrictModel):
    """Cross-entity comparison for multi-entity research."""

    comparison_id: str = Field(min_length=1)
    theme: str = Field(min_length=1)
    entity_ids: list[EntityId] = Field(min_length=2)
    comparison_timestamp: datetime
    dimensions: list[ComparisonDimension] = Field(min_length=1)
    relative_valuation: RelativeValuation
    cross_entity_risks: list[str] = Field(default_factory=list)
    top_picks: list[EntityId] = Field(default_factory=list)
    top_pick_rationale: str = ""
