"""Tests for CNMarketAdapter concept mapping (AUDIT-A6/A7/A8 regressions)."""

import pytest

from aegis.core.acquisition.fact_bridge import FactNormalizationBridge
from aegis.core.market_adapter.cn_adapter import CNMarketAdapter


@pytest.fixture
def adapter():
    return CNMarketAdapter()


class TestConceptMapDebtConcepts:
    """AUDIT-A6: debt line items must reach canonical keys."""

    def test_bonds_payable_mapped(self, adapter):
        adapted, _ = adapter.adapt_filing_data({"应付债券": 24_113_000_000})
        assert adapted["bonds_payable"] == 24_113_000_000

    def test_noncurrent_liab_due_1y_mapped(self, adapter):
        adapted, _ = adapter.adapt_filing_data(
            {"一年内到期的非流动负债": 146_045_000_000}
        )
        assert adapted["long_term_debt_current"] == 146_045_000_000

    def test_akshare_balance_map_captures_1y_noncurrent_liab(self):
        """akshare _BALANCE_MAP must fetch NONCURRENT_LIAB_1YEAR (eastmoney field)."""
        from aegis.core.acquisition.connectors.akshare_connector import _BALANCE_MAP
        assert _BALANCE_MAP.get("NONCURRENT_LIAB_1YEAR") == "一年内到期的非流动负债"


class TestConceptMapIncomeTax:
    """AUDIT-A8: 所得税费用 must map to income_tax_expense."""

    def test_income_tax_expense_mapped(self, adapter):
        adapted, _ = adapter.adapt_filing_data({
            "利润总额": 10_000_000,
            "所得税费用": 2_500_000,
        })
        assert adapted["profit_before_tax"] == 10_000_000
        assert adapted["income_tax_expense"] == 2_500_000


class TestYfinanceFallbackCASKeys:
    """AUDIT-A6-bonus: cninfo yfinance-fallback CAS names must be consumed,
    not silently passed through as raw Chinese keys."""

    def test_fallback_keys_mapped_to_canonical(self, adapter):
        cas_facts = {
            "折旧摊销": 5_000_000,       # yfinance Reconciled Depreciation
            "有息负债合计": 80_000_000,  # yfinance Total Debt
            "EBITDA": 30_000_000,
            "自由现金流": 12_000_000,    # yfinance Free Cash Flow
            "利息支出": 3_000_000,       # yfinance Interest Expense
        }
        adapted, _ = adapter.adapt_filing_data(cas_facts)
        assert adapted["depreciation_amortization"] == 5_000_000
        assert adapted["total_debt"] == 80_000_000
        assert adapted["ebitda"] == 30_000_000
        assert adapted["free_cash_flow"] == 12_000_000
        assert adapted["interest_expense"] == 3_000_000
        # Raw Chinese keys must no longer leak through
        for zh_key in cas_facts:
            assert zh_key not in adapted


class TestFullChainCNDebtAndTax:
    """CN adapter → fact bridge: A6 debt aggregation + A8 tax rate end-to-end."""

    def test_bond_heavy_issuer_total_debt(self, adapter):
        """万科A-style issuer: bonds + reclassified 1y-due debt must count."""
        cas_facts = {
            "营业收入": 340_000_000_000,
            "净利润": -49_000_000_000,
            "资产总计": 1_300_000_000_000,
            "货币资金": 88_000_000_000,
            "短期借款": 20_000_000_000,
            "长期借款": 180_000_000_000,
            "应付债券": 24_000_000_000,
            "一年内到期的非流动负债": 146_000_000_000,
        }
        adapted, _ = adapter.adapt_filing_data(cas_facts)
        bridge = FactNormalizationBridge()
        result = bridge.normalize(adapted, market_id="cn", currency="CNY")
        f = result.meta_facts
        # 短借 20 + 长借 180 + 应付债券 24 + 一年内到期 146 = 370
        assert f["total_debt"] == 370_000_000_000
        # net_debt = 370 - 88 = 282 (deeply positive — no fake "net cash")
        assert f["net_debt"] == 282_000_000_000

    def test_effective_tax_rate_end_to_end(self, adapter):
        """所得税费用/利润总额 → effective_tax_rate at the bridge output."""
        cas_facts = {
            "营业收入": 178_576_000_000,
            "净利润": 90_027_000_000,
            "归属于母公司所有者的净利润": 86_228_000_000,
            "资产总计": 320_000_000_000,
            "利润总额": 120_036_000_000,
            "所得税费用": 30_009_000_000,
        }
        adapted, _ = adapter.adapt_filing_data(cas_facts)
        bridge = FactNormalizationBridge()
        result = bridge.normalize(adapted, market_id="cn", currency="CNY")
        f = result.meta_facts
        # 30_009 / 120_036 ≈ 25.0%
        assert abs(f["effective_tax_rate"] - 30_009 / 120_036) < 1e-9
        # AUDIT-A7: net_income switched to 归母口径
        assert f["net_income"] == 86_228_000_000
        assert f["net_income_incl_minority"] == 90_027_000_000
