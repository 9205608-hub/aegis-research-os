"""Section 9.8 — Macro Snapshot Contract."""

from datetime import datetime

from pydantic import Field

from .common import BatchId, StrictModel


class MacroSnapshot(StrictModel):
    """Point-in-time snapshot of macroeconomic conditions for a region."""

    macro_snapshot_id: str = Field(min_length=1)
    snapshot_timestamp: datetime
    region: str = Field(min_length=1)  # "US", "CN", "EU", "JP", "Global"
    cycle_phase_estimate: str = Field(min_length=1)  # e.g. "late_expansion"

    # US-centric fields (nullable for non-US regions)
    fed_funds_rate: float | None = None
    us_10y_yield: float | None = None
    us_2y_yield: float | None = None
    yield_curve_slope_2s10s: float | None = None
    investment_grade_spread: float | None = None
    high_yield_spread: float | None = None
    vix: float | None = None

    # Activity
    pmi_manufacturing: float | None = None
    pmi_services: float | None = None

    # Inflation
    cpi_yoy: float | None = None
    core_pce_yoy: float | None = None

    # Labor
    unemployment_rate: float | None = None

    # Financial conditions
    financial_conditions_index: float | None = None
    usd_dxy: float | None = None

    # Forward-looking
    fed_dot_plot_terminal: float | None = None
    market_implied_cuts_12m: int | None = None

    # Signal summaries
    leading_indicators_signal: str | None = None
    credit_cycle_signal: str | None = None
    liquidity_signal: str | None = None

    # China-specific
    lpr_1y: float | None = None
    lpr_5y: float | None = None
    cn_pmi_official: float | None = None
    cn_pmi_caixin: float | None = None
    credit_pulse: float | None = None

    # Provenance
    source_ids: list[str] = Field(min_length=1)
    ingestion_batch_id: BatchId
