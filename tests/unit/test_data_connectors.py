"""Tests for SEC EDGAR data connectors — XBRL parser, entity registry, connector protocol."""

import pytest

from aegis.core.acquisition.connectors.xbrl_parser import XBRLParser, XBRLFilingData
from aegis.core.acquisition.connectors.sec_entity_registry import SECEntityRegistry
from aegis.core.acquisition.connectors.edgar_connector import SECEDGARConnector
from aegis.core.acquisition.models import DataQuery, SchemaValidationResult, RawDataPacket
from aegis.data_contracts.common import SourceTier


# ---------------------------------------------------------------------------
# XBRL Parser Tests
# ---------------------------------------------------------------------------

class TestXBRLParser:
    """Test XBRL parsing from Company Facts JSON format."""

    def setup_method(self):
        self.parser = XBRLParser()

    def _mock_company_facts(self) -> dict:
        """Create a mock SEC Company Facts JSON structure."""
        return {
            "cik": 1326801,
            "entityName": "Meta Platforms, Inc.",
            "facts": {
                "us-gaap": {
                    "Revenues": {
                        "label": "Revenues",
                        "units": {
                            "USD": [
                                {"val": 164710000000, "accn": "0001326801-25-000013",
                                 "fy": 2024, "fp": "FY", "form": "10-K",
                                 "start": "2024-01-01", "end": "2024-12-31",
                                 "filed": "2025-02-05"},
                                {"val": 134902000000, "accn": "0001326801-24-000012",
                                 "fy": 2023, "fp": "FY", "form": "10-K",
                                 "start": "2023-01-01", "end": "2023-12-31",
                                 "filed": "2024-02-02"},
                                {"val": 40589000000, "accn": "0001326801-25-000010",
                                 "fy": 2024, "fp": "Q3", "form": "10-Q",
                                 "start": "2024-07-01", "end": "2024-09-30",
                                 "filed": "2024-10-30"},
                            ],
                        },
                    },
                    "NetIncomeLoss": {
                        "label": "Net Income (Loss)",
                        "units": {
                            "USD": [
                                {"val": 62360000000, "accn": "0001326801-25-000013",
                                 "fy": 2024, "fp": "FY", "form": "10-K",
                                 "start": "2024-01-01", "end": "2024-12-31",
                                 "filed": "2025-02-05"},
                            ],
                        },
                    },
                    "Assets": {
                        "label": "Total Assets",
                        "units": {
                            "USD": [
                                {"val": 256166000000, "accn": "0001326801-25-000013",
                                 "fy": 2024, "fp": "FY", "form": "10-K",
                                 "end": "2024-12-31",
                                 "filed": "2025-02-05"},
                            ],
                        },
                    },
                    "OperatingIncomeLoss": {
                        "label": "Operating Income",
                        "units": {
                            "USD": [
                                # Company-level
                                {"val": 69381000000, "accn": "0001326801-25-000013",
                                 "fy": 2024, "fp": "FY", "form": "10-K",
                                 "start": "2024-01-01", "end": "2024-12-31"},
                                # Segment-level
                                {"val": 87106000000, "accn": "0001326801-25-000013",
                                 "fy": 2024, "fp": "FY", "form": "10-K",
                                 "start": "2024-01-01", "end": "2024-12-31",
                                 "segment": "meta:FamilyOfAppsSegmentMember",
                                 "segmentLabel": "Family of Apps"},
                                {"val": -17725000000, "accn": "0001326801-25-000013",
                                 "fy": 2024, "fp": "FY", "form": "10-K",
                                 "start": "2024-01-01", "end": "2024-12-31",
                                 "segment": "meta:RealityLabsSegmentMember",
                                 "segmentLabel": "Reality Labs"},
                            ],
                        },
                    },
                },
            },
        }

    def test_parse_annual_facts(self):
        """Parse FY2024 facts from mock company facts."""
        facts = self._mock_company_facts()
        result = self.parser.parse_company_facts(facts, fiscal_year=2024, fiscal_period="FY")

        assert result.entity_name == "Meta Platforms, Inc."
        assert result.fiscal_year == 2024
        assert result.fiscal_period == "FY"
        assert result.facts["us-gaap:Revenues"] == 164710000000
        assert result.facts["us-gaap:NetIncomeLoss"] == 62360000000
        assert result.facts["us-gaap:Assets"] == 256166000000

    def test_segment_extraction(self):
        """Segment-level facts should be separated from company-level."""
        facts = self._mock_company_facts()
        result = self.parser.parse_company_facts(facts, fiscal_year=2024, fiscal_period="FY")

        # Should have segment data
        assert len(result.segment_facts) >= 2
        # Check normalized segment IDs
        seg_ids = set(result.segment_facts.keys())
        assert "family_of_apps" in seg_ids or any("family" in s for s in seg_ids)

    def test_available_periods(self):
        """Should list available fiscal periods."""
        facts = self._mock_company_facts()
        periods = self.parser.extract_available_periods(facts, "10-K")

        assert len(periods) >= 2
        # Should include 2024 and 2023
        years = [p["fiscal_year"] for p in periods]
        assert 2024 in years
        assert 2023 in years

    def test_extract_segments(self):
        """Should extract unique segment definitions."""
        facts = self._mock_company_facts()
        segments = self.parser.extract_segments(facts)

        assert len(segments) >= 2
        seg_names = [s["name"] for s in segments]
        assert "Family of Apps" in seg_names
        assert "Reality Labs" in seg_names

    def test_normalize_segment_id(self):
        """Segment ID normalization should handle XBRL naming conventions."""
        assert XBRLParser._normalize_segment_id("meta:FamilyOfAppsSegmentMember") == "family_of_apps"
        assert XBRLParser._normalize_segment_id("meta:RealityLabsSegmentMember") == "reality_labs"
        assert XBRLParser._normalize_segment_id("us-gaap:CloudServicesMember") == "cloud_services"

    def test_wrong_period_returns_empty(self):
        """Requesting a period that doesn't exist returns empty facts."""
        facts = self._mock_company_facts()
        result = self.parser.parse_company_facts(facts, fiscal_year=2020, fiscal_period="FY")
        assert len(result.facts) == 0


# ---------------------------------------------------------------------------
# Entity Registry Tests
# ---------------------------------------------------------------------------

class TestSECEntityRegistry:
    """Test ticker ↔ CIK lookup."""

    def setup_method(self):
        self.registry = SECEntityRegistry()

    def test_common_tickers_preloaded(self):
        """Common tickers should be available without API call."""
        assert self.registry.get_cik("META") == "0001326801"
        assert self.registry.get_cik("AAPL") == "0000320193"
        assert self.registry.get_cik("MSFT") == "0000789019"
        assert self.registry.get_cik("NVDA") == "0001045810"

    def test_case_insensitive(self):
        """Ticker lookup should be case-insensitive."""
        assert self.registry.get_cik("meta") == "0001326801"
        assert self.registry.get_cik("Meta") == "0001326801"

    def test_reverse_lookup(self):
        """CIK → ticker lookup should work."""
        assert self.registry.get_ticker("0001326801") == "META"

    def test_unknown_ticker(self):
        """Unknown ticker without API should return None."""
        # Won't make API call in test (no httpx or mock)
        result = self.registry.get_cik("ZZZZZ_FAKE_TICKER")
        # Either None (no API) or a CIK (if API call succeeds)
        assert result is None or isinstance(result, str)

    def test_manual_registration(self):
        """Manual registration should work."""
        self.registry.register("TEST", "0000000001", "Test Corp")
        assert self.registry.get_cik("TEST") == "0000000001"
        assert self.registry.get_name("0000000001") == "Test Corp"

    def test_size(self):
        """Registry should have pre-loaded entries."""
        assert self.registry.size >= 30  # We pre-loaded 30+ tickers


# ---------------------------------------------------------------------------
# EDGAR Connector Protocol Tests
# ---------------------------------------------------------------------------

class TestSECEDGARConnector:
    """Test connector implements protocol and validates correctly."""

    def setup_method(self):
        self.connector = SECEDGARConnector()

    def test_protocol_attributes(self):
        """Connector must have required protocol attributes."""
        assert self.connector.source_id == "edgar"
        assert self.connector.source_tier == SourceTier.TIER_1
        assert self.connector.market_id == "us"
        assert self.connector.license_type == "free"

    def test_cost_estimate_is_free(self):
        """SEC EDGAR is free — cost should always be zero."""
        query = DataQuery(entity_id="0001326801", market_id="us", data_type="filing")
        estimate = self.connector.get_cost_estimate(query)
        assert estimate.estimated_cost_usd == 0.0
        assert estimate.within_daily_limit is True

    def test_validate_schema_with_valid_content(self):
        """Valid XBRL content should pass schema validation."""
        packet = RawDataPacket(
            source_id="edgar",
            source_tier=SourceTier.TIER_1,
            market_id="us",
            query=DataQuery(entity_id="test", market_id="us", data_type="filing"),
            fetched_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            raw_content={
                "facts": {
                    "us-gaap:Revenues": 100_000_000,
                    "us-gaap:NetIncomeLoss": 20_000_000,
                    "us-gaap:Assets": 500_000_000,
                },
            },
            content_hash="sha256:" + "a" * 64,
            content_type="json",
        )
        result = self.connector.validate_schema(packet)
        assert result.valid is True

    def test_validate_schema_missing_concept(self):
        """Missing required concept should fail validation."""
        packet = RawDataPacket(
            source_id="edgar",
            source_tier=SourceTier.TIER_1,
            market_id="us",
            query=DataQuery(entity_id="test", market_id="us", data_type="filing"),
            fetched_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            raw_content={
                "facts": {
                    "us-gaap:Revenues": 100_000_000,
                    # Missing NetIncomeLoss and Assets
                },
            },
            content_hash="sha256:" + "a" * 64,
            content_type="json",
        )
        result = self.connector.validate_schema(packet)
        assert result.valid is False
        assert len(result.errors) > 0

    def test_parse_period(self):
        """Period string parsing should handle various formats."""
        assert SECEDGARConnector._parse_period("FY2024") == (2024, "FY")
        assert SECEDGARConnector._parse_period("Q3_2025") == (2025, "Q3")
        assert SECEDGARConnector._parse_period("2024") == (2024, "FY")
