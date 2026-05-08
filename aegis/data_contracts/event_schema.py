"""Section 9.9 — Event Contract."""

from datetime import datetime

from pydantic import Field

from .common import EntityId, EventId, SourceTier, ThesisId, StrictModel


class EventContract(StrictModel):
    """A detected external event that may impact one or more entities."""

    event_id: EventId
    event_type: str = Field(min_length=1)  # "filing_8k", "earnings_release", etc.
    entity_id: EntityId
    event_timestamp: datetime
    detected_at: datetime
    event_category: str = Field(min_length=1)  # "material_disclosure", "market_event", etc.
    event_summary: str = Field(min_length=1)
    source_ref: str = Field(min_length=1)
    source_tier: SourceTier
    affected_thesis_ids: list[ThesisId] = Field(default_factory=list)
    cascade_entity_ids: list[EntityId] = Field(default_factory=list)
    triggered_actions: list[str] = Field(default_factory=list)
    processing_status: str = Field(pattern=r"^(pending|processing|completed|failed)$")
