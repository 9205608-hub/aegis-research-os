"""Common types and base models used across all data contracts."""

from datetime import date, datetime
from enum import Enum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    """Base model with strict validation for all data contracts."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        str_strip_whitespace=True,
    )


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class MarketId(str, Enum):
    US = "us"
    CN = "cn"
    HK = "hk"
    EU = "eu"
    JP = "jp"
    KR = "kr"
    IN = "in"
    GLOBAL = "global"


class AccountingStandard(str, Enum):
    US_GAAP = "US_GAAP"
    IFRS = "IFRS"
    CAS = "CAS"
    HKFRS = "HKFRS"
    J_GAAP = "J_GAAP"


class SourceTier(int, Enum):
    TIER_1 = 1  # Primary Regulatory Filings
    TIER_2 = 2  # Structured Market Data
    TIER_3 = 3  # Semi-Structured Research Material
    TIER_4 = 4  # Alternative & Supplementary


class PeriodType(str, Enum):
    DURATION = "duration"
    INSTANT = "instant"


class Severity(str, Enum):
    INFO = "info"
    WARN = "warn"
    BLOCK = "block"


class ConfidenceBucket(str, Enum):
    VERY_LOW = "very_low"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    VERY_HIGH = "very_high"


class PublishingStatus(str, Enum):
    DRAFT = "draft"
    UNDER_REVIEW = "under_review"
    PUBLISHED = "published"
    ACTIVE = "active"
    EXPIRED = "expired"
    KILLED = "killed"
    CONFIRMED = "confirmed"
    BLOCKED = "blocked"
    DOWNGRADED = "downgraded"


class ResearchMode(str, Enum):
    SINGLE_ENTITY = "single_entity"
    MULTI_ENTITY = "multi_entity"
    THEMATIC = "thematic"
    EVENT_IMPACT = "event_impact"
    SUPPLY_CHAIN = "supply_chain"
    PAIR_TRADE = "pair_trade"


class Direction(str, Enum):
    LONG = "long"
    SHORT = "short"
    NO_SIGNAL = "no_signal"


class EdgeType(str, Enum):
    ANALYTICAL = "analytical"
    INFORMATIONAL = "informational"
    BEHAVIORAL = "behavioral"
    STRUCTURAL = "structural"


class EdgeDurability(str, Enum):
    SHORT_TERM = "short_term"
    MEDIUM_TERM = "medium_term"
    LONG_TERM = "long_term"


class SBCTreatment(str, Enum):
    """How stock-based compensation is handled in valuation.

    EXPENSE_IN_FCF: SBC deducted from FCF as real economic cost; shares NOT diluted.
    DILUTION_ONLY: SBC NOT deducted from FCF; diluted share count used instead.
    BOTH_WITH_JUSTIFICATION: Both applied — requires explicit justification to avoid
        double-counting. Use only when the analyst can demonstrate the two adjustments
        address different economic effects.
    """

    EXPENSE_IN_FCF = "expense_in_fcf"
    DILUTION_ONLY = "dilution_only"
    BOTH_WITH_JUSTIFICATION = "both_with_justification"


class StatementType(str, Enum):
    INCOME = "income"
    BALANCE_SHEET = "balance_sheet"
    CASH_FLOW = "cash_flow"
    EQUITY = "equity"
    COMPREHENSIVE_INCOME = "comprehensive_income"


class SignConvention(str, Enum):
    COMPANY_REPORTED = "company_reported"
    STANDARDIZED_POSITIVE = "standardized_positive"


# ---------------------------------------------------------------------------
# Common field types
# ---------------------------------------------------------------------------

EntityId = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[a-z0-9_]+$")]
RunId = Annotated[str, Field(min_length=1, max_length=128)]
FactId = Annotated[str, Field(min_length=1, max_length=256)]
MetricId = Annotated[str, Field(min_length=1, max_length=128)]
DefinitionId = Annotated[str, Field(min_length=1, max_length=128)]
EvidenceId = Annotated[str, Field(min_length=1, max_length=256)]
JudgmentId = Annotated[str, Field(min_length=1, max_length=256)]
ThesisId = Annotated[str, Field(min_length=1, max_length=256)]
ScenarioId = Annotated[str, Field(min_length=1, max_length=256)]
EventId = Annotated[str, Field(min_length=1, max_length=256)]
ClaimId = Annotated[str, Field(min_length=1, max_length=256)]
BatchId = Annotated[str, Field(min_length=1, max_length=256)]
Sha256Hash = Annotated[str, Field(pattern=r"^sha256:[a-f0-9]{64}$")]
Currency = Annotated[str, Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")]
FiscalPeriod = Annotated[str, Field(pattern=r"^(FY|Q[1-4]|H[12])\d{4}$")]
