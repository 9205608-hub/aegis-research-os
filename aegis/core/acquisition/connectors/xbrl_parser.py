"""XBRL Parser — extract structured financial facts from SEC Company Facts.

Parses the SEC Company Facts JSON format into structured fact dictionaries
that can be mapped by the US Market Adapter's CONCEPT_MAP.

Also extracts segment-level data when available (XBRL dimensions).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class XBRLFact:
    """A single XBRL fact extracted from a filing."""

    concept: str  # e.g., "us-gaap:Revenues"
    value: float | int
    unit: str  # e.g., "USD", "shares", "pure"
    fiscal_year: int  # e.g., 2024
    fiscal_period: str  # "FY", "Q1", "Q2", "Q3", "Q4"
    form_type: str  # "10-K", "10-Q"
    accession_number: str
    period_start: str = ""  # YYYY-MM-DD (for duration items)
    period_end: str = ""  # YYYY-MM-DD
    filed_date: str = ""
    segment_id: str | None = None  # For segment-level data
    segment_name: str | None = None
    segment_dimension: str | None = None  # XBRL dimension axis


@dataclass
class XBRLFilingData:
    """Structured financial data extracted from XBRL for a specific filing period."""

    entity_name: str
    cik: str
    fiscal_year: int
    fiscal_period: str
    form_type: str
    facts: dict[str, float | int]  # concept → value
    segment_facts: dict[str, dict[str, float | int]] = field(default_factory=dict)
    # e.g., {"family_of_apps": {"revenue": 160826000000}, "reality_labs": {"revenue": 3884000000}}
    raw_facts: list[XBRLFact] = field(default_factory=list)


class XBRLParser:
    """Parse SEC Company Facts JSON into structured financial data.

    The SEC Company Facts API returns ALL facts for all periods.
    This parser filters to a specific fiscal year/period and extracts
    both company-level and segment-level data.
    """

    # XBRL segment dimensions that indicate operating segments
    SEGMENT_DIMENSIONS = {
        "us-gaap:StatementBusinessSegmentsAxis",
        "srt:ConsolidatedEntitiesAxis",
        "us-gaap:StatementOperatingActivitiesSegmentAxis",
    }

    # Concepts that commonly have segment breakdowns
    SEGMENT_CONCEPTS = {
        "us-gaap:Revenues",
        "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
        "us-gaap:OperatingIncomeLoss",
        "us-gaap:DepreciationAndAmortization",
        "us-gaap:Assets",
        "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment",
    }

    def parse_company_facts(
        self,
        facts_json: dict,
        fiscal_year: int,
        fiscal_period: str = "FY",
        form_type: str = "10-K",
    ) -> XBRLFilingData:
        """Extract financial data for a specific period from Company Facts JSON.

        Args:
            facts_json: Raw JSON from SEC Company Facts API
            fiscal_year: Target fiscal year (e.g., 2024)
            fiscal_period: "FY", "Q1", "Q2", "Q3", "Q4"
            form_type: "10-K" for annual, "10-Q" for quarterly
        """
        entity_name = facts_json.get("entityName", "")
        cik = str(facts_json.get("cik", ""))

        extracted_facts: dict[str, float | int] = {}
        segment_facts: dict[str, dict[str, float | int]] = {}
        raw_facts: list[XBRLFact] = []

        # Process each taxonomy (us-gaap, dei, etc.)
        for taxonomy, concepts in facts_json.get("facts", {}).items():
            for concept_name, concept_data in concepts.items():
                full_concept = f"{taxonomy}:{concept_name}"
                units = concept_data.get("units", {})

                for unit_type, fact_list in units.items():
                    for fact in fact_list:
                        fy = fact.get("fy")
                        fp = fact.get("fp", "")
                        ft = fact.get("form", "")

                        # Match target period
                        if fy != fiscal_year or fp != fiscal_period:
                            continue
                        if ft != form_type:
                            continue

                        val = fact.get("val")
                        if val is None:
                            continue

                        xbrl_fact = XBRLFact(
                            concept=full_concept,
                            value=val,
                            unit=unit_type,
                            fiscal_year=fy,
                            fiscal_period=fp,
                            form_type=ft,
                            accession_number=fact.get("accn", ""),
                            period_start=fact.get("start", ""),
                            period_end=fact.get("end", ""),
                            filed_date=fact.get("filed", ""),
                            segment_id=fact.get("segment"),
                            segment_name=fact.get("segmentLabel"),
                            segment_dimension=fact.get("dimension"),
                        )
                        raw_facts.append(xbrl_fact)

                        # Segment-level fact
                        if xbrl_fact.segment_id:
                            seg_id = self._normalize_segment_id(xbrl_fact.segment_id)
                            if seg_id not in segment_facts:
                                segment_facts[seg_id] = {}
                            segment_facts[seg_id][full_concept] = val
                        else:
                            # Company-level fact (take latest if duplicates)
                            extracted_facts[full_concept] = val

        return XBRLFilingData(
            entity_name=entity_name,
            cik=cik,
            fiscal_year=fiscal_year,
            fiscal_period=fiscal_period,
            form_type=form_type,
            facts=extracted_facts,
            segment_facts=segment_facts,
            raw_facts=raw_facts,
        )

    def extract_historical_annual(
        self,
        facts_json: dict,
        concepts: list[str],
        num_years: int = 5,
        form_type: str = "10-K",
    ) -> dict[int, dict[str, float | int]]:
        """Extract annual values for specified concepts over multiple years.

        Returns {fiscal_year: {canonical_concept: value}} for the most recent num_years.
        Handles the SEC pattern where a 10-K filing reports current + prior year comparatives.
        """
        from collections import defaultdict

        # Collect all annual facts keyed by (concept, end_date, fiscal_year)
        annual_data: dict[str, dict[str, list[tuple[int, float]]]] = defaultdict(
            lambda: defaultdict(list)
        )

        for taxonomy, tax_concepts in facts_json.get("facts", {}).items():
            for concept_name, concept_data in tax_concepts.items():
                full_concept = f"{taxonomy}:{concept_name}"
                if full_concept not in concepts:
                    continue

                for unit_type, fact_list in concept_data.get("units", {}).items():
                    for fact in fact_list:
                        fp = fact.get("fp", "")
                        ft = fact.get("form", "")
                        fy = fact.get("fy")
                        val = fact.get("val")
                        end = fact.get("end", "")
                        start = fact.get("start", "")

                        if fp != "FY" or ft != form_type or val is None or not fy:
                            continue

                        # Duration items have start+end; instant items have end only
                        # Use end date as the period identifier
                        annual_data[full_concept][end].append((fy, val))

        # For each concept, pick the value from the most recent filing that reports it
        concept_by_year: dict[str, dict[int, float]] = defaultdict(dict)
        for full_concept, end_data in annual_data.items():
            for end_date, entries in end_data.items():
                # From all filings that report this end_date, take the one from
                # the most recent fiscal year (latest filing)
                latest_fy, latest_val = max(entries, key=lambda x: x[0])
                # Map end_date to a fiscal year
                try:
                    year = int(end_date[:4])
                except (ValueError, IndexError):
                    year = latest_fy
                concept_by_year[full_concept][year] = latest_val

        # Determine the most recent num_years
        all_years: set[int] = set()
        for years_dict in concept_by_year.values():
            all_years.update(years_dict.keys())
        recent_years = sorted(all_years, reverse=True)[:num_years]

        # Build output: {year: {concept: value}}
        result: dict[int, dict[str, float | int]] = {}
        for year in sorted(recent_years):
            year_data: dict[str, float | int] = {}
            for full_concept, years_dict in concept_by_year.items():
                if year in years_dict:
                    year_data[full_concept] = years_dict[year]
            if year_data:
                result[year] = year_data

        return result

    def extract_available_periods(
        self, facts_json: dict, form_type: str = "10-K"
    ) -> list[dict[str, Any]]:
        """List all available fiscal periods in the company facts.

        Returns list of {"fiscal_year": int, "fiscal_period": str, "form_type": str}
        """
        periods: set[tuple[int, str]] = set()

        for taxonomy, concepts in facts_json.get("facts", {}).items():
            # Just check one common concept to find periods
            for concept_name in ["Revenues", "Assets", "NetIncomeLoss"]:
                if concept_name not in concepts:
                    continue
                for unit_type, fact_list in concepts[concept_name].get("units", {}).items():
                    for fact in fact_list:
                        fy = fact.get("fy")
                        fp = fact.get("fp", "")
                        ft = fact.get("form", "")
                        if fy and fp and ft == form_type:
                            periods.add((fy, fp))

        return sorted(
            [{"fiscal_year": fy, "fiscal_period": fp, "form_type": form_type}
             for fy, fp in periods],
            key=lambda x: (x["fiscal_year"], x["fiscal_period"]),
            reverse=True,
        )

    def extract_segments(self, facts_json: dict) -> list[dict[str, str]]:
        """Extract segment definitions from XBRL facts.

        Scans all facts for segment dimensions and returns unique segments.
        """
        segments: dict[str, str] = {}  # segment_id → segment_label

        for taxonomy, concepts in facts_json.get("facts", {}).items():
            for concept_name, concept_data in concepts.items():
                for unit_type, fact_list in concept_data.get("units", {}).items():
                    for fact in fact_list:
                        seg = fact.get("segment")
                        seg_label = fact.get("segmentLabel")
                        if seg and seg not in segments:
                            segments[seg] = seg_label or seg

        return [
            {"segment_id": self._normalize_segment_id(sid), "name": name, "raw_id": sid}
            for sid, name in segments.items()
        ]

    def parse_xbrl_instance_segments(
        self,
        xbrl_xml: bytes,
        target_start: str = "",
        target_end: str = "",
    ) -> dict[str, dict[str, dict[str, float | int]]]:
        """Extract segment-level facts from an XBRL instance document.

        Returns nested dict: {dimension_type: {segment_id: {concept: value}}}
        where dimension_type is "product", "geographic", or "business_segment".

        Args:
            xbrl_xml: Raw XML bytes of the XBRL instance document.
            target_start: Period start date (YYYY-MM-DD) to filter for.
            target_end: Period end date (YYYY-MM-DD) to filter for.
        """
        from xml.etree import ElementTree as ET

        root = ET.fromstring(xbrl_xml)
        ns = {
            "xbrli": "http://www.xbrl.org/2003/instance",
            "xbrldi": "http://xbrl.org/2006/xbrldi",
        }

        # Step 1: Build context map
        contexts: dict[str, dict] = {}
        for ctx in root.findall(".//xbrli:context", ns):
            ctx_id = ctx.get("id", "")
            period = ctx.find(".//xbrli:period", ns)
            start = end = instant = ""
            if period is not None:
                s = period.find("xbrli:startDate", ns)
                e = period.find("xbrli:endDate", ns)
                i = period.find("xbrli:instant", ns)
                if s is not None:
                    start = s.text or ""
                if e is not None:
                    end = e.text or ""
                if i is not None:
                    instant = i.text or ""

            segment = ctx.find(".//xbrli:segment", ns)
            dims: dict[str, str] = {}
            if segment is not None:
                for member in segment:
                    dim = member.get("dimension", "")
                    member_val = member.text or ""
                    dims[dim] = member_val

            contexts[ctx_id] = {
                "start": start, "end": end, "instant": instant, "dims": dims,
            }

        # Dimension axis → category mapping
        DIMENSION_CATEGORIES = {
            "srt:ProductOrServiceAxis": "product",
            "us-gaap:StatementBusinessSegmentsAxis": "business_segment",
            "srt:StatementGeographicalAxis": "geographic",
        }

        # Concepts to extract for segments
        SEGMENT_CONCEPTS_LOCAL = {
            "RevenueFromContractWithCustomerExcludingAssessedTax": "revenue",
            "Revenues": "revenue",
            "SalesRevenueNet": "revenue",
            "OperatingIncomeLoss": "operating_income",
            "DepreciationDepletionAndAmortization": "depreciation_amortization",
            "Assets": "total_assets",
            "CostOfGoodsAndServicesSold": "cost_of_revenue",
            "GrossProfit": "gross_profit",
            "NoncurrentAssets": "noncurrent_assets",
        }

        # Step 2: Iterate all fact elements and collect dimensional data
        result: dict[str, dict[str, dict[str, float | int]]] = {
            "product": {},
            "business_segment": {},
            "geographic": {},
        }

        for elem in root.iter():
            tag = elem.tag
            ctx_ref = elem.get("contextRef", "")
            if not ctx_ref or ctx_ref not in contexts:
                continue

            ctx = contexts[ctx_ref]

            # Filter by target period
            if target_start and ctx["start"] != target_start:
                if target_end and ctx["end"] != target_end:
                    if not ctx["instant"]:
                        continue
            if target_end and ctx["end"] != target_end and not ctx.get("instant"):
                continue

            dims = ctx["dims"]
            if not dims:
                continue

            # Extract local concept name from tag
            local_name = tag.split("}")[-1] if "}" in tag else tag.split(":")[-1]
            canonical = SEGMENT_CONCEPTS_LOCAL.get(local_name)
            if not canonical:
                continue

            # Parse value
            try:
                val = float(elem.text) if elem.text else None
                if val is None:
                    continue
                if val == int(val):
                    val = int(val)
            except (ValueError, TypeError):
                continue

            # Map dimension to category and segment ID
            for dim_axis, category in DIMENSION_CATEGORIES.items():
                if dim_axis in dims:
                    member = dims[dim_axis]
                    seg_id = self._normalize_segment_id(member)
                    if seg_id not in result[category]:
                        result[category][seg_id] = {}
                    # Keep the first value for each concept (avoid duplicates)
                    if canonical not in result[category][seg_id]:
                        result[category][seg_id][canonical] = val

        # Remove empty categories
        return {k: v for k, v in result.items() if v}

    @staticmethod
    def _normalize_segment_id(raw_segment: str) -> str:
        """Normalize XBRL segment identifier to a clean snake_case ID."""
        # XBRL segments look like "meta:FamilyOfAppsSegmentMember"
        # Normalize to "family_of_apps"
        s = raw_segment.split(":")[-1] if ":" in raw_segment else raw_segment
        # Remove common suffixes
        for suffix in ("SegmentMember", "Member", "Segment"):
            if s.endswith(suffix):
                s = s[:-len(suffix)]
        # CamelCase to snake_case
        result = []
        for i, c in enumerate(s):
            if c.isupper() and i > 0 and not s[i - 1].isupper():
                result.append("_")
            result.append(c.lower())
        return "".join(result).strip("_")
