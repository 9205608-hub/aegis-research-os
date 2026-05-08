"""Evidence Extraction Pipeline — Section 12.1.

Source -> Chunk -> Extract -> Normalize -> Translate (if needed)
-> Embed -> Packet -> Claim Link.

Evidence is NOT summary text — it is a structured support object
with conditions, provenance, and traceability.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from uuid import uuid4

from aegis.data_contracts.common import MarketId, SourceTier


@dataclass(frozen=True)
class SourceChunk:
    """A chunk of source text extracted from a filing or transcript."""

    chunk_id: str
    entity_id: str
    source_type: str  # "10K", "earnings_call", "annual_report", etc.
    source_ref: str
    source_date: date
    language: str  # "en", "zh", etc.
    text: str
    section: str  # "MD&A", "risk_factors", "management_discussion", etc.
    page_or_position: str = ""


@dataclass
class ExtractedAssertion:
    """An assertion extracted from a source chunk."""

    text: str
    assertion_type: str  # "revenue_guidance", "margin_outlook", "risk_factor", etc.
    stance: str  # "supports", "contradicts", "neutral", "ambiguous"
    confidence: float  # 0.0-1.0, extraction confidence
    source_chunk_id: str
    applicability_conditions: list[str] = field(default_factory=list)
    # Segment tagging — populated by SegmentDetector
    segment_id: str | None = None
    segment_name: str | None = None
    geographic_id: str | None = None


@dataclass
class EvidencePacketBuilder:
    """Builds evidence packets from extracted assertions."""

    entity_id: str
    market_id: MarketId
    source_type: str
    source_ref: str
    source_date: date
    source_tier: SourceTier
    extractor_version: str = "evidence_v1"

    def build_packet(
        self,
        assertion: ExtractedAssertion,
        chunk: SourceChunk,
        *,
        translated_text: str | None = None,
    ) -> dict:
        """Build a structured evidence packet from an extracted assertion.

        Returns dict matching EvidencePacket schema.
        """
        content_for_hash = f"{chunk.source_ref}:{chunk.text}:{assertion.text}"
        source_hash = "sha256:" + hashlib.sha256(
            content_for_hash.encode("utf-8")
        ).hexdigest()

        evidence_id = f"ev_{self.entity_id}_{uuid4().hex[:8]}"

        packet = {
            "evidence_id": evidence_id,
            "entity_id": self.entity_id,
            "market_id": self.market_id.value,
            "source_type": self.source_type,
            "source_ref": self.source_ref,
            "source_hash": source_hash,
            "source_date": self.source_date.isoformat(),
            "accepted_at": datetime.now(timezone.utc).isoformat(),
            "period_relevance": self._infer_period(chunk),
            "excerpt": chunk.text,
            "excerpt_language": chunk.language,
            "normalized_statement": assertion.text,
            "assertion_type": assertion.assertion_type,
            "stance": assertion.stance,
            "applicability_conditions": assertion.applicability_conditions,
            "evidence_class": self._classify_evidence_class(),
            "source_tier": self.source_tier.value,
            "extraction_method": "rule_then_model",
            "extractor_version": self.extractor_version,
            "linked_claim_ids": [],
            # Segment tagging
            "segment_id": assertion.segment_id,
            "segment_name": assertion.segment_name,
            "geographic_id": assertion.geographic_id,
        }

        if translated_text:
            packet["translated_statement"] = translated_text
            packet["translation_method"] = "llm_translation_with_financial_glossary"

        return packet

    def _classify_evidence_class(self) -> str:
        if self.source_type in ("10K", "10Q", "8K", "annual_report", "semi_annual"):
            return "primary_filing"
        elif self.source_type in ("earnings_call", "investor_presentation"):
            return "transcript"
        elif self.source_type in ("sell_side_report",):
            return "sell_side"
        return "other"

    @staticmethod
    def _infer_period(chunk: SourceChunk) -> str:
        year = chunk.source_date.year
        return f"FY{year}"


class EvidenceExtractor:
    """Coordinates the full evidence extraction pipeline.

    In Phase 2, this uses rule-based extraction.
    LLM-based extraction will be layered on top later.
    """

    VERSION = "evidence_v1"

    def __init__(self, segment_detector: SegmentDetector | None = None) -> None:
        self._extractors: dict[str, "RuleExtractor"] = {}
        self._segment_detector = segment_detector or SegmentDetector()
        self._register_default_extractors()

    def _register_default_extractors(self) -> None:
        """Register rule-based extractors for common assertion types."""
        self._extractors["revenue_guidance"] = KeywordExtractor(
            assertion_type="revenue_guidance",
            keywords=["guidance", "expect", "outlook", "forecast", "project",
                       "预计", "指引", "展望", "预期"],
            stance_default="neutral",
        )
        self._extractors["margin_outlook"] = KeywordExtractor(
            assertion_type="margin_outlook",
            keywords=["margin", "profitability", "efficiency", "cost reduction",
                       "毛利率", "净利率", "盈利能力", "降本增效"],
            stance_default="neutral",
        )
        self._extractors["risk_factor"] = KeywordExtractor(
            assertion_type="risk_factor",
            keywords=["risk", "uncertainty", "challenge", "headwind", "adverse",
                       "风险", "不确定性", "挑战", "逆风"],
            stance_default="contradicts",
        )
        self._extractors["competitive_position"] = KeywordExtractor(
            assertion_type="competitive_position",
            keywords=["market share", "competitive", "moat", "differentiat",
                       "市场份额", "竞争", "护城河", "壁垒"],
            stance_default="neutral",
        )
        self._extractors["capital_allocation"] = KeywordExtractor(
            assertion_type="capital_allocation",
            keywords=["buyback", "repurchase", "dividend", "acquisition", "capex",
                       "回购", "分红", "收购", "资本开支"],
            stance_default="neutral",
        )

    def extract_from_chunks(
        self, chunks: list[SourceChunk],
    ) -> list[ExtractedAssertion]:
        """Extract assertions from a list of source chunks.

        Each assertion is automatically tagged with segment information
        if the text references a known business segment.
        """
        assertions = []
        for chunk in chunks:
            for extractor in self._extractors.values():
                result = extractor.extract(chunk)
                if result:
                    # Tag with segment info
                    self._segment_detector.tag_assertion(result, chunk)
                    assertions.append(result)
        return assertions


@dataclass
class SegmentPattern:
    """A pattern that identifies a business segment in text."""

    segment_id: str
    segment_name: str
    keywords: list[str]  # Case-insensitive keywords to match
    geographic_id: str | None = None  # Optional geographic tag


class SegmentDetector:
    """Detects business segment references in text and tags assertions.

    Maintains a registry of segment patterns (keywords → segment_id).
    Can be loaded from entity-specific configs or sector packs.

    Usage:
        detector = SegmentDetector()
        detector.register_entity_segments("meta_platforms", [
            SegmentPattern("foa", "Family of Apps", ["family of apps", "foa", "facebook", "instagram", "whatsapp"]),
            SegmentPattern("rl", "Reality Labs", ["reality labs", "metaverse", "quest", "horizon"]),
        ])
        detector.tag_assertion(assertion, chunk)
    """

    def __init__(self) -> None:
        # entity_id → list of SegmentPattern
        self._patterns: dict[str, list[SegmentPattern]] = {}
        self._register_common_segments()

    def _register_common_segments(self) -> None:
        """Pre-register segment patterns for well-known companies."""
        # Meta Platforms
        self.register_entity_segments("meta_platforms", [
            SegmentPattern("foa", "Family of Apps",
                           ["family of apps", "foa", "facebook", "instagram", "whatsapp", "messenger", "social media"]),
            SegmentPattern("rl", "Reality Labs",
                           ["reality labs", "metaverse", "quest", "horizon", "vr headset", "mixed reality"]),
        ])
        # Amazon
        self.register_entity_segments("amzn", [
            SegmentPattern("aws", "Amazon Web Services",
                           ["aws", "amazon web services", "cloud computing", "cloud segment"]),
            SegmentPattern("na_retail", "North America Retail",
                           ["north america", "domestic retail", "na segment"]),
            SegmentPattern("intl_retail", "International Retail",
                           ["international segment", "international retail"]),
        ])
        # Alphabet / Google
        self.register_entity_segments("googl", [
            SegmentPattern("google_services", "Google Services",
                           ["google services", "google search", "youtube", "google play", "android"]),
            SegmentPattern("google_cloud", "Google Cloud",
                           ["google cloud", "gcp", "cloud platform"]),
            SegmentPattern("other_bets", "Other Bets",
                           ["other bets", "waymo", "verily", "calico"]),
        ])
        # Apple
        self.register_entity_segments("aapl", [
            SegmentPattern("iphone", "iPhone", ["iphone"]),
            SegmentPattern("services", "Services",
                           ["services segment", "app store", "apple music", "icloud", "apple tv+"]),
            SegmentPattern("mac", "Mac", ["mac", "macbook", "imac"]),
            SegmentPattern("ipad", "iPad", ["ipad"]),
            SegmentPattern("wearables", "Wearables, Home and Accessories",
                           ["wearables", "apple watch", "airpods", "homepod"]),
        ])
        # Microsoft
        self.register_entity_segments("msft", [
            SegmentPattern("intelligent_cloud", "Intelligent Cloud",
                           ["intelligent cloud", "azure", "server products"]),
            SegmentPattern("productivity", "Productivity and Business Processes",
                           ["productivity", "office 365", "linkedin", "dynamics"]),
            SegmentPattern("personal_computing", "More Personal Computing",
                           ["personal computing", "windows", "xbox", "surface", "bing"]),
        ])
        # Geographic patterns (entity-agnostic, used as fallback)
        self._geo_patterns = [
            SegmentPattern("na", "North America", ["north america", "u.s.", "united states", "us market", "domestic"],
                           geographic_id="na"),
            SegmentPattern("europe", "Europe", ["europe", "emea", "eu market", "european"],
                           geographic_id="europe"),
            SegmentPattern("apac", "Asia Pacific", ["asia pacific", "apac", "asia", "china", "japan", "india"],
                           geographic_id="apac"),
            SegmentPattern("row", "Rest of World", ["rest of world", "row", "latin america", "latam"],
                           geographic_id="row"),
        ]

    def register_entity_segments(
        self, entity_id: str, patterns: list[SegmentPattern],
    ) -> None:
        """Register segment patterns for an entity."""
        self._patterns[entity_id] = patterns

    def detect_segment(
        self, text: str, entity_id: str,
    ) -> tuple[str | None, str | None, str | None]:
        """Detect which segment the text refers to.

        Returns (segment_id, segment_name, geographic_id).
        All None if no segment detected.
        """
        text_lower = text.lower()

        # Check entity-specific patterns first
        entity_patterns = self._patterns.get(entity_id, [])
        best_match: SegmentPattern | None = None
        best_count = 0

        for pattern in entity_patterns:
            count = sum(1 for kw in pattern.keywords if kw.lower() in text_lower)
            if count > best_count:
                best_count = count
                best_match = pattern

        if best_match:
            return best_match.segment_id, best_match.segment_name, best_match.geographic_id

        # Check geographic patterns as fallback
        for pattern in self._geo_patterns:
            if any(kw.lower() in text_lower for kw in pattern.keywords):
                return None, None, pattern.geographic_id

        return None, None, None

    def tag_assertion(
        self, assertion: ExtractedAssertion, chunk: SourceChunk,
    ) -> ExtractedAssertion:
        """Tag an assertion with segment information based on its text.

        Modifies the assertion in-place and returns it.
        """
        seg_id, seg_name, geo_id = self.detect_segment(
            chunk.text, chunk.entity_id,
        )
        assertion.segment_id = seg_id
        assertion.segment_name = seg_name
        assertion.geographic_id = geo_id
        return assertion


@dataclass
class KeywordExtractor:
    """Rule-based extractor that matches keywords in text."""

    assertion_type: str
    keywords: list[str]
    stance_default: str = "neutral"

    def extract(self, chunk: SourceChunk) -> ExtractedAssertion | None:
        text_lower = chunk.text.lower()
        matched = [kw for kw in self.keywords if kw.lower() in text_lower]
        if not matched:
            return None

        return ExtractedAssertion(
            text=chunk.text[:500],  # Truncate for normalized statement
            assertion_type=self.assertion_type,
            stance=self.stance_default,
            confidence=min(0.3 + 0.1 * len(matched), 0.9),
            source_chunk_id=chunk.chunk_id,
            applicability_conditions=[f"keyword_match:{','.join(matched)}"],
        )
