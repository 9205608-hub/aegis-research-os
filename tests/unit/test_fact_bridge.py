"""Tests for FactNormalizationBridge."""

import pytest
from aegis.core.acquisition.fact_bridge import (
    FactNormalizationBridge,
    BridgeResult,
    CRITICAL_FIELDS,
)


@pytest.fixture
def bridge():
    return FactNormalizationBridge()


@pytest.fixture
def full_adapted_data():
    """Simulates USMarketAdapter.adapt_filing_data() output for META FY2024."""
    return {
        "revenue": 164_710_000_000,
        "cost_of_revenue": 27_928_000_000,
        "gross_profit": 136_782_000_000,
        "operating_income": 69_381_000_000,
        "net_income": 62_360_000_000,
        "total_assets": 256_166_000_000,
        "total_liabilities": 90_304_000_000,
        "shareholders_equity": 165_862_000_000,
        "cash_and_equivalents": 58_069_000_000,
        "long_term_debt": 28_826_000_000,
        "cfo": 91_145_000_000,
        "capex_ppe": 39_200_000_000,
        "sbc": 19_007_000_000,
        "diluted_shares": 2_579_000_000,
        "shares_outstanding": 2_514_000_000,
        "current_assets": 75_860_000_000,
        "current_liabilities": 31_480_000_000,
        "eps_diluted": 24.18,
    }


class TestFactNormalizationBridge:

    def test_basic_normalization(self, bridge, full_adapted_data):
        result = bridge.normalize(full_adapted_data)
        assert isinstance(result, BridgeResult)
        assert result.is_complete
        assert len(result.missing_fields) == 0

    def test_alias_resolution(self, bridge, full_adapted_data):
        """cfo → operating_cash_flow, capex_ppe → capex."""
        result = bridge.normalize(full_adapted_data)
        f = result.meta_facts

        # Both alias and canonical should exist
        assert f["operating_cash_flow"] == 91_145_000_000
        assert f["cfo"] == 91_145_000_000
        assert f["capex"] == 39_200_000_000
        assert f["capex_ppe"] == 39_200_000_000
        assert f["total_equity"] == 165_862_000_000
        assert f["shareholders_equity"] == 165_862_000_000

    def test_derived_fields(self, bridge):
        """Should derive free_cash_flow, net_debt, nwc, ebitda from inputs."""
        data = {
            "revenue": 100_000,
            "net_income": 20_000,
            "total_assets": 200_000,
            "operating_income": 30_000,
            "depreciation_amortization": 5_000,
            "cfo": 35_000,
            "capex_ppe": 10_000,
            "long_term_debt": 50_000,
            "short_term_debt": 10_000,
            "cash_and_equivalents": 25_000,
            "current_assets": 80_000,
            "current_liabilities": 40_000,
        }
        result = bridge.normalize(data)
        f = result.meta_facts

        assert f["free_cash_flow"] == 25_000  # 35000 - 10000
        assert f["total_debt"] == 60_000  # 50000 + 10000
        assert f["net_debt"] == 35_000  # 60000 - 25000
        assert f["nwc"] == 40_000  # 80000 - 40000
        assert f["ebitda"] == 35_000  # 30000 + 5000

        assert "free_cash_flow" in result.derived_fields
        assert "total_debt" in result.derived_fields
        assert "net_debt" in result.derived_fields

    def test_missing_critical_fields(self, bridge):
        """Should report missing critical fields."""
        result = bridge.normalize({"revenue": 100})
        assert not result.is_complete
        assert "net_income" in result.missing_fields
        assert "total_assets" in result.missing_fields

    def test_all_critical_present(self, bridge):
        data = {"revenue": 100, "net_income": 20, "total_assets": 500}
        result = bridge.normalize(data)
        assert result.is_complete
        assert len(result.missing_fields) == 0

    def test_unmapped_xbrl_concepts_ignored(self, bridge):
        """XBRL concepts not mapped by adapter should be skipped."""
        data = {
            "revenue": 100,
            "net_income": 20,
            "total_assets": 500,
            "us-gaap:SomeObscureConcept": 42,
        }
        result = bridge.normalize(data)
        assert "us-gaap:SomeObscureConcept" not in result.meta_facts

    def test_dilution_rate_derived(self, bridge):
        data = {
            "revenue": 100, "net_income": 20, "total_assets": 500,
            "diluted_shares": 1050, "shares_outstanding": 1000,
        }
        result = bridge.normalize(data)
        assert abs(result.meta_facts["dilution_rate"] - 0.05) < 0.001
        assert "dilution_rate" in result.derived_fields

    def test_segment_data_processing(self, bridge):
        """Segment facts should be adapted and stored."""
        data = {"revenue": 100, "net_income": 20, "total_assets": 500}
        segments = {
            "aws": {"us-gaap:Revenues": 80},
            "retail": {"us-gaap:Revenues": 20},
        }
        result = bridge.normalize(data, segment_facts=segments)
        assert "aws" in result.segment_data
        assert "retail" in result.segment_data
        assert result.segment_data["aws"]["revenue"] == 80

    def test_filing_context_stored(self, bridge):
        data = {"revenue": 100, "net_income": 20, "total_assets": 500}
        ctx = {"entity_id": "meta_platforms", "fiscal_year": 2024}
        result = bridge.normalize(data, filing_context=ctx)
        assert result.meta_facts["__entity_id"] == "meta_platforms"
        assert result.meta_facts["__fiscal_year"] == 2024

    def test_total_debt_from_components(self, bridge):
        """total_debt should be derived from long_term + short_term."""
        data = {
            "revenue": 100, "net_income": 20, "total_assets": 500,
            "long_term_debt": 30, "short_term_debt": 10,
        }
        result = bridge.normalize(data)
        assert result.meta_facts["total_debt"] == 40
        assert "total_debt" in result.derived_fields

    def test_total_equity_derived(self, bridge):
        """total_equity = total_assets - total_liabilities if not present."""
        data = {
            "revenue": 100, "net_income": 20,
            "total_assets": 500, "total_liabilities": 200,
        }
        result = bridge.normalize(data)
        assert result.meta_facts["total_equity"] == 300
        assert "total_equity" in result.derived_fields

    def test_no_double_derivation(self, bridge):
        """If a field already exists, don't re-derive it."""
        data = {
            "revenue": 100, "net_income": 20, "total_assets": 500,
            "gross_profit": 70,  # Already provided
            "cost_of_revenue": 25,  # Would give 75 if derived
        }
        result = bridge.normalize(data)
        assert result.meta_facts["gross_profit"] == 70  # Original value preserved
        assert "gross_profit" not in result.derived_fields

    def test_empty_input(self, bridge):
        result = bridge.normalize({})
        assert not result.is_complete
        assert len(result.missing_fields) == len(CRITICAL_FIELDS)


class TestFactBridgeWithFullAdaptedData:
    """Integration-style tests with realistic adapted data."""

    def test_full_meta_pipeline(self, bridge, full_adapted_data):
        """Verify the bridge produces everything the demo pipeline needs."""
        result = bridge.normalize(full_adapted_data)
        f = result.meta_facts

        # All keys the demo pipeline relies on should be present
        required_keys = [
            "revenue", "net_income", "operating_income",
            "total_assets", "total_equity",
            "operating_cash_flow", "capex",
            "diluted_shares",
        ]
        for key in required_keys:
            assert key in f, f"Missing key: {key}"

        # Derived fields should be present
        assert "free_cash_flow" in f
        assert "total_debt" in f
        assert "net_debt" in f
        assert "nwc" in f

        # Values should be sane
        assert f["free_cash_flow"] == f["operating_cash_flow"] - f["capex"]
        assert f["nwc"] == f["current_assets"] - f["current_liabilities"]
