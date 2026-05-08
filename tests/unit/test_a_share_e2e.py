"""Tests for A-share end-to-end compatibility fixes.

Round 23b — A股端到端兼容性修复.
Tests cover:
- FactBridge CAS field handling + currency tagging
- Historical data extraction from yfinance multi-year
- DCFInput currency field
- CN adapter → Bridge → meta_facts full flow
- Currency symbol in report generation context
"""

import pytest
from datetime import datetime, timezone

from conftest import require_network
from aegis.core.acquisition.fact_bridge import (
    FactNormalizationBridge,
    BridgeResult,
    CRITICAL_FIELDS,
)
from aegis.core.market_adapter.cn_adapter import CNMarketAdapter
from aegis.core.truth.scenario_engine.dcf_engine import DCFInput, DCFEngine
from aegis.core.orchestrator.auto_research import AutoResearchOrchestrator


# ============================================================
# FactBridge CAS Compatibility Tests
# ============================================================

class TestFactBridgeCAS:
    """Verify FactBridge handles CAS-adapted data correctly."""

    def test_cas_canonical_fields_pass_through(self):
        """CN adapter outputs canonical names (revenue, net_income) — bridge should accept."""
        bridge = FactNormalizationBridge()

        # Simulated CN adapter output (already mapped from CAS → canonical)
        adapted = {
            "revenue": 178_576_000_000,
            "net_income": 90_027_000_000,
            "total_assets": 320_000_000_000,
            "total_liabilities": 80_000_000_000,
            "cash_and_equivalents": 150_000_000_000,
            "operating_income": 119_000_000_000,
            "cfo": 95_000_000_000,
            "capex_ppe": 4_678_000_000,
            "cost_of_revenue": 13_789_000_000,
        }
        result = bridge.normalize(
            adapted_data=adapted,
            market_id="cn",
            currency="CNY",
        )
        assert result.meta_facts["revenue"] == 178_576_000_000
        assert result.meta_facts["net_income"] == 90_027_000_000
        assert result.meta_facts["operating_cash_flow"] == 95_000_000_000  # alias from cfo
        assert result.meta_facts["capex"] == 4_678_000_000  # alias from capex_ppe
        assert len(result.missing_fields) == 0

    def test_currency_tagged(self):
        """Bridge should tag currency on meta_facts."""
        bridge = FactNormalizationBridge()
        result = bridge.normalize(
            adapted_data={"revenue": 100, "net_income": 50, "total_assets": 200},
            currency="CNY",
            market_id="cn",
        )
        assert result.meta_facts["__currency"] == "CNY"
        assert result.meta_facts["__market_id"] == "cn"

    def test_usd_default(self):
        bridge = FactNormalizationBridge()
        result = bridge.normalize(
            adapted_data={"revenue": 100, "net_income": 50, "total_assets": 200},
        )
        assert result.meta_facts["__currency"] == "USD"
        assert result.meta_facts["__market_id"] == "us"

    def test_derived_fields_work_with_cas(self):
        """Derived fields (gross_profit, free_cash_flow, etc.) should compute from CAS data."""
        bridge = FactNormalizationBridge()
        adapted = {
            "revenue": 178_576_000_000,
            "cost_of_revenue": 13_789_000_000,
            "net_income": 90_027_000_000,
            "total_assets": 320_000_000_000,
            "total_liabilities": 80_000_000_000,
            "operating_income": 119_000_000_000,
            "cfo": 95_000_000_000,
            "capex_ppe": 4_678_000_000,
            "depreciation_amortization": 2_064_000_000,
            "cash_and_equivalents": 150_000_000_000,
            "current_assets": 200_000_000_000,
            "current_liabilities": 60_000_000_000,
        }
        result = bridge.normalize(adapted, market_id="cn", currency="CNY")

        # Derived fields
        assert "gross_profit" in result.meta_facts
        assert result.meta_facts["gross_profit"] == 178_576_000_000 - 13_789_000_000
        assert "free_cash_flow" in result.meta_facts
        assert result.meta_facts["free_cash_flow"] == 95_000_000_000 - 4_678_000_000
        assert "ebitda" in result.meta_facts
        assert "nwc" in result.meta_facts

    def test_cn_adapter_to_bridge_flow(self):
        """Full flow: CAS facts → CN adapter → Bridge → meta_facts."""
        adapter = CNMarketAdapter()

        cas_facts = {
            "营业收入": 178_576_000_000,
            "净利润": 90_027_000_000,
            "资产总计": 320_000_000_000,
            "负债合计": 80_000_000_000,
            "货币资金": 150_000_000_000,
            "营业利润": 119_000_000_000,
            "经营活动产生的现金流量净额": 95_000_000_000,
            "购建固定资产、无形资产和其他长期资产支付的现金": 4_678_000_000,
            "营业成本": 13_789_000_000,
            "流动资产合计": 200_000_000_000,
            "流动负债合计": 60_000_000_000,
        }

        adapted, metadata = adapter.adapt_filing_data(cas_facts)
        assert adapted["revenue"] == 178_576_000_000
        assert adapted["cfo"] == 95_000_000_000

        bridge = FactNormalizationBridge()
        result = bridge.normalize(adapted, market_id="cn", currency="CNY")

        assert result.meta_facts["revenue"] == 178_576_000_000
        assert result.meta_facts["__currency"] == "CNY"
        assert len(result.missing_fields) == 0
        assert "gross_profit" in result.derived_fields or "gross_profit" in result.meta_facts


# ============================================================
# DCFInput Currency Tests
# ============================================================

class TestDCFInputCurrency:

    def test_dcf_input_default_usd(self):
        inp = DCFInput(
            base_revenue=100e9, revenue_growth_path=[0.1] * 10,
            operating_margin_path=[0.3] * 10, capex_to_revenue_path=[0.05] * 10,
            effective_tax_rate=0.15, nwc_to_revenue_delta=0.01,
            terminal_growth_rate=0.03, wacc=0.095,
            sbc_to_revenue=0.0, dilution_rate_annual=0.02,
            shares_outstanding=2.5e9, net_debt=0, horizon_years=10,
        )
        assert inp.currency == "USD"

    def test_dcf_input_cny(self):
        inp = DCFInput(
            base_revenue=178e9, revenue_growth_path=[0.08] * 10,
            operating_margin_path=[0.65] * 10, capex_to_revenue_path=[0.03] * 10,
            effective_tax_rate=0.25, nwc_to_revenue_delta=0.01,
            terminal_growth_rate=0.03, wacc=0.08,
            sbc_to_revenue=0.0, dilution_rate_annual=0.005,
            shares_outstanding=1.256e9, net_debt=-150e9, horizon_years=10,
            currency="CNY",
        )
        assert inp.currency == "CNY"

    def test_dcf_computes_correctly_regardless_of_currency(self):
        """DCF engine is pure math — should work with any currency."""
        inp = DCFInput(
            base_revenue=178e9, revenue_growth_path=[0.08] * 10,
            operating_margin_path=[0.65] * 10, capex_to_revenue_path=[0.03] * 10,
            effective_tax_rate=0.25, nwc_to_revenue_delta=0.01,
            terminal_growth_rate=0.03, wacc=0.08,
            sbc_to_revenue=0.0, dilution_rate_annual=0.005,
            shares_outstanding=1.256e9, net_debt=-150e9, horizon_years=10,
            currency="CNY",
        )
        engine = DCFEngine()
        output = engine.compute_dcf(inp)
        assert output.per_share_value > 0
        # Moutai should have a very high per-share value in CNY
        assert output.per_share_value > 500  # At least ¥500


# ============================================================
# A-share Historical Extraction Tests
# ============================================================

class TestAShareHistorical:

    def test_extract_historical_returns_data(self):
        """Should extract multi-year revenue data from yfinance."""
        require_network()
        logs = []
        data, rev_series = AutoResearchOrchestrator._extract_a_share_historical(
            "600519", "600519", lambda msg: logs.append(msg),
        )
        # This requires network — skip if fails
        if not data:
            pytest.skip("yfinance network unavailable")

        assert len(data) >= 2  # At least 2 years
        assert len(rev_series) >= 2
        # Revenue should be in billions of CNY
        for yr, rev in rev_series:
            assert rev > 1e9  # At least 1B CNY
            assert isinstance(yr, int)

        # Years should be sorted
        years = [yr for yr, _ in rev_series]
        assert years == sorted(years)

    def test_extract_historical_unknown_ticker(self):
        """Unknown A-share should return empty data gracefully."""
        logs = []
        data, rev_series = AutoResearchOrchestrator._extract_a_share_historical(
            "999999", "999999", lambda msg: logs.append(msg),
        )
        # Should not crash, may return empty
        assert isinstance(data, dict)
        assert isinstance(rev_series, list)


# ============================================================
# Orchestrator A-share Detection Regression Tests
# ============================================================

class TestASharePipelineDetection:

    def test_is_a_share_comprehensive(self):
        # Positive cases
        assert AutoResearchOrchestrator._is_a_share_ticker("600519") is True
        assert AutoResearchOrchestrator._is_a_share_ticker("000858") is True
        assert AutoResearchOrchestrator._is_a_share_ticker("300750") is True
        assert AutoResearchOrchestrator._is_a_share_ticker("688981") is True
        assert AutoResearchOrchestrator._is_a_share_ticker("002594") is True
        assert AutoResearchOrchestrator._is_a_share_ticker("600519.SS") is True
        assert AutoResearchOrchestrator._is_a_share_ticker("000858.SZ") is True

        # Negative cases
        assert AutoResearchOrchestrator._is_a_share_ticker("META") is False
        assert AutoResearchOrchestrator._is_a_share_ticker("AAPL") is False
        assert AutoResearchOrchestrator._is_a_share_ticker("600519.SS.extra") is False
        assert AutoResearchOrchestrator._is_a_share_ticker("12345") is False
        assert AutoResearchOrchestrator._is_a_share_ticker("") is False


# ============================================================
# Full CN Adapter → Bridge → DCF Integration
# ============================================================

class TestFullCNPipelineIntegration:
    """Simulate the full A-share data pipeline without network."""

    def test_moutai_mock_pipeline(self):
        """Simulate: CAS data → CN adapter → bridge → DCF → per-share value."""
        # Step 1: CAS financial data (mock — based on real Moutai numbers)
        cas_facts = {
            "营业收入": 178_576_000_000,
            "营业成本": 13_789_000_000,
            "净利润": 90_027_000_000,
            "归属于母公司所有者的净利润": 86_228_000_000,
            "基本每股收益": 68.66,
            "资产总计": 320_000_000_000,
            "负债合计": 80_000_000_000,
            "所有者权益合计": 240_000_000_000,
            "货币资金": 150_000_000_000,
            "流动资产合计": 200_000_000_000,
            "流动负债合计": 60_000_000_000,
            "营业利润": 119_688_000_000,
            "经营活动产生的现金流量净额": 92_463_000_000,
            "购建固定资产、无形资产和其他长期资产支付的现金": 4_678_000_000,
        }

        # Step 2: CN adapter
        adapter = CNMarketAdapter()
        adapted, metadata = adapter.adapt_filing_data(cas_facts)
        assert metadata.currency == "CNY"
        assert adapted["revenue"] == 178_576_000_000

        # Step 3: Bridge
        bridge = FactNormalizationBridge()
        result = bridge.normalize(adapted, market_id="cn", currency="CNY")
        meta_facts = result.meta_facts
        assert meta_facts["revenue"] == 178_576_000_000
        assert meta_facts["__currency"] == "CNY"

        # Step 4: DCF
        dcf_input = DCFInput(
            base_revenue=meta_facts["revenue"],
            revenue_growth_path=[0.08, 0.08, 0.07, 0.06, 0.06, 0.05, 0.05, 0.04, 0.04, 0.03],
            operating_margin_path=[0.67] * 10,
            capex_to_revenue_path=[0.026] * 10,
            effective_tax_rate=0.25,
            nwc_to_revenue_delta=0.01,
            terminal_growth_rate=0.03,
            wacc=0.08,
            sbc_to_revenue=0.0,
            dilution_rate_annual=0.0,
            shares_outstanding=1_256_197_800,
            net_debt=-(meta_facts.get("cash_and_equivalents", 0)),
            horizon_years=10,
            currency="CNY",
        )
        engine = DCFEngine()
        output = engine.compute_dcf(dcf_input)

        # Per-share value should be in reasonable range for Moutai (¥800-¥3000)
        assert output.per_share_value > 500
        assert output.per_share_value < 5000
        assert dcf_input.currency == "CNY"
