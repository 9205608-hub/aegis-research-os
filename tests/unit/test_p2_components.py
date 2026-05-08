"""Tests for P2 components: Market Data, Report Templates, China Connector."""

import pytest
from datetime import datetime, timezone
from pathlib import Path

from conftest import require_network
from aegis.core.acquisition.connectors.market_data_connector import (
    MarketDataConnector,
    MarketSnapshot,
    PricePoint,
)
from aegis.core.acquisition.connectors.cninfo_connector import (
    CninfoConnector,
    CASFinancialData,
    COMMON_A_SHARES,
)
from aegis.core.acquisition.models import DataQuery
from aegis.core.reports.templates.engine import (
    TemplateEngine,
    _format_number,
    _format_pct,
    _format_currency,
    _format_multiple,
)


# ============================================================
# Market Data Connector Tests
# ============================================================

class TestMarketSnapshot:

    def test_to_market_data_dict(self):
        snap = MarketSnapshot(
            ticker="META",
            current_price=585.0,
            market_cap=1_510_000_000_000,
            shares_outstanding=2_579_000_000,
        )
        d = snap.to_market_data_dict()
        assert d["current_price"] == 585.0
        assert d["market_cap"] == 1_510_000_000_000
        assert d["shares_outstanding"] == 2_579_000_000

    def test_snapshot_defaults(self):
        snap = MarketSnapshot(
            ticker="TEST", current_price=0, market_cap=0, shares_outstanding=0,
        )
        assert snap.pe_trailing is None
        assert snap.analyst_recommendation is None
        assert snap.source == "yahoo_finance"


class TestMarketDataConnector:

    def test_connector_attributes(self):
        c = MarketDataConnector()
        assert c.source_id == "yahoo_finance"
        assert c.source_tier == "tier_3"

    def test_cache_hit(self):
        """Second call within TTL should return cached data."""
        c = MarketDataConnector()
        # Pre-populate cache
        snap = MarketSnapshot(
            ticker="TEST", current_price=100.0,
            market_cap=1000, shares_outstanding=10,
        )
        c._cache["TEST"] = (snap, datetime.now(timezone.utc).timestamp())

        result = c.get_snapshot("TEST")
        assert result.current_price == 100.0  # From cache

    def test_multi_snapshot(self):
        """get_multi_snapshot should return dict of snapshots."""
        c = MarketDataConnector()
        # Pre-populate cache
        for t in ["A", "B"]:
            snap = MarketSnapshot(
                ticker=t, current_price=float(ord(t)),
                market_cap=1000, shares_outstanding=10,
            )
            c._cache[t] = (snap, datetime.now(timezone.utc).timestamp())

        result = c.get_multi_snapshot(["A", "B"])
        assert len(result) == 2
        assert "A" in result and "B" in result


# ============================================================
# Template Engine Tests
# ============================================================

class TestTemplateFormatters:

    def test_format_number_billions(self):
        assert _format_number(164_710_000_000) == "$164.7B"

    def test_format_number_millions(self):
        assert _format_number(27_928_000) == "$27.9M"

    def test_format_number_trillions(self):
        assert _format_number(1_500_000_000_000) == "$1.5T"

    def test_format_number_thousands(self):
        assert _format_number(5_400) == "$5.4K"

    def test_format_number_none(self):
        assert _format_number(None) == "N/A"

    def test_format_pct(self):
        assert _format_pct(0.421) == "42.1%"

    def test_format_pct_none(self):
        assert _format_pct(None) == "N/A"

    def test_format_currency(self):
        assert _format_currency(585.0) == "$585"

    def test_format_multiple(self):
        assert _format_multiple(9.0) == "9.0x"

    def test_format_multiple_none(self):
        assert _format_multiple(None) == "N/A"


class TestTemplateEngine:

    def test_engine_init(self):
        engine = TemplateEngine()
        templates = engine.list_templates()
        assert "investment_report" in templates
        assert "comparison_report" in templates

    def test_render_investment_report_md(self):
        engine = TemplateEngine()
        md = engine.render("investment_report", format="markdown", context={
            "entity": "meta_platforms",
            "run_id": "run_test",
            "status": "published",
            "confidence": "medium",
            "bias_status": "passed",
            "bear": 400.0,
            "base": 600.0,
            "bull": 800.0,
            "price": 585.0,
            "edge": {"type": "analytical", "durability": "medium_term",
                     "why_market_wrong": "Test", "decay_trigger": "Test"},
            "metrics": {"gross_margin": 0.83, "operating_margin": 0.42},
            "facts": {"revenue": 164_710_000_000, "net_income": 62_360_000_000},
            "agents": [{"name": "business_analyst", "observations": 3,
                        "inferences": 2, "counterarguments": 1,
                        "top_inferences": ["Strong growth"]}],
            "critics": [{"type": "logic_critic", "issues": 0,
                         "block": False, "risk": "low", "issue_details": []}],
            "dcf_projections": [],
            "sensitivity_rankings": [],
            "kill_criteria": [],
            "monitorables": [],
            "core_thesis": "AI monetization upside",
            "variant": "Market underprices AI capex returns",
        })

        assert "meta_platforms" in md
        assert "published" in md
        assert "$600" in md  # base case
        assert "AI monetization" in md
        assert "Business Analyst" in md

    def test_render_comparison_report_md(self):
        engine = TemplateEngine()
        md = engine.render("comparison_report", format="markdown", context={
            "theme": "Ad Platform",
            "entity_ids": ["meta_platforms", "googl"],
            "dimensions": [
                {"name": "gross_margin", "rankings": {"meta_platforms": 1, "googl": 2},
                 "values": {"meta_platforms": 0.83, "googl": 0.58}},
            ],
            "top_picks": ["meta_platforms"],
            "rationale": "Higher margins",
            "per_entity_metrics": {},
            "per_entity_dcf": {"meta_platforms": 600, "googl": 180},
            "relative_valuation": {
                "metric": "ev_to_revenue",
                "values": {"meta_platforms": 9.0, "googl": 6.3},
                "median": 7.65,
            },
            "risks": ["Concentration risk"],
        })

        assert "Ad Platform" in md
        assert "meta_platforms" in md
        assert "Concentration risk" in md


# ============================================================
# China Market Connector Tests
# ============================================================

class TestCninfoConnector:

    def test_entity_lookup(self):
        c = CninfoConnector()
        info = c.get_entity_info("600519")
        assert info is not None
        assert info["name"] == "贵州茅台"
        assert info["exchange"] == "SSE"

    def test_entity_lookup_unknown(self):
        c = CninfoConnector()
        assert c.get_entity_info("999999") is None

    def test_common_a_shares_coverage(self):
        assert len(COMMON_A_SHARES) >= 10
        assert "600519" in COMMON_A_SHARES  # 茅台
        assert "300750" in COMMON_A_SHARES  # 宁德时代
        assert "002594" in COMMON_A_SHARES  # 比亚迪

    def test_fetch_returns_data(self):
        """Fetching a known A-share should return real data via yfinance."""
        require_network()
        c = CninfoConnector()
        query = DataQuery(
            entity_id="600519", market_id="cn", data_type="filing",
            extra_params={"fiscal_year": 2024, "fiscal_period": "annual"},
        )
        packet = c.fetch(query)
        assert packet.raw_content is not None
        assert packet.raw_content["stock_code"] == "600519"
        # yfinance now returns real data — should have facts
        assert packet.raw_content.get("data_source") == "yfinance" or packet.raw_content.get("facts")

    def test_fetch_with_mock_data(self):
        """Mock data should be returned correctly."""
        mock = CASFinancialData(
            stock_code="600519",
            company_name="贵州茅台",
            fiscal_year=2024,
            fiscal_period="annual",
            report_type="年报",
            income_statement={
                "营业收入": 173_600_000_000,
                "净利润": 86_200_000_000,
            },
            balance_sheet={
                "总资产": 278_900_000_000,
            },
            government_subsidies=120_000_000,
        )
        c = CninfoConnector(mock_data={"600519_2024_annual": mock})
        query = DataQuery(
            entity_id="600519", market_id="cn", data_type="filing",
            extra_params={"fiscal_year": 2024, "fiscal_period": "annual"},
        )
        packet = c.fetch(query)

        assert packet.raw_content is not None
        facts = packet.raw_content["facts"]
        assert facts["营业收入"] == 173_600_000_000
        assert facts["净利润"] == 86_200_000_000
        assert packet.raw_content["government_subsidies"] == 120_000_000

    def test_validate_schema_with_data(self):
        mock = CASFinancialData(
            stock_code="600519", company_name="贵州茅台",
            fiscal_year=2024, fiscal_period="annual", report_type="年报",
            income_statement={"营业收入": 173_600_000_000, "净利润": 86_200_000_000},
            balance_sheet={"总资产": 278_900_000_000},
        )
        c = CninfoConnector(mock_data={"600519_2024_annual": mock})
        query = DataQuery(
            entity_id="600519", market_id="cn", data_type="filing",
            extra_params={"fiscal_year": 2024, "fiscal_period": "annual"},
        )
        packet = c.fetch(query)
        result = c.validate_schema(packet)
        assert result.valid is True

    def test_validate_schema_missing_concepts(self):
        mock = CASFinancialData(
            stock_code="600519", company_name="贵州茅台",
            fiscal_year=2024, fiscal_period="annual", report_type="年报",
            income_statement={"营业收入": 100},  # Missing 净利润 and 总资产
        )
        c = CninfoConnector(mock_data={"600519_2024_annual": mock})
        query = DataQuery(
            entity_id="600519", market_id="cn", data_type="filing",
            extra_params={"fiscal_year": 2024, "fiscal_period": "annual"},
        )
        packet = c.fetch(query)
        result = c.validate_schema(packet)
        assert result.valid is False
        assert len(result.errors) == 2  # Missing 净利润 + 总资产

    def test_fetch_filing_list(self):
        c = CninfoConnector()
        query = DataQuery(
            entity_id="600519", market_id="cn", data_type="filing_list",
        )
        packet = c.fetch(query)
        assert packet.raw_content is not None
        assert packet.raw_content["stock_code"] == "600519"

    def test_connector_attributes(self):
        c = CninfoConnector()
        assert c.source_id == "cninfo"
        assert c.market_id == "cn"

    def test_cost_estimate_free(self):
        c = CninfoConnector()
        query = DataQuery(entity_id="600519", market_id="cn", data_type="filing")
        cost = c.get_cost_estimate(query)
        assert cost.estimated_cost_usd == 0.0
