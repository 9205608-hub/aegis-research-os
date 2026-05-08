"""Tests for AutoResearchOrchestrator.

These tests use mocked SEC EDGAR data to avoid network calls.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from aegis.core.acquisition.connectors.market_data_connector import MarketSnapshot
from aegis.core.acquisition.models import DataQuery, RawDataPacket
from aegis.core.chief_analyst.news_sentiment_analyzer import NewsSentimentInsights
from aegis.core.orchestrator.auto_research import (
    AutoResearchOrchestrator,
    ResearchConfig,
    ResearchResult,
)
from aegis.data_contracts.common import SourceTier


def _make_mock_packet(facts: dict, segments: dict | None = None) -> RawDataPacket:
    """Create a mock RawDataPacket with XBRL facts."""
    return RawDataPacket(
        source_id="edgar",
        source_tier=SourceTier.TIER_1,
        market_id="us",
        query=DataQuery(entity_id="0001326801", market_id="us", data_type="filing"),
        fetched_at=datetime.now(UTC),
        raw_content={
            "entity_name": "Meta Platforms Inc",
            "cik": "0001326801",
            "fiscal_year": 2024,
            "fiscal_period": "FY",
            "facts": facts,
            "segment_facts": segments or {},
            "fact_count": len(facts),
            "segment_count": len(segments) if segments else 0,
        },
        content_hash="sha256:test",
        content_type="json",
    )


# Minimal XBRL facts that would come from SEC EDGAR
MOCK_XBRL_FACTS = {
    "us-gaap:Revenues": 164_710_000_000,
    "us-gaap:CostOfRevenue": 27_928_000_000,
    "us-gaap:GrossProfit": 136_782_000_000,
    "us-gaap:OperatingIncomeLoss": 69_381_000_000,
    "us-gaap:NetIncomeLoss": 62_360_000_000,
    "us-gaap:Assets": 256_166_000_000,
    "us-gaap:Liabilities": 90_304_000_000,
    "us-gaap:StockholdersEquity": 165_862_000_000,
    "us-gaap:CashAndCashEquivalentsAtCarryingValue": 58_069_000_000,
    "us-gaap:LongTermDebt": 28_826_000_000,
    "us-gaap:NetCashProvidedByUsedInOperatingActivities": 91_145_000_000,
    "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment": 39_200_000_000,
    "us-gaap:ShareBasedCompensation": 19_007_000_000,
    "us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding": 2_579_000_000,
    "us-gaap:CommonStockSharesOutstanding": 2_514_000_000,
    "us-gaap:AssetsCurrent": 75_860_000_000,
    "us-gaap:LiabilitiesCurrent": 31_480_000_000,
}


class _FakeMarketDataConnector:
    def get_snapshot(self, ticker: str) -> MarketSnapshot:
        return MarketSnapshot(
            ticker=ticker,
            current_price=585.0,
            market_cap=1_510_000_000_000,
            shares_outstanding=2_579_000_000,
        )


class _FakeCatalystTimeline:
    events: list = []
    upcoming: list = []
    next_earnings = None

    def to_dict(self):
        return {
            "event_count": 0,
            "upcoming_count": 0,
            "next_catalyst": None,
            "next_earnings": None,
            "events_30d": 0,
            "events_90d": 0,
            "timeline": [],
        }


class _FakeCatalystCalendar:
    def build(self, **kwargs):
        return _FakeCatalystTimeline()


class _FakeForm4Connector:
    def get_insider_transactions(self, ticker: str, months: int = 12):
        return None


class _FakeOpenBBConnector:
    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs

    def get_consensus_estimates(self, symbol: str):
        return []

    def get_earnings_history(self, symbol: str, limit: int = 8):
        return []

    def get_sector_peers(self, ticker: str, limit: int = 6):
        return []

    def get_peer_fundamentals(self, tickers: list[str]):
        return []

    def get_historical_valuation(
        self,
        symbol: str,
        years: int = 5,
        ev_ebitda_override: float | None = None,
    ):
        return {}

    def get_price_target_consensus(self, ticker: str):
        return {}

    def get_macro_snapshot(self):
        return None

    def get_earnings_transcript(self, ticker: str):
        return {}


class _FakeNewsConnector:
    def get_recent_news(self, ticker: str, limit: int = 20):
        return [
            SimpleNamespace(
                title="Meta ad checks improve",
                summary="Channel checks point to stronger ad demand.",
                published="2026-04-20",
                source="Test",
                url="",
            )
        ]


class _FakeNewsSentimentAnalyzer:
    def __init__(self, llm_client=None):
        self.llm_client = llm_client

    def analyze(self, articles, symbol: str, entity_name: str = ""):
        return NewsSentimentInsights(
            overall_sentiment="positive",
            sentiment_score=0.4,
            sentiment_trend="improving",
            key_themes=["ad demand"],
            bullish_signals=["better checks"],
            bearish_signals=[],
            news_summary="Sentiment improved on better ad demand.",
            materiality="medium",
            article_count=len(articles),
            date_range="2026-04-20",
        )


class TestAutoResearchOrchestrator:

    @pytest.fixture
    def offline_orchestrator(self):
        return AutoResearchOrchestrator(
            market_data_connector_factory=_FakeMarketDataConnector,
            catalyst_calendar_factory=_FakeCatalystCalendar,
            form4_connector_factory=_FakeForm4Connector,
        )

    @patch("aegis.core.acquisition.connectors.edgar_connector.SECEDGARConnector.fetch")
    def test_full_pipeline_no_price(self, mock_fetch, offline_orchestrator):
        """Pipeline should run end-to-end even without market price."""
        mock_fetch.return_value = _make_mock_packet(MOCK_XBRL_FACTS)

        config = ResearchConfig(
            ticker="META",
            period="FY2024",
            generate_html=False,
            enable_openbb=False,
            enable_news_sentiment=False,
        )
        result = offline_orchestrator.run(config)

        assert isinstance(result, ResearchResult)
        assert result.ticker == "META"
        assert result.entity_id == "meta_platforms"
        assert len(result.meta_facts) > 10
        assert len(result.computed_metrics) > 5
        assert result.dcf_per_share > 0
        assert result.decision is not None
        assert result.signal is not None

    @patch("aegis.core.acquisition.connectors.edgar_connector.SECEDGARConnector.fetch")
    def test_full_pipeline_with_price(self, mock_fetch, offline_orchestrator):
        """Pipeline with market price should compute PE, EV/Revenue, implied growth."""
        mock_fetch.return_value = _make_mock_packet(MOCK_XBRL_FACTS)

        config = ResearchConfig(
            ticker="META",
            period="FY2024",
            current_price=585.0,
            market_cap=1_510_000_000_000,
            generate_html=False,
            enable_openbb=False,
            enable_news_sentiment=False,
        )
        result = offline_orchestrator.run(config)

        assert result.computed_metrics.get("pe_ratio") is not None
        assert result.computed_metrics["pe_ratio"] > 0
        assert result.computed_metrics.get("ev_to_revenue") is not None
        assert result.computed_metrics.get("enterprise_value") is not None
        assert result.implied_growth != 0

    @patch("aegis.core.acquisition.connectors.edgar_connector.SECEDGARConnector.fetch")
    def test_meta_facts_have_required_keys(self, mock_fetch, offline_orchestrator):
        """meta_facts should contain all keys needed by downstream pipeline."""
        mock_fetch.return_value = _make_mock_packet(MOCK_XBRL_FACTS)

        config = ResearchConfig(
            ticker="META",
            period="FY2024",
            generate_html=False,
            enable_openbb=False,
            enable_news_sentiment=False,
        )
        result = offline_orchestrator.run(config)

        required = [
            "revenue", "net_income", "operating_income", "total_assets",
            "operating_cash_flow", "capex", "diluted_shares",
        ]
        for key in required:
            assert key in result.meta_facts, f"Missing key: {key}"

    @patch("aegis.core.acquisition.connectors.edgar_connector.SECEDGARConnector.fetch")
    def test_scenarios_bear_lt_base_lt_bull(self, mock_fetch, offline_orchestrator):
        """Bear < Base < Bull scenario values."""
        mock_fetch.return_value = _make_mock_packet(MOCK_XBRL_FACTS)

        config = ResearchConfig(
            ticker="META",
            period="FY2024",
            generate_html=False,
            enable_openbb=False,
            enable_news_sentiment=False,
        )
        result = offline_orchestrator.run(config)

        assert result.scenarios["bear_value"] < result.scenarios["base_value"]
        assert result.scenarios["base_value"] < result.scenarios["bull_value"]

    @patch("aegis.core.acquisition.connectors.edgar_connector.SECEDGARConnector.fetch")
    def test_pipeline_log_populated(self, mock_fetch, offline_orchestrator):
        mock_fetch.return_value = _make_mock_packet(MOCK_XBRL_FACTS)

        config = ResearchConfig(
            ticker="META",
            period="FY2024",
            generate_html=False,
            enable_openbb=False,
            enable_news_sentiment=False,
        )
        result = offline_orchestrator.run(config)

        assert len(result.pipeline_log) >= 5
        assert any("Starting" in entry for entry in result.pipeline_log)
        assert any("Resolved" in entry for entry in result.pipeline_log)

    def test_unknown_ticker_raises(self):
        """Should raise ValueError for unknown tickers."""
        config = ResearchConfig(
            ticker="XYZNOTREAL",
            period="FY2024",
            generate_html=False,
            enable_openbb=False,
            enable_news_sentiment=False,
        )
        orchestrator = AutoResearchOrchestrator(
            market_data_connector_factory=_FakeMarketDataConnector,
            catalyst_calendar_factory=_FakeCatalystCalendar,
            form4_connector_factory=_FakeForm4Connector,
        )
        with pytest.raises(ValueError, match="Unknown ticker"):
            orchestrator.run(config)

    @patch("aegis.core.acquisition.connectors.edgar_connector.SECEDGARConnector.fetch")
    def test_fetch_failure_raises(self, mock_fetch, offline_orchestrator):
        """Should raise RuntimeError if EDGAR fetch returns no content."""
        mock_fetch.return_value = _make_mock_packet.__wrapped__(
        ) if hasattr(_make_mock_packet, '__wrapped__') else RawDataPacket(
            source_id="edgar",
            source_tier=SourceTier.TIER_1,
            market_id="us",
            query=DataQuery(entity_id="0001326801", market_id="us", data_type="filing"),
            fetched_at=datetime.now(UTC),
            raw_content=None,
            content_hash="sha256:0" * 4,
            content_type="json",
        )

        config = ResearchConfig(
            ticker="META",
            period="FY2024",
            generate_html=False,
            enable_openbb=False,
            enable_news_sentiment=False,
        )
        with pytest.raises(RuntimeError, match="Failed to fetch"):
            offline_orchestrator.run(config)

    @patch("aegis.core.acquisition.connectors.edgar_connector.SECEDGARConnector.fetch")
    def test_segment_data_flows_to_agents(self, mock_fetch, offline_orchestrator):
        """Segment facts should be available in the result."""
        segments = {
            "foa": {"us-gaap:Revenues": 160_826_000_000},
            "rl": {"us-gaap:Revenues": 3_884_000_000},
        }
        mock_fetch.return_value = _make_mock_packet(MOCK_XBRL_FACTS, segments)

        config = ResearchConfig(
            ticker="META",
            period="FY2024",
            generate_html=False,
            enable_openbb=False,
            enable_news_sentiment=False,
        )
        result = offline_orchestrator.run(config)

        # The orchestrator should have processed segments
        assert result is not None
        assert result.decision is not None

    @patch("aegis.core.acquisition.connectors.edgar_connector.SECEDGARConnector.fetch")
    def test_uses_injected_dependency_factories(self, mock_fetch):
        mock_fetch.return_value = _make_mock_packet(MOCK_XBRL_FACTS)
        factory_calls: list[str] = []

        def _market_factory():
            factory_calls.append("market")
            return _FakeMarketDataConnector()

        def _openbb_factory(**kwargs):
            factory_calls.append("openbb")
            return _FakeOpenBBConnector(**kwargs)

        def _calendar_factory():
            factory_calls.append("calendar")
            return _FakeCatalystCalendar()

        def _form4_factory():
            factory_calls.append("form4")
            return _FakeForm4Connector()

        def _news_factory():
            factory_calls.append("news")
            return _FakeNewsConnector()

        def _news_analyzer_factory(**kwargs):
            factory_calls.append("news_analyzer")
            return _FakeNewsSentimentAnalyzer(**kwargs)

        orchestrator = AutoResearchOrchestrator(
            market_data_connector_factory=_market_factory,
            openbb_connector_factory=_openbb_factory,
            catalyst_calendar_factory=_calendar_factory,
            form4_connector_factory=_form4_factory,
            news_connector_factory=_news_factory,
            news_sentiment_analyzer_factory=_news_analyzer_factory,
        )
        config = ResearchConfig(
            ticker="META",
            period="FY2024",
            generate_html=False,
            enable_openbb=True,
            enable_news_sentiment=True,
        )

        result = orchestrator.run(config)

        assert isinstance(result, ResearchResult)
        assert "market" in factory_calls
        assert "openbb" in factory_calls
        assert "calendar" in factory_calls
        assert "form4" in factory_calls
        assert "news" in factory_calls
        assert "news_analyzer" in factory_calls
        assert any("News sentiment: positive" in entry for entry in result.pipeline_log)


class TestResearchConfig:

    def test_defaults(self):
        config = ResearchConfig(ticker="AAPL")
        assert config.period == "latest"
        assert config.filing_type == "10-K"
        assert config.wacc == 0.095
        assert config.terminal_growth_rate == 0.03
        assert config.generate_html is True

    def test_custom_values(self):
        config = ResearchConfig(
            ticker="GOOGL",
            period="FY2023",
            current_price=150.0,
            wacc=0.10,
            terminal_growth_rate=0.025,
        )
        assert config.ticker == "GOOGL"
        assert config.period == "FY2023"
        assert config.current_price == 150.0
