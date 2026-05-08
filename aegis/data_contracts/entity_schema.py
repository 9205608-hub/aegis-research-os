"""Section 9.1 — Entity Contract."""

from datetime import date

from pydantic import Field

from .common import (
    AccountingStandard,
    EntityId,
    MarketId,
    StrictModel,
)


class EntityContract(StrictModel):
    """Master record for a research entity (company)."""

    entity_id: EntityId
    issuer_legal_name: str = Field(min_length=1)
    ticker: str = Field(min_length=1, max_length=20)
    exchange: str = Field(min_length=1, max_length=20)
    primary_security_id: str = Field(min_length=1)
    reporting_currency: str = Field(min_length=3, max_length=3)
    price_currency: str = Field(min_length=3, max_length=3)
    fiscal_year_end: str = Field(pattern=r"^\d{2}-\d{2}$")  # MM-DD
    accounting_standard: AccountingStandard
    market_id: MarketId
    sector_scheme: str
    sector: str
    industry_group: str
    sector_pack_id: str
    country_of_domicile: str = Field(min_length=2, max_length=2)
    country_of_primary_listing: str = Field(min_length=2, max_length=2)
    functional_currencies: list[str] = Field(min_length=1)
    dual_class_structure: bool = False
    vie_structure: bool = False
    state_owned: bool = False
    active_from: date
    active_to: date | None = None
    entity_flags: list[str] = Field(default_factory=list)

    # Segment definitions — enables segment-level analysis
    reportable_segments: list[dict] = Field(default_factory=list)
    # e.g., [{"segment_id": "foa", "name": "Family of Apps", "type": "operating"},
    #         {"segment_id": "rl", "name": "Reality Labs", "type": "operating"}]
    geographic_segments: list[dict] = Field(default_factory=list)
    # e.g., [{"geo_id": "na", "name": "North America"},
    #         {"geo_id": "europe", "name": "Europe"}]
