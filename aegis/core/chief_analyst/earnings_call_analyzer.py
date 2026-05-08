"""Earnings Call Analyzer — LLM-driven earnings call transcript analysis.

Extracts structured insights from earnings call transcripts:
1. Management tone and sentiment shifts
2. Key guidance and KPI updates
3. Analyst question focus areas (what buy-side cares about)
4. Hedging language and evasive answers
5. Guidance accuracy vs prior quarter promises
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class EarningsCallInsights:
    """Structured output from earnings call transcript analysis."""

    # Management tone
    overall_tone: str  # "confident", "cautiously_optimistic", "defensive", "neutral"
    tone_shift_vs_prior: str  # "more_confident", "less_confident", "unchanged", "unknown"
    notable_language_changes: list[str]  # Specific phrasing shifts

    # Guidance and forward-looking statements
    guidance_items: list[dict[str, str]]  # [{metric, guidance_text, direction}]
    # direction: "raised", "maintained", "lowered", "new", "withdrawn"

    # What analysts are focused on (from Q&A section)
    analyst_focus_topics: list[str]  # Top 3-5 topics analysts pressed on

    # Evasive or hedging language
    hedging_signals: list[dict[str, str]]  # [{topic, signal, quote_snippet}]

    # Key numbers mentioned by management
    management_key_numbers: list[str]  # "Revenue guidance: $X-$Y billion"

    # One-paragraph executive summary of the call
    call_summary: str

    # Confidence that this call is material for the investment thesis
    materiality: str  # "high", "medium", "low"

    # Metadata
    quarter: str = ""
    year: int = 0
    word_count: int = 0


EARNINGS_CALL_TOOL_SCHEMA = {
    "type": "object",
    "required": [
        "overall_tone", "tone_shift_vs_prior", "notable_language_changes",
        "guidance_items", "analyst_focus_topics", "hedging_signals",
        "management_key_numbers", "call_summary", "materiality",
    ],
    "properties": {
        "overall_tone": {
            "type": "string",
            "enum": ["confident", "cautiously_optimistic", "defensive", "neutral"],
            "description": "The overall tone of management during the call.",
        },
        "tone_shift_vs_prior": {
            "type": "string",
            "enum": ["more_confident", "less_confident", "unchanged", "unknown"],
            "description": "How management's tone has shifted compared to the previous quarter's call. 'unknown' if you don't have prior call context.",
        },
        "notable_language_changes": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 5,
            "description": "Specific phrasing or language shifts that are investment-relevant. e.g. 'Shifted from \"strong demand\" to \"steady demand\" for cloud products' or 'First time using \"headwinds\" to describe China market'.",
        },
        "guidance_items": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["metric", "guidance_text", "direction"],
                "properties": {
                    "metric": {"type": "string", "description": "The metric being guided. e.g. 'Revenue', 'Operating Margin', 'CapEx', 'Headcount'"},
                    "guidance_text": {"type": "string", "description": "The actual guidance language. e.g. 'Revenue of $38-40 billion for Q2'"},
                    "direction": {"type": "string", "enum": ["raised", "maintained", "lowered", "new", "withdrawn"]},
                },
            },
            "description": "Forward-looking guidance provided by management.",
        },
        "analyst_focus_topics": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 5,
            "description": "The top 3-5 topics that analysts asked about most or pushed hardest on. This reveals what the buy-side cares about. e.g. 'AI monetization timeline', 'Margin trajectory in 2025', 'China regulatory risk'.",
        },
        "hedging_signals": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["topic", "signal"],
                "properties": {
                    "topic": {"type": "string"},
                    "signal": {"type": "string", "description": "What the hedging suggests. e.g. 'Management deflected margin guidance questions twice, suggesting internal pressure on costs'"},
                    "quote_snippet": {"type": "string", "description": "A very brief 5-10 word snippet showing the hedging."},
                },
            },
            "description": "Instances where management was evasive, deflected questions, or used notably hedging language.",
        },
        "management_key_numbers": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 8,
            "description": "Key numbers and data points mentioned by management. Format: 'Metric: value'. e.g. 'Q2 Revenue Guidance: $38-40B', 'Cloud ARR: $35B (+22% YoY)', 'Headcount reduction: 5,000'.",
        },
        "call_summary": {
            "type": "string",
            "description": "A 3-5 sentence executive summary of the earnings call from an investment analyst's perspective. Focus on what MATTERS for the stock, not a neutral summary.",
        },
        "materiality": {
            "type": "string",
            "enum": ["high", "medium", "low"],
            "description": "'high' if the call contains material new information or tone shifts that could move the stock. 'medium' if it confirms existing narratives. 'low' if it's a non-event.",
        },
    },
}


EARNINGS_CALL_SYSTEM_PROMPT = """You are a senior buy-side analyst reading an earnings call transcript.

YOUR ROLE: Extract the INVESTMENT-RELEVANT signals from this transcript. You are NOT summarizing the call — you are identifying what MATTERS for the stock.

HOW A TOP ANALYST READS A TRANSCRIPT:
1. TONE FIRST: How does management SOUND? Confident? Defensive? Has the tone changed from last quarter?
2. LANGUAGE SHIFTS: Did they change phrasing? "Strong demand" → "steady demand" is a signal.
3. GUIDANCE: What are they guiding to? Raised, maintained, or lowered? Any new guidance or withdrawn guidance?
4. ANALYST QUESTIONS: What are the buy-side analysts pushing on? Their questions reveal what the market cares about.
5. EVASION: Where did management dodge, hedge, or give non-answers? That's often where the risk is.
6. KEY NUMBERS: What specific numbers did management volunteer? These are what they want you to anchor on.

WHAT MAKES A GOOD ANALYSIS:
- GOOD: "Management shifted from 'excited about AI opportunity' to 'early stages of AI monetization', suggesting the timeline is longer than previously implied"
- BAD: "Management discussed AI initiatives"

- GOOD: "CFO deflected three questions about 2025 margins, only offering 'we expect to invest in growth' — suggests margin pressure ahead"
- BAD: "CFO discussed margins"

HARD CONSTRAINTS:
- Do NOT invent quotes — only reference language actually in the transcript
- Do NOT fabricate numbers not in the transcript
- DO be specific about tone shifts and language changes
- DO identify what the buy-side cares about from the Q&A section
- If the transcript is too short or low-quality to analyze meaningfully, set materiality to "low"
"""


class EarningsCallAnalyzer:
    """LLM-driven earnings call transcript analysis."""

    def __init__(self, llm_client: Any = None) -> None:
        self._llm = llm_client

    def analyze(
        self,
        transcript_text: str,
        symbol: str,
        quarter: int = 0,
        year: int = 0,
        entity_name: str = "",
    ) -> EarningsCallInsights:
        """Analyze an earnings call transcript."""
        # Truncate very long transcripts to fit context window
        max_chars = 60_000  # ~15K tokens
        if len(transcript_text) > max_chars:
            transcript_text = transcript_text[:max_chars] + "\n\n[...TRANSCRIPT TRUNCATED...]"

        user_message = self._build_message(
            transcript_text, symbol, quarter, year, entity_name,
        )

        raw = self._llm.call_structured(
            system_prompt=EARNINGS_CALL_SYSTEM_PROMPT,
            user_message=user_message,
            tool_schema=EARNINGS_CALL_TOOL_SCHEMA,
            tool_name="earnings_call_analysis",
            role="chief_analyst",
        )

        # BUG-Y26: harden list parse boundaries
        from aegis.core._coerce import coerce_list
        return EarningsCallInsights(
            overall_tone=raw.get("overall_tone", "neutral"),
            tone_shift_vs_prior=raw.get("tone_shift_vs_prior", "unknown"),
            notable_language_changes=coerce_list(raw.get("notable_language_changes", [])),
            guidance_items=coerce_list(raw.get("guidance_items", [])),
            analyst_focus_topics=coerce_list(raw.get("analyst_focus_topics", [])),
            hedging_signals=coerce_list(raw.get("hedging_signals", [])),
            management_key_numbers=coerce_list(raw.get("management_key_numbers", [])),
            call_summary=raw.get("call_summary", ""),
            materiality=raw.get("materiality", "medium"),
            quarter=f"Q{quarter}" if quarter else "",
            year=year,
            word_count=len(transcript_text.split()),
        )

    def _build_message(
        self,
        transcript_text: str,
        symbol: str,
        quarter: int,
        year: int,
        entity_name: str,
    ) -> str:
        parts = [
            f"=== EARNINGS CALL TRANSCRIPT: {entity_name or symbol} ({symbol}) Q{quarter} {year} ===",
            "",
            "Read the following transcript and extract investment-relevant signals.",
            "",
            "=== TRANSCRIPT BEGIN ===",
            transcript_text,
            "=== TRANSCRIPT END ===",
        ]
        return "\n".join(parts)
