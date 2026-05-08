"""Data Acquisition Layer — shared models and protocols.

Section 5: Data Acquisition Layer is the system's sole data interface.
All data must pass through this layer before entering Truth Layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Literal, Protocol, runtime_checkable

from aegis.data_contracts.common import MarketId, SourceTier


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RateLimitConfig:
    """Rate limit configuration for a data source."""

    requests_per_second: float = 1.0
    requests_per_day: int = 10_000
    burst_size: int = 5


# ---------------------------------------------------------------------------
# Data query & response
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DataQuery:
    """A structured query to a data source."""

    entity_id: str
    market_id: str
    data_type: str  # "filing", "price", "consensus", "transcript", etc.
    period: str | None = None  # e.g. "FY2025", "Q3_2025"
    filing_type: str | None = None  # e.g. "10-K", "10-Q", "annual_report"
    date_from: datetime | None = None
    date_to: datetime | None = None
    extra_params: dict[str, Any] = field(default_factory=dict)


@dataclass
class RawDataPacket:
    """Raw data fetched from an external source, before processing."""

    source_id: str
    source_tier: SourceTier
    market_id: str
    query: DataQuery
    fetched_at: datetime
    raw_content: Any  # Could be dict, str, bytes depending on source
    content_hash: str  # sha256 of raw_content
    content_type: str  # "json", "xml", "xbrl", "pdf", "html"
    response_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FreshnessReport:
    """Report on data freshness for an entity."""

    entity_id: str
    source_id: str
    last_fetched_at: datetime | None
    latest_available_at: datetime | None
    is_stale: bool
    staleness_reason: str = ""


@dataclass(frozen=True)
class CostEstimate:
    """Estimated cost of a data query."""

    source_id: str
    estimated_api_calls: int
    estimated_cost_usd: float
    within_daily_limit: bool


# ---------------------------------------------------------------------------
# Schema validation result (for raw data)
# ---------------------------------------------------------------------------

@dataclass
class SchemaValidationResult:
    """Result of validating raw data against expected schema."""

    valid: bool
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Source Connector Protocol (Section 5.2)
# ---------------------------------------------------------------------------

@runtime_checkable
class SourceConnector(Protocol):
    """Protocol that all data source connectors must implement."""

    source_id: str
    source_tier: SourceTier
    market_id: str
    license_type: str
    rate_limit: RateLimitConfig

    def fetch(self, query: DataQuery) -> RawDataPacket: ...
    def validate_schema(self, raw: RawDataPacket) -> SchemaValidationResult: ...
    def check_freshness(self, entity_id: str) -> FreshnessReport: ...
    def get_cost_estimate(self, query: DataQuery) -> CostEstimate: ...
