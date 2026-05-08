"""Section 7.4 — Scenario Modeling Contracts."""

from pydantic import Field

from .common import EntityId, EvidenceId, RunId, ScenarioId, StrictModel


class AssumptionSource(StrictModel):
    """Provenance for a single assumption in a scenario."""

    basis: str = Field(min_length=1)
    evidence_ids: list[EvidenceId] = Field(default_factory=list)
    calculation_id: str | None = None
    agent: str = Field(min_length=1)


class ScenarioDefinition(StrictModel):
    """A complete set of assumptions for one scenario.

    Agent provides assumptions; Scenario Modeling Engine executes calculations.
    """

    scenario_id: ScenarioId
    entity_id: EntityId
    run_id: RunId
    scenario_name: str = Field(pattern=r"^(bear|base|bull|stress|custom_.+)$")
    scenario_probability_weight: float | None = None  # null until calibrated
    horizon_years: int = Field(ge=1, le=30)

    # Assumption paths (year-by-year arrays)
    assumptions: dict[str, list[float] | float] = Field(min_length=1)

    # Each key in assumptions must have a source
    assumption_sources: dict[str, AssumptionSource] = Field(min_length=1)

    created_by_agent: str = Field(min_length=1)
    approved_by_critic: bool = False


class SensitivityRanking(StrictModel):
    """Impact of a single assumption on output value."""

    assumption: str = Field(min_length=1)
    impact_on_value_pct: float = Field(ge=0, le=1)


class ScenarioOutput(StrictModel):
    """Deterministic output from the Scenario Modeling Engine."""

    valuation_output_id: str = Field(min_length=1)
    scenario_id: ScenarioId
    method: str = Field(min_length=1)  # "dcf_fcfe", "dcf_fcff", "ddm", etc.
    projected_financials: dict[str, list[float]] = Field(min_length=1)
    terminal_value: float
    enterprise_value: float
    equity_value: float
    per_share_value: float
    sensitivity_table: dict | None = None
    assumption_sensitivity_ranking: list[SensitivityRanking] = Field(min_length=1)
    calculation_engine_version: str = Field(min_length=1)
    deterministic: bool = True  # Must always be True


class KeyAssumptionDisagreement(StrictModel):
    """Where our view diverges from the market on a key assumption."""

    assumption: str = Field(min_length=1)
    bear_value: str
    base_value: str
    bull_value: str
    market_implied: str
    my_view: str
    this_is_the_variant: bool = False


class ScenarioMatrix(StrictModel):
    """A set of scenarios for one entity, with price decomposition."""

    scenario_matrix_id: str = Field(min_length=1)
    entity_id: EntityId
    scenarios: list[dict] = Field(min_length=3)  # At least bear/base/bull
    current_price: float = Field(gt=0)
    implied_scenario_weights: dict | None = None
    key_assumption_disagreements: list[KeyAssumptionDisagreement] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Driver Tree — revenue/cost decomposition
# ---------------------------------------------------------------------------

class DriverNode(StrictModel):
    """A single node in a revenue/cost driver decomposition tree."""

    name: str = Field(min_length=1)
    formula: str = ""
    current_value: float | None = None
    unit: str = ""
    growth_assumption: str = ""
    children: list["DriverNode"] = Field(default_factory=list)


# Rebuild for self-referential type resolution
DriverNode.model_rebuild()


class DriverTree(StrictModel):
    """Structured decomposition of a key business metric into its drivers.

    Example: Revenue = DAU x Sessions/DAU x Ads/Session x CPM/1000
    """

    root_metric: str = Field(min_length=1)
    entity_id: str = Field(min_length=1)
    sector_pack_id: str = ""
    decomposition_formula: str = ""
    nodes: list[DriverNode] = Field(min_length=1)


# ---------------------------------------------------------------------------
# Variant Decomposition — waterfall of value gap drivers
# ---------------------------------------------------------------------------

class VariantContribution(StrictModel):
    """Contribution of a single driver to the total variant value gap."""

    driver: str = Field(min_length=1)  # e.g., "revenue_growth", "operating_margin"
    market_assumption: str = ""
    my_assumption: str = ""
    delta_value_per_share: float = 0.0
    pct_of_total_variant: float = 0.0


class VariantDecomposition(StrictModel):
    """Structured decomposition of the value gap between our view and market's.

    ΔV = ΔV_growth + ΔV_margin + ΔV_reinvestment + ...
    """

    entity_id: str = Field(min_length=1)
    current_price: float = 0.0
    base_case_value: float = 0.0
    total_variant_per_share: float = 0.0
    contributions: list[VariantContribution] = Field(min_length=1)
    decomposition_method: str = "sensitivity_partial_derivatives"
