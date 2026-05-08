"""OpenBB Data Connector — Unified data bridge for consensus, macro, and peer data.

Leverages the OpenBB Platform to fill critical data gaps:
  1. Consensus estimates (FMP provider) → fills ConsensusStore → activates Variant Analyst
  2. Macro data (FRED provider) → automates MacroSnapshot (fed funds, CPI, PMI, etc.)
  3. Peer fundamentals (FMP provider) → activates Comparative Analyst
  4. Earnings history (FMP provider) → beat/miss tracking

Tier 2 source — aggregated third-party data.

Usage:
    connector = OpenBBConnector(fmp_api_key="...", fred_api_key="...")
    consensus = connector.get_consensus_estimates("AAPL")
    macro = connector.get_macro_snapshot()
    peers = connector.get_peer_fundamentals(["AAPL", "MSFT", "GOOG"])
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ConsensusEstimate:
    """Analyst consensus for a single metric and period."""

    symbol: str
    period: str                     # "FY2025", "FY2026", "Q1_2025"
    period_type: str                # "annual" | "quarterly"
    metric: str                     # "revenue", "eps", "ebitda"
    consensus_mean: float
    consensus_high: float
    consensus_low: float
    analyst_count: int
    fetched_at: str = ""
    source: str = "openbb_fmp"


# Consensus high/low are routinely contaminated by mis-period analyst
# entries (e.g. an FY2027 estimate filed under FY_Next). The mean is
# usually robust because it averages across all analysts, but the high
# and low extremes can be nonsense — we've seen FY_Next high = 1.59× mean
# (301358) where the "high" was actually a 2-year-out forecast misfiled.
#
# Heuristic: identify outlier tails by combining TWO signals:
#   1. The tail itself is wide (>1.5× from mean)
#   2. EITHER the opposite tail is tight (<1.2×, signaling the wide tail
#      is alone in its extremity) OR the asymmetry ratio between tails
#      exceeds 15% (signaling one side is materially more extreme)
# We don't touch tails when both sides are uniformly wide — that's
# genuine analyst disagreement on a speculative estimate, not a data
# error. Clip the offending side back to a defensible 1.5× bound.
_CONSENSUS_OUTLIER_RATIO = 1.5     # tail >1.5× from mean is candidate for clipping
_CONSENSUS_TIGHT_RATIO = 1.2       # opposite tail this tight → wide tail is alone
_CONSENSUS_ASYMMETRY = 1.15        # tail-to-tail ratio above this = asymmetric
_CONSENSUS_CLIP_HIGH = 1.5         # clip outlier high to this × mean
_CONSENSUS_CLIP_LOW = 1.0 / 1.5    # clip outlier low to this × mean


def _clip_consensus_tails(
    mean: float, high: float, low: float, metric: str = ""
) -> tuple[float, float, bool]:
    """Detect and clip asymmetric outlier tails on a consensus estimate.

    Returns (clipped_high, clipped_low, was_clipped). Logs when clipping
    fires so we can audit data-source quality over time.
    """
    if mean is None or mean <= 0:
        # Negative-mean case (e.g. EPS for a money-losing company): clip
        # logic uses ratios that don't transfer cleanly. Pass through.
        return high, low, False
    high = float(high) if high is not None else mean
    low = float(low) if low is not None else mean
    high_dev = high / mean if mean > 0 else 1.0
    low_dev = mean / low if low > 0 else 1.0
    clipped = False
    new_high = high
    new_low = low
    # Asymmetry: which tail is bigger relative to the other?
    asym_high = (high_dev / low_dev) if low_dev > 0 else 1.0
    asym_low = (low_dev / high_dev) if high_dev > 0 else 1.0
    # High tail outlier: wide AND (opposite is tight OR materially asymmetric)
    if high_dev > _CONSENSUS_OUTLIER_RATIO and (
        low_dev < _CONSENSUS_TIGHT_RATIO or asym_high > _CONSENSUS_ASYMMETRY
    ):
        new_high = mean * _CONSENSUS_CLIP_HIGH
        clipped = True
    # Low tail outlier: wide AND (opposite is tight OR materially asymmetric)
    if low_dev > _CONSENSUS_OUTLIER_RATIO and (
        high_dev < _CONSENSUS_TIGHT_RATIO or asym_low > _CONSENSUS_ASYMMETRY
    ):
        new_low = mean * _CONSENSUS_CLIP_LOW
        clipped = True
    if clipped:
        logger.info(
            f"consensus {metric or '?'}: clipped asymmetric tails — "
            f"raw low/mean/high = {low:.2g}/{mean:.2g}/{high:.2g} → "
            f"{new_low:.2g}/{mean:.2g}/{new_high:.2g}"
        )
    return new_high, new_low, clipped


@dataclass
class EarningsHistoryItem:
    """Historical earnings beat/miss record."""

    symbol: str
    report_date: str
    eps_consensus: float | None = None
    eps_actual: float | None = None
    revenue_consensus: float | None = None
    revenue_actual: float | None = None

    @property
    def eps_surprise_pct(self) -> float | None:
        if self.eps_consensus and self.eps_actual and self.eps_consensus != 0:
            return (self.eps_actual - self.eps_consensus) / abs(self.eps_consensus)
        return None

    @property
    def revenue_surprise_pct(self) -> float | None:
        if self.revenue_consensus and self.revenue_actual and self.revenue_consensus != 0:
            return (self.revenue_actual - self.revenue_consensus) / abs(self.revenue_consensus)
        return None


@dataclass
class MacroDataPoint:
    """A single macro time series observation."""

    series_id: str
    date: str
    value: float
    label: str = ""


@dataclass
class PeerFundamentals:
    """Financial snapshot for a single peer company."""

    symbol: str
    name: str = ""
    market_cap: float = 0
    revenue: float = 0
    net_income: float = 0
    gross_margin: float = 0
    operating_margin: float = 0
    net_margin: float = 0
    roe: float = 0
    roic: float | None = None
    pe_trailing: float | None = None
    pe_forward: float | None = None
    ev_to_ebitda: float | None = None
    ev_to_revenue: float | None = None
    revenue_growth_yoy: float | None = None
    dividend_yield: float | None = None
    debt_to_equity: float | None = None


@dataclass
class OpenBBMacroSnapshot:
    """Full macro snapshot derived from FRED and other OpenBB sources."""

    fed_funds_rate: float = 0.0
    us_10y_yield: float = 0.0
    us_2y_yield: float = 0.0
    yield_curve_slope_2s10s: float = 0.0   # bps
    cpi_yoy: float = 0.0
    core_cpi_yoy: float = 0.0
    unemployment_rate: float = 0.0
    pmi_manufacturing: float = 0.0
    vix: float = 0.0
    usd_dxy: float = 0.0
    fetched_at: str = ""


# FRED series IDs for macro data
FRED_SERIES = {
    "fed_funds_rate": "DFF",             # Daily Federal Funds Rate
    "us_10y_yield": "DGS10",             # 10-Year Treasury Constant Maturity
    "us_2y_yield": "DGS2",              # 2-Year Treasury
    "cpi_yoy": "CPIAUCSL",              # CPI for All Urban Consumers
    "core_cpi_yoy": "CPILFESL",         # Core CPI (ex Food & Energy)
    "unemployment_rate": "UNRATE",       # Civilian Unemployment Rate
    "pmi_manufacturing": "MANEMP",       # ISM Manufacturing PMI proxy
    "vix": "VIXCLS",                     # VIX
    "usd_dxy": "DTWEXBGS",             # Trade Weighted US Dollar Index
}


class OpenBBConnector:
    """Unified connector for OpenBB Platform data sources.

    Wraps OpenBB's Python API to provide structured data for Aegis.
    Falls back gracefully if OpenBB is not installed or API keys are missing.
    """

    source_id: str = "openbb"
    source_tier: str = "tier_2"

    def __init__(
        self,
        fmp_api_key: str | None = None,
        fred_api_key: str | None = None,
    ) -> None:
        self._fmp_key = fmp_api_key
        self._fred_key = fred_api_key
        self._obb = None
        self._initialized = False

    def _ensure_init(self) -> bool:
        """Lazy-init OpenBB. Returns True if available."""
        if self._initialized:
            return self._obb is not None
        self._initialized = True
        try:
            # SSL bypass for proxy environments (e.g., Clash Verge)
            import ssl
            ssl._create_default_https_context = ssl._create_unverified_context

            from openbb import obb
            self._obb = obb
            if self._fmp_key:
                obb.user.credentials.fmp_api_key = self._fmp_key
            if self._fred_key:
                obb.user.credentials.fred_api_key = self._fred_key
            logger.info("OpenBB Platform initialized successfully")
            return True
        except ImportError:
            logger.warning("OpenBB not installed. Install with: pip install openbb openbb-fmp openbb-fred")
            return False
        except Exception as e:
            logger.warning(f"OpenBB initialization failed: {e}")
            return False

    # ── Consensus Estimates ──────────────────────────────────────────

    def get_consensus_estimates(
        self,
        symbol: str,
        periods: int = 5,
        period_type: str = "annual",
    ) -> list[ConsensusEstimate]:
        """Fetch analyst consensus estimates for revenue, EPS, and EBITDA.

        Uses FMP via OpenBB if API key is available, otherwise falls back
        to yfinance (free, no key required).
        """
        # Try yfinance first (free, always available)
        results = self._get_consensus_yfinance(symbol, period_type)
        if results:
            return results

        # Fallback to FMP via OpenBB if key is available
        if not self._ensure_init() or not self._fmp_key:
            return []

        results = []
        now_str = datetime.now(timezone.utc).isoformat()

        try:
            data = self._obb.equity.estimates.historical(
                symbol=symbol,
                period=period_type,
                limit=periods,
                provider="fmp",
            )
            df = data.to_dataframe()
            if df.empty:
                logger.warning(f"No consensus data returned for {symbol}")
                return []

            for _, row in df.iterrows():
                date_str = str(row.get("date", ""))
                # Determine fiscal period label
                if period_type == "annual":
                    try:
                        year = datetime.strptime(date_str[:10], "%Y-%m-%d").year
                        period_label = f"FY{year}"
                    except (ValueError, TypeError):
                        period_label = date_str
                else:
                    period_label = date_str

                # Revenue estimate
                rev_avg = row.get("estimated_revenue_avg")
                if rev_avg and rev_avg > 0:
                    rev_high, rev_low, _ = _clip_consensus_tails(
                        float(rev_avg),
                        float(row.get("estimated_revenue_high", rev_avg)),
                        float(row.get("estimated_revenue_low", rev_avg)),
                        metric=f"{symbol} {period_label} revenue",
                    )
                    results.append(ConsensusEstimate(
                        symbol=symbol,
                        period=period_label,
                        period_type=period_type,
                        metric="revenue",
                        consensus_mean=float(rev_avg),
                        consensus_high=rev_high,
                        consensus_low=rev_low,
                        analyst_count=int(row.get("number_analysts_estimated_revenue", 0)),
                        fetched_at=now_str,
                    ))

                # EPS estimate
                eps_avg = row.get("estimated_eps_avg")
                if eps_avg is not None:
                    eps_high, eps_low, _ = _clip_consensus_tails(
                        float(eps_avg),
                        float(row.get("estimated_eps_high", eps_avg)),
                        float(row.get("estimated_eps_low", eps_avg)),
                        metric=f"{symbol} {period_label} eps",
                    )
                    results.append(ConsensusEstimate(
                        symbol=symbol,
                        period=period_label,
                        period_type=period_type,
                        metric="eps",
                        consensus_mean=float(eps_avg),
                        consensus_high=eps_high,
                        consensus_low=eps_low,
                        analyst_count=int(row.get("number_analysts_eps", 0)),
                        fetched_at=now_str,
                    ))

                # EBITDA estimate
                ebitda_avg = row.get("estimated_ebitda_avg")
                if ebitda_avg and ebitda_avg > 0:
                    ebitda_high, ebitda_low, _ = _clip_consensus_tails(
                        float(ebitda_avg),
                        float(row.get("estimated_ebitda_high", ebitda_avg)),
                        float(row.get("estimated_ebitda_low", ebitda_avg)),
                        metric=f"{symbol} {period_label} ebitda",
                    )
                    results.append(ConsensusEstimate(
                        symbol=symbol,
                        period=period_label,
                        period_type=period_type,
                        metric="ebitda",
                        consensus_mean=float(ebitda_avg),
                        consensus_high=ebitda_high,
                        consensus_low=ebitda_low,
                        analyst_count=int(row.get("number_analysts_estimated_ebitda", 0)),
                        fetched_at=now_str,
                    ))

            logger.info(f"Fetched {len(results)} consensus estimates for {symbol}")
        except Exception as e:
            logger.warning(f"Failed to fetch consensus estimates for {symbol}: {e}")

        return results

    def get_price_target_consensus(self, symbol: str) -> dict[str, Any]:
        """Get consensus price target and recommendation.

        Uses yfinance (free) — no API key needed.
        """
        try:
            import yfinance as yf
            stock = yf.Ticker(symbol)
            info = stock.info or {}
            target_mean = info.get("targetMeanPrice")
            if not target_mean:
                return {}
            return {
                "target_high": float(info.get("targetHighPrice", 0)),
                "target_low": float(info.get("targetLowPrice", 0)),
                "target_consensus": float(target_mean),
                "target_median": float(info.get("targetMedianPrice", target_mean)),
                "analyst_count": int(info.get("numberOfAnalystOpinions", 0)),
                "recommendation": info.get("recommendationKey", ""),
            }
        except Exception as e:
            logger.warning(f"Failed to fetch price target for {symbol}: {e}")
            return {}

    # ── Earnings History ─────────────────────────────────────────────

    def get_earnings_history(
        self, symbol: str, limit: int = 12,
    ) -> list[EarningsHistoryItem]:
        """Fetch historical earnings beat/miss data.

        Uses yfinance (free) first, falls back to FMP via OpenBB.
        """
        # Try yfinance first
        results = self._get_earnings_yfinance(symbol, limit)
        if results:
            return results

        # Fallback to FMP
        if not self._ensure_init() or not self._fmp_key:
            return []

        results = []
        try:
            data = self._obb.equity.calendar.earnings(
                symbol=symbol, provider="fmp",
            )
            df = data.to_dataframe()
            for _, row in df.iterrows():
                results.append(EarningsHistoryItem(
                    symbol=symbol,
                    report_date=str(row.get("report_date", "")),
                    eps_consensus=_safe_float(row.get("eps_consensus")),
                    eps_actual=_safe_float(row.get("eps_actual")),
                    revenue_consensus=_safe_float(row.get("revenue_consensus")),
                    revenue_actual=_safe_float(row.get("revenue_actual")),
                ))
            logger.info(f"Fetched {len(results)} earnings history items for {symbol}")
        except Exception as e:
            logger.warning(f"Failed to fetch earnings history for {symbol}: {e}")

        return results[:limit]

    # ── Macro Data (FRED) ────────────────────────────────────────────

    def get_macro_snapshot(self) -> OpenBBMacroSnapshot:
        """Build a complete macro snapshot from FRED data.

        Fetches the latest values for key macro indicators.
        """
        if not self._ensure_init():
            return OpenBBMacroSnapshot(fetched_at=datetime.now(timezone.utc).isoformat())

        snap = OpenBBMacroSnapshot(fetched_at=datetime.now(timezone.utc).isoformat())

        # Fetch each series
        for field_name, series_id in FRED_SERIES.items():
            try:
                val = self._get_fred_latest(series_id)
                if val is not None:
                    # Convert percentage values (FRED reports rates as percentages)
                    if field_name in ("fed_funds_rate", "us_10y_yield", "us_2y_yield",
                                      "unemployment_rate"):
                        val = val / 100.0
                    setattr(snap, field_name, val)
            except Exception as e:
                logger.debug(f"Failed to fetch FRED {series_id}: {e}")

        # CPI: FRED gives index level, need to compute YoY% from last 13 months
        try:
            cpi_series = self.get_fred_series("CPIAUCSL", limit=13)
            if len(cpi_series) >= 13:
                current = cpi_series[-1].value
                year_ago = cpi_series[0].value
                if year_ago > 0:
                    snap.cpi_yoy = (current - year_ago) / year_ago
            core_series = self.get_fred_series("CPILFESL", limit=13)
            if len(core_series) >= 13:
                current = core_series[-1].value
                year_ago = core_series[0].value
                if year_ago > 0:
                    snap.core_cpi_yoy = (current - year_ago) / year_ago
        except Exception as e:
            logger.debug(f"CPI YoY computation failed: {e}")

        # Compute yield curve slope
        if snap.us_10y_yield and snap.us_2y_yield:
            snap.yield_curve_slope_2s10s = round(
                (snap.us_10y_yield - snap.us_2y_yield) * 10000, 1,
            )  # in bps

        return snap

    def get_fred_series(
        self, series_id: str, start_date: str | None = None, limit: int = 12,
    ) -> list[MacroDataPoint]:
        """Fetch a specific FRED time series."""
        if not self._ensure_init():
            return []

        try:
            kwargs: dict[str, Any] = {"symbol": series_id, "provider": "fred"}
            if start_date:
                kwargs["start_date"] = start_date
            data = self._obb.economy.fred_series(**kwargs)
            df = data.to_dataframe()
            results = []
            for idx, row in df.tail(limit).iterrows():
                # Column is named after series_id, not "value"
                val = float(row.get(series_id, 0)) if series_id in row.index else float(row.iloc[0])
                results.append(MacroDataPoint(
                    series_id=series_id,
                    date=str(idx),
                    value=val,
                ))
            return results
        except Exception as e:
            logger.warning(f"Failed to fetch FRED series {series_id}: {e}")
            return []

    def _get_fred_latest(self, series_id: str) -> float | None:
        """Get the most recent value for a FRED series."""
        try:
            data = self._obb.economy.fred_series(symbol=series_id, provider="fred")
            df = data.to_dataframe()
            if df.empty:
                return None
            # OpenBB FRED returns column named after series_id (e.g., "DFF"), not "value"
            last_row = df.iloc[-1]
            if series_id in last_row.index:
                return float(last_row[series_id])
            # Fallback: try first numeric column
            for col in last_row.index:
                try:
                    return float(last_row[col])
                except (ValueError, TypeError):
                    continue
            return None
        except Exception:
            return None

    # ── Peer Fundamentals ────────────────────────────────────────────

    def get_peer_fundamentals(
        self, symbols: list[str],
    ) -> list[PeerFundamentals]:
        """Fetch key financial metrics for peer companies.

        Uses FMP's financial ratios and key metrics endpoints.
        """
        if not self._ensure_init():
            return []

        results: list[PeerFundamentals] = []
        for symbol in symbols:
            try:
                peer = self._fetch_single_peer(symbol)
                if peer:
                    results.append(peer)
            except Exception as e:
                logger.warning(f"Failed to fetch peer data for {symbol}: {e}")

        return results

    def _fetch_single_peer(self, symbol: str) -> PeerFundamentals | None:
        """Fetch fundamentals for a single peer company via yfinance (free)."""
        try:
            import yfinance as yf
            stock = yf.Ticker(symbol)
            info = stock.info or {}

            if not info.get("marketCap"):
                return None

            # Currency normalization: yfinance reports financials in financialCurrency
            # but marketCap is always in the listing currency (often USD for US-listed ADRs).
            # For non-USD financial statements (TSM=TWD, ASML=EUR), convert revenue/income to USD.
            fin_currency = (info.get("financialCurrency") or "USD").upper()
            price_currency = (info.get("currency") or "USD").upper()
            fx_rate = 1.0
            if fin_currency != "USD":
                # Approximate FX rates (refreshed per-session from yfinance if available)
                _fx_approx = {
                    "TWD": 0.031, "EUR": 1.08, "GBP": 1.27, "JPY": 0.0067,
                    "KRW": 0.00074, "CNY": 0.138, "HKD": 0.128, "INR": 0.012,
                    "CAD": 0.74, "AUD": 0.65, "CHF": 1.13, "SEK": 0.097,
                }
                fx_rate = _fx_approx.get(fin_currency, 1.0)
                # Try live FX from yfinance
                try:
                    fx_ticker = yf.Ticker(f"{fin_currency}USD=X")
                    fx_info = fx_ticker.info or {}
                    live_rate = fx_info.get("regularMarketPrice") or fx_info.get("previousClose")
                    if live_rate and live_rate > 0:
                        fx_rate = live_rate
                except Exception:
                    pass  # Use approximate rate

            peer = PeerFundamentals(
                symbol=symbol,
                name=info.get("shortName", symbol),
                market_cap=float(info.get("marketCap", 0)),  # already in listing currency (usually USD)
                pe_trailing=_safe_float(info.get("trailingPE")),
                pe_forward=_safe_float(info.get("forwardPE")),
                dividend_yield=_safe_float(info.get("dividendYield")),
            )

            # Get margins from info (ratios are currency-independent)
            peer.gross_margin = float(info.get("grossMargins", 0) or 0)
            peer.operating_margin = float(info.get("operatingMargins", 0) or 0)
            peer.net_margin = float(info.get("profitMargins", 0) or 0)
            peer.roe = float(info.get("returnOnEquity", 0) or 0)
            # Convert absolute values to USD
            peer.revenue = float(info.get("totalRevenue", 0) or 0) * fx_rate
            peer.net_income = float(info.get("netIncomeToCommon", 0) or 0) * fx_rate
            peer.debt_to_equity = _safe_float(info.get("debtToEquity"))
            if peer.debt_to_equity:
                peer.debt_to_equity = peer.debt_to_equity / 100  # yfinance reports as percentage

            # EV/EBITDA
            peer.ev_to_ebitda = _safe_float(info.get("enterpriseToEbitda"))

            return peer
        except Exception as e:
            logger.debug(f"Peer fetch error for {symbol}: {e}")
            return None

    # ── yfinance fallback methods (free, no API key) ────────────────

    def _get_consensus_yfinance(
        self, symbol: str, period_type: str = "annual",
    ) -> list[ConsensusEstimate]:
        """Get consensus estimates from yfinance (free, no key needed)."""
        results: list[ConsensusEstimate] = []
        now_str = datetime.now(timezone.utc).isoformat()

        try:
            import yfinance as yf
            stock = yf.Ticker(symbol)

            # Revenue estimates
            rev_est = stock.revenue_estimate
            if rev_est is not None and not rev_est.empty:
                period_map = {"0q": "CQ", "+1q": "NQ", "0y": "FY_Current", "+1y": "FY_Next"}
                for period_code, row in rev_est.iterrows():
                    period_label = period_map.get(str(period_code), str(period_code))
                    avg = _safe_float(row.get("avg"))
                    if avg and avg > 0:
                        rev_high, rev_low, _ = _clip_consensus_tails(
                            avg,
                            float(row.get("high", avg)),
                            float(row.get("low", avg)),
                            metric=f"{symbol} {period_label} revenue",
                        )
                        results.append(ConsensusEstimate(
                            symbol=symbol,
                            period=period_label,
                            period_type="quarterly" if "q" in str(period_code) else "annual",
                            metric="revenue",
                            consensus_mean=avg,
                            consensus_high=rev_high,
                            consensus_low=rev_low,
                            analyst_count=int(row.get("numberOfAnalysts", 0)),
                            fetched_at=now_str,
                            source="yfinance",
                        ))

            # EPS estimates
            eps_est = stock.earnings_estimate
            if eps_est is not None and not eps_est.empty:
                period_map = {"0q": "CQ", "+1q": "NQ", "0y": "FY_Current", "+1y": "FY_Next"}
                for period_code, row in eps_est.iterrows():
                    period_label = period_map.get(str(period_code), str(period_code))
                    avg = _safe_float(row.get("avg"))
                    if avg is not None:
                        eps_high, eps_low, _ = _clip_consensus_tails(
                            avg,
                            float(row.get("high", avg)),
                            float(row.get("low", avg)),
                            metric=f"{symbol} {period_label} eps",
                        )
                        results.append(ConsensusEstimate(
                            symbol=symbol,
                            period=period_label,
                            period_type="quarterly" if "q" in str(period_code) else "annual",
                            metric="eps",
                            consensus_mean=avg,
                            consensus_high=eps_high,
                            consensus_low=eps_low,
                            analyst_count=int(row.get("numberOfAnalysts", 0)),
                            fetched_at=now_str,
                            source="yfinance",
                        ))

            if results:
                logger.info(f"yfinance: {len(results)} consensus estimates for {symbol}")

        except Exception as e:
            logger.debug(f"yfinance consensus failed for {symbol}: {e}")

        return results

    def _get_earnings_yfinance(
        self, symbol: str, limit: int = 12,
    ) -> list[EarningsHistoryItem]:
        """Get earnings history (beat/miss) from yfinance (free)."""
        results: list[EarningsHistoryItem] = []

        try:
            import yfinance as yf
            stock = yf.Ticker(symbol)
            earns = stock.earnings_dates

            if earns is None or earns.empty:
                return []

            for idx, row in earns.iterrows():
                eps_est = _safe_float(row.get("EPS Estimate"))
                eps_act = _safe_float(row.get("Reported EPS"))
                # Only include rows that have actual reported data
                if eps_act is None:
                    continue
                results.append(EarningsHistoryItem(
                    symbol=symbol,
                    report_date=str(idx)[:10],
                    eps_consensus=eps_est,
                    eps_actual=eps_act,
                ))

            if results:
                logger.info(f"yfinance: {len(results)} earnings history for {symbol}")

        except Exception as e:
            logger.debug(f"yfinance earnings failed for {symbol}: {e}")

        return results[:limit]

    # ── Historical Valuation ──────────────────────────────────────────

    def get_historical_valuation(
        self,
        symbol: str,
        years: int = 5,
        ev_ebitda_override: float | None = None,
    ) -> dict[str, Any]:
        """Fetch historical P/E and EV/EBITDA multiples using yfinance (free).

        Returns dict with monthly data points for chart rendering:
        {
            "dates": ["2020-01", "2020-02", ...],
            "pe_ratio": [25.3, 26.1, ...],
            "ev_ebitda": [18.2, 19.0, ...],
            "pe_stats": {"min": 15, "max": 45, "median": 28, "p25": 22, "p75": 33},
            "ev_ebitda_stats": {"min": 10, "max": 30, "median": 20, ...},
        }
        """
        # BUG-fix (2026-04-15): yfinance .SZ/.SS coverage is unreliable —
        # ticker.info and quarterly_financials routinely return incomplete
        # objects that cause NoneType subscript errors deeper in this
        # function (logged as "Historical valuation failed for 301358.SZ:
        # 'NoneType' object is not subscriptable" but non-fatal).
        # Mirror the earnings_history skip pattern: A-share tickers don't
        # have usable historical multiples via yfinance, so bail early
        # with a debug log rather than crashing and emitting a warning.
        if symbol.upper().endswith((".SZ", ".SS")):
            logger.debug(
                f"{symbol}: historical valuation skipped for A-share "
                f"(yfinance coverage unreliable)"
            )
            return {}

        try:
            import yfinance as yf
            from datetime import datetime, timedelta
            import statistics

            ticker = yf.Ticker(symbol)
            end = datetime.now()
            start = end - timedelta(days=years * 365)

            # Get monthly price history
            hist = ticker.history(start=start.strftime("%Y-%m-%d"), interval="1mo")
            if hist.empty:
                return {}

            # Get key financials for trailing multiples
            info = ticker.info or {}
            current_pe = info.get("trailingPE") or info.get("forwardPE")
            current_ev_ebitda = info.get("enterpriseToEbitda")

            # ── IPO filter (BUG-fix 2026-04-15) ──
            # For recent IPOs, the requested 5y window may extend before the
            # company started trading. yfinance usually returns only post-IPO
            # data, but we belt-and-brace it by also filtering against the
            # explicit firstTradeDate when available. This protects against
            # any data-source quirk that injects pre-IPO synthetic prices.
            first_trade_epoch = info.get("firstTradeDateEpochUtc")
            if first_trade_epoch:
                first_trade_dt = datetime.fromtimestamp(first_trade_epoch)
                # Mask off any rows before first trade date
                if hist.index.tz is not None:
                    import pandas as _pd
                    first_trade_dt = _pd.Timestamp(first_trade_dt).tz_localize(hist.index.tz)
                pre_ipo_count = (hist.index < first_trade_dt).sum()
                if pre_ipo_count > 0:
                    logger.info(
                        f"{symbol}: filtered {pre_ipo_count} pre-IPO month(s) "
                        f"(first trade {first_trade_dt:%Y-%m-%d})"
                    )
                    hist = hist[hist.index >= first_trade_dt]
                    if hist.empty:
                        return {}

            # Build monthly series from quarterly financials + monthly prices
            dates = []
            pe_series = []
            ev_ebitda_series = []
            pe_methodology = "price_scaled"  # default; upgraded below if real EPS available

            # Try to fetch quarterly net income — needed for true historical TTM EPS
            quarterly_ni_series: list[tuple[datetime, float]] = []
            try:
                qf = ticker.quarterly_financials
                if qf is not None and not qf.empty:
                    # qf columns are quarter-end dates (most recent first), rows are metrics
                    ni_row_label = None
                    for candidate in ("Net Income", "Net Income Common Stockholders",
                                       "Net Income From Continuing Operation Net Minority Interest"):
                        if candidate in qf.index:
                            ni_row_label = candidate
                            break
                    if ni_row_label:
                        for col in qf.columns:
                            val = qf.loc[ni_row_label, col]
                            if val is not None and not (isinstance(val, float) and (val != val)):  # skip NaN
                                qd = col.to_pydatetime() if hasattr(col, "to_pydatetime") else col
                                quarterly_ni_series.append((qd, float(val)))
                        # Sort ascending so we can walk forward
                        quarterly_ni_series.sort(key=lambda x: x[0])
            except Exception as _qe:
                logger.debug(f"{symbol}: quarterly NI fetch failed ({_qe}), "
                             f"falling back to price-scaled P/E")

            # Get current shares (yfinance most-current value used as a stable
            # divisor; share counts change much more slowly than NI so this
            # approximation is acceptable for ratio history).
            shares_for_eps = (info.get("sharesOutstanding") or info.get("impliedSharesOutstanding"))

            current_price = hist["Close"].iloc[-1] if not hist.empty else info.get("currentPrice", 0)

            if quarterly_ni_series and shares_for_eps and shares_for_eps > 0 and len(quarterly_ni_series) >= 4:
                # ── REAL TTM EPS path ──
                # For each month in price history, sum the 4 most recent
                # quarters whose end-date is <= that month → TTM NI → TTM EPS
                # → P/E. This produces a correct historical multiple even
                # when earnings have shifted dramatically.
                pe_methodology = "true_ttm"
                # Strip tz from quarter dates for comparison
                qni_naive = [
                    (d.replace(tzinfo=None) if hasattr(d, "tzinfo") and d.tzinfo else d, v)
                    for d, v in quarterly_ni_series
                ]
                for date, row in hist.iterrows():
                    price = row["Close"]
                    if not (price and price > 0):
                        continue
                    month_naive = date.to_pydatetime().replace(tzinfo=None) if hasattr(date, "to_pydatetime") else date
                    # Find quarters ending on or before this month
                    eligible = [v for d, v in qni_naive if d <= month_naive]
                    if len(eligible) < 4:
                        continue  # need 4 quarters for TTM
                    ttm_ni = sum(eligible[-4:])
                    if ttm_ni <= 0:
                        # Loss-making period: P/E undefined, skip
                        continue
                    ttm_eps = ttm_ni / shares_for_eps
                    if ttm_eps <= 0:
                        continue
                    pe_est = price / ttm_eps
                    if 0 < pe_est < 500:  # sanity cap
                        dates.append(date.strftime("%Y-%m"))
                        pe_series.append(round(pe_est, 1))

            if not pe_series and current_pe and current_pe > 0:
                # ── FALLBACK: price-scaled (constant-EPS) ──
                # Used when quarterly_financials is unavailable (some A-shares
                # / illiquid tickers) or coverage is too short for TTM.
                pe_methodology = "price_scaled"
                if current_price and current_price > 0:
                    eps_implied = current_price / current_pe
                    for date, row in hist.iterrows():
                        price = row["Close"]
                        if price > 0 and eps_implied > 0:
                            dates.append(date.strftime("%Y-%m"))
                            pe_est = price / eps_implied
                            pe_series.append(round(pe_est, 1))

            # BUG-25 fix: yfinance enterpriseToEbitda can return stale/wrong
            # values (e.g. META showed 7.9x vs correct 16.8x). When caller
            # provides a more reliable ev_ebitda_override (computed from SEC
            # data), prefer it.  Also sanity-check yfinance value against
            # override: if they diverge by >50%, use the override.
            if ev_ebitda_override and ev_ebitda_override > 0:
                if (current_ev_ebitda and current_ev_ebitda > 0
                        and abs(current_ev_ebitda - ev_ebitda_override) / ev_ebitda_override > 0.50):
                    logger.warning(
                        f"{symbol}: yfinance EV/EBITDA ({current_ev_ebitda:.1f}x) "
                        f"diverges >50% from computed ({ev_ebitda_override:.1f}x) "
                        f"— using computed value"
                    )
                current_ev_ebitda = ev_ebitda_override

            # EV/EBITDA scaling (rough but useful for the chart)
            if current_ev_ebitda and current_ev_ebitda > 0 and dates:
                cprice = hist["Close"].iloc[-1]
                for i, (date, row) in enumerate(hist.iterrows()):
                    if i < len(dates):
                        price_ratio = row["Close"] / cprice if cprice > 0 else 1
                        ev_ebitda_est = current_ev_ebitda * price_ratio
                        ev_ebitda_series.append(round(ev_ebitda_est, 1))

            if not dates:
                return {}

            # Compute stats
            def _stats(series: list[float]) -> dict[str, float]:
                clean = [v for v in series if v and v > 0 and v < 500]
                if len(clean) < 3:
                    return {}
                # BUG-23 fix: "current" must be the LATEST value in the time
                # series, not the max.  Previously clean was sorted before
                # taking [-1], which returned max instead of most-recent.
                latest = 0.0
                for v in reversed(series):
                    if v and v > 0 and v < 500:
                        latest = v
                        break
                clean.sort()
                n = len(clean)
                return {
                    "min": round(min(clean), 1),
                    "max": round(max(clean), 1),
                    "median": round(statistics.median(clean), 1),
                    "mean": round(statistics.mean(clean), 1),
                    "p25": round(clean[n // 4], 1),
                    "p75": round(clean[3 * n // 4], 1),
                    "current": round(latest, 1) if latest else 0,
                }

            # Actual months of data — may be < years*12 for recent IPOs
            months = len(dates)
            return {
                "dates": dates,
                "pe_ratio": pe_series,
                "ev_ebitda": ev_ebitda_series,
                "pe_stats": _stats(pe_series),
                "ev_ebitda_stats": _stats(ev_ebitda_series),
                "symbol": symbol,
                "years": years,  # requested window
                "months": months,  # actual data months
                # 2026-04-15: tag whether P/E history was computed from real
                # quarterly NI (true_ttm) or extrapolated by scaling current
                # P/E with historical prices (price_scaled). Reports may want
                # to show a "(estimated)" caveat for the latter.
                "pe_methodology": pe_methodology,
            }

        except ImportError:
            logger.warning("yfinance not installed for historical valuation")
            return {}
        except Exception as e:
            logger.warning(f"Historical valuation failed for {symbol}: {e}")
            return {}

    # ── Peer List Discovery ──────────────────────────────────────────

    def get_sector_peers(self, symbol: str, limit: int = 8) -> list[str]:
        """Discover peer companies in the same sector.

        Uses a built-in mapping for major tickers, with FMP API as fallback.
        """
        # Built-in peer mapping for common tickers (no API key needed)
        _PEER_MAP: dict[str, list[str]] = {
            # US Stocks
            # BUG-54: SAMSUNG.KS removed — yfinance returns 404 for that symbol.
            # Samsung Electronics trades on KRX as 005930.KS but coverage
            # is unreliable, and US-listed peers are sufficient for comps.
            "AAPL": ["MSFT", "GOOG", "SONY", "DELL", "HPQ"],
            "MSFT": ["AAPL", "GOOG", "AMZN", "CRM", "ORCL", "SAP"],
            "GOOG": ["META", "MSFT", "AMZN", "SNAP", "PINS", "TTD"],
            "GOOGL": ["META", "MSFT", "AMZN", "SNAP", "PINS", "TTD"],
            "META": ["GOOG", "SNAP", "PINS", "TTD", "MSFT", "AMZN"],
            "AMZN": ["MSFT", "GOOG", "BABA", "JD", "SHOP", "WMT"],
            "NVDA": ["AMD", "INTC", "AVGO", "QCOM", "TSM", "ASML"],
            "TSM": ["NVDA", "INTC", "ASML", "AVGO", "QCOM", "AMD"],
            "TSLA": ["F", "GM", "RIVN", "NIO", "BYD", "LI"],
            "JPM": ["BAC", "WFC", "C", "GS", "MS", "USB"],
            "JNJ": ["PFE", "MRK", "ABBV", "LLY", "BMY", "AMGN"],
            "V": ["MA", "PYPL", "SQ", "AXP", "FIS", "GPN"],
            "WMT": ["COST", "TGT", "AMZN", "KR", "DG", "DLTR"],
            "PG": ["UL", "CL", "KMB", "CLX", "CHD", "EL"],
            "XOM": ["CVX", "SHEL", "TTE", "BP", "COP", "EOG"],
            "DIS": ["NFLX", "CMCSA", "WBD", "PARA", "SONY", "FOX"],
            # A-share (use yfinance .SS/.SZ format for peer fundamentals)
            "600519": ["000858.SZ", "000568.SZ", "603369.SS", "002304.SZ", "000799.SZ"],  # 白酒
            "000858": ["600519.SS", "000568.SZ", "603369.SS", "002304.SZ", "000799.SZ"],
            "601318": ["601628.SS", "601336.SS", "600030.SS", "601688.SS", "601211.SS"],  # 保险/金融
            "000333": ["000651.SZ", "600690.SS", "002032.SZ", "600060.SS", "002508.SZ"],  # 家电
            "600036": ["601166.SS", "000001.SZ", "601288.SS", "601398.SS", "601818.SS"],  # 银行
            "000651": ["000333.SZ", "600690.SS", "002032.SZ", "600060.SS", "002508.SZ"],
            "300750": ["002594.SZ", "600438.SS", "002812.SZ", "300014.SZ", "688005.SS"],  # 新能源
            "002594": ["300750.SZ", "601238.SS", "600104.SS", "000625.SZ", "601127.SS"],  # 汽车
            "601888": ["600138.SS", "000069.SZ", "002007.SZ", "300144.SZ", "600258.SS"],  # 旅游免税
            "600276": ["000538.SZ", "600196.SS", "300122.SZ", "002422.SZ", "300015.SZ"],  # 医药
            "688981": ["002371.SZ", "600584.SS", "603501.SS", "688012.SS", "300223.SZ"],  # 半导体
        }

        upper = symbol.upper()
        if upper in _PEER_MAP:
            return _PEER_MAP[upper][:limit]

        # Fallback: try FMP API if key available
        if self._fmp_key:
            try:
                import requests
                url = f"https://financialmodelingprep.com/api/v4/stock_peers?symbol={symbol}&apikey={self._fmp_key}"
                resp = requests.get(url, timeout=10)
                if resp.ok:
                    data = resp.json()
                    if data and isinstance(data, list) and data[0].get("peersList"):
                        peers = data[0]["peersList"][:limit]
                        return [p for p in peers if p != symbol]
            except Exception as e:
                logger.debug(f"FMP peer discovery failed for {symbol}: {e}")

        return []


    # ── Earnings Call Transcript ──────────────────────────────────

    def get_earnings_transcript(
        self,
        symbol: str,
        year: int | None = None,
        quarter: int | None = None,
    ) -> dict[str, Any]:
        """Fetch earnings call transcript from FMP.

        Returns dict with keys: content, date, year, quarter, symbol.
        Falls back to most recent if year/quarter not specified.
        """
        if not self._fmp_key:
            logger.warning("FMP API key required for earnings transcripts")
            return {}

        try:
            import requests
            import ssl
            ssl._create_default_https_context = ssl._create_unverified_context

            # If no year/quarter specified, get the list first to find most recent
            if year is None or quarter is None:
                list_url = (
                    f"https://financialmodelingprep.com/api/v3/earning_call_transcript"
                    f"/{symbol}?apikey={self._fmp_key}"
                )
                resp = requests.get(list_url, timeout=15, verify=False)
                if not resp.ok or not resp.json():
                    logger.warning(f"No transcript list for {symbol}")
                    return {}
                data = resp.json()
                if isinstance(data, list) and data:
                    # Most recent first
                    latest = data[0]
                    year = latest.get("year", year)
                    quarter = latest.get("quarter", quarter)

            if year is None or quarter is None:
                return {}

            # Fetch the actual transcript
            url = (
                f"https://financialmodelingprep.com/api/v3/earning_call_transcript"
                f"/{symbol}?quarter={quarter}&year={year}&apikey={self._fmp_key}"
            )
            resp = requests.get(url, timeout=30, verify=False)
            if not resp.ok:
                logger.warning(f"Transcript fetch failed for {symbol} Q{quarter} {year}: {resp.status_code}")
                return {}

            data = resp.json()
            if isinstance(data, list) and data:
                item = data[0]
                content = item.get("content", "")
                return {
                    "content": content,
                    "date": item.get("date", ""),
                    "year": year,
                    "quarter": quarter,
                    "symbol": symbol,
                    "word_count": len(content.split()) if content else 0,
                }
            return {}
        except Exception as e:
            logger.warning(f"Failed to fetch transcript for {symbol}: {e}")
            return {}


def _safe_float(val: Any) -> float | None:
    """Safely convert to float, returning None on failure or non-finite.

    BUG-Y40 (2026-05-06): previously caught NaN via the ``f != f`` trick
    but allowed ``inf`` / ``-inf`` through, which then leaked to the DCF
    engine and HTML JSON. Reject all non-finite values at the parse
    boundary.
    """
    if val is None:
        return None
    try:
        f = float(val)
    except (ValueError, TypeError):
        return None
    import math as _math
    if not _math.isfinite(f):
        return None
    return f
