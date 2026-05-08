"""SEC Entity Registry — ticker ↔ CIK lookup.

Maintains a mapping between tickers, CIK numbers, and company names.
Uses SEC EDGAR's full-text search and company tickers JSON.

Cached in memory — refresh from SEC when needed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .sec_api_client import SECAPIClient


# Ticker → company name (for report display)
COMMON_NAMES: dict[str, str] = {
    "META": "Meta Platforms, Inc.",
    "AAPL": "Apple Inc.",
    "AMZN": "Amazon.com, Inc.",
    "GOOGL": "Alphabet Inc.",
    "GOOG": "Alphabet Inc.",
    "MSFT": "Microsoft Corporation",
    "NVDA": "NVIDIA Corporation",
    "TSLA": "Tesla, Inc.",
    "NFLX": "Netflix, Inc.",
    "AMD": "Advanced Micro Devices, Inc.",
    "INTC": "Intel Corporation",
    "AVGO": "Broadcom Inc.",
    "TSM": "Taiwan Semiconductor Manufacturing",
    "CRM": "Salesforce, Inc.",
    "SNOW": "Snowflake Inc.",
    "NOW": "ServiceNow, Inc.",
    "JPM": "JPMorgan Chase & Co.",
    "BAC": "Bank of America Corporation",
    "GS": "The Goldman Sachs Group, Inc.",
    "LLY": "Eli Lilly and Company",
    "JNJ": "Johnson & Johnson",
    "PFE": "Pfizer Inc.",
    "ABBV": "AbbVie Inc.",
    "PG": "The Procter & Gamble Company",
    "KO": "The Coca-Cola Company",
    "PEP": "PepsiCo, Inc.",
    "XOM": "Exxon Mobil Corporation",
    "CVX": "Chevron Corporation",
    "GE": "GE Aerospace",
    "HON": "Honeywell International Inc.",
    "CAT": "Caterpillar Inc.",
    "PLD": "Prologis, Inc.",
    "AMT": "American Tower Corporation",
    "BABA": "Alibaba Group Holding Limited",
    "JD": "JD.com, Inc.",
    "PDD": "PDD Holdings Inc.",
    "SE": "Sea Limited",
    "MELI": "MercadoLibre, Inc.",
}


# Common tickers pre-populated for demo and testing
# In production, this would be refreshed from SEC's company_tickers.json
COMMON_TICKERS: dict[str, str] = {
    # FAANG+
    "META": "0001326801",
    "AAPL": "0000320193",
    "AMZN": "0001018724",
    "GOOGL": "0001652044",
    "GOOG": "0001652044",
    "MSFT": "0000789019",
    "NVDA": "0001045810",
    "TSLA": "0001318605",
    "NFLX": "0001065280",
    # Semis
    "AMD": "0000002488",
    "INTC": "0000050863",
    "AVGO": "0001649338",
    "TSM": "0001046179",
    # SaaS / Cloud
    "CRM": "0001108524",
    "SNOW": "0001640147",
    "NOW": "0001373715",
    # Banking
    "JPM": "0000019617",
    "BAC": "0000070858",
    "GS": "0000886982",
    # Pharma
    "LLY": "0000059478",
    "JNJ": "0000200406",
    "PFE": "0000078003",
    "ABBV": "0001551152",
    # Consumer
    "PG": "0000080424",
    "KO": "0000021344",
    "PEP": "0000077476",
    # Energy
    "XOM": "0000034088",
    "CVX": "0000093410",
    # Industrial
    "GE": "0000040554",
    "HON": "0000773840",
    "CAT": "0000018230",
    # REITs
    "PLD": "0001045609",
    "AMT": "0001053507",
    # E-commerce
    "BABA": "0001577552",
    "JD": "0001549802",
    "PDD": "0001737806",
    "SE": "0001713445",
    "MELI": "0001099590",
}


class SECEntityRegistry:
    """Registry for ticker ↔ CIK mappings.

    Pre-populated with common tickers. Can be refreshed from SEC.
    Thread-safe for read operations (dict reads are atomic in CPython).
    """

    def __init__(self, client: SECAPIClient | None = None):
        self._client = client or SECAPIClient()
        self._ticker_to_cik: dict[str, str] = dict(COMMON_TICKERS)
        self._cik_to_ticker: dict[str, str] = {
            v: k for k, v in COMMON_TICKERS.items()
        }
        self._ticker_to_name: dict[str, str] = dict(COMMON_NAMES)
        self._cik_to_name: dict[str, str] = {}

    def get_cik(self, ticker: str) -> str | None:
        """Get CIK by ticker symbol (case-insensitive).

        Returns 10-digit zero-padded CIK or None if not found.
        Falls back to SEC API lookup if not in cache.
        """
        ticker = ticker.upper()
        cik = self._ticker_to_cik.get(ticker)
        if cik:
            return cik

        # Fallback: query SEC
        info = self._client.get_company_info(ticker)
        if info and info.cik:
            self._ticker_to_cik[ticker] = info.cik
            self._cik_to_ticker[info.cik] = ticker
            self._cik_to_name[info.cik] = info.name
            return info.cik
        return None

    def get_ticker(self, cik: str) -> str | None:
        """Get ticker by CIK."""
        cik = cik.zfill(10)
        return self._cik_to_ticker.get(cik)

    def get_name(self, cik: str) -> str | None:
        """Get company name by CIK."""
        cik = cik.zfill(10)
        return self._cik_to_name.get(cik)

    def get_name_by_ticker(self, ticker: str) -> str | None:
        """Get company name by ticker symbol."""
        ticker = ticker.upper()
        return self._ticker_to_name.get(ticker)

    def register(self, ticker: str, cik: str, name: str = "") -> None:
        """Manually register a ticker → CIK mapping."""
        ticker = ticker.upper()
        cik = cik.zfill(10)
        self._ticker_to_cik[ticker] = cik
        self._cik_to_ticker[cik] = ticker
        if name:
            self._cik_to_name[cik] = name

    def list_tickers(self) -> list[str]:
        """List all known tickers."""
        return sorted(self._ticker_to_cik.keys())

    @property
    def size(self) -> int:
        """Number of registered entities."""
        return len(self._ticker_to_cik)
