"""Tests for segment-aware evidence extraction (P0-2)."""

import pytest
from datetime import date

from aegis.core.evidence.extraction import (
    SourceChunk,
    ExtractedAssertion,
    EvidenceExtractor,
    EvidencePacketBuilder,
    SegmentDetector,
    SegmentPattern,
    KeywordExtractor,
)
from aegis.data_contracts.common import MarketId, SourceTier


@pytest.fixture
def detector():
    return SegmentDetector()


@pytest.fixture
def extractor():
    return EvidenceExtractor()


def _make_chunk(text: str, entity_id: str = "meta_platforms") -> SourceChunk:
    return SourceChunk(
        chunk_id="test_chunk_1",
        entity_id=entity_id,
        source_type="10K",
        source_ref="META-10K-FY2024",
        source_date=date(2025, 2, 1),
        language="en",
        text=text,
        section="MD&A",
    )


class TestSegmentDetector:

    def test_detect_meta_foa(self, detector):
        seg_id, seg_name, geo = detector.detect_segment(
            "Family of Apps revenue grew 15% year-over-year",
            "meta_platforms",
        )
        assert seg_id == "foa"
        assert seg_name == "Family of Apps"

    def test_detect_meta_rl(self, detector):
        seg_id, seg_name, geo = detector.detect_segment(
            "Reality Labs operating loss widened to $17.7B",
            "meta_platforms",
        )
        assert seg_id == "rl"
        assert seg_name == "Reality Labs"

    def test_detect_meta_instagram(self, detector):
        """Instagram keyword should map to FoA segment."""
        seg_id, seg_name, _ = detector.detect_segment(
            "Instagram Reels engagement increased significantly",
            "meta_platforms",
        )
        assert seg_id == "foa"

    def test_detect_aws(self, detector):
        seg_id, seg_name, _ = detector.detect_segment(
            "AWS revenue grew 19% to $25.0B",
            "amzn",
        )
        assert seg_id == "aws"
        assert seg_name == "Amazon Web Services"

    def test_detect_google_cloud(self, detector):
        seg_id, _, _ = detector.detect_segment(
            "Google Cloud revenue reached $11.4B",
            "googl",
        )
        assert seg_id == "google_cloud"

    def test_detect_no_segment(self, detector):
        """Text without segment references returns all None."""
        seg_id, seg_name, geo = detector.detect_segment(
            "The company reported strong results",
            "meta_platforms",
        )
        assert seg_id is None
        assert seg_name is None
        assert geo is None

    def test_detect_geographic_fallback(self, detector):
        """Geographic patterns should be detected as fallback."""
        _, _, geo = detector.detect_segment(
            "North America revenue growth was strong",
            "unknown_entity",
        )
        assert geo == "na"

    def test_detect_europe(self, detector):
        _, _, geo = detector.detect_segment(
            "European market showed improvement in ARPU",
            "unknown_entity",
        )
        assert geo == "europe"

    def test_detect_apac(self, detector):
        _, _, geo = detector.detect_segment(
            "Asia Pacific region continues to expand",
            "unknown_entity",
        )
        assert geo == "apac"

    def test_best_match_wins(self, detector):
        """When multiple segments match, pick the one with more keyword hits."""
        # "Facebook" and "Instagram" both map to FoA; "metaverse" maps to RL
        # If text mentions both, the one with more matches wins
        seg_id, _, _ = detector.detect_segment(
            "Facebook and Instagram saw strong growth while Reality Labs expanded",
            "meta_platforms",
        )
        # FoA has 2 keyword matches (facebook, instagram), RL has 1 (reality labs)
        assert seg_id == "foa"

    def test_register_custom_segments(self, detector):
        detector.register_entity_segments("custom_co", [
            SegmentPattern("widget_div", "Widget Division", ["widget", "gadget"]),
        ])
        seg_id, _, _ = detector.detect_segment(
            "Widget sales increased 20%",
            "custom_co",
        )
        assert seg_id == "widget_div"

    def test_tag_assertion(self, detector):
        assertion = ExtractedAssertion(
            text="AWS revenue grew 19%",
            assertion_type="revenue_guidance",
            stance="neutral",
            confidence=0.7,
            source_chunk_id="chunk_1",
        )
        chunk = _make_chunk("AWS revenue grew 19% to $25.0B", entity_id="amzn")
        detector.tag_assertion(assertion, chunk)

        assert assertion.segment_id == "aws"
        assert assertion.segment_name == "Amazon Web Services"


class TestSegmentAwareExtraction:

    def test_extractor_tags_segments(self, extractor):
        """EvidenceExtractor should automatically tag assertions with segments."""
        chunks = [
            _make_chunk(
                "Family of Apps revenue guidance is expected to grow 15%",
                entity_id="meta_platforms",
            ),
        ]
        assertions = extractor.extract_from_chunks(chunks)
        assert len(assertions) >= 1
        # The "revenue_guidance" extractor should match "guidance"/"expect"
        guidance_assertion = next(
            (a for a in assertions if a.assertion_type == "revenue_guidance"), None,
        )
        assert guidance_assertion is not None
        assert guidance_assertion.segment_id == "foa"
        assert guidance_assertion.segment_name == "Family of Apps"

    def test_extractor_no_segment_when_generic(self, extractor):
        """Generic text should have segment_id=None."""
        chunks = [
            _make_chunk(
                "The company expects revenue to grow steadily",
                entity_id="meta_platforms",
            ),
        ]
        assertions = extractor.extract_from_chunks(chunks)
        guidance = next(
            (a for a in assertions if a.assertion_type == "revenue_guidance"), None,
        )
        assert guidance is not None
        assert guidance.segment_id is None

    def test_evidence_packet_includes_segment(self):
        """EvidencePacketBuilder should include segment fields."""
        assertion = ExtractedAssertion(
            text="AWS revenue guidance strong",
            assertion_type="revenue_guidance",
            stance="supports",
            confidence=0.8,
            source_chunk_id="chunk_1",
            segment_id="aws",
            segment_name="Amazon Web Services",
            geographic_id=None,
        )
        chunk = _make_chunk("AWS revenue guidance strong", entity_id="amzn")
        builder = EvidencePacketBuilder(
            entity_id="amzn",
            market_id=MarketId.US,
            source_type="10K",
            source_ref="AMZN-10K-FY2024",
            source_date=date(2025, 2, 1),
            source_tier=SourceTier.TIER_1,
        )
        packet = builder.build_packet(assertion, chunk)

        assert packet["segment_id"] == "aws"
        assert packet["segment_name"] == "Amazon Web Services"
        assert packet["geographic_id"] is None


class TestExtractedAssertionSegmentFields:

    def test_default_segment_fields(self):
        a = ExtractedAssertion(
            text="test", assertion_type="test", stance="neutral",
            confidence=0.5, source_chunk_id="c1",
        )
        assert a.segment_id is None
        assert a.segment_name is None
        assert a.geographic_id is None

    def test_segment_fields_set(self):
        a = ExtractedAssertion(
            text="test", assertion_type="test", stance="neutral",
            confidence=0.5, source_chunk_id="c1",
            segment_id="aws", segment_name="AWS", geographic_id="na",
        )
        assert a.segment_id == "aws"
        assert a.segment_name == "AWS"
        assert a.geographic_id == "na"
