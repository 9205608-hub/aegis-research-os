"""Report Editor — Chief Analyst report shaping layer.

Runs AFTER thesis synthesis to determine:
1. What goes on the front page and in what order
2. Which exhibits/charts are most critical
3. How to frame the executive summary
4. What to emphasize vs. de-emphasize
5. The report's narrative arc

This REPLACES the fixed-order serializer with editorial judgment,
while still requiring all content to be traceable to evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aegis.core.chief_analyst.preamble import (
    AEGIS_PROJECT_PREAMBLE, resolve_display, fmt_money_big, fmt_money_small,
)


@dataclass
class EditedReport:
    """The editor's decisions about report structure and content."""

    # Executive summary — the "front page"
    headline: str  # One-line headline (like a newspaper)
    executive_summary: str  # 3-5 sentence executive summary
    front_page_numbers: list[dict]  # key numbers for front page: [{label, value, context}]

    # Section ordering — what comes first
    section_order: list[str]  # e.g. ["variant_thesis", "business_quality", "valuation", "risks", ...]
    section_emphasis: dict[str, str]  # section_name → "primary" | "secondary" | "appendix"

    # Key exhibit selection
    key_exhibits: list[dict]  # [{type, title, why_important}] — which charts matter most

    # Narrative framing
    opening_paragraph: str  # The report's opening (written by LLM editor)
    closing_paragraph: str  # The report's conclusion
    risk_summary: str  # Compressed risk section (not exhaustive list, but prioritized)

    # What NOT to emphasize
    de_emphasized: list[str] = field(default_factory=list)  # Sections/topics to push to appendix


EDITOR_TOOL_SCHEMA = {
    "type": "object",
    "required": [
        "headline", "executive_summary", "front_page_numbers",
        "section_order", "section_emphasis", "key_exhibits",
        "opening_paragraph", "closing_paragraph", "risk_summary",
    ],
    "properties": {
        "headline": {
            "type": "string",
            "description": "A one-line headline that captures the thesis. Like a newspaper headline or research note title — must be specific, opinionated, and grounded in THIS entity's actual financials (named segments, products, or competitive dynamics). Do not reference Meta, Apple, or any other company by name unless they are actual peers in the data.",
        },
        "executive_summary": {
            "type": "string",
            "description": "3-5 sentences that tell the COMPLETE story. A PM who reads ONLY this should understand: what the entity is, what our thesis is, why now, and what the risk is.",
        },
        "front_page_numbers": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["label", "value", "context"],
                "properties": {
                    "label": {"type": "string"},
                    "value": {"type": "string"},
                    "context": {"type": "string", "description": "Why this number matters. One sentence."},
                },
            },
            "minItems": 4,
            "maxItems": 6,
            "description": "The 4-6 most important numbers for the front page, with context explaining why each matters.",
        },
        "section_order": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Ordered list of report sections. NOT fixed — ordered by importance for THIS entity. Available sections: variant_thesis, business_quality, segment_economics, valuation_analysis, management_assessment, risk_assessment, accounting_quality, sector_context, macro_context, monitoring_plan",
        },
        "section_emphasis": {
            "type": "object",
            "description": "For each section, whether it's 'primary' (full detail, prominent), 'secondary' (included but condensed), or 'appendix' (pushed to end, minimal).",
            "additionalProperties": {"type": "string", "enum": ["primary", "secondary", "appendix"]},
        },
        "key_exhibits": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["type", "title", "why_important"],
                "properties": {
                    "type": {"type": "string", "enum": ["valuation_scenario_chart", "margin_radar", "sensitivity_heatmap", "segment_revenue_breakdown", "historical_growth_chart", "dcf_projection_table", "peer_comparison", "driver_tree", "consensus_vs_ours"]},
                    "title": {"type": "string"},
                    "why_important": {"type": "string"},
                },
            },
            "minItems": 3,
            "maxItems": 6,
            "description": "Which charts/exhibits are most important and should be prominent. Order = importance.",
        },
        "opening_paragraph": {
            "type": "string",
            "description": "The report's opening paragraph. This is where a top analyst shines. Start with the MOST IMPORTANT THING, not 'Company X is a ...' boilerplate. Be specific, opinionated, and grounded in data. This paragraph should make the reader want to read more.",
        },
        "closing_paragraph": {
            "type": "string",
            "description": "The report's conclusion. Summarize the thesis, the key risk, and the actionable takeaway. End with conviction.",
        },
        "risk_summary": {
            "type": "string",
            "description": "A compressed risk summary (3-5 sentences). NOT an exhaustive list — prioritize the 2-3 risks that actually matter most and could invalidate the thesis.",
        },
        "de_emphasized": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Topics or sections that should be pushed to appendix because they're not central to THIS entity's thesis.",
        },
    },
}


REPORT_EDITOR_SYSTEM_PROMPT = """You are the Editor-in-Chief shaping the final research report.

You have the complete thesis synthesis and all supporting data. Your job is to decide HOW to present this research for maximum impact and clarity.

THINK LIKE A BLOOMBERG OR GOLDMAN SACHS RESEARCH EDITOR:
1. The headline must be memorable and specific — it's the one thing people see in their inbox
2. The executive summary must be self-contained — many readers stop here
3. Front page numbers should tell a story, not just list metrics
4. Section order should match what matters for THIS entity, not a generic template
5. Key exhibits should support the thesis narrative

WHAT MAKES A GREAT FRONT PAGE:
- Opens with the most distinctive feature of the entity
- Immediately states the thesis (not background)
- Shows 4-6 numbers that support the thesis
- Makes clear whether this is a buy/sell/hold situation and why

EDITORIAL PRINCIPLES:
- Lead with insight, not description
- Numbers without context are useless — always explain WHY a number matters
- Risk section should be surgical, not exhaustive — what 2-3 risks actually matter?
- Don't bury the lead — if the most interesting thing is a segment economics story, put it first, not after generic business description

SECTION ORDERING GUIDANCE:
- There is NO fixed order. For a company where the variant is about valuation, lead with valuation.
- For a company where the story is about business quality, lead with business quality.
- For a turnaround, lead with what's changing.
- Push generic/less-relevant sections to appendix.

HARD CONSTRAINTS:
- All numbers must come from the provided data
- Do NOT invent facts or metrics
- Do NOT change any financial figures
- The editorial layer shapes PRESENTATION, not SUBSTANCE
- All claims must be traceable to the thesis synthesis or agent analyses

VALUATION ANCHORING (zero tolerance):
- When stating a per-share "fair value", "target", "intrinsic value", or
  similar in ANY field (headline, executive_summary, front_page_numbers,
  chart captions), you MUST use one of the three DCF scenario values
  (bear / base / bull) from the MARKET & VALUATION CONTEXT.
- You may NOT invent a new dollar-denominated fair-value number that does
  not match one of those three. If you want to express a blended view,
  cite the probability-weighted value that the model already computed.
- The synthesizer narrative occasionally contains the tag "[see DCF scenarios]"
  (Chinese reports: "〔详见DCF情景估值〕") — this is a marker that the
  upstream layer rewrote an off-scenario value. Preserve this tag or replace
  it with the correct scenario number. Do NOT fill in a guess.
- Violating this produces reports where the headline says one number and
  the scenario table shows another. The editor is the LAST line of defense.

EXPECTATIONS-FIRST HEADLINE RULES (Aegis 2.0 methodology, zero tolerance):
- The headline / executive_summary lead with what the price IMPLIES and
  whether verifiable facts support it — the arc is: market-implied
  expectations (quote the EXPECTATIONS FRONTIER conditionally: "at margin
  X%, the price requires ~Y% growth") → expectations vs verifiable facts
  (RECENT DISCLOSED EVENTS + financial evidence) → verification /
  falsification checkpoints.
- PROHIBITED as the headline claim: a bare "XX% 下行空间 / XX% upside /
  XX% downside" percentage. Rephrase as an expectations judgment, e.g.
  "现价隐含预期显著高于可验证基本面，关键验证点见正文". The DCF
  scenarios and DCF-vs-price gap remain quotable INSIDE the body as
  supporting evidence — never delete or hide them.
- Never quote a single-point "市场隐含增速 Z%" — only the conditional
  frontier form ("若利润率 X%，需 Y% 增速").
- When a PRICING REGIME narrative frame is provided, the opening should
  read through that frame (steady / growth / turnaround / story / mixed);
  the regime never justifies omitting the valuation gap."""


class ReportEditor:
    """Shapes the final report based on editorial judgment."""

    def __init__(self, llm_client: Any = None) -> None:
        self._llm = llm_client

    def edit(
        self,
        entity_name: str,
        synthesized_thesis: Any,  # SynthesizedThesis
        directive: Any,  # ResearchDirective
        computed_metrics: dict[str, float],
        market_data: dict[str, float],
        scenarios: dict[str, float],
        meta_facts: dict[str, Any] | None = None,
        segment_detail: dict[str, Any] | None = None,
    ) -> EditedReport:
        """Generate editorial decisions for the report."""
        user_message = self._build_message(
            entity_name, synthesized_thesis, directive,
            computed_metrics, market_data, scenarios,
            meta_facts, segment_detail,
        )
        # AUDIT 2026-07-12 R2-1：编辑层同样事前注入估值引用规则——headline/
        # 摘要是"发明锚 + 残骸%"到达读者的最后一跳，事后清洗只是兜底。
        try:
            from aegis.core.chief_analyst.thesis_synthesizer import (
                valuation_constraint_block,
            )
            user_message += valuation_constraint_block(scenarios, market_data)
        except Exception:
            pass

        _sys_prompt = AEGIS_PROJECT_PREAMBLE + REPORT_EDITOR_SYSTEM_PROMPT
        if isinstance(scenarios, dict) and scenarios.get("currency") == "CNY":
            _sys_prompt += (
                "\n\nLANGUAGE: This is an A-share (China) entity. Write ALL natural-language "
                "output fields (headline, executive_summary, opening_paragraph, "
                "closing_paragraph, risk_summary, section narratives) in Simplified Chinese "
                "(简体中文). Keep JSON keys, enum values, and section identifiers in English. "
                "Use A-share conventions: 市值以'亿'为单位、货币符号用 ¥。"
            )

        raw = self._llm.call_structured(
            system_prompt=_sys_prompt,
            user_message=user_message,
            tool_schema=EDITOR_TOOL_SCHEMA,
            tool_name="edited_report",
            role="planner",
        )

        # Post-validation: scrub off-scenario fair-value claims from editor
        # text fields, same logic we apply to the synthesizer. Without this,
        # the headline / executive_summary / opening_paragraph can quote a
        # different fair-value number than the DCF table.
        try:
            from aegis.core.chief_analyst.thesis_synthesizer import (
                _scrub_fair_value_claims,
                _valuation_sanity_verdict,
                frontier_sanctioned_growth_pcts,
                relative_valuation_sanctioned_pcts,
            )
            editor_fields = (
                "headline", "executive_summary", "opening_paragraph",
                "closing_paragraph", "risk_summary",
            )
            # 设计红线 9：前沿隐含增速/margin 档百分数是 sanctioned numbers；
            # Phase 1 同则：相对估值锚的 PE/PB/分位数字同步注册。
            # AUDIT 2026-07-12: 估值失配时 editor 字段同样进入 strict 清洗
            # ——headline/摘要正是"残影%"到达读者的最后一跳。
            _sanity = _valuation_sanity_verdict(scenarios, market_data)
            scrubbed, warns = _scrub_fair_value_claims(
                raw, scenarios, market_data, fields=editor_fields,
                extra_sanctioned_pcts=(
                    frontier_sanctioned_growth_pcts(
                        (meta_facts or {}).get("__expectations_frontier")
                    )
                    + relative_valuation_sanctioned_pcts(
                        (meta_facts or {}).get("__relative_valuation")
                    )
                ),
                strict=bool(_sanity and _sanity.get("mismatch")),
            )
            if warns:
                for k in editor_fields:
                    if k in scrubbed:
                        raw[k] = scrubbed[k]
                # BUG-A15 (2026-05-04): surface scrubber warnings so the
                # pipeline log shows when Editor invented alternate-framework
                # numbers. Was silently dropped before, making it impossible
                # to diagnose "why does headline say -45% when DCF says -98%?"
                import sys as _sys
                for _w in warns:
                    print(f"  ⚠ ReportEditor scrubber: {_w[:240]}", file=_sys.stderr, flush=True)
        except Exception:
            pass  # Never block the report on scrubber failure

        # BUG-Y26: harden list parse boundaries — same JSON-string-as-list
        # gotcha as Director (BUG-Y25). Editor's section_order being string
        # would be especially broken since it controls report layout.
        from aegis.core._coerce import coerce_list
        return EditedReport(
            headline=raw.get("headline", ""),
            executive_summary=raw.get("executive_summary", ""),
            front_page_numbers=[
                {"label": n.get("label", ""), "value": n.get("value", ""), "context": n.get("context", "")}
                for n in coerce_list(raw.get("front_page_numbers", []))
                if isinstance(n, dict)
            ],
            section_order=coerce_list(raw.get("section_order", [])),
            section_emphasis=raw.get("section_emphasis", {}),
            key_exhibits=[
                {"type": e.get("type", ""), "title": e.get("title", ""), "why_important": e.get("why_important", "")}
                for e in coerce_list(raw.get("key_exhibits", []))
                if isinstance(e, dict)
            ],
            opening_paragraph=raw.get("opening_paragraph", ""),
            closing_paragraph=raw.get("closing_paragraph", ""),
            risk_summary=raw.get("risk_summary", ""),
            de_emphasized=coerce_list(raw.get("de_emphasized", [])),
        )

    def _build_message(
        self,
        entity_name: str,
        thesis: Any,
        directive: Any,
        computed_metrics: dict[str, float],
        market_data: dict[str, float],
        scenarios: dict[str, float],
        meta_facts: dict[str, Any] | None,
        segment_detail: dict[str, Any] | None,
    ) -> str:
        parts = [
            f"=== EDITING TASK: {entity_name} Research Report ===",
            "",
        ]

        # Thesis synthesis
        parts.append("=== SYNTHESIZED THESIS ===")
        parts.append(f"Core Thesis: {thesis.core_thesis}")
        parts.append(f"Our Variant: {thesis.my_variant}")
        parts.append(f"Variant Magnitude: {thesis.variant_magnitude}")
        parts.append(f"Why Now: {thesis.why_now}")
        parts.append(f"Market's Story: {thesis.market_implied_story}")
        parts.append(f"Key Disagreement: {thesis.key_assumption_disagreement}")
        parts.append(f"Counter-Thesis: {thesis.counter_thesis}")
        parts.append(f"Why Market Is Wrong: {thesis.why_market_is_wrong}")
        parts.append(f"What Would Change Mind: {thesis.what_would_change_my_mind}")
        parts.append(f"Conviction: {thesis.conviction_narrative}")
        if thesis.unresolved_tensions:
            parts.append(f"Unresolved Tensions: {'; '.join(thesis.unresolved_tensions)}")
        parts.append("")

        # Director's angle
        if directive:
            parts.append("=== RESEARCH DIRECTOR'S FRAMING ===")
            parts.append(f"Opening Angle: {directive.opening_angle}")
            parts.append(f"Salient Characteristics: {'; '.join(directive.salient_characteristics)}")
            parts.append(f"Key Numbers to Feature: {'; '.join(directive.key_numbers)}")
            parts.append("")

        # BUG-A22: A-share inputs were USD-formatted before; Editor now
        # uses meta_facts["__display"] context like the rest.
        disp = resolve_display(meta_facts)
        _per_share = "/股" if disp["currency"] == "CNY" else ""

        # Market data
        parts.append("=== MARKET & VALUATION ===")
        price = market_data.get("current_price", 0)
        parts.append(f"Current Price: {fmt_money_small(price, disp)}{_per_share}")
        for k, v in scenarios.items():
            if isinstance(v, (int, float)):
                parts.append(f"  {k}: {fmt_money_small(v, disp)}{_per_share}")
            else:
                parts.append(f"  {k}: {v}")
        parts.append("")

        # ── Aegis 2.0 Phase 0：预期前沿 / 定价体制 / 近事件事实块 ──
        from aegis.core.chief_analyst.thesis_synthesizer import (
            frontier_prompt_lines,
        )
        _lang = "zh" if disp["currency"] == "CNY" else "en"
        _frontier_lines = frontier_prompt_lines(
            (meta_facts or {}).get("__expectations_frontier"), _lang,
        )
        if _frontier_lines:
            parts.append("=== MARKET-IMPLIED EXPECTATIONS FRONTIER (conditional reverse-DCF) ===")
            parts.append(
                "(Headline/lede must use the conditional form; bare single-point "
                "implied growth or bare ±% return claims are prohibited.)"
            )
            for _ln in _frontier_lines:
                parts.append(f"  - {_ln}")
            parts.append("")

        _regime = (meta_facts or {}).get("__pricing_regime")
        if isinstance(_regime, dict) and _regime.get("weights"):
            parts.append("=== PRICING REGIME (narrative frame only) ===")
            parts.append(
                "Weights: "
                + ", ".join(f"{k}={float(v):.2f}" for k, v in _regime["weights"].items())
                + f" | dominant: {_regime.get('dominant', '')}"
            )
            _frame = (
                _regime.get("narrative_frame_zh") if _lang == "zh"
                else _regime.get("narrative_frame_en")
            )
            if _frame:
                parts.append(f"Narrative frame: {_frame}")
            parts.append("")

        _events_block = (meta_facts or {}).get("__recent_events_prompt")
        if isinstance(_events_block, str) and _events_block.strip():
            parts.append("=== RECENT DISCLOSED EVENTS (the ONLY sanctioned catalyst source) ===")
            parts.append(_events_block)
            parts.append("")

        # Key metrics for front page selection
        parts.append("=== ALL AVAILABLE METRICS ===")
        for k, v in sorted(computed_metrics.items()):
            if isinstance(v, float):
                if abs(v) < 10:
                    parts.append(f"  {k}: {v:.4f}" if abs(v) < 1 else f"  {k}: {v:.2f}")
                else:
                    parts.append(f"  {k}: {v:,.0f}")
        parts.append("")

        # Key financials
        if meta_facts:
            parts.append("=== KEY FINANCIALS ===")
            for label, key in [("Revenue", "revenue"), ("Net Income", "net_income"),
                               ("EBITDA", "ebitda"), ("FCF", "free_cash_flow"),
                               ("SBC", "sbc"), ("R&D", "research_and_development")]:
                val = meta_facts.get(key)
                if val and isinstance(val, (int, float)):
                    parts.append(f"  {label}: {fmt_money_big(val, disp)}")
            parts.append("")

        # Segments
        if segment_detail:
            parts.append("=== SEGMENTS ===")
            for cat, segs in segment_detail.items():
                if segs:
                    for seg_id, data in sorted(segs.items(),
                                               key=lambda x: x[1].get("revenue", 0), reverse=True):
                        rev = data.get("revenue", 0)
                        if rev > 0:
                            parts.append(f"  {seg_id}: {fmt_money_big(rev, disp)}")
            parts.append("")

        parts.append("Based on all the above, make your editorial decisions for the report.")
        return "\n".join(parts)
