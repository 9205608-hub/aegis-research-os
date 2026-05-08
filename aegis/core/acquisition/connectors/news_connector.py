"""News Connector — fetches recent news via yfinance.

Data source: Yahoo Finance (free, no API key required).
Supports US tickers and A-share tickers (auto-converts to .SS/.SZ format).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class NewsArticle:
    """A single news article."""

    title: str
    summary: str
    published: str  # ISO date string
    source: str  # provider display name
    url: str


def _to_yfinance_symbol(ticker: str) -> str:
    """Convert ticker to yfinance format.

    US tickers pass through unchanged.
    A-share 6-digit codes → .SS/.SZ suffix.
    """
    clean = ticker.replace(".SS", "").replace(".SZ", "").strip()
    if len(clean) == 6 and clean.isdigit():
        if clean.startswith("6"):
            return f"{clean}.SS"
        elif clean.startswith(("0", "3")):
            return f"{clean}.SZ"
    return ticker


class NewsConnector:
    """Fetches recent news for a ticker via yfinance."""

    def get_recent_news(
        self, ticker: str, limit: int = 20
    ) -> list[NewsArticle]:
        """Fetch recent news articles for a ticker.

        Args:
            ticker: Stock ticker (US or A-share 6-digit code).
            limit: Maximum articles to return.

        Returns:
            List of NewsArticle sorted by date (newest first).
        """
        # BUG-29 (extension): yfinance has effectively no news coverage for
        # A-shares (Yahoo doesn't index Chinese-language sources) but still
        # prints "$TICKER: possibly delisted" to stderr on each empty fetch,
        # which misleads users into thinking the company is delisted. Skip
        # A-share tickers entirely; A-share news flow comes from the news
        # sentiment analyzer's eastmoney/sina path elsewhere if enabled.
        clean = ticker.replace(".SS", "").replace(".SZ", "").strip()
        if (clean.isdigit() and len(clean) == 6) or ticker.endswith((".SS", ".SZ")):
            return []

        try:
            import yfinance as yf
        except ImportError:
            return []

        symbol = _to_yfinance_symbol(ticker)

        try:
            t = yf.Ticker(symbol)
            raw_news = t.news or []
        except Exception:
            return []

        articles: list[NewsArticle] = []
        for item in raw_news[:limit]:
            content = item.get("content", {}) if isinstance(item, dict) else {}
            if not content:
                continue

            title = content.get("title", "").strip()
            if not title:
                continue

            summary = content.get("summary", "").strip()
            pub_date = content.get("pubDate", "")
            provider = content.get("provider", {})
            source = provider.get("displayName", "") if isinstance(provider, dict) else ""
            canonical = content.get("canonicalUrl", {})
            url = canonical.get("url", "") if isinstance(canonical, dict) else ""

            # Normalize date to YYYY-MM-DD
            date_str = ""
            if pub_date:
                try:
                    dt = datetime.fromisoformat(pub_date.replace("Z", "+00:00"))
                    date_str = dt.strftime("%Y-%m-%d")
                except (ValueError, TypeError):
                    date_str = pub_date[:10] if len(pub_date) >= 10 else pub_date

            articles.append(NewsArticle(
                title=title,
                summary=summary,
                published=date_str,
                source=source,
                url=url,
            ))

        return articles
