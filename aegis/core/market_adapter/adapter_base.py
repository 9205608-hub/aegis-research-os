"""Market Adapter Base — Section 6.

Global Market Adapter is a cross-cutting service that converts
different markets' data formats, accounting standards, and regulatory
systems into a unified internal representation.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date

from aegis.data_contracts.common import AccountingStandard, MarketId


@dataclass(frozen=True)
class MarketConfig:
    """Configuration for a specific market."""

    market_id: MarketId
    regulatory_body: str
    filing_format: str  # "XBRL_inline", "PDF_structured", "ESEF_XBRL", etc.
    accounting_standards: list[AccountingStandard]
    fiscal_year_convention: str  # "company_specific", "calendar_year"
    primary_currency: str
    trading_calendar: str
    disclosure_languages: list[str]
    connector_ids: list[str]
    special_considerations: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AdaptedData:
    """Data that has been processed through a market adapter."""

    market_id: MarketId
    original_format: str
    adapted: bool = True
    currency: str = ""
    accounting_standard: AccountingStandard | None = None
    fiscal_year_end: str = ""  # "MM-DD"
    adaptation_notes: list[str] = field(default_factory=list)


class MarketAdapter(ABC):
    """Abstract base class for market-specific adapters.

    Each market must have its own adapter. Unsupported markets
    must raise explicit errors, not silently process data.
    """

    @property
    @abstractmethod
    def config(self) -> MarketConfig: ...

    @abstractmethod
    def adapt_filing_data(self, raw_data: dict) -> tuple[dict, AdaptedData]:
        """Convert market-specific filing data to internal format.

        Returns:
            Tuple of (adapted data dict, adaptation metadata).
        """
        ...

    @abstractmethod
    def get_fiscal_year_end(self, entity_id: str) -> str:
        """Get fiscal year end date for an entity in this market.

        Returns "MM-DD" format.
        """
        ...

    @abstractmethod
    def get_default_accounting_standard(self) -> AccountingStandard:
        """Get the default accounting standard for this market."""
        ...

    def validate_market_data(self, data: dict) -> list[str]:
        """Validate that data conforms to this market's expectations.

        Returns list of validation errors (empty = valid).
        """
        errors = []
        if not data:
            errors.append("Empty data")
        return errors
