"""Tests for A-share consensus estimates integration.

Round 28 — A股一致预期.
Tests cover:
- A-share ticker → yfinance symbol conversion
- ConsensusEstimate dataclass with CNY data
- OpenBB connector yfinance consensus for A-shares
- Guard logic: A-shares get consensus but skip US-only features
- DCF revenue calibration compatibility with A-share consensus
"""

import pytest

from aegis.core.orchestrator.auto_research import AutoResearchOrchestrator
from aegis.core.acquisition.connectors.openbb_connector import (
    ConsensusEstimate,
)


# ============================================================
# Ticker Conversion Tests
# ============================================================

class TestYfinanceSymbolConversion:

    def test_shanghai_main_board(self):
        assert AutoResearchOrchestrator._to_yfinance_symbol("600519") == "600519.SS"

    def test_shanghai_600xxx(self):
        assert AutoResearchOrchestrator._to_yfinance_symbol("601398") == "601398.SS"

    def test_shenzhen_000xxx(self):
        assert AutoResearchOrchestrator._to_yfinance_symbol("000858") == "000858.SZ"

    def test_chinext_300xxx(self):
        assert AutoResearchOrchestrator._to_yfinance_symbol("300750") == "300750.SZ"

    def test_star_market_688xxx(self):
        assert AutoResearchOrchestrator._to_yfinance_symbol("688981") == "688981.SS"

    def test_us_ticker_unchanged(self):
        assert AutoResearchOrchestrator._to_yfinance_symbol("AAPL") == "AAPL"
        assert AutoResearchOrchestrator._to_yfinance_symbol("NVDA") == "NVDA"

    def test_already_formatted_ss(self):
        assert AutoResearchOrchestrator._to_yfinance_symbol("600519.SS") == "600519.SS"

    def test_already_formatted_sz(self):
        assert AutoResearchOrchestrator._to_yfinance_symbol("000858.SZ") == "000858.SZ"


# ============================================================
# ConsensusEstimate for A-Shares
# ============================================================

class TestAShareConsensusEstimate:

    def test_create_cny_revenue_estimate(self):
        est = ConsensusEstimate(
            symbol="600519.SS",
            period="FY_Current",
            period_type="annual",
            metric="revenue",
            consensus_mean=180_872_804_220,
            consensus_high=190_452_689_290,
            consensus_low=171_827_082_000,
            analyst_count=16,
            source="yfinance",
        )
        assert est.symbol == "600519.SS"
        assert est.analyst_count == 16
        assert est.consensus_mean > 170e9

    def test_create_cny_eps_estimate(self):
        est = ConsensusEstimate(
            symbol="600519.SS",
            period="FY_Current",
            period_type="annual",
            metric="eps",
            consensus_mean=66.5,
            consensus_high=72.0,
            consensus_low=61.0,
            analyst_count=16,
            source="yfinance",
        )
        assert est.metric == "eps"
        assert est.consensus_mean == 66.5


# ============================================================
# Guard Logic Verification
# ============================================================

class TestAShareGuardLogic:

    def test_is_a_share_ticker_true(self):
        assert AutoResearchOrchestrator._is_a_share_ticker("600519") is True
        assert AutoResearchOrchestrator._is_a_share_ticker("000858") is True
        assert AutoResearchOrchestrator._is_a_share_ticker("300750") is True
        assert AutoResearchOrchestrator._is_a_share_ticker("688981") is True

    def test_is_a_share_ticker_false(self):
        assert AutoResearchOrchestrator._is_a_share_ticker("AAPL") is False
        assert AutoResearchOrchestrator._is_a_share_ticker("NVDA") is False

    def test_consensus_not_blocked_for_a_shares(self):
        """Verify the orchestrator code no longer blocks consensus for A-shares."""
        import inspect
        src = inspect.getsource(AutoResearchOrchestrator.run)

        # The consensus section should use yf_symbol (not blocked by `not is_a_share`)
        consensus_idx = src.index("Consensus estimates")
        consensus_block = src[max(0, consensus_idx - 200):consensus_idx + 200]
        # Should NOT have `not is_a_share` directly guarding consensus
        assert "not is_a_share" not in consensus_block

    def test_fred_macro_still_us_only(self):
        """FRED macro data should remain US-only."""
        import inspect
        src = inspect.getsource(AutoResearchOrchestrator.run)
        # Find "US-only features" comment which guards FRED and price target
        idx = src.index("US-only features")
        guard_block = src[idx:idx + 600]
        assert "not is_a_share" in guard_block
        assert "get_macro_snapshot" in guard_block

    def test_form4_still_us_only(self):
        """SEC Form 4 should remain US-only."""
        import inspect
        src = inspect.getsource(AutoResearchOrchestrator.run)
        form4_idx = src.index("Form 4")
        form4_block = src[max(0, form4_idx - 100):form4_idx + 100]
        assert "not is_a_share" in form4_block
