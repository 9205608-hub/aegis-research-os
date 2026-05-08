"""Tests for A-share connector activation.

Round 23 — A股 Connector 激活.
Tests cover:
- CninfoConnector yfinance ticker conversion
- CninfoConnector data fetching (mock + structure)
- CNMarketAdapter CAS concept mapping
- Orchestrator A-share ticker detection
- CAS → canonical concept bridge
"""

import pytest
from datetime import datetime, timezone

from conftest import require_network
from aegis.core.acquisition.connectors.cninfo_connector import (
    CninfoConnector,
    CASFinancialData,
    COMMON_A_SHARES,
)
from aegis.core.acquisition.models import DataQuery
from aegis.core.market_adapter.cn_adapter import CNMarketAdapter
from aegis.core.orchestrator.auto_research import AutoResearchOrchestrator


# ============================================================
# Ticker Conversion Tests
# ============================================================

class TestYFinanceTickerConversion:

    def test_shanghai_main_board(self):
        assert CninfoConnector._to_yfinance_ticker("600519") == "600519.SS"

    def test_shenzhen_main_board(self):
        assert CninfoConnector._to_yfinance_ticker("000858") == "000858.SZ"

    def test_chinext(self):
        """ChiNext (创业板) 300xxx → SZSE."""
        assert CninfoConnector._to_yfinance_ticker("300750") == "300750.SZ"

    def test_star_market(self):
        """STAR Market (科创板) 688xxx → SSE."""
        assert CninfoConnector._to_yfinance_ticker("688981") == "688981.SS"

    def test_invalid_code_returns_none(self):
        assert CninfoConnector._to_yfinance_ticker("AAPL") is None
        assert CninfoConnector._to_yfinance_ticker("12345") is None
        assert CninfoConnector._to_yfinance_ticker("") is None
        assert CninfoConnector._to_yfinance_ticker("1234567") is None


# ============================================================
# Orchestrator A-share Detection Tests
# ============================================================

class TestAShareDetection:

    def test_detects_6_digit_code(self):
        assert AutoResearchOrchestrator._is_a_share_ticker("600519") is True
        assert AutoResearchOrchestrator._is_a_share_ticker("000858") is True
        assert AutoResearchOrchestrator._is_a_share_ticker("300750") is True

    def test_detects_yfinance_suffixed(self):
        assert AutoResearchOrchestrator._is_a_share_ticker("600519.SS") is True
        assert AutoResearchOrchestrator._is_a_share_ticker("000858.SZ") is True

    def test_rejects_us_tickers(self):
        assert AutoResearchOrchestrator._is_a_share_ticker("META") is False
        assert AutoResearchOrchestrator._is_a_share_ticker("AAPL") is False
        assert AutoResearchOrchestrator._is_a_share_ticker("NVDA") is False
        assert AutoResearchOrchestrator._is_a_share_ticker("GOOGL") is False


# ============================================================
# CNINFO Connector Mock Data Tests
# ============================================================

class TestCninfoConnectorMockData:

    def test_fetch_with_mock_data(self):
        """Fetch with pre-loaded mock data should return CAS facts."""
        mock = CASFinancialData(
            stock_code="600519",
            company_name="贵州茅台",
            fiscal_year=2024,
            fiscal_period="annual",
            report_type="年报",
            income_statement={"营业收入": 178_576_000_000, "净利润": 90_027_000_000},
            balance_sheet={"资产总计": 320_000_000_000, "负债合计": 80_000_000_000},
            cash_flow={"经营活动产生的现金流量净额": 95_000_000_000},
        )
        connector = CninfoConnector(mock_data={"600519_2024_annual": mock})
        query = DataQuery(
            entity_id="600519", market_id="cn", data_type="filing",
            period="FY2024", filing_type="annual",
            extra_params={"fiscal_year": 2024, "fiscal_period": "annual"},
        )
        packet = connector.fetch(query)
        assert packet.raw_content is not None
        facts = packet.raw_content["facts"]
        assert facts["营业收入"] == 178_576_000_000
        assert facts["净利润"] == 90_027_000_000
        assert packet.raw_content["fact_count"] == 5

    def test_entity_lookup(self):
        connector = CninfoConnector()
        info = connector.get_entity_info("600519")
        assert info is not None
        assert info["name"] == "贵州茅台"
        assert info["name_en"] == "Kweichow Moutai"
        assert info["exchange"] == "SSE"

    def test_entity_lookup_unknown(self):
        connector = CninfoConnector()
        assert connector.get_entity_info("999999") is None


# ============================================================
# CNINFO Connector yfinance Integration Tests
# ============================================================

class TestCninfoYFinanceIntegration:

    def test_yfinance_fetch_returns_facts(self):
        """yfinance should return real financial data for a known A-share."""
        require_network()
        connector = CninfoConnector()
        content = connector._fetch_via_yfinance("600519", 2024, "annual")

        # This test requires network access — skip if it fails
        if content is None:
            pytest.skip("yfinance not available or network error")

        assert content["stock_code"] == "600519"
        assert content["company_name"] == "贵州茅台"
        assert content["data_source"] == "yfinance"
        assert content["currency"] == "CNY"

        facts = content["facts"]
        assert len(facts) > 10  # Should have many financial facts

        # Check CAS concept mapping
        assert "营业收入" in facts or "_yf_totalRevenue" in facts
        assert content["market_data"]["current_price"] > 0
        assert content["market_data"]["market_cap"] > 0

    def test_yfinance_fetch_unknown_code(self):
        """Unknown stock code should return None gracefully."""
        require_network()
        connector = CninfoConnector()
        content = connector._fetch_via_yfinance("999999", 2024, "annual")
        # May return None or empty facts depending on yfinance behavior
        if content is not None:
            assert content.get("facts") is not None


# ============================================================
# CN Market Adapter Tests
# ============================================================

class TestCNMarketAdapter:

    def test_adapt_cas_concepts(self):
        adapter = CNMarketAdapter()
        raw_data = {
            "营业收入": 178_576_000_000,
            "净利润": 90_027_000_000,
            "资产总计": 320_000_000_000,
            "负债合计": 80_000_000_000,
            "货币资金": 150_000_000_000,
            "经营活动产生的现金流量净额": 95_000_000_000,
            "研发费用": 2_000_000_000,
        }
        adapted, metadata = adapter.adapt_filing_data(raw_data)
        assert adapted["revenue"] == 178_576_000_000
        assert adapted["net_income"] == 90_027_000_000
        assert adapted["total_assets"] == 320_000_000_000
        assert adapted["cash_and_equivalents"] == 150_000_000_000
        assert adapted["cfo"] == 95_000_000_000
        assert metadata.currency == "CNY"
        assert metadata.fiscal_year_end == "12-31"

    def test_government_subsidy_flagged(self):
        adapter = CNMarketAdapter()
        raw_data = {
            "营业收入": 100_000_000_000,
            "政府补助": 500_000_000,
        }
        adapted, metadata = adapter.adapt_filing_data(raw_data)
        assert adapted["government_subsidy"] == 500_000_000
        assert any("Government subsidy" in n for n in metadata.adaptation_notes)

    def test_fiscal_year_end_always_dec31(self):
        adapter = CNMarketAdapter()
        assert adapter.get_fiscal_year_end("600519") == "12-31"
        assert adapter.get_fiscal_year_end("000858") == "12-31"

    def test_related_party_validation(self):
        adapter = CNMarketAdapter()
        errors = adapter.validate_market_data({
            "related_party_transactions": {"amount_ratio": 0.35},
        })
        assert any("Related party" in e for e in errors)


# ============================================================
# Common A-share Registry Tests
# ============================================================

class TestCommonAShares:

    def test_registry_coverage(self):
        """Registry should have key A-share blue chips."""
        assert "600519" in COMMON_A_SHARES  # Moutai
        assert "000858" in COMMON_A_SHARES  # Wuliangye
        assert "601318" in COMMON_A_SHARES  # Ping An
        assert "300750" in COMMON_A_SHARES  # CATL
        assert "002594" in COMMON_A_SHARES  # BYD

    def test_registry_fields(self):
        for code, info in COMMON_A_SHARES.items():
            assert "name" in info
            assert "name_en" in info
            assert "exchange" in info
            assert len(code) == 6
            assert code.isdigit()

    def test_exchange_mapping(self):
        # Shanghai stocks start with 6
        for code, info in COMMON_A_SHARES.items():
            if code.startswith("6"):
                assert info["exchange"] in ("SSE", "SSE-STAR")
            elif code.startswith(("0", "3")):
                assert info["exchange"] in ("SZSE", "SZSE-ChiNext")


# ============================================================
# Schema Validation Tests
# ============================================================

class TestCninfoSchemaValidation:

    def test_valid_cas_data(self):
        mock = CASFinancialData(
            stock_code="600519",
            company_name="贵州茅台",
            fiscal_year=2024,
            fiscal_period="annual",
            report_type="年报",
            income_statement={"营业收入": 178_576_000_000, "净利润": 90_027_000_000},
            balance_sheet={"总资产": 320_000_000_000},
        )
        connector = CninfoConnector(mock_data={"600519_2024_annual": mock})
        query = DataQuery(
            entity_id="600519", market_id="cn", data_type="filing",
            period="FY2024",
            extra_params={"fiscal_year": 2024, "fiscal_period": "annual"},
        )
        packet = connector._fetch_financials(query)
        result = connector.validate_schema(packet)
        assert packet.raw_content is not None
        assert packet.response_metadata["data_source"] == "mock"
        assert result.valid is True

    def test_empty_content(self):
        from aegis.core.acquisition.models import RawDataPacket
        connector = CninfoConnector()
        packet = RawDataPacket(
            source_id="cninfo", source_tier=1, market_id="cn",
            query=DataQuery(entity_id="999999", market_id="cn",
                            data_type="filing", period="FY2024"),
            fetched_at=datetime.now(timezone.utc),
            raw_content=None,
            content_hash="sha256:0",
            content_type="json",
        )
        result = connector.validate_schema(packet)
        assert result.valid is False
