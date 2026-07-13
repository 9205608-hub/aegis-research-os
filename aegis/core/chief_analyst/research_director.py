"""Research Director — Chief Analyst pre-agent layer.

Runs BEFORE specialist agents to:
1. Identify the entity's most salient characteristics
2. Form an initial hypothesis (the "research angle")
3. Determine what matters most for THIS specific entity
4. Guide agent weighting and research emphasis
5. Define what the report's opening should convey

This is the "定调" step — the chief analyst sets the research direction
before any specialist work begins.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aegis.core.chief_analyst.preamble import (
    AEGIS_PROJECT_PREAMBLE, resolve_display, fmt_money_big, fmt_money_small,
)


@dataclass
class ResearchDirective:
    """Output of the Research Director — guides the entire research process."""

    # Core identity: what makes this entity distinctive
    salient_characteristics: list[str]  # Top 3-5 most notable features

    # Research hypothesis: the chief analyst's initial read
    initial_hypothesis: str  # One-paragraph thesis draft
    hypothesis_type: str  # "growth", "value", "turnaround", "quality_compounder", "cyclical_play", "event_driven", "structural_change"

    # What matters most for this entity
    key_variables: list[str]  # 2-4 variables that will determine the outcome
    key_controversy: str  # What the market is debating
    what_consensus_likely_believes: str  # The "priced-in narrative"

    # Research emphasis guidance for agents
    agent_emphasis: dict[str, str]  # agent_name → specific focus instruction
    agent_depth: dict[str, str]  # agent_name → "deep" | "standard" | "light" | "skip"
    research_priority_order: list[str]  # Which aspects to investigate first

    # Report framing
    opening_angle: str  # How the report should open (the "hook")
    why_now: str  # Why this entity deserves attention right now
    key_numbers: list[str]  # 3-5 numbers that should appear on page one

    # Confidence in initial read
    initial_confidence: str  # "high", "medium", "low"
    what_could_flip_my_view: str  # What evidence would change this hypothesis


# Tool schema for structured output
DIRECTIVE_TOOL_SCHEMA = {
    "type": "object",
    "required": [
        "salient_characteristics", "initial_hypothesis", "hypothesis_type",
        "key_variables", "key_controversy", "what_consensus_likely_believes",
        "agent_emphasis", "agent_depth", "research_priority_order",
        "opening_angle", "why_now", "key_numbers",
        "initial_confidence", "what_could_flip_my_view",
    ],
    "properties": {
        "salient_characteristics": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 3,
            "maxItems": 5,
            "description": "The 3-5 most distinctive, notable features of this entity. What would a top analyst mention FIRST? e.g. 'Operating margins expanding from 25% to 42% in 2 years', '67% revenue concentration in top 4 customers'. IMPORTANT: reference ONLY this specific entity — do not mention any other company by name.",
        },
        "initial_hypothesis": {
            "type": "string",
            "description": "Your initial investment hypothesis in one paragraph. Be specific and opinionated. Not 'this is a good company' but a concrete claim about WHY the market may be wrong and what specific variable drives the thesis. IMPORTANT: reference ONLY this specific entity — do not mention any other company (Meta, Apple, etc.) by name.",
        },
        "hypothesis_type": {
            "type": "string",
            "enum": ["growth", "value", "turnaround", "quality_compounder", "cyclical_play", "event_driven", "structural_change"],
        },
        "key_variables": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 2,
            "maxItems": 4,
            "description": "The 2-4 variables that will most determine whether this investment works. These should be concrete, observable numbers specific to THIS entity — not boilerplate. Do not reference other companies by name.",
        },
        "key_controversy": {
            "type": "string",
            "description": "What is the market currently debating about this entity? The central disagreement.",
        },
        "what_consensus_likely_believes": {
            "type": "string",
            "description": "What the market's priced-in narrative probably is. Be specific.",
        },
        "agent_emphasis": {
            "type": "object",
            "description": "Specific focus instructions for each specialist agent. Keys: accounting_analyst, business_analyst, management_analyst, valuation_analyst, variant_analyst, risk_analyst. Values: what this agent should pay EXTRA attention to for this specific entity.",
            "additionalProperties": {"type": "string"},
        },
        "agent_depth": {
            "type": "object",
            "description": "How deeply each agent should analyze. Keys: accounting_analyst, business_analyst, management_analyst, valuation_analyst, variant_analyst, risk_analyst. Values: 'deep' (this agent's domain is CENTRAL to the thesis — produce thorough, detailed analysis), 'standard' (normal depth), 'light' (this dimension is less relevant — focus only on red flags or dealbreakers), 'skip' (this agent adds nothing for this entity — do not run). Example: for a pure-play SaaS company, you might set management_analyst='light' but business_analyst='deep'. Be strategic — not everything matters equally for every entity.",
            "additionalProperties": {"type": "string", "enum": ["deep", "standard", "light", "skip"]},
        },
        "research_priority_order": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Which aspects matter most, in priority order. e.g. ['segment_economics', 'capital_allocation', 'competitive_moat', 'valuation'] — this is NOT a fixed order, it's entity-specific.",
        },
        "opening_angle": {
            "type": "string",
            "description": "How should the report open? The 'hook'. What a top analyst would write as the first sentence about THIS specific entity. Must reference concrete numbers from the data provided. IMPORTANT: do not reference any other company by name.",
        },
        "why_now": {
            "type": "string",
            "description": "Why does this entity deserve research attention RIGHT NOW? What has changed or is about to change?",
        },
        "key_numbers": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 3,
            "maxItems": 5,
            "description": "The 3-5 most important numbers that should appear prominently. Format: 'label: value'. e.g. 'Operating Margin: 42%', 'FCF Yield: 4.2%', 'Revenue CAGR (3Y): 14%'",
        },
        "initial_confidence": {
            "type": "string",
            "enum": ["high", "medium", "low"],
        },
        "what_could_flip_my_view": {
            "type": "string",
            "description": "What specific evidence from the specialist agents would make you abandon or reverse this hypothesis?",
        },
    },
}


RESEARCH_DIRECTOR_SYSTEM_PROMPT = """You are the Chief Research Analyst for Aegis Research OS.

YOUR ROLE: You are the FIRST analyst to look at this entity. Before any specialist agent runs, you must form an initial view — like a senior portfolio manager who glances at the key data and immediately identifies what matters.

HOW A TOP ANALYST THINKS:
1. First, what JUMPS OUT? What's the most distinctive thing about this entity?
2. What's the market narrative? What does consensus believe?
3. Where could consensus be wrong? What's the potential variant?
4. What are the 2-3 variables that will determine the outcome?
5. How should we frame this research to be maximally useful?

YOU ARE NOT A TEMPLATE-FILLER. You are forming a JUDGMENT. Your output should read like something a top-5 sell-side analyst would think in the first 10 minutes of looking at a company.

BE SPECIFIC AND OPINIONATED:
- BAD: "This is a large technology company with strong margins"
- GOOD: Concrete claim grounded in the specific entity's actual financials, naming the specific revenue, margin, and valuation numbers provided to you, with a clear contrarian angle

- BAD: "Revenue growth is important"
- GOOD: Name the ONE variable that most determines the thesis and quantify what outcome would validate or break it

CRITICAL — CONTEXT DISCIPLINE:
- You are analyzing ONE specific entity. Never reference other companies by name (e.g. Meta, Apple, Microsoft) unless they appear as actual peers in the data provided.
- All examples should feel native to THIS entity's industry, product line, and financial profile.
- Use the data you are given — historical revenue trajectory, segment breakdown, peer comparison — to ground every claim.

USE THE DATA PROVIDED:
- You receive computed metrics, macro context, sector pack, segment breakdowns, consensus estimates
- Ground your hypothesis in specific numbers
- Reference actual margin levels, growth rates, valuation multiples
- Compare to what the sector pack says is "healthy" for this type of business

YOUR OUTPUT GUIDES THE ENTIRE RESEARCH PROCESS:
- Your agent_emphasis tells each specialist what to focus on for THIS entity
- Your research_priority_order determines what matters most
- Your opening_angle becomes the backbone of the final report
- Your key_variables become the thesis's central tests

HARD CONSTRAINTS:
- Do NOT fabricate numbers not in the provided data
- Do NOT make definitive buy/sell calls — form a hypothesis to be tested
- DO be specific enough that someone could disagree with you
- DO identify what would change your mind

EXPECTATIONS-FIRST FRAMING (Aegis 2.0 methodology):
The research question is NOT "what is this company worth" but "what
expectations does the current price embed, are they compatible with
verifiable facts, and what signals would falsify them?" Accordingly:
- When a MARKET-IMPLIED EXPECTATIONS FRONTIER is provided, anchor
  what_consensus_likely_believes and key_controversy on it, quoting the
  conditional form ("at margin X%, the price requires ~Y% growth"). Never
  assert a single-point "market implies Z% growth" — one price cannot
  identify both growth and margin.
- When RECENT DISCLOSED EVENTS are provided, they are the ONLY sanctioned
  catalyst source — do not hypothesize undisclosed M&A / order stories.
- When a PRICING REGIME is provided, use its narrative frame (steady /
  growth / turnaround / story / mixed) to set the research angle. The
  regime only selects the frame and verification points; the DCF-vs-price
  gap must still be stated openly — the gap itself is information.
- The opening_angle should read as an expectations judgment with named
  verification points, not a bare "XX% downside/upside" claim."""


class ResearchDirector:
    """Pre-agent Chief Analyst layer that sets research direction."""

    def __init__(self, llm_client: Any = None) -> None:
        self._llm = llm_client

    def direct(
        self,
        entity_id: str,
        entity_name: str,
        meta_facts: dict[str, Any],
        computed_metrics: dict[str, float],
        macro_context: dict[str, Any],
        sector_pack: dict[str, Any],
        segment_detail: dict[str, Any] | None = None,
        market_data: dict[str, float] | None = None,
        consensus_estimates: dict[str, Any] | None = None,
        price_target_consensus: dict[str, Any] | None = None,
        historical_data: dict | None = None,
        scenarios: dict[str, float] | None = None,
        implied_growth: float | None = None,
        sensitivity_rankings: list[dict] | None = None,
    ) -> ResearchDirective:
        """Generate research directive from entity data."""
        user_message = self._build_message(
            entity_id, entity_name, meta_facts, computed_metrics,
            macro_context, sector_pack, segment_detail, market_data,
            consensus_estimates, price_target_consensus, historical_data,
            scenarios, implied_growth, sensitivity_rankings,
        )

        _sys_prompt = AEGIS_PROJECT_PREAMBLE + RESEARCH_DIRECTOR_SYSTEM_PROMPT
        if (isinstance(macro_context, dict) and macro_context.get("language") == "zh-CN") or \
           (isinstance(scenarios, dict) and scenarios.get("currency") == "CNY"):
            # Baseline 600568 run (2026-05-05) caught Director writing
            # "市值（$5B）" for a ¥53亿 (~$750M) company — a 6.7x hallucinated
            # number AND wrong currency. Director's language directive used
            # to lack the currency rule that synthesizer/editor/architect
            # already had. Bring it to parity + add an explicit anti-
            # hallucination rule on market_cap (use the input value, do
            # NOT round-shift to a different magnitude).
            _sys_prompt += (
                "\n\nLANGUAGE: This is an A-share (China) entity. Write ALL natural-language "
                "fields (opening_angle, initial_hypothesis, key_variables, key_controversy, "
                "agent_emphasis values, salient_characteristics) in Simplified Chinese (简体中文). "
                "Keep JSON keys and enum values in English. "
                "\n\nCURRENCY: ALL monetary amounts MUST use ¥ and '亿' / '万' units, "
                "NEVER $ / B / M. Examples: '市值¥53亿' (NOT '$5B'); "
                "'净现金¥3.81亿' (NOT '$0.38B'); 'DCF基准¥-0.32/股' (NOT '$-0.32/share'). "
                "Per-share values use ¥X.XX/股 format. "
                "\n\nNUMBER FIDELITY: Use the EXACT market_cap, revenue, and net_cash values "
                "provided in the input. Do NOT round across magnitude boundaries (e.g. "
                "¥53亿 → '$5B' is BOTH wrong currency AND wrong magnitude — ¥53亿 ≈ $750M). "
                "If the input says market_cap=5,300,000,000 CNY, write '市值¥53亿', not "
                "'$5亿' or '$5B' or any other re-scaled form."
            )

        raw = self._llm.call_structured(
            system_prompt=_sys_prompt,
            user_message=user_message,
            tool_schema=DIRECTIVE_TOOL_SCHEMA,
            tool_name="research_directive",
            role="planner",
        )

        # BUG-Y25 (2026-05-06): LLM occasionally returns list-typed fields
        # as JSON-encoded strings, e.g. `key_variables="[\"a\", \"b\"]"`. The
        # dataclass stores it as-is (Python dataclasses don't validate types)
        # and downstream `', '.join(key_variables)` then iterates char-by-char
        # producing `[, ", a, ", ,, ", b, ", ]` in logs. Use the same
        # `_coerce_list` helper as thesis_synthesizer for all list-typed
        # fields to harden the parse boundary.
        from aegis.core._coerce import coerce_dict
        from aegis.core.chief_analyst.thesis_synthesizer import _coerce_list
        return ResearchDirective(
            salient_characteristics=_coerce_list(raw.get("salient_characteristics", [])),
            initial_hypothesis=raw.get("initial_hypothesis", ""),
            hypothesis_type=raw.get("hypothesis_type", "growth"),
            key_variables=_coerce_list(raw.get("key_variables", [])),
            key_controversy=raw.get("key_controversy", ""),
            what_consensus_likely_believes=raw.get("what_consensus_likely_believes", ""),
            # BUG-Y25 dict 版（2026-07-13 R7 宁德实锤）：LLM 偶发把 dict 字段
            # 序列化成 JSON 字符串，orchestrator agent_depth.get(n) 直接炸
            # （'str' object has no attribute 'get'）。列表字段当年同款事故
            # 由 _coerce_list 收口，这两个 dict 字段此前无人设防。
            agent_emphasis=coerce_dict(raw.get("agent_emphasis", {})),
            agent_depth=coerce_dict(raw.get("agent_depth", {})),
            research_priority_order=_coerce_list(raw.get("research_priority_order", [])),
            opening_angle=raw.get("opening_angle", ""),
            why_now=raw.get("why_now", ""),
            key_numbers=_coerce_list(raw.get("key_numbers", [])),
            initial_confidence=raw.get("initial_confidence", "medium"),
            what_could_flip_my_view=raw.get("what_could_flip_my_view", ""),
        )

    def _build_message(
        self,
        entity_id: str,
        entity_name: str,
        meta_facts: dict[str, Any],
        computed_metrics: dict[str, float],
        macro_context: dict[str, Any],
        sector_pack: dict[str, Any],
        segment_detail: dict[str, Any] | None,
        market_data: dict[str, float] | None,
        consensus_estimates: dict[str, Any] | None,
        price_target_consensus: dict[str, Any] | None,
        historical_data: dict | None,
        scenarios: dict[str, float] | None,
        implied_growth: float | None,
        sensitivity_rankings: list[dict] | None,
    ) -> str:
        # BUG-A22: resolve display context once so KEY FINANCIALS / MARKET
        # DATA / DCF SCENARIOS render in the entity's actual currency. Was
        # hard-coded `$XB` regardless of CNY/USD before — caused Director
        # to write `市值$5B` for ¥53亿 A-share entities (LLM echoed input).
        disp = resolve_display(meta_facts)
        parts = [
            f"=== ENTITY: {entity_name} ({entity_id}) ===",
            "",
        ]

        # Key financials
        if meta_facts:
            parts.append("=== KEY FINANCIALS ===")
            key_fins = [
                ("Revenue", "revenue"), ("Net Income", "net_income"),
                ("EBITDA", "ebitda"), ("Operating Income", "operating_income"),
                ("Free Cash Flow", "free_cash_flow"), ("Operating Cash Flow", "operating_cash_flow"),
                ("Total Debt", "total_debt"), ("Cash & Equivalents", "cash_and_equivalents"),
                ("Share Buybacks", "share_buybacks"), ("SBC", "sbc"),
                ("R&D Expense", "research_and_development"),
            ]
            for label, key in key_fins:
                val = meta_facts.get(key)
                if val is not None:
                    if isinstance(val, float) and abs(val) < 1:
                        parts.append(f"  {label}: {val:.1%}")
                    elif isinstance(val, (int, float)):
                        parts.append(f"  {label}: {fmt_money_big(val, disp)}")
            parts.append("")

        # Computed metrics
        if computed_metrics:
            parts.append("=== PROFITABILITY & QUALITY METRICS ===")
            for k, v in sorted(computed_metrics.items()):
                if isinstance(v, float):
                    if abs(v) < 10:
                        parts.append(f"  {k}: {v:.4f}" if abs(v) < 1 else f"  {k}: {v:.2f}")
                    else:
                        parts.append(f"  {k}: {v:,.0f}")
            parts.append("")

        # Market data and valuation
        if market_data:
            parts.append("=== MARKET DATA ===")
            # BUG-Y16/Y17 (2026-05-06): the old dispatch picked between big
            # vs small formatter purely by magnitude (`v > big_scale`), which
            # was wrong both ways:
            #  - market_cap ¥8159亿 < big_scale (¥1万亿) → used fmt_money_small,
            #    rendering as `¥815900000000.00` (12-digit raw blob)
            #  - shares_outstanding 0.42B < big_scale → also fmt_money_small,
            #    rendering as `¥420000000.00` (¥ sigil on a share count)
            # Now dispatch by KEY NAME instead: per-share fields stay tiny
            # (¥1903.99); aggregate-money fields use the auto-scaling
            # fmt_money_big (¥X亿 / ¥X万亿); share counts get a plain count
            # with `亿股` / `B shares` suffix matching the entity currency.
            _per_share_keys = {"current_price", "price", "stock_price"}
            _share_count_keys = {"shares_outstanding", "shares", "share_count", "diluted_shares"}
            _share_unit = "亿股" if disp["currency"] == "CNY" else "B shares"
            for k, v in market_data.items():
                if not isinstance(v, (int, float)) or not v:
                    continue
                if k in _per_share_keys:
                    parts.append(f"  {k}: {fmt_money_small(v, disp)}")
                elif k in _share_count_keys:
                    parts.append(f"  {k}: {v / 1e8:.2f}{_share_unit}"
                                 if disp["currency"] == "CNY"
                                 else f"  {k}: {v / 1e9:.2f}{_share_unit}")
                else:
                    # Default: aggregate money — auto-scaled by fmt_money_big.
                    parts.append(f"  {k}: {fmt_money_big(v, disp)}")
            parts.append("")

        if scenarios:
            parts.append("=== DCF SCENARIOS ===")
            for k, v in scenarios.items():
                if isinstance(v, (int, float)):
                    parts.append(f"  {k}: {fmt_money_small(v, disp)}/股"
                                 if disp["currency"] == "CNY"
                                 else f"  {k}: {fmt_money_small(v, disp)}/share")
            if implied_growth is not None:
                # BUG-Y20: respect ReverseDCF unreliable flag — don't feed
                # the LLM a fake-clean number when the bisection didn't
                # converge. Director's Opening Angle would otherwise build
                # narrative around a non-existent growth implication.
                if meta_facts.get("__implied_growth_unreliable"):
                    _bnd = meta_facts.get("__implied_growth_boundary_hit", "?")
                    parts.append(
                        f"  market_implied_revenue_growth: n/a "
                        f"(reverse-DCF non-monotonic; bisection hit {_bnd} bound)"
                    )
                else:
                    try:
                        parts.append(f"  market_implied_revenue_growth: {float(implied_growth):.1%}")
                    except (ValueError, TypeError):
                        parts.append(f"  market_implied_revenue_growth: {implied_growth}")
            parts.append("")

        # ── Aegis 2.0 Phase 0：预期前沿 / 定价体制 / 近事件事实块 ──
        # macro_context (= orchestrator agent_macro) 已按市场语言渲染好。
        _priced_in = (macro_context or {}).get("priced_in") or {}
        _frontier = _priced_in.get("expectations_frontier") or {}
        if _frontier.get("lines"):
            parts.append("=== MARKET-IMPLIED EXPECTATIONS FRONTIER (conditional reverse-DCF) ===")
            parts.append(
                "(Quote conditionally — 'at margin X%, the price requires ~Y% "
                "growth'. Single-point implied growth is prohibited.)"
            )
            for _ln in _frontier["lines"]:
                parts.append(f"  - {_ln}")
            parts.append("")

        _regime = (macro_context or {}).get("pricing_regime") or {}
        if _regime.get("weights"):
            parts.append("=== PRICING REGIME (narrative frame only) ===")
            parts.append(
                "Weights: "
                + ", ".join(f"{k}={float(v):.2f}" for k, v in _regime["weights"].items())
                + f" | dominant: {_regime.get('dominant', '')}"
            )
            if _regime.get("narrative_frame"):
                parts.append(f"Narrative frame: {_regime['narrative_frame']}")
            for _vf in _regime.get("verification_focus") or []:
                parts.append(f"  verify: {_vf}")
            parts.append("")

        _events_block = (macro_context or {}).get("recent_events")
        if isinstance(_events_block, str) and _events_block.strip():
            parts.append("=== RECENT DISCLOSED EVENTS (the ONLY sanctioned catalyst source) ===")
            parts.append(_events_block)
            parts.append("")

        if sensitivity_rankings:
            parts.append("=== TOP VALUATION SENSITIVITIES ===")
            for sr in sensitivity_rankings[:5]:
                parts.append(f"  {sr.get('assumption', '')}: {float(sr.get('impact_pct', 0)):.1f}% impact")
            parts.append("")

        # Historical growth
        hist_growth = meta_facts.get("__historical_growth", {})
        if hist_growth:
            parts.append("=== HISTORICAL REVENUE GROWTH ===")
            for yr, g in sorted(hist_growth.items()):
                parts.append(f"  {yr}: {g:.1%}")
            cagr = meta_facts.get("__revenue_cagr")
            if cagr:
                parts.append(f"  CAGR: {cagr:.1%}")
            parts.append("")

        # Segment breakdown
        if segment_detail:
            parts.append("=== SEGMENT BREAKDOWN ===")
            for category, segments in segment_detail.items():
                if not segments:
                    continue
                parts.append(f"  [{category.replace('_', ' ').title()}]")
                for seg_id, data in sorted(segments.items(),
                                           key=lambda x: x[1].get("revenue", 0), reverse=True):
                    rev = data.get("revenue", 0)
                    if rev > 0:
                        # BUG-Y14 (2026-05-06): A-share segment breakdown was
                        # leaking ${rev/1e9:.1f}B into Director input. Use the
                        # __display-aware helper so CN reports get ¥X亿.
                        parts.append(f"    {seg_id.replace('_', ' ').title()}: {fmt_money_big(rev, disp)} revenue")
            parts.append("")

        # Macro context
        if macro_context:
            parts.append("=== MACRO CONTEXT ===")
            cycle = macro_context.get("cycle_phase", "")
            parts.append(f"  Cycle phase: {cycle}")
            parts.append("")

        # Sector pack
        if sector_pack:
            parts.append(f"=== SECTOR: {sector_pack.get('sector_name', '')} ===")
            comp = sector_pack.get("competitive_dynamics", {})
            moats = comp.get("moat_sources", [])
            risks = comp.get("disruption_risks", [])
            if moats:
                parts.append(f"  Moat sources: {', '.join(moats[:4])}")
            if risks:
                parts.append(f"  Disruption risks: {', '.join(risks[:4])}")
            parts.append("")

        # Consensus
        if consensus_estimates:
            parts.append("=== SELL-SIDE CONSENSUS ===")
            for key, est in list(consensus_estimates.items())[:6]:
                # BUG-Y14: same A-share leak in consensus estimates summary.
                parts.append(f"  {key}: mean={fmt_money_big(est.get('mean', 0), disp)} ({est.get('analyst_count', 0)} analysts)")
            parts.append("")

        if price_target_consensus:
            parts.append("=== PRICE TARGET CONSENSUS ===")
            for k, v in price_target_consensus.items():
                parts.append(f"  {k}: {v}")
            parts.append("")

        parts.append("Based on all the above, form your initial research directive.")
        return "\n".join(parts)
