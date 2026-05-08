"""Section 9.4 — Metric Definition Contract."""

from datetime import date

from pydantic import Field

from .common import AccountingStandard, DefinitionId, MetricId, StrictModel


class MetricFailureMode(StrictModel):
    """A known failure mode for a metric definition."""

    description: str = Field(min_length=1)


class MetricDefinition(StrictModel):
    """A versioned, governed metric definition in the Metric Registry.

    Definitions are versioned assets, not ad-hoc prompt text.
    """

    metric_name: MetricId
    display_name: str = Field(min_length=1)
    definition_id: DefinitionId
    definition_status: str = Field(pattern=r"^(approved|draft|deprecated)$")
    formula_version: int = Field(ge=1)
    expression: str = Field(min_length=1)
    allowed_inputs: list[str] = Field(min_length=1)
    disallowed_inputs: list[str] = Field(default_factory=list)
    unit_policy: str = Field(min_length=1)  # "currency" | "ratio" | "percentage" | "count"
    period_compatibility: list[str] = Field(min_length=1)  # ["quarterly", "annual"]
    accounting_standard_compatibility: list[AccountingStandard] = Field(min_length=1)
    cross_standard_notes: str = ""
    sector_applicability: list[str] = Field(default_factory=list)
    quality_tier: str = Field(pattern=r"^[A-D]$")
    publishable: bool
    common_failure_modes: list[str] = Field(default_factory=list)
    validation_rules: list[str] = Field(default_factory=list)
    effective_from: date
    effective_to: date | None = None
    supersedes: DefinitionId | None = None
    superseded_by: DefinitionId | None = None
