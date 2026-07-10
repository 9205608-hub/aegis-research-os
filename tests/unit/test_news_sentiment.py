"""Tests for News Sentiment Analysis system.

Round 27 — News Sentiment.
Tests cover:
- NewsArticle dataclass
- NewsConnector._to_yfinance_symbol conversion
- NewsSentimentInsights dataclass
- Rule-based fallback sentiment
- NewsSentimentAnalyzer (no-LLM mode)
- Date range computation

NOTE (Phase 0, DESIGN_2.0): the HTML report card tests
(TestNewsSentimentCard) were removed together with html_report_legacy.py —
they exercised `_build_news_sentiment_card`, a legacy-renderer-only helper
with no production caller.
"""

import pytest
from dataclasses import dataclass

from aegis.core.acquisition.connectors.news_connector import (
    NewsArticle,
    NewsConnector,
    _to_yfinance_symbol,
)
from aegis.core.chief_analyst.news_sentiment_analyzer import (
    NewsSentimentAnalyzer,
    NewsSentimentInsights,
    rule_based_sentiment,
)


# ============================================================
# NewsArticle Tests
# ============================================================

class TestNewsArticle:

    def test_creation(self):
        a = NewsArticle(
            title="Apple beats earnings",
            summary="Apple reported Q1 earnings that exceeded expectations.",
            published="2026-04-10",
            source="Yahoo Finance",
            url="https://example.com/article",
        )
        assert a.title == "Apple beats earnings"
        assert a.published == "2026-04-10"

    def test_empty_summary(self):
        a = NewsArticle(
            title="Test", summary="", published="", source="", url=""
        )
        assert a.summary == ""


# ============================================================
# Ticker Conversion Tests
# ============================================================

class TestYfinanceSymbol:

    def test_us_ticker_passthrough(self):
        assert _to_yfinance_symbol("AAPL") == "AAPL"

    def test_shanghai_ticker(self):
        assert _to_yfinance_symbol("600519") == "600519.SS"

    def test_shenzhen_ticker(self):
        assert _to_yfinance_symbol("000858") == "000858.SZ"

    def test_chinext_ticker(self):
        assert _to_yfinance_symbol("300750") == "300750.SZ"

    def test_already_formatted(self):
        # If already has suffix, strip and re-add
        assert _to_yfinance_symbol("600519.SS") == "600519.SS"

    def test_star_market(self):
        assert _to_yfinance_symbol("688981") == "688981.SS"


# ============================================================
# Rule-Based Sentiment Tests
# ============================================================

class TestRuleBasedSentiment:

    def test_positive_titles(self):
        titles = [
            "Apple beats Q1 earnings expectations",
            "Strong growth in iPhone sales",
            "Analysts upgrade Apple stock to buy",
        ]
        result = rule_based_sentiment(titles)
        assert result.overall_sentiment == "positive"
        assert result.sentiment_score > 0

    def test_negative_titles(self):
        titles = [
            "Apple faces antitrust investigation",
            "iPhone sales decline amid slowdown concerns",
            "Warning signs in Apple supply chain",
        ]
        result = rule_based_sentiment(titles)
        assert result.overall_sentiment == "negative"
        assert result.sentiment_score < 0

    def test_neutral_titles(self):
        titles = [
            "Apple announces WWDC dates",
            "Tim Cook visits China factory",
        ]
        result = rule_based_sentiment(titles)
        assert result.overall_sentiment == "neutral"

    def test_mixed_titles(self):
        titles = [
            "Apple beats earnings but warns of risk",
            "Strong growth offset by decline in China",
            "Analysts upgrade despite supply chain concerns",
        ]
        result = rule_based_sentiment(titles)
        assert result.overall_sentiment in ("mixed", "positive", "negative")

    def test_empty_titles(self):
        result = rule_based_sentiment([])
        assert result.overall_sentiment == "neutral"
        assert result.sentiment_score == 0.0

    def test_article_count(self):
        titles = ["Some headline", "Another headline"]
        result = rule_based_sentiment(titles)
        assert result.article_count == 2


# ============================================================
# NewsSentimentInsights Tests
# ============================================================

class TestNewsSentimentInsights:

    def test_creation(self):
        insights = NewsSentimentInsights(
            overall_sentiment="positive",
            sentiment_score=0.6,
            sentiment_trend="improving",
            key_themes=["AI monetization", "iPhone cycle"],
            bullish_signals=["Strong services growth"],
            bearish_signals=["China weakness"],
            news_summary="Apple sentiment is broadly positive.",
            materiality="medium",
            article_count=15,
            date_range="Apr 01 - Apr 13, 2026",
        )
        assert insights.overall_sentiment == "positive"
        assert insights.sentiment_score == 0.6
        assert len(insights.key_themes) == 2

    def test_defaults(self):
        insights = NewsSentimentInsights(
            overall_sentiment="neutral",
            sentiment_score=0.0,
            sentiment_trend="stable",
            key_themes=[],
            bullish_signals=[],
            bearish_signals=[],
            news_summary="",
            materiality="low",
        )
        assert insights.article_count == 0
        assert insights.date_range == ""


# ============================================================
# Analyzer Tests (No LLM)
# ============================================================

class TestNewsSentimentAnalyzer:

    def _make_articles(self, titles):
        return [
            NewsArticle(
                title=t, summary="", published=f"2026-04-{10+i:02d}",
                source="Test", url=""
            )
            for i, t in enumerate(titles)
        ]

    def test_no_articles_returns_neutral(self):
        analyzer = NewsSentimentAnalyzer()
        result = analyzer.analyze([], "AAPL")
        assert result.overall_sentiment == "neutral"
        assert result.article_count == 0
        assert result.materiality == "low"

    def test_rule_based_positive(self):
        articles = self._make_articles([
            "Apple beats expectations with record revenue",
            "Strong growth in services segment",
            "Analysts upgrade Apple to outperform",
        ])
        analyzer = NewsSentimentAnalyzer()  # no LLM
        result = analyzer.analyze(articles, "AAPL", entity_name="Apple Inc.")
        assert result.overall_sentiment == "positive"
        assert result.article_count == 3

    def test_rule_based_negative(self):
        articles = self._make_articles([
            "Apple faces lawsuit over privacy concerns",
            "iPhone sales miss expectations amid decline",
            "Warning from analysts about Apple slowdown",
        ])
        analyzer = NewsSentimentAnalyzer()
        result = analyzer.analyze(articles, "AAPL")
        assert result.overall_sentiment == "negative"

    def test_date_range_computed(self):
        articles = self._make_articles(["Article 1", "Article 2", "Article 3"])
        analyzer = NewsSentimentAnalyzer()
        result = analyzer.analyze(articles, "AAPL")
        assert result.date_range  # Should not be empty

    def test_date_range_computation(self):
        analyzer = NewsSentimentAnalyzer()
        dr = analyzer._compute_date_range(["2026-04-01", "2026-04-10", "2026-04-13"])
        assert "Apr 01" in dr
        assert "Apr 13" in dr

    def test_date_range_single(self):
        analyzer = NewsSentimentAnalyzer()
        dr = analyzer._compute_date_range(["2026-04-10"])
        assert dr == "2026-04-10"

    def test_date_range_empty(self):
        analyzer = NewsSentimentAnalyzer()
        dr = analyzer._compute_date_range([])
        assert dr == ""

    def test_message_building(self):
        articles = self._make_articles(["Apple Q1 earnings beat", "iPhone sales strong"])
        analyzer = NewsSentimentAnalyzer()
        msg = analyzer._build_message(articles, "AAPL", "Apple Inc.")
        assert "Apple Inc." in msg
        assert "AAPL" in msg
        assert "Apple Q1 earnings beat" in msg
