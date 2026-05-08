"""SEC Form 4 Insider Trading Connector.

Fetches and parses SEC Form 4 filings (insider buys/sells) from EDGAR.
Uses the existing SECAPIClient for rate-limited API access and
SECEntityRegistry for ticker → CIK resolution.

Data source: SEC EDGAR (free, no API key required).
Rate limit: 10 req/sec (enforced by SECAPIClient).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .sec_api_client import SECAPIClient
from .sec_entity_registry import SECEntityRegistry


@dataclass
class InsiderTransaction:
    """A single insider transaction from SEC Form 4."""

    filer_name: str
    filer_title: str  # CEO, CFO, Director, 10% Owner, etc.
    transaction_date: str  # YYYY-MM-DD
    transaction_type: str  # "P" (purchase), "S" (sale), "A" (award/grant)
    shares: float
    price_per_share: float
    total_value: float
    shares_owned_after: float
    is_direct: bool  # direct vs indirect ownership
    filing_date: str  # when the Form 4 was filed
    accession_number: str


@dataclass
class InsiderSummary:
    """Aggregated insider trading activity for a ticker."""

    ticker: str
    period_months: int
    transactions: list[InsiderTransaction] = field(default_factory=list)
    net_shares: float = 0.0
    net_value: float = 0.0
    buy_count: int = 0
    sell_count: int = 0
    total_buy_value: float = 0.0
    total_sell_value: float = 0.0
    notable_transactions: list[InsiderTransaction] = field(default_factory=list)
    cluster_detected: bool = False
    sentiment: str = "neutral"  # bullish / bearish / neutral / mixed


# Title keywords for identifying C-suite officers
_CSUITE_TITLES = {"ceo", "cfo", "coo", "president"}
_CSUITE_PREFIXES = {"chief "}  # "Chief Financial Officer", etc.


def is_csuite(title: str) -> bool:
    """Check if a filer title indicates C-suite officer."""
    lower = title.lower()
    # Exact word match for abbreviations (avoid "director" matching "cto")
    words = set(lower.replace(",", " ").split())
    if words & _CSUITE_TITLES:
        return True
    # Prefix match for "Chief ..." titles
    return any(lower.startswith(p) or f" {p}" in lower for p in _CSUITE_PREFIXES)


class SECForm4Connector:
    """Fetches and analyzes SEC Form 4 insider trading data.

    Uses SECAPIClient for EDGAR access and SECEntityRegistry for
    ticker → CIK resolution.
    """

    # Thresholds
    NOTABLE_VALUE_THRESHOLD = 1_000_000  # $1M
    CLUSTER_WINDOW_DAYS = 30
    CLUSTER_MIN_FILERS = 3
    MAX_FILINGS = 50

    def __init__(
        self,
        sec_client: SECAPIClient | None = None,
        registry: SECEntityRegistry | None = None,
    ):
        self._client = sec_client or SECAPIClient()
        self._registry = registry or SECEntityRegistry(self._client)

    def get_insider_transactions(
        self, ticker: str, months: int = 12
    ) -> InsiderSummary | None:
        """Fetch and analyze insider transactions for a ticker.

        Args:
            ticker: US stock ticker symbol.
            months: Lookback period in months (default 12).

        Returns:
            InsiderSummary with parsed transactions and analytics,
            or None if CIK lookup fails or no data available.
        """
        cik = self._registry.get_cik(ticker)
        if not cik:
            return None

        # Fetch recent Form 4 filings
        filings = self._client.get_recent_filings(
            cik=cik,
            form_types=["4", "4/A"],
            count=self.MAX_FILINGS,
        )
        if not filings:
            return InsiderSummary(ticker=ticker, period_months=months)

        # Filter by date window
        cutoff = (datetime.now() - timedelta(days=months * 30)).strftime("%Y-%m-%d")
        filings = [f for f in filings if f.filing_date >= cutoff]

        # Parse each Form 4 XML
        all_transactions: list[InsiderTransaction] = []
        for filing in filings:
            xml_url = filing.primary_document_url
            if not xml_url or not xml_url.endswith(".xml"):
                # Try to construct XML URL from accession
                acc_clean = filing.accession_number.replace("-", "")
                xml_url = (
                    f"https://www.sec.gov/Archives/edgar/data/"
                    f"{cik.lstrip('0') or '0'}/{acc_clean}/"
                    f"{filing.primary_document}"
                )

            xml_content = self._fetch_xml(xml_url)
            if xml_content:
                txns = self._parse_form4_xml(
                    xml_content, filing.filing_date, filing.accession_number
                )
                all_transactions.extend(txns)

        # Filter transactions by date (Form 4 filing date may differ from transaction date)
        all_transactions = [
            t for t in all_transactions
            if t.transaction_date >= cutoff
        ]

        return self._compute_summary(ticker, all_transactions, months)

    def _fetch_xml(self, url: str) -> str | None:
        """Fetch raw XML content from SEC EDGAR."""
        try:
            import httpx
        except ImportError:
            return None

        self._client._rate_limit()
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.get(url, headers=self._client.headers)
                resp.raise_for_status()
                return resp.text
        except Exception:
            return None

    def _parse_form4_xml(
        self,
        xml_content: str,
        filing_date: str,
        accession_number: str,
    ) -> list[InsiderTransaction]:
        """Parse a Form 4 XML document into InsiderTransaction objects.

        Handles both <nonDerivativeTransaction> and <derivativeTransaction>.
        """
        try:
            root = ET.fromstring(xml_content)
        except ET.ParseError:
            return []

        # Extract filer info from <reportingOwner>
        filer_name = ""
        filer_title = ""
        owner = root.find(".//reportingOwner")
        if owner is not None:
            oid = owner.find("reportingOwnerId")
            if oid is not None:
                name_el = oid.find("rptOwnerName")
                filer_name = name_el.text.strip() if name_el is not None and name_el.text else ""
            rel = owner.find("reportingOwnerRelationship")
            if rel is not None:
                filer_title = self._extract_title(rel)

        transactions: list[InsiderTransaction] = []

        # Non-derivative transactions (common stock buys/sells)
        for txn in root.findall(".//nonDerivativeTransaction"):
            parsed = self._parse_transaction_element(
                txn, filer_name, filer_title, filing_date, accession_number,
                is_derivative=False,
            )
            if parsed:
                transactions.append(parsed)

        # Derivative transactions (options exercises, etc.)
        for txn in root.findall(".//derivativeTransaction"):
            parsed = self._parse_transaction_element(
                txn, filer_name, filer_title, filing_date, accession_number,
                is_derivative=True,
            )
            if parsed:
                transactions.append(parsed)

        return transactions

    def _parse_transaction_element(
        self,
        txn: ET.Element,
        filer_name: str,
        filer_title: str,
        filing_date: str,
        accession_number: str,
        is_derivative: bool = False,
    ) -> InsiderTransaction | None:
        """Parse a single transaction XML element."""
        # Transaction date
        date_el = txn.find(".//transactionDate/value")
        txn_date = date_el.text.strip() if date_el is not None and date_el.text else filing_date

        # Transaction code: P=Purchase, S=Sale, A=Award, M=Exercise, G=Gift
        code_el = txn.find(".//transactionCoding/transactionCode")
        txn_code = code_el.text.strip() if code_el is not None and code_el.text else ""

        # Skip gifts, exercises-only, and other non-market transactions
        if txn_code not in ("P", "S", "A"):
            return None

        # Shares
        shares_el = txn.find(".//transactionAmounts/transactionShares/value")
        shares = _safe_float(shares_el)

        # Price per share
        price_el = txn.find(".//transactionAmounts/transactionPricePerShare/value")
        price = _safe_float(price_el)

        # Acquisition (A) or Disposition (D)
        ad_el = txn.find(".//transactionAmounts/transactionAcquiredDisposedCode/value")
        ad_code = ad_el.text.strip() if ad_el is not None and ad_el.text else ""

        # For sales, treat as negative
        if ad_code == "D":
            txn_code = "S"
        elif ad_code == "A" and txn_code not in ("P",):
            txn_code = "A"  # award/grant

        # Shares owned after
        if is_derivative:
            owned_el = txn.find(
                ".//postTransactionAmounts/sharesOwnedFollowingTransaction/value"
            )
        else:
            owned_el = txn.find(
                ".//postTransactionAmounts/sharesOwnedFollowingTransaction/value"
            )
        owned_after = _safe_float(owned_el)

        # Direct vs indirect
        ownership_el = txn.find(".//ownershipNature/directOrIndirectOwnership/value")
        is_direct = True
        if ownership_el is not None and ownership_el.text:
            is_direct = ownership_el.text.strip().upper() == "D"

        total_value = shares * price

        return InsiderTransaction(
            filer_name=filer_name,
            filer_title=filer_title,
            transaction_date=txn_date,
            transaction_type=txn_code,
            shares=shares,
            price_per_share=price,
            total_value=total_value,
            shares_owned_after=owned_after,
            is_direct=is_direct,
            filing_date=filing_date,
            accession_number=accession_number,
        )

    @staticmethod
    def _extract_title(rel: ET.Element) -> str:
        """Extract human-readable title from reportingOwnerRelationship."""
        parts = []
        if rel.find("isOfficer") is not None:
            title_el = rel.find("officerTitle")
            if title_el is not None and title_el.text:
                parts.append(title_el.text.strip())
        if rel.find("isDirector") is not None:
            val = rel.find("isDirector")
            if val is not None and val.text and val.text.strip() in ("1", "true"):
                parts.append("Director")
        if rel.find("isTenPercentOwner") is not None:
            val = rel.find("isTenPercentOwner")
            if val is not None and val.text and val.text.strip() in ("1", "true"):
                parts.append("10% Owner")
        return ", ".join(parts) if parts else "Insider"

    def _compute_summary(
        self,
        ticker: str,
        transactions: list[InsiderTransaction],
        months: int,
    ) -> InsiderSummary:
        """Compute aggregate statistics from parsed transactions."""
        buys = [t for t in transactions if t.transaction_type == "P"]
        sells = [t for t in transactions if t.transaction_type == "S"]

        total_buy_value = sum(t.total_value for t in buys)
        total_sell_value = sum(t.total_value for t in sells)
        net_value = total_buy_value - total_sell_value

        buy_shares = sum(t.shares for t in buys)
        sell_shares = sum(t.shares for t in sells)
        net_shares = buy_shares - sell_shares

        # Notable transactions (> $1M)
        notable = sorted(
            [t for t in transactions if t.total_value >= self.NOTABLE_VALUE_THRESHOLD],
            key=lambda t: t.total_value,
            reverse=True,
        )

        # Cluster detection
        cluster = self._detect_cluster(transactions)

        # Sentiment
        sentiment = self._compute_sentiment(
            buys, sells, total_buy_value, total_sell_value, cluster
        )

        return InsiderSummary(
            ticker=ticker,
            period_months=months,
            transactions=transactions,
            net_shares=net_shares,
            net_value=net_value,
            buy_count=len(buys),
            sell_count=len(sells),
            total_buy_value=total_buy_value,
            total_sell_value=total_sell_value,
            notable_transactions=notable,
            cluster_detected=cluster,
            sentiment=sentiment,
        )

    def _detect_cluster(self, transactions: list[InsiderTransaction]) -> bool:
        """Detect cluster buying/selling (3+ unique filers within 30 days, same direction)."""
        if len(transactions) < self.CLUSTER_MIN_FILERS:
            return False

        # Group by direction
        for direction in ("P", "S"):
            directed = [t for t in transactions if t.transaction_type == direction]
            if len(directed) < self.CLUSTER_MIN_FILERS:
                continue

            # Sort by date
            directed.sort(key=lambda t: t.transaction_date)

            # Sliding window: check if 3+ unique filers within 30-day window
            for i in range(len(directed)):
                window_end = directed[i].transaction_date
                try:
                    end_dt = datetime.strptime(window_end, "%Y-%m-%d")
                    start_dt = end_dt - timedelta(days=self.CLUSTER_WINDOW_DAYS)
                    window_start = start_dt.strftime("%Y-%m-%d")
                except ValueError:
                    continue

                filers_in_window = set()
                for t in directed:
                    if window_start <= t.transaction_date <= window_end:
                        filers_in_window.add(t.filer_name)

                if len(filers_in_window) >= self.CLUSTER_MIN_FILERS:
                    return True

        return False

    @staticmethod
    def _compute_sentiment(
        buys: list[InsiderTransaction],
        sells: list[InsiderTransaction],
        total_buy_value: float,
        total_sell_value: float,
        cluster: bool,
    ) -> str:
        """Determine overall insider sentiment."""
        if not buys and not sells:
            return "neutral"

        # Strong signals
        csuite_buys = [t for t in buys if is_csuite(t.filer_title)]
        if csuite_buys and total_buy_value > 500_000:
            return "bullish"

        if total_buy_value > 0 and total_sell_value > 0:
            ratio = total_buy_value / total_sell_value if total_sell_value else float("inf")
            if ratio > 2.0:
                return "bullish"
            elif ratio < 0.1:
                return "bearish"
            else:
                return "mixed"

        if total_buy_value > 0 and total_sell_value == 0:
            return "bullish"
        if total_sell_value > 0 and total_buy_value == 0:
            # Pure selling is common (vesting + tax), only bearish if large
            if total_sell_value > 10_000_000:
                return "bearish"
            return "neutral"

        return "neutral"


def _safe_float(el: ET.Element | None) -> float:
    """Safely extract float from XML element."""
    if el is None or not el.text:
        return 0.0
    try:
        return float(el.text.strip())
    except (ValueError, TypeError):
        return 0.0
