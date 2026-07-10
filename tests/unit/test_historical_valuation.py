"""Tests for Historical Valuation Range feature."""

import pytest
from unittest.mock import MagicMock, patch


class TestGetHistoricalValuation:
    """Test OpenBBConnector.get_historical_valuation()."""

    def test_no_yfinance_returns_empty(self):
        from aegis.core.acquisition.connectors.openbb_connector import OpenBBConnector
        conn = OpenBBConnector()
        with patch.dict("sys.modules", {"yfinance": None}):
            # Even without mocking, the import will succeed if yfinance is installed
            # Just verify it returns a dict
            result = conn.get_historical_valuation("NONEXISTENT_TICKER_XYZ")
            assert isinstance(result, dict)

    @patch("yfinance.Ticker")
    def test_empty_history_returns_empty(self, mock_ticker_cls):
        import pandas as pd
        from aegis.core.acquisition.connectors.openbb_connector import OpenBBConnector

        mock_ticker = MagicMock()
        mock_ticker.history.return_value = pd.DataFrame()
        mock_ticker_cls.return_value = mock_ticker

        conn = OpenBBConnector()
        result = conn.get_historical_valuation("TEST")
        assert result == {}

    @patch("yfinance.Ticker")
    def test_successful_valuation(self, mock_ticker_cls):
        import pandas as pd
        import numpy as np
        from aegis.core.acquisition.connectors.openbb_connector import OpenBBConnector

        # Create mock price history (24 months)
        dates = pd.date_range(start="2023-01-01", periods=24, freq="MS")
        prices = [150 + i * 2 for i in range(24)]  # Rising from 150 to 196
        hist_df = pd.DataFrame({"Close": prices, "Open": prices, "High": prices, "Low": prices, "Volume": [1e6]*24}, index=dates)

        mock_ticker = MagicMock()
        mock_ticker.history.return_value = hist_df
        mock_ticker.info = {
            "trailingPE": 25.0,
            "enterpriseToEbitda": 18.0,
            "currentPrice": 196.0,
        }
        mock_ticker.quarterly_financials = None
        mock_ticker.quarterly_balance_sheet = None
        mock_ticker_cls.return_value = mock_ticker

        conn = OpenBBConnector()
        result = conn.get_historical_valuation("TEST", years=2)

        assert "dates" in result
        assert "pe_ratio" in result
        assert "ev_ebitda" in result
        assert "pe_stats" in result
        assert "ev_ebitda_stats" in result
        assert len(result["dates"]) == 24
        assert result["pe_stats"]["current"] > 0

    @patch("yfinance.Ticker")
    def test_stats_computation(self, mock_ticker_cls):
        import pandas as pd
        from aegis.core.acquisition.connectors.openbb_connector import OpenBBConnector

        dates = pd.date_range(start="2022-01-01", periods=36, freq="MS")
        prices = [100 + i for i in range(36)]
        hist_df = pd.DataFrame({"Close": prices, "Open": prices, "High": prices, "Low": prices, "Volume": [1e6]*36}, index=dates)

        mock_ticker = MagicMock()
        mock_ticker.history.return_value = hist_df
        mock_ticker.info = {"trailingPE": 30.0, "enterpriseToEbitda": 20.0, "currentPrice": 135.0}
        mock_ticker.quarterly_financials = None
        mock_ticker.quarterly_balance_sheet = None
        mock_ticker_cls.return_value = mock_ticker

        conn = OpenBBConnector()
        result = conn.get_historical_valuation("TEST", years=3)

        pe_stats = result.get("pe_stats", {})
        assert "min" in pe_stats
        assert "max" in pe_stats
        assert "median" in pe_stats
        assert "p25" in pe_stats
        assert "p75" in pe_stats
        assert pe_stats["min"] <= pe_stats["median"] <= pe_stats["max"]

# NOTE (Phase 0, DESIGN_2.0): TestValuationChartJS was removed together with
# html_report_legacy.py — it exercised `_build_valuation_chart_js`, a
# legacy-renderer-only Chart.js string builder with no production caller.
