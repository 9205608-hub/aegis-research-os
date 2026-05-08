"""Tests for Earnings Call Transcript integration."""

import pytest
from unittest.mock import MagicMock, patch
from aegis.core.chief_analyst.earnings_call_analyzer import (
    EarningsCallAnalyzer,
    EarningsCallInsights,
)


def _mock_llm_response():
    """Realistic EarningsCallAnalyzer LLM response."""
    return {
        "overall_tone": "cautiously_optimistic",
        "tone_shift_vs_prior": "less_confident",
        "notable_language_changes": [
            "Shifted from 'strong momentum' to 'steady progress' for cloud business",
            "First mention of 'macro headwinds' in prepared remarks",
        ],
        "guidance_items": [
            {"metric": "Revenue", "guidance_text": "Revenue of $38-40 billion for Q2", "direction": "maintained"},
            {"metric": "Operating Margin", "guidance_text": "Expect margins to expand 100-200bps YoY", "direction": "raised"},
            {"metric": "CapEx", "guidance_text": "Full-year capex of $35-40 billion", "direction": "raised"},
        ],
        "analyst_focus_topics": [
            "AI monetization timeline and ROI",
            "Margin trajectory through 2025",
            "Impact of regulatory changes on advertising",
        ],
        "hedging_signals": [
            {"topic": "AI Revenue", "signal": "Management deflected direct questions about AI revenue contribution, suggesting monetization is still nascent", "quote_snippet": "early stages of monetization"},
        ],
        "management_key_numbers": [
            "Q1 Revenue: $39.1B (+12% YoY)",
            "Operating Margin: 42% (up from 38%)",
            "DAU: 3.27B (+5% YoY)",
            "CapEx Guidance: $35-40B for FY2025",
        ],
        "call_summary": "Meta reported a solid Q1 with 12% revenue growth driven by Reels monetization and AI-powered ad targeting improvements. Management raised margin guidance but significantly increased capex guidance for AI infrastructure, creating tension between near-term profitability and long-term AI investment. The Q&A session revealed analyst concern about AI monetization timelines.",
        "materiality": "high",
    }


class TestEarningsCallAnalyzerParsing:
    """Test EarningsCallInsights parsing from LLM output."""

    def test_parse_valid_response(self):
        mock_llm = MagicMock()
        mock_llm.call_structured.return_value = _mock_llm_response()

        analyzer = EarningsCallAnalyzer(llm_client=mock_llm)
        insights = analyzer.analyze(
            transcript_text="This is a test transcript...",
            symbol="META",
            quarter=1,
            year=2025,
            entity_name="Meta Platforms",
        )

        assert isinstance(insights, EarningsCallInsights)
        assert insights.overall_tone == "cautiously_optimistic"
        assert insights.tone_shift_vs_prior == "less_confident"
        assert insights.materiality == "high"
        assert insights.quarter == "Q1"
        assert insights.year == 2025

    def test_guidance_items(self):
        mock_llm = MagicMock()
        mock_llm.call_structured.return_value = _mock_llm_response()

        analyzer = EarningsCallAnalyzer(llm_client=mock_llm)
        insights = analyzer.analyze(
            transcript_text="test", symbol="META",
            quarter=1, year=2025,
        )

        assert len(insights.guidance_items) == 3
        rev_guidance = insights.guidance_items[0]
        assert rev_guidance["metric"] == "Revenue"
        assert rev_guidance["direction"] == "maintained"

    def test_analyst_focus(self):
        mock_llm = MagicMock()
        mock_llm.call_structured.return_value = _mock_llm_response()

        analyzer = EarningsCallAnalyzer(llm_client=mock_llm)
        insights = analyzer.analyze(
            transcript_text="test", symbol="META",
            quarter=1, year=2025,
        )

        assert len(insights.analyst_focus_topics) == 3
        assert "AI monetization" in insights.analyst_focus_topics[0]

    def test_hedging_signals(self):
        mock_llm = MagicMock()
        mock_llm.call_structured.return_value = _mock_llm_response()

        analyzer = EarningsCallAnalyzer(llm_client=mock_llm)
        insights = analyzer.analyze(
            transcript_text="test", symbol="META",
            quarter=1, year=2025,
        )

        assert len(insights.hedging_signals) == 1
        assert insights.hedging_signals[0]["topic"] == "AI Revenue"

    def test_call_summary_non_empty(self):
        mock_llm = MagicMock()
        mock_llm.call_structured.return_value = _mock_llm_response()

        analyzer = EarningsCallAnalyzer(llm_client=mock_llm)
        insights = analyzer.analyze(
            transcript_text="test", symbol="META",
            quarter=1, year=2025,
        )

        assert len(insights.call_summary) > 50

    def test_transcript_truncation(self):
        """Very long transcripts should be truncated."""
        mock_llm = MagicMock()
        mock_llm.call_structured.return_value = _mock_llm_response()

        analyzer = EarningsCallAnalyzer(llm_client=mock_llm)
        long_text = "word " * 20_000  # ~100K chars

        insights = analyzer.analyze(
            transcript_text=long_text, symbol="TEST",
            quarter=1, year=2025,
        )

        # Should still work (truncated internally)
        assert isinstance(insights, EarningsCallInsights)
        # The LLM should have been called with truncated text
        call_args = mock_llm.call_structured.call_args
        user_msg = call_args.kwargs.get("user_message", "") or call_args[1].get("user_message", "")
        assert "TRUNCATED" in user_msg

    def test_word_count_tracking(self):
        mock_llm = MagicMock()
        mock_llm.call_structured.return_value = _mock_llm_response()

        analyzer = EarningsCallAnalyzer(llm_client=mock_llm)
        insights = analyzer.analyze(
            transcript_text="This is exactly five words",
            symbol="TEST", quarter=1, year=2025,
        )

        assert insights.word_count == 5


class TestOpenBBTranscriptConnector:
    """Test get_earnings_transcript method."""

    def test_no_api_key_returns_empty(self):
        from aegis.core.acquisition.connectors.openbb_connector import OpenBBConnector
        conn = OpenBBConnector(fmp_api_key=None)
        result = conn.get_earnings_transcript("AAPL")
        assert result == {}

    @patch("requests.get")
    def test_successful_fetch(self, mock_get):
        from aegis.core.acquisition.connectors.openbb_connector import OpenBBConnector

        # Mock list response
        list_resp = MagicMock()
        list_resp.ok = True
        list_resp.json.return_value = [{"year": 2025, "quarter": 1}]

        # Mock transcript response
        transcript_resp = MagicMock()
        transcript_resp.ok = True
        transcript_resp.json.return_value = [{
            "content": "Good afternoon everyone. Welcome to the Q1 earnings call.",
            "date": "2025-04-25",
        }]

        mock_get.side_effect = [list_resp, transcript_resp]

        conn = OpenBBConnector(fmp_api_key="test_key")
        result = conn.get_earnings_transcript("AAPL")

        assert result["symbol"] == "AAPL"
        assert result["year"] == 2025
        assert result["quarter"] == 1
        assert result["word_count"] > 0
        assert "Welcome" in result["content"]

    @patch("requests.get")
    def test_empty_list_returns_empty(self, mock_get):
        from aegis.core.acquisition.connectors.openbb_connector import OpenBBConnector

        resp = MagicMock()
        resp.ok = True
        resp.json.return_value = []
        mock_get.return_value = resp

        conn = OpenBBConnector(fmp_api_key="test_key")
        result = conn.get_earnings_transcript("UNKNOWN")
        assert result == {}


class TestAgentMacroInjection:
    """Test that earnings call insights are properly structured for agent context."""

    def test_agent_macro_structure(self):
        """Verify the dict structure injected into agent_macro."""
        insights = EarningsCallInsights(
            overall_tone="confident",
            tone_shift_vs_prior="more_confident",
            notable_language_changes=["test change"],
            guidance_items=[{"metric": "Revenue", "guidance_text": "$40B", "direction": "raised"}],
            analyst_focus_topics=["AI monetization"],
            hedging_signals=[],
            management_key_numbers=["Revenue: $40B"],
            call_summary="Strong quarter.",
            materiality="high",
            quarter="Q1",
            year=2025,
        )

        # Build the same dict the orchestrator would build
        ec_context = {
            "tone": insights.overall_tone,
            "tone_shift": insights.tone_shift_vs_prior,
            "materiality": insights.materiality,
            "call_summary": insights.call_summary,
            "guidance_items": insights.guidance_items,
            "analyst_focus_topics": insights.analyst_focus_topics,
            "hedging_signals": insights.hedging_signals,
            "management_key_numbers": insights.management_key_numbers,
            "notable_language_changes": insights.notable_language_changes,
        }

        assert ec_context["tone"] == "confident"
        assert len(ec_context["guidance_items"]) == 1
        assert ec_context["guidance_items"][0]["direction"] == "raised"
