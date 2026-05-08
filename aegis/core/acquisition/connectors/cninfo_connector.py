"""China A-share Financial Data Connector.

Data source: yfinance (free) for financial statements and market data,
with future support for cninfo.com.cn API as Tier 1 regulatory source.

Capabilities:
- Fetch annual/quarterly financial statements (利润表/资产负债表/现金流量表)
- List available filings for an entity
- Support CAS (Chinese Accounting Standards) data extraction
- Market data via yfinance (.SS for Shanghai, .SZ for Shenzhen)

Requires company stock code (6-digit, e.g., "600519" for 贵州茅台).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from aegis.core.acquisition.models import (
    CostEstimate,
    DataQuery,
    FreshnessReport,
    RateLimitConfig,
    RawDataPacket,
    SchemaValidationResult,
)
from aegis.data_contracts.common import SourceTier

logger = logging.getLogger(__name__)


# Common A-share stock codes for demo
COMMON_A_SHARES: dict[str, dict[str, str]] = {
    "600519": {"name": "贵州茅台", "name_en": "Kweichow Moutai", "exchange": "SSE"},
    "000858": {"name": "五粮液", "name_en": "Wuliangye", "exchange": "SZSE"},
    "601318": {"name": "中国平安", "name_en": "Ping An Insurance", "exchange": "SSE"},
    "000333": {"name": "美的集团", "name_en": "Midea Group", "exchange": "SZSE"},
    "600036": {"name": "招商银行", "name_en": "China Merchants Bank", "exchange": "SSE"},
    "000651": {"name": "格力电器", "name_en": "Gree Electric", "exchange": "SZSE"},
    "601888": {"name": "中国中免", "name_en": "China Tourism Group Duty Free", "exchange": "SSE"},
    "300750": {"name": "宁德时代", "name_en": "CATL", "exchange": "SZSE"},
    "002594": {"name": "比亚迪", "name_en": "BYD", "exchange": "SZSE"},
    "600900": {"name": "长江电力", "name_en": "China Yangtze Power", "exchange": "SSE"},
    "601398": {"name": "工商银行", "name_en": "ICBC", "exchange": "SSE"},
    "600276": {"name": "恒瑞医药", "name_en": "Hengrui Medicine", "exchange": "SSE"},
    "000002": {"name": "万科A", "name_en": "China Vanke", "exchange": "SZSE"},
    "601012": {"name": "隆基绿能", "name_en": "LONGi Green Energy", "exchange": "SSE"},
    "688981": {"name": "中芯国际", "name_en": "SMIC", "exchange": "SSE-STAR"},
    "301358": {"name": "湖南裕能", "name_en": "Hunan Yuneng New Energy Battery Material", "exchange": "SZSE-ChiNext"},
}


@dataclass
class CASFinancialData:
    """Parsed Chinese Accounting Standards financial data."""

    stock_code: str
    company_name: str
    fiscal_year: int
    fiscal_period: str  # "annual", "Q1", "Q2", "Q3"
    report_type: str  # "年报", "季报", "半年报"

    # CAS concept → value mapping
    income_statement: dict[str, float] = field(default_factory=dict)
    balance_sheet: dict[str, float] = field(default_factory=dict)
    cash_flow: dict[str, float] = field(default_factory=dict)

    # CAS-specific items
    government_subsidies: float = 0.0
    related_party_transactions: list[dict] = field(default_factory=list)
    segment_data: dict[str, dict[str, float]] = field(default_factory=dict)


class CninfoConnector:
    """巨潮资讯 A-share financial data connector.

    Implements the SourceConnector protocol for China market data.

    In production, this connects to cninfo.com.cn API.
    Currently supports:
    - Entity lookup (stock code → company info)
    - Financial statement structure (CAS-formatted)
    - Mock data for testing/demo
    """

    source_id: str = "cninfo"
    source_tier: SourceTier = SourceTier.TIER_1
    market_id: str = "cn"
    license_type: str = "free"
    rate_limit: RateLimitConfig = RateLimitConfig(
        requests_per_second=5.0,
        requests_per_day=50_000,
        burst_size=10,
    )

    # Required CAS concepts for a valid annual report
    REQUIRED_CONCEPTS = {"营业收入", "净利润", "总资产"}

    def __init__(self, mock_data: dict[str, CASFinancialData] | None = None):
        self._mock_data = mock_data or {}

    def fetch(self, query: DataQuery) -> RawDataPacket:
        """Fetch financial data from cninfo.

        query.entity_id: Stock code (e.g., "600519")
        query.data_type: "filing" (financials) or "filing_list" (metadata only)
        query.period: "FY2024", "Q3_2024", etc.
        """
        if query.data_type == "filing_list":
            return self._fetch_filing_list(query)
        return self._fetch_financials(query)

    def _fetch_financials(self, query: DataQuery) -> RawDataPacket:
        """Fetch financial statements for a stock code and period.

        Priority (2026-04-15 refactor): mock > akshare (eastmoney) > yfinance.
        akshare hits eastmoney directly and updates within hours of a filing;
        yfinance typically lags 1-2 weeks on A-shares.
        """
        stock_code = query.entity_id
        fiscal_year = query.extra_params.get("fiscal_year")
        fiscal_period = query.extra_params.get("fiscal_period", "annual")

        data_source = "none"
        mock_key = f"{stock_code}_{fiscal_year}_{fiscal_period}"
        if mock_key in self._mock_data:
            cas_data = self._mock_data[mock_key]
            content = self._cas_to_dict(cas_data)
            data_source = "mock"
        else:
            # Primary: akshare (fastest refresh after filings)
            content = self._fetch_via_akshare(stock_code)
            if content and content.get("facts"):
                data_source = "akshare_eastmoney"
            else:
                logger.info(f"akshare unavailable for {stock_code}, falling back to yfinance")
                content = self._fetch_via_yfinance(stock_code, fiscal_year, fiscal_period)
                if content and content.get("facts"):
                    data_source = "yfinance"

        return RawDataPacket(
            source_id=self.source_id,
            source_tier=self.source_tier,
            market_id=self.market_id,
            query=query,
            fetched_at=datetime.now(timezone.utc),
            raw_content=content,
            content_hash="sha256:" + "0" * 64,
            content_type="json",
            response_metadata={
                "stock_code": stock_code,
                "fiscal_year": fiscal_year,
                "data_source": data_source,
            },
        )

    def _fetch_via_akshare(self, stock_code: str) -> dict | None:
        """Primary A-share path: akshare → eastmoney. Fresher than yfinance."""
        try:
            from aegis.core.acquisition.connectors.akshare_connector import AkShareConnector
        except ImportError:
            return None
        try:
            conn = AkShareConnector()
            data = conn.fetch(stock_code)
            if data is None or not data.facts:
                return None
        except Exception as e:
            logger.debug(f"akshare fetch exception for {stock_code}: {e}")
            return None

        company_registry = COMMON_A_SHARES.get(stock_code, {})
        company_name = data.company_name or company_registry.get("name") or stock_code

        return {
            "stock_code": stock_code,
            "company_name": company_name,
            "company_name_en": company_registry.get("name_en", ""),
            "facts": data.facts,
            "historical_by_year": data.historical,
            "segment_facts": {},
            "segment_detail": {},
            "fiscal_year": data.fiscal_year,
            "fiscal_period": "annual",
            "report_type": "年报",
            "market_data": data.market_data,
            "company_info": data.company_info,
            "source": "akshare_eastmoney",
        }

    def _fetch_via_yfinance(
        self, stock_code: str, fiscal_year: int | None, fiscal_period: str,
    ) -> dict | None:
        """Fetch financial data from yfinance for an A-share stock."""
        yf_ticker = self._to_yfinance_ticker(stock_code)
        if not yf_ticker:
            return None

        import ssl
        ssl._create_default_https_context = ssl._create_unverified_context
        import yfinance as yf

        _MAX_YF_RETRIES = 3
        _last_err: Exception | None = None

        for _yf_attempt in range(_MAX_YF_RETRIES):
            try:
                return self._yfinance_fetch_inner(
                    yf, stock_code, yf_ticker, fiscal_year, fiscal_period,
                )
            except ImportError:
                raise  # not retryable
            except (ConnectionError, OSError, TimeoutError) as e:
                _last_err = e
                delay = 1.0 * (2 ** _yf_attempt)  # 1s, 2s, 4s
                logger.warning(
                    f"⚠ yfinance attempt {_yf_attempt + 1}/{_MAX_YF_RETRIES} "
                    f"failed for {stock_code}: {e}, retrying in {delay:.0f}s..."
                )
                import time as _t
                _t.sleep(delay)
            except Exception as e:
                # Non-network errors: don't retry
                logger.warning(f"yfinance fetch failed for {stock_code}: {e}")
                return None

        logger.warning(
            f"yfinance exhausted {_MAX_YF_RETRIES} retries for {stock_code}: {_last_err}"
        )
        return None

    def _yfinance_fetch_inner(
        self, yf, stock_code: str, yf_ticker: str,
        fiscal_year: int | None, fiscal_period: str,
    ) -> dict | None:
        """Inner yfinance fetch logic (extracted for retry wrapper)."""
        try:
            stock = yf.Ticker(yf_ticker)
            info = stock.info or {}

            company = COMMON_A_SHARES.get(stock_code, {})
            company_name = company.get("name", info.get("shortName", stock_code))
            company_name_en = company.get("name_en", info.get("shortName", ""))

            # Extract financial statements
            facts: dict[str, float] = {}
            segment_facts: dict[str, dict[str, float]] = {}

            # Income statement
            fin = stock.financials
            if fin is not None and not fin.empty:
                col = fin.columns[0]  # Most recent year
                for idx in fin.index:
                    val = fin.at[idx, col]
                    if val is not None and val == val:  # Not NaN
                        facts[str(idx)] = float(val)

            # Balance sheet
            bs = stock.balance_sheet
            if bs is not None and not bs.empty:
                col = bs.columns[0]
                for idx in bs.index:
                    val = bs.at[idx, col]
                    if val is not None and val == val:
                        facts[str(idx)] = float(val)

            # Cash flow statement
            cf = stock.cashflow
            if cf is not None and not cf.empty:
                col = cf.columns[0]
                for idx in cf.index:
                    val = cf.at[idx, col]
                    if val is not None and val == val:
                        facts[str(idx)] = float(val)

            # Map yfinance field names → CAS concept names for CN adapter
            yf_to_cas = {
                "Total Revenue": "营业收入",
                "Operating Revenue": "营业收入",
                "Cost Of Revenue": "营业成本",
                "Reconciled Cost Of Revenue": "营业成本",
                "Gross Profit": "毛利润",
                "Operating Income": "营业利润",
                "Total Operating Income As Reported": "营业利润",
                "Pretax Income": "利润总额",
                "Net Income": "净利润",
                "Net Income From Continuing Operation Net Minority Interest": "归属于母公司所有者的净利润",
                "Basic EPS": "基本每股收益",
                "Diluted EPS": "稀释每股收益",
                "Basic Average Shares": "基本每股收益_shares",
                "Diluted Average Shares": "稀释每股收益_shares",
                "Total Assets": "资产总计",
                "Total Liabilities Net Minority Interest": "负债合计",
                "Stockholders Equity": "所有者权益合计",
                "Common Stock Equity": "归属于母公司所有者权益合计",
                "Cash And Cash Equivalents": "货币资金",
                "Cash Cash Equivalents And Short Term Investments": "货币资金",
                "Current Assets": "流动资产合计",
                "Current Liabilities": "流动负债合计",
                "Current Debt": "短期借款",
                "Long Term Debt": "长期借款",
                "Operating Cash Flow": "经营活动产生的现金流量净额",
                "Investing Cash Flow": "投资活动产生的现金流量净额",
                "Financing Cash Flow": "筹资活动产生的现金流量净额",
                "Capital Expenditure": "购建固定资产、无形资产和其他长期资产支付的现金",
                "Free Cash Flow": "自由现金流",
                "EBITDA": "EBITDA",
                "EBIT": "EBIT",
                "Reconciled Depreciation": "折旧摊销",
                "Research And Development": "研发费用",
                "Goodwill": "商誉",
                "Total Debt": "有息负债合计",
                "Minority Interest": "少数股东权益",
                "Interest Expense": "利息支出",
            }

            cas_facts: dict[str, float] = {}
            for yf_name, cas_name in yf_to_cas.items():
                if yf_name in facts:
                    cas_facts[cas_name] = facts[yf_name]

            # Also keep raw yfinance fields for the bridge
            all_facts = {**cas_facts}
            # Add yfinance info-level data
            for key in ("totalRevenue", "netIncomeToCommon", "totalDebt",
                        "totalCash", "operatingCashflow", "freeCashflow",
                        "returnOnEquity", "returnOnAssets", "debtToEquity",
                        "currentRatio", "grossMargins", "operatingMargins",
                        "profitMargins"):
                val = info.get(key)
                if val is not None:
                    all_facts[f"_yf_{key}"] = float(val)

            # Detect actual fiscal year from financials column header
            detected_fy = fiscal_year
            if fin is not None and not fin.empty:
                try:
                    detected_fy = fin.columns[0].year
                except Exception:
                    pass

            logger.info(f"yfinance: {len(all_facts)} facts for {stock_code} ({company_name})")

            return {
                "stock_code": stock_code,
                "company_name": company_name,
                "company_name_en": company_name_en,
                "exchange": company.get("exchange", info.get("exchange", "")),
                "fiscal_year": detected_fy or fiscal_year,
                "fiscal_period": fiscal_period,
                "report_type": "年报" if fiscal_period == "annual" else "季报",
                "facts": all_facts,
                "segment_facts": segment_facts,
                "fact_count": len(all_facts),
                "segment_count": 0,
                "data_source": "yfinance",
                "currency": "CNY",
                # Market data from info
                "market_data": {
                    "current_price": info.get("currentPrice", 0),
                    "market_cap": info.get("marketCap", 0),
                    "shares_outstanding": info.get("sharesOutstanding", 0),
                    "pe_trailing": info.get("trailingPE"),
                    "pe_forward": info.get("forwardPE"),
                    "dividend_yield": info.get("dividendYield"),
                    "beta": info.get("beta"),
                    "fifty_two_week_high": info.get("fiftyTwoWeekHigh"),
                    "fifty_two_week_low": info.get("fiftyTwoWeekLow"),
                    "currency": info.get("currency", "CNY"),
                },
            }

        except Exception:
            # Let all exceptions propagate to the retry wrapper
            raise

    @staticmethod
    def _to_yfinance_ticker(stock_code: str) -> str | None:
        """Convert 6-digit A-share code to yfinance ticker format.

        Shanghai (6xxxxx) → XXXXXX.SS
        Shenzhen (0xxxxx, 3xxxxx) → XXXXXX.SZ
        STAR Market (68xxxx) → XXXXXX.SS
        """
        code = stock_code.strip()
        if len(code) != 6 or not code.isdigit():
            return None
        if code.startswith("6"):
            return f"{code}.SS"
        elif code.startswith(("0", "3")):
            return f"{code}.SZ"
        return None

    def _fetch_filing_list(self, query: DataQuery) -> RawDataPacket:
        """Fetch list of available filings for a stock code."""
        stock_code = query.entity_id
        company = COMMON_A_SHARES.get(stock_code, {})

        # Would query cninfo API in production
        content = {
            "stock_code": stock_code,
            "company_name": company.get("name", "Unknown"),
            "exchange": company.get("exchange", ""),
            "filings": [],  # Would be populated by API
        }

        return RawDataPacket(
            source_id=self.source_id,
            source_tier=self.source_tier,
            market_id=self.market_id,
            query=query,
            fetched_at=datetime.now(timezone.utc),
            raw_content=content,
            content_hash="sha256:" + "0" * 64,
            content_type="json",
        )

    def validate_schema(self, raw: RawDataPacket) -> SchemaValidationResult:
        """Validate that CAS data contains required concepts."""
        content = raw.raw_content
        if content is None:
            return SchemaValidationResult(valid=False, errors=["No content"])

        facts = content.get("facts", {})
        if not facts:
            status = content.get("status", "")
            if status == "no_data_available":
                return SchemaValidationResult(
                    valid=False, errors=["No data available — API not connected"],
                )
            return SchemaValidationResult(valid=True, errors=[])

        errors = []
        for required in self.REQUIRED_CONCEPTS:
            if required not in facts:
                errors.append(f"Missing required CAS concept: {required}")

        return SchemaValidationResult(valid=len(errors) == 0, errors=errors)

    def check_freshness(self, entity_id: str) -> FreshnessReport:
        return FreshnessReport(
            entity_id=entity_id,
            source_id=self.source_id,
            last_fetched_at=None,
            latest_available_at=None,
            is_stale=False,
            staleness_reason="yfinance live data",
        )

    def get_cost_estimate(self, query: DataQuery) -> CostEstimate:
        return CostEstimate(
            source_id=self.source_id,
            estimated_api_calls=1,
            estimated_cost_usd=0.0,
            within_daily_limit=True,
        )

    def get_entity_info(self, stock_code: str) -> dict[str, str] | None:
        """Look up company info by stock code."""
        return COMMON_A_SHARES.get(stock_code)

    @staticmethod
    def _cas_to_dict(cas_data: CASFinancialData) -> dict:
        """Convert CASFinancialData to raw content dict."""
        all_facts = {}
        all_facts.update(cas_data.income_statement)
        all_facts.update(cas_data.balance_sheet)
        all_facts.update(cas_data.cash_flow)

        return {
            "stock_code": cas_data.stock_code,
            "company_name": cas_data.company_name,
            "fiscal_year": cas_data.fiscal_year,
            "fiscal_period": cas_data.fiscal_period,
            "report_type": cas_data.report_type,
            "facts": all_facts,
            "segment_facts": cas_data.segment_data,
            "government_subsidies": cas_data.government_subsidies,
            "related_party_transactions": cas_data.related_party_transactions,
            "fact_count": len(all_facts),
            "segment_count": len(cas_data.segment_data),
        }
