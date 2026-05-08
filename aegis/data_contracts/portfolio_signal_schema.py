"""Section 9.10 — Portfolio Signal Contract."""

from datetime import date

from pydantic import Field

from .common import (
    ConfidenceBucket,
    Direction,
    EdgeDurability,
    EdgeType,
    EntityId,
    PublishingStatus,
    StrictModel,
    ThesisId,
)


class CorrelationWarning(StrictModel):
    """Warning about correlation with another entity."""

    with_entity: EntityId
    correlation_type: str = Field(min_length=1)


class PortfolioSignal(StrictModel):
    """Standardized signal for portfolio management system consumption."""

    signal_id: str = Field(min_length=1)
    thesis_id: ThesisId
    thesis_version: int = Field(ge=1)
    entity_id: EntityId
    signal_type: str = Field(pattern=r"^(thesis_based|event_driven|rebalance)$")
    direction: Direction
    conviction_bucket: ConfidenceBucket
    variant_magnitude: str = Field(min_length=1)
    edge_type: EdgeType
    edge_durability: EdgeDurability
    suggested_sizing_tier: str = Field(
        pattern=r"^(full_position|standard_position|starter_position|no_position)$"
    )
    scenario_upside_pct: float | None = None
    scenario_downside_pct: float | None = None
    risk_reward_ratio: float | None = None
    risk_flags: list[str] = Field(default_factory=list)
    correlation_warnings: list[CorrelationWarning] = Field(default_factory=list)
    thesis_horizon: str = Field(min_length=1)
    review_date: date
    data_quality_tier: str = Field(pattern=r"^[A-D]$")
    publishing_status: PublishingStatus
