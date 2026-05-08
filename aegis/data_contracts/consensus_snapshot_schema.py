"""Section 9.3 — Consensus Snapshot Contract."""

from datetime import datetime

from pydantic import Field

from .common import (
    BatchId,
    Currency,
    DefinitionId,
    EntityId,
    MetricId,
    SourceTier,
    StrictModel,
)


class ConsensusSnapshot(StrictModel):
    """Point-in-time snapshot of sell-side consensus for a metric."""

    snapshot_id: str = Field(min_length=1)
    entity_id: EntityId
    snapshot_timestamp: datetime
    metric_id: MetricId
    definition_id: DefinitionId
    period: str = Field(min_length=1)  # e.g. "FY2026"
    period_type: str = Field(min_length=1)  # "annual" | "quarterly"
    consensus_mean: float
    consensus_median: float
    estimate_count: int = Field(ge=0)
    high_estimate: float
    low_estimate: float
    standard_deviation: float = Field(ge=0)
    revision_1w: float | None = None
    revision_1m: float | None = None
    revision_3m: float | None = None
    revision_6m: float | None = None
    unit: Currency
    source: str = Field(min_length=1)
    source_tier: SourceTier
    ingestion_batch_id: BatchId
