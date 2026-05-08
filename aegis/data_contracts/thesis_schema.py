"""Section 9.7 — Thesis Contract."""

from datetime import date

from pydantic import Field

from .common import (
    AccountingStandard,
    ClaimId,
    ConfidenceBucket,
    Direction,
    EdgeDurability,
    EdgeType,
    EntityId,
    MarketId,
    PublishingStatus,
    ResearchMode,
    RunId,
    SourceTier,
    ThesisId,
    StrictModel,
)


class EdgeClassification(StrictModel):
    """Classification of the information edge behind a thesis."""

    primary_edge_type: EdgeType
    edge_source: str = Field(min_length=1)
    edge_durability: EdgeDurability
    edge_decay_trigger: str = Field(min_length=1)


class Monitorable(StrictModel):
    """Something that must be actively monitored."""

    description: str = Field(min_length=1)
    check_frequency: str = Field(min_length=1)  # "daily", "weekly", "quarterly"
    data_source: str = Field(min_length=1)


class KillCriterion(StrictModel):
    """A condition that, if met, kills the thesis."""

    description: str = Field(min_length=1)
    threshold: str = Field(min_length=1)
    check_frequency: str = Field(min_length=1)


class ThesisContract(StrictModel):
    """The core output of a research run — a structured investment thesis."""

    thesis_id: ThesisId
    thesis_version: int = Field(ge=1)
    parent_thesis_id: ThesisId | None = None
    run_id: RunId
    entity_id: EntityId
    research_mode: ResearchMode
    related_entity_ids: list[EntityId] = Field(default_factory=list)

    # Core thesis content
    core_thesis: str = Field(min_length=1)
    why_now: str = Field(min_length=1)
    market_implied_story: str = Field(min_length=1)
    my_variant: str = Field(min_length=1)
    variant_magnitude: str = Field(min_length=1)

    # Edge
    edge_classification: EdgeClassification

    # Scenarios
    scenario_matrix_id: str = Field(min_length=1)
    bear_case_value: float | None = None
    base_case_value: float | None = None
    bull_case_value: float | None = None
    key_assumption_disagreement: str = Field(min_length=1)

    # Evidence & counter-thesis
    supporting_claim_ids: list[ClaimId] = Field(min_length=1)
    counter_thesis: str = Field(min_length=1)
    fragility_points: list[str] = Field(min_length=1)
    disconfirming_triggers: list[str] = Field(min_length=1)
    kill_criteria: list[KillCriterion] = Field(min_length=1)
    must_monitor: list[Monitorable] = Field(min_length=1)
    open_questions: list[str] = Field(default_factory=list)

    # Context
    macro_dependency: str = Field(min_length=1)
    sector_cycle_position: str = Field(min_length=1)
    management_quality_summary: str = Field(min_length=1)
    capital_allocation_assessment: str = Field(min_length=1)
    supply_chain_risk_summary: str = ""

    # Publishing
    publishing_status: PublishingStatus
    confidence_bucket: ConfidenceBucket
    confidence_basis: str = Field(default="not_calibrated")
    bias_check_status: str = Field(pattern=r"^(passed|warned|blocked)$")

    # Portfolio signal hints
    position_sizing_signal: str | None = None
    risk_interaction_flags: list[str] = Field(default_factory=list)
    data_source_tiers_used: list[SourceTier] = Field(min_length=1)
    markets_covered: list[MarketId] = Field(min_length=1)
    accounting_standards_used: list[AccountingStandard] = Field(min_length=1)
    thesis_horizon: str = Field(min_length=1)  # e.g. "12_months"
    review_date: date

    # Version tracking
    version_change_summary: str | None = None
    version_change_trigger: str | None = None
