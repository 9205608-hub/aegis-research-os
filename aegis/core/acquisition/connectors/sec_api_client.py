"""SEC EDGAR API Client — wraps SEC REST APIs with rate limiting.

SEC EDGAR APIs used:
1. Company Search: https://efts.sec.gov/LATEST/search-index?q={query}
2. Submissions: https://data.sec.gov/submissions/CIK{cik}.json
3. Company Facts (XBRL): https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json
4. Company Concept: https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/{taxonomy}/{concept}.json

Rate limit: SEC requests max 10 requests/second.
User-Agent header REQUIRED (SEC blocks requests without it).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False


@dataclass
class FilingMetadata:
    """Metadata for a single SEC filing."""

    accession_number: str
    filing_date: str  # YYYY-MM-DD
    form_type: str  # "10-K", "10-Q", "8-K", etc.
    primary_document: str  # filename
    primary_document_url: str
    reporting_date: str = ""  # period of report
    company_name: str = ""
    cik: str = ""


@dataclass
class CompanyInfo:
    """Basic company information from SEC."""

    cik: str
    name: str
    ticker: str = ""
    sic: str = ""
    fiscal_year_end: str = ""  # MMDD format
    state_of_incorporation: str = ""
    filings_url: str = ""


class SECAPIClient:
    """Client for SEC EDGAR REST APIs.

    Handles:
    - Company lookup by ticker or CIK
    - Filing search (10-K, 10-Q, 8-K, etc.)
    - XBRL company facts download
    - Rate limiting (10 req/sec max)
    """

    BASE_URL = "https://data.sec.gov"
    EFTS_URL = "https://efts.sec.gov/LATEST"

    def __init__(self, user_agent: str | None = None):
        self._user_agent = (
            user_agent
            or os.environ.get("SEC_USER_AGENT")
            or os.environ.get("EDGAR_USER_AGENT")
            or "Aegis Research OS research@example.com"
        )
        self._last_request_time = 0.0
        self._min_interval = 0.1  # 10 req/sec

    @property
    def headers(self) -> dict[str, str]:
        return {
            "User-Agent": self._user_agent,
            "Accept": "application/json",
        }

    def _rate_limit(self) -> None:
        """Enforce SEC rate limit."""
        now = time.time()
        elapsed = now - self._last_request_time
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request_time = time.time()

    def _get(self, url: str) -> dict | list | None:
        """Make a rate-limited GET request."""
        if not HAS_HTTPX:
            return None
        self._rate_limit()
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.get(url, headers=self.headers)
                resp.raise_for_status()
                return resp.json()
        except (httpx.HTTPError, json.JSONDecodeError) as e:
            return None

    def get_company_info(self, cik_or_ticker: str) -> CompanyInfo | None:
        """Look up company info by CIK or ticker.

        Uses SEC EDGAR submissions endpoint.
        """
        cik = self._normalize_cik(cik_or_ticker)
        url = f"{self.BASE_URL}/submissions/CIK{cik}.json"
        data = self._get(url)
        if not data:
            return None

        return CompanyInfo(
            cik=cik,
            name=data.get("name", ""),
            ticker=",".join(data.get("tickers", [])),
            sic=data.get("sic", ""),
            fiscal_year_end=data.get("fiscalYearEnd", ""),
            state_of_incorporation=data.get("stateOfIncorporation", ""),
            filings_url=url,
        )

    def get_recent_filings(
        self,
        cik: str,
        form_types: list[str] | None = None,
        count: int = 10,
    ) -> list[FilingMetadata]:
        """Get recent filings for a company.

        Args:
            cik: Central Index Key (with or without leading zeros)
            form_types: Filter by form type (e.g., ["10-K", "10-Q"])
            count: Max filings to return
        """
        cik = self._normalize_cik(cik)
        url = f"{self.BASE_URL}/submissions/CIK{cik}.json"
        data = self._get(url)
        if not data or "filings" not in data:
            return []

        recent = data["filings"].get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        accessions = recent.get("accessionNumber", [])
        primary_docs = recent.get("primaryDocument", [])
        report_dates = recent.get("reportDate", [])

        filings = []
        for i in range(min(len(forms), len(dates), len(accessions))):
            form = forms[i]
            if form_types and form not in form_types:
                continue

            acc = accessions[i].replace("-", "")
            doc = primary_docs[i] if i < len(primary_docs) else ""
            doc_url = (
                f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"
                if doc else ""
            )

            filings.append(FilingMetadata(
                accession_number=accessions[i],
                filing_date=dates[i],
                form_type=form,
                primary_document=doc,
                primary_document_url=doc_url,
                reporting_date=report_dates[i] if i < len(report_dates) else "",
                company_name=data.get("name", ""),
                cik=cik,
            ))

            if len(filings) >= count:
                break

        return filings

    def get_company_facts(self, cik: str) -> dict | None:
        """Fetch ALL XBRL facts for a company from SEC Company Facts API.

        Returns structured XBRL data with all reported facts across all filings.
        This is the primary data source for financial extraction.

        Structure:
        {
          "cik": 1326801,
          "entityName": "Meta Platforms, Inc.",
          "facts": {
            "us-gaap": {
              "Revenues": {
                "label": "Revenues",
                "units": {
                  "USD": [
                    {"val": 164710000000, "accn": "...", "fy": 2024, "fp": "FY", ...},
                    ...
                  ]
                }
              },
              ...
            }
          }
        }
        """
        cik = self._normalize_cik(cik)
        url = f"{self.BASE_URL}/api/xbrl/companyfacts/CIK{cik}.json"
        return self._get(url)

    def get_company_concept(
        self, cik: str, taxonomy: str, concept: str
    ) -> dict | None:
        """Fetch a single XBRL concept for a company.

        e.g., get_company_concept("0001326801", "us-gaap", "Revenues")
        """
        cik = self._normalize_cik(cik)
        url = f"{self.BASE_URL}/api/xbrl/companyconcept/CIK{cik}/{taxonomy}/{concept}.json"
        return self._get(url)

    @staticmethod
    def _normalize_cik(cik: str) -> str:
        """Normalize CIK to 10-digit zero-padded format."""
        # Remove any non-digit characters
        digits = "".join(c for c in cik if c.isdigit())
        return digits.zfill(10)

    @staticmethod
    def compute_content_hash(content: Any) -> str:
        """Compute SHA-256 hash of content."""
        serialized = json.dumps(content, sort_keys=True, default=str)
        return f"sha256:{hashlib.sha256(serialized.encode()).hexdigest()}"
