"""SEC EDGAR Connector — implements SourceConnector protocol.

Tier 1 data source: Primary regulatory filings (10-K, 10-Q, 8-K, etc.)
from the SEC Electronic Data Gathering, Analysis, and Retrieval system.

Free API — no license required. Rate limit: 10 req/sec.
Requires User-Agent header per SEC fair access policy.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from aegis.core.acquisition.models import (
    CostEstimate,
    DataQuery,
    FreshnessReport,
    RateLimitConfig,
    RawDataPacket,
    SchemaValidationResult,
)
from aegis.data_contracts.common import SourceTier

from .sec_api_client import SECAPIClient
from .xbrl_parser import XBRLParser


class SECEDGARConnector:
    """SEC EDGAR data connector — Tier 1 regulatory filings.

    Implements the SourceConnector protocol for the ingestion pipeline.

    Capabilities:
    - Fetch company financial facts via XBRL Company Facts API
    - List available filings (10-K, 10-Q, 8-K, etc.)
    - Extract segment-level data from XBRL dimensions
    - Validate XBRL data completeness
    """

    source_id: str = "edgar"
    source_tier: SourceTier = SourceTier.TIER_1
    market_id: str = "us"
    license_type: str = "free"
    rate_limit: RateLimitConfig = RateLimitConfig(
        requests_per_second=10.0,
        requests_per_day=100_000,
        burst_size=20,
    )

    # Minimum XBRL concepts required for a valid filing extraction
    REQUIRED_CONCEPTS = {
        "us-gaap:Revenues",
        "us-gaap:NetIncomeLoss",
        "us-gaap:Assets",
    }

    # Alternative concepts that satisfy the same requirement
    ALTERNATIVE_CONCEPTS = {
        "us-gaap:Revenues": [
            "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
        ],
    }

    def __init__(self, user_agent: str | None = None):
        self._client = SECAPIClient(user_agent=user_agent)
        self._parser = XBRLParser()

    def fetch(self, query: DataQuery) -> RawDataPacket:
        """Fetch financial data from SEC EDGAR.

        query.entity_id: CIK number (e.g., "0001326801" for Meta)
        query.data_type: "filing" (XBRL facts) or "filing_list" (metadata only)
        query.filing_type: "10-K", "10-Q", etc.
        query.period: "FY2024", "Q3_2025", etc.
        query.extra_params: {"fiscal_year": 2024, "fiscal_period": "FY"}
        """
        if query.data_type == "filing_list":
            return self._fetch_filing_list(query)
        else:
            return self._fetch_xbrl_facts(query)

    def _fetch_xbrl_facts(self, query: DataQuery) -> RawDataPacket:
        """Fetch XBRL company facts and parse for target period."""
        cik = query.entity_id

        # Get all company facts (XBRL)
        facts_json = self._client.get_company_facts(cik)
        if facts_json is None:
            return RawDataPacket(
                source_id=self.source_id,
                source_tier=self.source_tier,
                market_id=self.market_id,
                query=query,
                fetched_at=datetime.now(timezone.utc),
                raw_content=None,
                content_hash="sha256:" + "0" * 64,
                content_type="json",
                response_metadata={"error": "Failed to fetch from SEC EDGAR"},
            )

        # Determine target period
        fiscal_year = query.extra_params.get("fiscal_year")
        fiscal_period = query.extra_params.get("fiscal_period", "FY")
        form_type = query.filing_type or "10-K"

        if fiscal_year is None and query.period:
            # Parse "FY2024" → fiscal_year=2024, fiscal_period="FY"
            fiscal_year, fiscal_period = self._parse_period(query.period)

        # Parse XBRL for the target period
        if fiscal_year:
            filing_data = self._parser.parse_company_facts(
                facts_json, fiscal_year, fiscal_period, form_type,
            )

            # If no segments from CompanyFacts, try XBRL instance document
            segment_facts = filing_data.segment_facts
            segment_detail = {}
            if not segment_facts:
                import time
                time.sleep(1.5)  # Respect SEC rate limit before segment fetch
                for attempt in range(2):
                    try:
                        seg_result = self._fetch_xbrl_segment_data(
                            cik, fiscal_year, fiscal_period, form_type,
                        )
                        if seg_result:
                            segment_facts = seg_result.get("flat_segments", {})
                            segment_detail = seg_result.get("detail", {})
                            break
                    except Exception:
                        if attempt == 0:
                            time.sleep(3)  # Retry after longer wait

            content = {
                "entity_name": filing_data.entity_name,
                "cik": filing_data.cik,
                "fiscal_year": filing_data.fiscal_year,
                "fiscal_period": filing_data.fiscal_period,
                "facts": filing_data.facts,
                "segment_facts": segment_facts,
                "segment_detail": segment_detail,
                "fact_count": len(filing_data.facts),
                "segment_count": len(segment_facts),
            }
        else:
            # Return available periods if no specific period requested
            periods = self._parser.extract_available_periods(facts_json, form_type)
            segments = self._parser.extract_segments(facts_json)
            content = {
                "entity_name": facts_json.get("entityName", ""),
                "cik": str(facts_json.get("cik", "")),
                "available_periods": periods[:20],
                "segments": segments,
            }

        content_hash = self._client.compute_content_hash(content)

        return RawDataPacket(
            source_id=self.source_id,
            source_tier=self.source_tier,
            market_id=self.market_id,
            query=query,
            fetched_at=datetime.now(timezone.utc),
            raw_content=content,
            content_hash=content_hash,
            content_type="json",
            response_metadata={
                "api_endpoint": "companyfacts",
                "cik": cik,
                "fiscal_year": fiscal_year,
                "fiscal_period": fiscal_period,
            },
        )

    def _fetch_xbrl_segment_data(
        self, cik: str, fiscal_year: int, fiscal_period: str, form_type: str,
    ) -> dict | None:
        """Fetch segment data from the XBRL instance document.

        The CompanyFacts API doesn't include dimensional/segment data.
        We need to download the actual XBRL instance XML from the filing.
        """
        try:
            import httpx
        except ImportError:
            return None

        # Step 1: Find the filing accession number
        form_filter = [form_type] if form_type else ["10-K"]
        filings = self._client.get_recent_filings(cik, form_filter, count=5)

        # Find the filing for our target fiscal year
        target_filing = None
        for f in filings:
            # Match by reporting date year
            if f.reporting_date and str(fiscal_year) in f.reporting_date:
                target_filing = f
                break
        if not target_filing and filings:
            target_filing = filings[0]  # Fallback to most recent
        if not target_filing:
            return None

        # Step 2: Get filing index to find XBRL instance document
        cik_clean = cik.lstrip("0") or "0"
        accn_clean = target_filing.accession_number.replace("-", "")
        index_url = (
            f"https://www.sec.gov/Archives/edgar/data/{cik_clean}/{accn_clean}/index.json"
        )

        self._client._rate_limit()
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.get(index_url, headers=self._client.headers, follow_redirects=True)
                if resp.status_code != 200:
                    return None
                index_data = resp.json()
        except Exception:
            return None

        # Find the _htm.xml file (XBRL instance document)
        items = index_data.get("directory", {}).get("item", [])
        xbrl_filename = None
        for item in items:
            name = item.get("name", "")
            if name.endswith("_htm.xml"):
                xbrl_filename = name
                break

        if not xbrl_filename:
            return None

        # Step 3: Download XBRL instance document
        xbrl_url = (
            f"https://www.sec.gov/Archives/edgar/data/{cik_clean}/{accn_clean}/{xbrl_filename}"
        )

        self._client._rate_limit()
        try:
            with httpx.Client(timeout=60.0) as client:
                resp = client.get(xbrl_url, headers=self._client.headers, follow_redirects=True)
                if resp.status_code != 200:
                    return None
                xbrl_xml = resp.content
        except Exception:
            return None

        # Step 4: Determine target period dates from the filing
        # Use reporting_date to guess the period end
        target_end = target_filing.reporting_date or ""

        # Step 5: Parse segments from XBRL instance
        segment_data = self._parser.parse_xbrl_instance_segments(
            xbrl_xml, target_end=target_end,
        )

        if not segment_data:
            return None

        # Step 6: Flatten into the format expected by the bridge
        # flat_segments: {seg_id: {concept: value}} — combines all dimension types
        flat_segments: dict[str, dict] = {}
        for category, segments in segment_data.items():
            for seg_id, facts in segments.items():
                prefixed_id = f"{category}__{seg_id}"
                flat_segments[prefixed_id] = facts

        return {
            "flat_segments": flat_segments,
            "detail": segment_data,
        }

    def _fetch_filing_list(self, query: DataQuery) -> RawDataPacket:
        """Fetch list of recent filings for an entity."""
        cik = query.entity_id
        form_types = [query.filing_type] if query.filing_type else None
        count = query.extra_params.get("count", 10)

        filings = self._client.get_recent_filings(cik, form_types, count)
        content = {
            "cik": cik,
            "filings": [
                {
                    "accession_number": f.accession_number,
                    "filing_date": f.filing_date,
                    "form_type": f.form_type,
                    "reporting_date": f.reporting_date,
                    "company_name": f.company_name,
                    "document_url": f.primary_document_url,
                }
                for f in filings
            ],
        }
        content_hash = self._client.compute_content_hash(content)

        return RawDataPacket(
            source_id=self.source_id,
            source_tier=self.source_tier,
            market_id=self.market_id,
            query=query,
            fetched_at=datetime.now(timezone.utc),
            raw_content=content,
            content_hash=content_hash,
            content_type="json",
            response_metadata={"api_endpoint": "submissions", "cik": cik},
        )

    def validate_schema(self, raw: RawDataPacket) -> SchemaValidationResult:
        """Validate that XBRL data contains required concepts."""
        content = raw.raw_content
        if content is None:
            return SchemaValidationResult(valid=False, errors=["No content"])

        if isinstance(content, dict) and "filings" in content:
            # Filing list — always valid if non-empty
            return SchemaValidationResult(
                valid=len(content["filings"]) > 0,
                errors=[] if content["filings"] else ["No filings found"],
            )

        if isinstance(content, dict) and "facts" in content:
            errors = []
            facts = content["facts"]

            for required in self.REQUIRED_CONCEPTS:
                if required not in facts:
                    # Check alternatives
                    alts = self.ALTERNATIVE_CONCEPTS.get(required, [])
                    if not any(alt in facts for alt in alts):
                        errors.append(f"Missing required concept: {required}")

            return SchemaValidationResult(
                valid=len(errors) == 0,
                errors=errors,
            )

        return SchemaValidationResult(valid=False, errors=["Unexpected content format"])

    def check_freshness(self, entity_id: str) -> FreshnessReport:
        """Check if we have the latest filing for an entity."""
        filings = self._client.get_recent_filings(entity_id, ["10-K", "10-Q"], count=1)

        latest_at = None
        if filings:
            try:
                latest_at = datetime.strptime(
                    filings[0].filing_date, "%Y-%m-%d"
                ).replace(tzinfo=timezone.utc)
            except ValueError:
                pass

        return FreshnessReport(
            entity_id=entity_id,
            source_id=self.source_id,
            last_fetched_at=None,  # Would track in production
            latest_available_at=latest_at,
            is_stale=latest_at is None,
            staleness_reason="" if latest_at else "Could not determine latest filing date",
        )

    def get_cost_estimate(self, query: DataQuery) -> CostEstimate:
        """SEC EDGAR is free — always returns zero cost."""
        return CostEstimate(
            source_id=self.source_id,
            estimated_api_calls=2,  # submissions + companyfacts
            estimated_cost_usd=0.0,
            within_daily_limit=True,
        )

    @staticmethod
    def _parse_period(period: str) -> tuple[int, str]:
        """Parse period string like 'FY2024' or 'Q3_2025' into (year, fp)."""
        period = period.upper()
        if period.startswith("FY"):
            return int(period[2:]), "FY"
        elif period.startswith("Q"):
            parts = period.replace("_", "").replace("Q", "")
            quarter = int(parts[0])
            year = int(parts[1:])
            return year, f"Q{quarter}"
        else:
            # Try to parse as year only
            try:
                return int(period), "FY"
            except ValueError:
                return 0, "FY"
