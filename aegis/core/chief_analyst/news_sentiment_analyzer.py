"""News Sentiment Analyzer — LLM-driven news sentiment analysis.

Analyzes recent news articles for investment-relevant sentiment signals:
1. Overall sentiment polarity and score
2. Sentiment trend direction
3. Key themes and catalysts
4. Bullish and bearish signal extraction
5. Materiality assessment
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class NewsSentimentInsights:
    """Structured output from news sentiment analysis."""

    overall_sentiment: str  # "positive", "negative", "neutral", "mixed"
    sentiment_score: float  # -1.0 (very negative) to +1.0 (very positive)
    sentiment_trend: str  # "improving", "deteriorating", "stable"

    key_themes: list[str]  # 3-5 dominant themes
    bullish_signals: list[str]  # Positive catalysts/developments
    bearish_signals: list[str]  # Risk factors/negative developments

    news_summary: str  # 2-3 sentence investment-focused summary
    materiality: str  # "high", "medium", "low"

    article_count: int = 0
    date_range: str = ""  # "Apr 1 - Apr 13, 2026"


NEWS_SENTIMENT_TOOL_SCHEMA = {
    "type": "object",
    "required": [
        "overall_sentiment", "sentiment_score", "sentiment_trend",
        "key_themes", "bullish_signals", "bearish_signals",
        "news_summary", "materiality",
    ],
    "properties": {
        "overall_sentiment": {
            "type": "string",
            "enum": ["positive", "negative", "neutral", "mixed"],
            "description": "Net sentiment polarity across all articles.",
        },
        "sentiment_score": {
            "type": "number",
            "minimum": -1.0,
            "maximum": 1.0,
            "description": "Numeric sentiment from -1.0 (very bearish) to +1.0 (very bullish). 0.0 is neutral.",
        },
        "sentiment_trend": {
            "type": "string",
            "enum": ["improving", "deteriorating", "stable"],
            "description": "Are more recent articles more positive or negative than older ones?",
        },
        "key_themes": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 5,
            "description": "Dominant themes across the news (e.g., 'tariff risk', 'AI monetization', 'management changes').",
        },
        "bullish_signals": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 5,
            "description": "Specific positive developments from the news that could support the stock.",
        },
        "bearish_signals": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 5,
            "description": "Specific risk factors or negative developments from the news.",
        },
        "news_summary": {
            "type": "string",
            "description": "2-3 sentence investment-focused summary of the news landscape. Not a list — a cohesive narrative.",
        },
        "materiality": {
            "type": "string",
            "enum": ["high", "medium", "low"],
            "description": "'high' if news contains stock-moving events. 'medium' if incremental. 'low' if noise.",
        },
    },
}


NEWS_SENTIMENT_SYSTEM_PROMPT = """You are a senior buy-side analyst reading recent news headlines and summaries about a company.

YOUR ROLE: Assess the INVESTMENT SENTIMENT from these news articles. You are not summarizing news — you are identifying what matters for the stock price.

HOW TO READ NEWS AS AN ANALYST:
1. SEPARATE SIGNAL FROM NOISE: Earnings previews, management changes, regulatory actions = signal. Listicles, SEO articles = noise.
2. IDENTIFY CATALYSTS: What events could move the stock? Product launches, legal outcomes, macro shifts.
3. DETECT NARRATIVE SHIFTS: Is the market narrative changing? From growth to value? From defensive to offensive?
4. WEIGHT BY RECENCY: More recent articles matter more. A bearish article from yesterday outweighs a bullish article from last month.
5. CONSIDER SOURCE: Major financial outlets (WSJ, Bloomberg, FT) carry more weight than aggregator content.

WHAT MAKES A GOOD ANALYSIS:
- GOOD: "Sentiment has deteriorated over the past week as three major outlets covered potential antitrust action — this could pressure the multiple even if fundamentals remain strong"
- BAD: "Some articles are positive and some are negative"

HARD CONSTRAINTS:
- Do NOT invent news stories not present in the input
- DO distinguish between opinion pieces and factual reporting
- DO note if the news is mostly noise (set materiality to 'low')
- If fewer than 3 relevant articles, set materiality to 'low'
"""


# Simple word lists for rule-based fallback (no LLM)
_POSITIVE_WORDS = {
    "beat", "beats", "exceeded", "surpass", "upgrade", "upgraded", "outperform",
    "growth", "strong", "surge", "rally", "bullish", "record", "profit",
    "gains", "positive", "boost", "innovation", "breakthrough", "expansion",
    "upside", "buy", "optimistic", "momentum", "robust", "accelerat",
}

_NEGATIVE_WORDS = {
    "miss", "missed", "decline", "fall", "drop", "downgrade", "underperform",
    "weak", "loss", "bearish", "risk", "concern", "warning", "cut",
    "layoff", "lawsuit", "investigation", "probe", "slowdown", "recession",
    "sell", "pessimistic", "headwind", "pressure", "threat", "default",
}


def rule_based_sentiment(titles: list[str]) -> NewsSentimentInsights:
    """Simple keyword-based sentiment when LLM is not available."""
    pos_count = 0
    neg_count = 0

    for title in titles:
        lower = title.lower()
        for w in _POSITIVE_WORDS:
            if w in lower:
                pos_count += 1
        for w in _NEGATIVE_WORDS:
            if w in lower:
                neg_count += 1

    total = pos_count + neg_count
    if total == 0:
        score = 0.0
        sentiment = "neutral"
    else:
        score = round((pos_count - neg_count) / total, 2)
        if score > 0.2:
            sentiment = "positive"
        elif score < -0.2:
            sentiment = "negative"
        elif pos_count > 0 and neg_count > 0:
            sentiment = "mixed"
        else:
            sentiment = "neutral"

    return NewsSentimentInsights(
        overall_sentiment=sentiment,
        sentiment_score=score,
        sentiment_trend="stable",
        key_themes=[],
        bullish_signals=[],
        bearish_signals=[],
        news_summary=f"Rule-based analysis of {len(titles)} headlines: "
                     f"{pos_count} positive signals, {neg_count} negative signals.",
        materiality="low",
        article_count=len(titles),
    )


class NewsSentimentAnalyzer:
    """LLM-driven news sentiment analysis with rule-based fallback."""

    def __init__(self, llm_client: Any = None) -> None:
        self._llm = llm_client

    def analyze(
        self,
        articles: list[Any],
        symbol: str,
        entity_name: str = "",
    ) -> NewsSentimentInsights:
        """Analyze news articles for investment sentiment.

        Args:
            articles: List of NewsArticle objects.
            symbol: Ticker symbol.
            entity_name: Company name for context.

        Returns:
            NewsSentimentInsights with sentiment analysis.
        """
        if not articles:
            return NewsSentimentInsights(
                overall_sentiment="neutral",
                sentiment_score=0.0,
                sentiment_trend="stable",
                key_themes=[],
                bullish_signals=[],
                bearish_signals=[],
                news_summary="No recent news articles available.",
                materiality="low",
                article_count=0,
            )

        titles = [getattr(a, "title", "") for a in articles]
        dates = [getattr(a, "published", "") for a in articles]
        date_range = self._compute_date_range(dates)

        # Rule-based fallback if no LLM
        if not self._llm:
            result = rule_based_sentiment(titles)
            result.article_count = len(articles)
            result.date_range = date_range
            return result

        # LLM analysis
        user_message = self._build_message(articles, symbol, entity_name)

        try:
            raw = self._llm.call_structured(
                system_prompt=NEWS_SENTIMENT_SYSTEM_PROMPT,
                user_message=user_message,
                tool_schema=NEWS_SENTIMENT_TOOL_SCHEMA,
                tool_name="news_sentiment_analysis",
                role="chief_analyst",
            )
        except Exception:
            # Fall back to rule-based on LLM failure
            result = rule_based_sentiment(titles)
            result.article_count = len(articles)
            result.date_range = date_range
            return result

        score = raw.get("sentiment_score", 0.0)
        if isinstance(score, str):
            try:
                score = float(score)
            except (ValueError, TypeError):
                score = 0.0
        score = max(-1.0, min(1.0, score))

        # BUG-Y26: harden list parse boundaries
        from aegis.core._coerce import coerce_list
        return NewsSentimentInsights(
            overall_sentiment=raw.get("overall_sentiment", "neutral"),
            sentiment_score=score,
            sentiment_trend=raw.get("sentiment_trend", "stable"),
            key_themes=coerce_list(raw.get("key_themes", [])),
            bullish_signals=coerce_list(raw.get("bullish_signals", [])),
            bearish_signals=coerce_list(raw.get("bearish_signals", [])),
            news_summary=raw.get("news_summary", ""),
            materiality=raw.get("materiality", "medium"),
            article_count=len(articles),
            date_range=date_range,
        )

    def _build_message(
        self,
        articles: list[Any],
        symbol: str,
        entity_name: str,
    ) -> str:
        parts = [
            f"=== RECENT NEWS: {entity_name or symbol} ({symbol}) ===",
            "",
            f"Analyze the following {len(articles)} recent news articles for investment sentiment.",
            "",
            "=== ARTICLES BEGIN ===",
        ]

        for i, a in enumerate(articles, 1):
            title = getattr(a, "title", "")
            summary = getattr(a, "summary", "")
            date = getattr(a, "published", "")
            source = getattr(a, "source", "")
            parts.append(f"\n[{i}] {date} — {source}")
            parts.append(f"Title: {title}")
            if summary:
                # Truncate long summaries
                if len(summary) > 300:
                    summary = summary[:300] + "..."
                parts.append(f"Summary: {summary}")

        parts.append("\n=== ARTICLES END ===")
        return "\n".join(parts)

    @staticmethod
    def _compute_date_range(dates: list[str]) -> str:
        """Compute human-readable date range from ISO date strings."""
        valid = sorted([d for d in dates if d and len(d) >= 10])
        if not valid:
            return ""
        if len(valid) == 1:
            return valid[0]

        from datetime import datetime as dt
        try:
            start = dt.strptime(valid[0][:10], "%Y-%m-%d")
            end = dt.strptime(valid[-1][:10], "%Y-%m-%d")
            return f"{start.strftime('%b %d')} - {end.strftime('%b %d, %Y')}"
        except ValueError:
            return f"{valid[0]} - {valid[-1]}"
