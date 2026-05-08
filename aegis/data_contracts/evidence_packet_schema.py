"""Section 9.5 — Evidence Packet Contract."""

from datetime import date, datetime

from pydantic import Field

from .common import (
    ClaimId,
    EntityId,
    EvidenceId,
    MarketId,
    Sha256Hash,
    SourceTier,
    StrictModel,
)


class EvidencePacket(StrictModel):
    """A structured, traceable evidence object extracted from a source document."""

    evidence_id: EvidenceId
    entity_id: EntityId
    market_id: MarketId
    source_type: str = Field(min_length=1)  # "10K", "10Q", "earnings_call", etc.
    source_ref: str = Field(min_length=1)
    source_hash: Sha256Hash
    source_date: date
    accepted_at: datetime
    period_relevance: str = Field(min_length=1)  # e.g. "FY2025"
    excerpt: str = Field(min_length=1)
    excerpt_language: str = Field(min_length=2, max_length=5)
    excerpt_embedding_id: str | None = None
    normalized_statement: str = Field(min_length=1)
    assertion_type: str = Field(min_length=1)
    stance: str = Field(pattern=r"^(supports|contradicts|neutral|ambiguous)$")
    applicability_conditions: list[str] = Field(default_factory=list)
    evidence_class: str = Field(min_length=1)  # "primary_filing", "transcript", etc.
    source_tier: SourceTier
    extraction_method: str = Field(min_length=1)  # "rule_then_model", "manual", etc.
    extractor_version: str = Field(min_length=1)
    linked_claim_ids: list[ClaimId] = Field(default_factory=list)

    # Segment tagging — allows evidence to be scoped to a specific segment
    segment_id: str | None = None  # e.g., "aws", "foa", "cloud"
    segment_name: str | None = None  # Human-readable
    geographic_id: str | None = None  # e.g., "na", "europe", "china"
