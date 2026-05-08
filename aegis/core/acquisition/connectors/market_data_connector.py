"""Market Data Connector — real-time prices, market cap, and basic fundamentals.

Uses Yahoo Finance (yfinance) as the free data source.
Provides current price, market cap, shares outstanding, and basic valuation ratios.

Tier 3 source — market data, not regulatory filings.

Usage:
    connector = MarketDataConnector()
    snapshot = connector.get_snapshot("META")
    # snapshot.current_price, snapshot.market_cap, snapshot.shares_outstanding, ...

    history = connector.get_price_history("META", period="1y")
    # list of (date, close_price)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class MarketSnapshot:
    """Point-in-time market data for an entity."""

    ticker: str
    current_price: float
    market_cap: float
    shares_outstanding: float
    currency: str = "USD"
    exchange: str = ""

    # Basic valuation from market data
    pe_trailing: float | None = None
    pe_forward: float | None = None
    price_to_book: float | None = None
    dividend_yield: float | None = None
    beta: float | None = None

    # Consensus
    analyst_target_mean: float | None = None
    analyst_target_low: float | None = None
    analyst_target_high: float | None = None
    analyst_recommendation: str | None = None  # "buy", "hold", "sell"
    analyst_count: int | None = None

    # 52-week
    fifty_two_week_high: float | None = None
    fifty_two_week_low: float | None = None

    # Liquidity
    average_volume: float | None = None        # 3-month avg daily volume
    average_volume_10d: float | None = None    # 10-day avg daily volume

    # Metadata
    fetched_at: str = ""
    source: str = "yahoo_finance"

    def to_market_data_dict(self) -> dict[str, float]:
        """Convert to the market_data dict format used by the pipeline."""
        return {
            "current_price": self.current_price,
            "market_cap": self.market_cap,
            "shares_outstanding": self.shares_outstanding,
        }


@dataclass
class PricePoint:
    """A single price data point."""

    date: str
    close: float
    volume: int = 0


class MarketDataConnector:
    """Market data connector using Yahoo Finance.

    Free, no API key required. Rate-limited by yfinance internally.
    """

    source_id: str = "yahoo_finance"
    source_tier: str = "tier_3"
    market_id: str = "global"

    def __init__(self) -> None:
        self._cache: dict[str, tuple[MarketSnapshot, float]] = {}
        self._cache_ttl = 300  # 5 minutes

    def get_snapshot(self, ticker: str) -> MarketSnapshot:
        """Get current market data for a ticker.

        Returns cached data if less than 5 minutes old.
        """
        now = datetime.now(timezone.utc).timestamp()

        # Check cache
        if ticker in self._cache:
            cached, cached_at = self._cache[ticker]
            if now - cached_at < self._cache_ttl:
                return cached

        snapshot = self._fetch_snapshot(ticker)
        self._cache[ticker] = (snapshot, now)
        return snapshot

    def get_price_history(
        self, ticker: str, period: str = "1y",
    ) -> list[PricePoint]:
        """Get historical price data.

        Args:
            ticker: Stock ticker
            period: yfinance period string ("1mo", "3mo", "6mo", "1y", "2y", "5y")
        """
        # BUG-29: yfinance.history() for A-shares is unreliable in this
        # environment and prints noisy "may be delisted" stderr. Skip the
        # round-trip; A-share price history is supplied via akshare elsewhere.
        if self._to_cn_code(ticker) is not None:
            return []
        try:
            import yfinance as yf
            stock = yf.Ticker(ticker)
            hist = stock.history(period=period)

            points = []
            for idx, row in hist.iterrows():
                points.append(PricePoint(
                    date=idx.strftime("%Y-%m-%d"),
                    close=float(row["Close"]),
                    volume=int(row["Volume"]),
                ))
            return points
        except Exception:
            return []

    def get_multi_snapshot(self, tickers: list[str]) -> dict[str, MarketSnapshot]:
        """Get snapshots for multiple tickers."""
        return {t: self.get_snapshot(t) for t in tickers}

    def _fetch_snapshot(self, ticker: str) -> MarketSnapshot:
        """Fetch live market data. yfinance primary; Tencent/Sina fallback
        for A-shares when yfinance returns zero price (BUG-25, 2026-04-23).

        BUG-29 (2026-05-05): for A-share tickers, skip yfinance entirely.
        Under the user's Clash Verge proxy yfinance reliably blanks out for
        .SS/.SZ symbols and prints '$TICKER: possibly delisted' to stderr,
        which mis-signals delisting in our logs. The Tencent / Sina path
        below is authoritative for A-shares anyway, so skipping the yfinance
        round-trip both quietens the log AND saves a 5-15s timeout.
        """
        info: dict[str, Any] = {}
        is_a_share = self._to_cn_code(ticker) is not None
        if not is_a_share:
            try:
                import yfinance as yf
                stock = yf.Ticker(ticker)
                info = stock.info or {}
            except Exception:
                info = {}

        price = (info.get("currentPrice")
                 or info.get("regularMarketPrice")
                 or 0.0)
        market_cap = info.get("marketCap") or 0.0
        shares = info.get("sharesOutstanding") or 0.0
        currency = info.get("currency", "USD") if not is_a_share else "CNY"

        # BUG-25: A-share fallback via Tencent / Sina when yfinance blanks out.
        # Triggers on 600089 / 600089.SS / sh600089 etc. Does NOT override a
        # good yfinance result. We only patch fields yfinance left zero so
        # downstream 52w / analyst / beta fields remain authoritative when
        # yfinance did answer.
        if price <= 0:
            cn_code = self._to_cn_code(ticker)
            if cn_code:
                try:
                    from .tencent_sina_quote import fetch_cn_quote
                    q = fetch_cn_quote(cn_code)
                except Exception:
                    q = None
                if q is not None and q.current_price > 0:
                    price = q.current_price
                    currency = "CNY"
                    # Backfill market_cap / shares from Tencent's tick when
                    # yfinance left them zero (typical for A-share under the
                    # user's Clash proxy). Values are internally consistent
                    # because shares is derived from cap/price at Tencent.
                    if market_cap <= 0 and q.market_cap > 0:
                        market_cap = q.market_cap
                    if shares <= 0 and q.shares_outstanding > 0:
                        shares = q.shares_outstanding

        return MarketSnapshot(
            ticker=ticker,
            current_price=price,
            market_cap=market_cap,
            shares_outstanding=shares,
            currency=currency,
            exchange=info.get("exchange", ""),
            pe_trailing=info.get("trailingPE"),
            pe_forward=info.get("forwardPE"),
            price_to_book=info.get("priceToBook"),
            dividend_yield=info.get("dividendYield"),
            beta=info.get("beta"),
            analyst_target_mean=info.get("targetMeanPrice"),
            analyst_target_low=info.get("targetLowPrice"),
            analyst_target_high=info.get("targetHighPrice"),
            analyst_recommendation=info.get("recommendationKey"),
            analyst_count=info.get("numberOfAnalystOpinions"),
            fifty_two_week_high=info.get("fiftyTwoWeekHigh"),
            fifty_two_week_low=info.get("fiftyTwoWeekLow"),
            average_volume=info.get("averageVolume"),
            average_volume_10d=info.get("averageVolume10days"),
            fetched_at=datetime.now(timezone.utc).isoformat(),
        )

    @staticmethod
    def _to_cn_code(ticker: str) -> str | None:
        """Extract a bare 6-digit A-share code from common inputs, else None.

        Accepts: '600089', '600089.SS', '000001.SZ', 'sh600089', 'SZ301358'.
        Rejects: 'AAPL', 'BRK.B', anything without exactly 6 digits after
        stripping exchange prefixes.
        """
        t = ticker.strip().upper()
        # Strip exchange prefixes/suffixes we know
        for prefix in ("SH", "SZ"):
            if t.startswith(prefix):
                t = t[len(prefix):]
                break
        for suffix in (".SS", ".SZ"):
            if t.endswith(suffix):
                t = t[:-len(suffix)]
                break
        if t.isdigit() and len(t) == 6:
            return t
        return None
