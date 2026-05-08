"""Section 9.2 — Atomic Accounting Fact Contract."""

from datetime import date, datetime

from pydantic import Field

from .common import (
    AccountingStandard,
    BatchId,
    Currency,
    EntityId,
    FactId,
    FiscalPeriod,
    MarketId,
    PeriodType,
    Sha256Hash,
    SignConvention,
    SourceTier,
    StatementType,
    StrictModel,
)


class AtomicAccountingFact(StrictModel):
    """A single as-reported or normalized accounting fact.

    Stores raw values in original units (never display-scaled like USD_bn).
    """

    fact_id: FactId
    entity_id: EntityId
    market_id: MarketId
    statement_type: StatementType
    source_concept_id: str = Field(min_length=1)
    canonical_concept_id: str = Field(min_length=1)
    period_start: date
    period_end: date
    fiscal_period: FiscalPeriod
    period_type: PeriodType
    value_raw: int | float  # Original unit value, never display-scaled
    unit: Currency
    scale_hint: int = 0  # 0 = units, 3 = thousands, 6 = millions, 9 = billions
    sign_convention: SignConvention
    accession_no: str = Field(min_length=1)
    accepted_at: datetime
    effective_at: datetime
    as_reported: bool
    restatement_flag: bool = False
    amendment_flag: bool = False
    accounting_standard: AccountingStandard
    parser_version: str = Field(min_length=1)
    source_ref: str = Field(min_length=1)
    source_hash: Sha256Hash
    fact_version: int = Field(ge=1)
    ingestion_batch_id: BatchId
    source_tier: SourceTier

    # Segment tagging — enables segment-level analysis for any entity
    segment_id: str | None = None  # e.g., "foa", "reality_labs", "cloud", "aws"
    segment_name: str | None = None  # Human-readable: "Family of Apps"
    geographic_id: str | None = None  # e.g., "na", "europe", "apac", "row"
