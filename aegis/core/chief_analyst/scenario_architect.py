"""Scenario Architect — LLM-driven narrative scenario construction.

Runs AFTER base DCF is computed but BEFORE bear/bull DCF variants, replacing
the mechanical ±3-4% growth/margin adjustments with narrative-driven business
scenarios that a top analyst would construct.

Each scenario is a complete business story, not a number adjustment:
  Bear: "AWS growth slows to 15% as enterprise migration cycle matures + ad revenue drops 8% in recession"
  Base: "AWS maintains 25% growth (AI workload offsetting) + ad revenue mid-single-digit recovery"
  Bull: "AI becomes new AWS-scale business + retail media flywheel accelerates"
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from aegis.core.chief_analyst.preamble import (
    AEGIS_PROJECT_PREAMBLE, resolve_display, fmt_money_big, fmt_money_small,
)


@dataclass
class ScenarioCase:
    """A single scenario with narrative justification and quantified adjustments."""

    name: str  # "bear", "base", "bull"
    probability: float  # 0.10-0.50, all three sum to 1.0
    narrative: str  # 2-3 sentence business story
    key_driver: str  # The single most important driver for this scenario
    revenue_growth_delta: list[float]  # 10-year path, delta vs base (e.g. [-0.04, -0.03, ...])
    margin_delta: list[float]  # 10-year path, delta vs base operating margin
    # Driver-specific deltas (optional): {"DAU": [-0.01, ...], "CPM": [-0.02, ...]}
    # When present, these are applied to individual drivers and override revenue_growth_delta
    driver_deltas: dict[str, list[float]] = field(default_factory=dict)


@dataclass
class ScenarioBlueprint:
    """Output of the Scenario Architect — narrative-driven three-scenario framework."""

    scenarios: list[ScenarioCase]  # Exactly 3: bear, base, bull (base deltas are all zeros)

    # Where our view diverges from market consensus
    key_disagreements: list[str]  # 2-3 specific disagreements

    # The single variable that swings the outcome most
    primary_swing_factor: str

    def get_case(self, name: str) -> ScenarioCase | None:
        """Get a specific scenario by name."""
        for s in self.scenarios:
            if s.name == name:
                return s
        return None


# ── Tool Schema ──────────────────────────────────────────────────────────

SCENARIO_TOOL_SCHEMA = {
    "type": "object",
    "required": ["scenarios", "key_disagreements", "primary_swing_factor"],
    "properties": {
        "scenarios": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name", "probability", "narrative", "key_driver",
                             "revenue_growth_delta", "margin_delta"],
                "properties": {
                    "name": {
                        "type": "string",
                        "enum": ["bear", "base", "bull"],
                    },
                    "probability": {
                        "type": "number",
                        "minimum": 0.10,
                        "maximum": 0.50,
                        "description": "Probability weight for this scenario. All three must sum to 1.0. Reflect current market conditions — NOT a fixed 25/50/25.",
                    },
                    "narrative": {
                        "type": "string",
                        "description": "A 2-3 sentence BUSINESS STORY for this scenario. NOT 'growth declines by 4%' but 'Enterprise cloud migration cycle matures, reducing AWS growth from 30% to 15%, while advertising revenue contracts 8% as consumer discretionary spending pulls back in a mild recession.' Each scenario must be a distinct business path, not a mirror of the base case.",
                    },
                    "key_driver": {
                        "type": "string",
                        "description": "The single most important business driver for this scenario. e.g. 'Cloud migration cycle maturity' or 'AI monetization breakthrough'.",
                    },
                    "revenue_growth_delta": {
                        "type": "array",
                        "items": {"type": "number"},
                        "minItems": 10,
                        "maxItems": 10,
                        "description": "10-year array of revenue growth DELTAS vs the base case. For the base scenario, these must all be 0.0. For bear: negative values (e.g. [-0.05, -0.04, -0.03, ...]). For bull: positive values. The first 2-3 years can differ significantly; years 4-10 should converge toward 0 as uncertainty resolves. Values are percentage points (e.g. -0.04 means 4% lower growth than base).",
                    },
                    "margin_delta": {
                        "type": "array",
                        "items": {"type": "number"},
                        "minItems": 10,
                        "maxItems": 10,
                        "description": "10-year array of operating margin DELTAS vs the base case. Same rules as revenue_growth_delta. Values are percentage points (e.g. -0.03 means 3% lower margin than base).",
                    },
                    "driver_deltas": {
                        "type": "object",
                        "description": "Optional. Driver-specific growth deltas when revenue drivers are provided. Keys are driver names (e.g. 'DAU', 'CPM'), values are 10-element arrays of growth rate deltas for that driver. When provided, these override revenue_growth_delta for more granular scenario modeling. Only include this if the REVENUE DRIVERS section is present in the input.",
                        "additionalProperties": {
                            "type": "array",
                            "items": {"type": "number"},
                            "minItems": 10,
                            "maxItems": 10,
                        },
                    },
                },
            },
            "minItems": 3,
            "maxItems": 3,
            "description": "Exactly three scenarios: bear, base, bull. Each must be a DISTINCT business path with a coherent story.",
        },
        "key_disagreements": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 2,
            "maxItems": 3,
            "description": "2-3 specific points where our analysis might diverge from market consensus. e.g. 'Market prices in 20% cloud growth for 5 years; we think migration cycle peaks in Y2' or 'Consensus underestimates margin expansion from AI-driven efficiency gains'.",
        },
        "primary_swing_factor": {
            "type": "string",
            "description": "The single variable that most determines which scenario plays out. e.g. 'AI monetization timeline' or 'Consumer spending trajectory in 2025-2026'.",
        },
    },
}


# ── System Prompt ────────────────────────────────────────────────────────

SCENARIO_ARCHITECT_SYSTEM_PROMPT = """You are the Scenario Architect for Aegis Research OS.

YOUR ROLE: Construct three investment scenarios (bear, base, bull) as distinct BUSINESS STORIES, not mechanical number adjustments. You run AFTER the deterministic base-case DCF has been computed, and your job is to define what the bear and bull cases look like as real-world business outcomes.

HOW A TOP ANALYST CONSTRUCTS SCENARIOS:
1. Each scenario is a STORY about what happens to the business — not "+3% growth"
2. Bear and bull are NOT symmetric mirrors — they represent different business realities
3. The bear case is NOT "everything goes wrong" — it's "the specific risk that matters materializes"
4. The bull case is NOT "everything goes right" — it's "the specific opportunity the market underestimates plays out"
5. Probabilities reflect YOUR assessment of current conditions, not a fixed split

WHAT MAKES A GOOD SCENARIO:
- GOOD Bear: A specific business story — "the key risk materializes" — with named products, segments, or customers from THIS entity's actual business, plus quantified revenue/margin impact
- BAD Bear: Generic "Revenue growth declines by 4% and margins compress by 3%"

- GOOD Bull: A specific business story — "the specific opportunity plays out" — with named products, segments, or tailwinds from THIS entity, plus quantified revenue/margin upside
- BAD Bull: Generic "Revenue growth increases by 3% and margins expand by 2%"

CRITICAL — CONTEXT DISCIPLINE:
- Use THIS entity's actual segments, products, and drivers provided in the context.
- Do NOT reference Meta, Apple, or any other company by name unless they appear as competitors in the data.
- Every scenario story must feel native to THIS entity's industry.

QUANTIFICATION RULES:
- revenue_growth_delta: difference from base case in percentage points. If base Y1 growth is 12%, and your bear scenario has 7% growth for Y1, the delta is -0.05
- margin_delta: difference from base case in percentage points. If base Y1 margin is 35%, and your bear scenario has 32% margin, the delta is -0.03
- Years 1-3 can have large deltas (this is where scenarios diverge most)
- Years 4-10 should CONVERGE toward 0 (long-term economics tend to revert)
- Be SPECIFIC and GROUNDED — don't just pick round numbers

PROBABILITY RULES:
- All three must sum to 1.0
- Base case is typically 0.40-0.55 (but can be lower if uncertainty is high)
- Bear and bull are typically 0.20-0.35 each
- If the macro environment is risky, bear probability should be higher
- If there's a clear catalyst, bull probability can be higher

USE THE DATA PROVIDED:
- Ground your narratives in the actual financials, segments, and metrics
- Reference specific products, segments, or business lines by name
- Your deltas must be REALISTIC relative to the base case assumptions shown
- Consider the macro environment and sector dynamics

HARD CONSTRAINTS:
- Do NOT fabricate business segments or products not present in the data
- Do NOT make the base case delta anything other than all zeros
- Do NOT make bear/bull just ± a fixed number across all years
- DO make each scenario's narrative internally consistent with its delta path

SIGN DISCIPLINE — ZERO TOLERANCE (the single most common failure mode):
- Bear scenario: revenue_growth_delta and margin_delta MUST be ≤ 0 at every
  position. A bear case cannot have higher growth or margins than base. If
  year 1 bear is identical to base, use 0.0, not +0.001.
- Bull scenario: revenue_growth_delta and margin_delta MUST be ≥ 0 at every
  position. A bull case cannot have lower growth or margins than base.
- Base scenario: all deltas must be exactly 0.0.
- Violating this produces Bull < Base < Bear scenario inversions that break
  the entire decision engine downstream. If you're uncertain about direction,
  pick 0.0 — never invent a sign you're not sure of."""


# ── Parse-side hardening (AUDIT-A5a / AUDIT-A5b, 2026-07) ────────────────

# AUDIT-A5b: the LLM occasionally violates the name enum ("Bearish",
# "Bear Case", Chinese labels — Y24 precedent). get_case() matches
# strictly on the canonical name, so normalize at the parse boundary.
_CASE_NAME_ALIASES = {
    "bear": "bear", "bearish": "bear", "bear case": "bear",
    "悲观": "bear", "悲观情景": "bear", "悲观情形": "bear", "熊市": "bear",
    "bull": "bull", "bullish": "bull", "bull case": "bull",
    "乐观": "bull", "乐观情景": "bull", "乐观情形": "bull", "牛市": "bull",
    "base": "base", "base case": "base", "baseline": "base",
    "neutral": "base", "基准": "base", "基准情景": "base",
    "中性": "base", "中性情景": "base",
}


def _normalize_case_name(raw_name: Any) -> str | None:
    """Map an LLM-returned case name to canonical bear/base/bull.

    Returns None when the name is missing or unrecognizable — the caller
    treats that as an incomplete case and raises.
    """
    if not isinstance(raw_name, str):
        return None
    name = raw_name.lower().strip()
    if name in _CASE_NAME_ALIASES:
        return _CASE_NAME_ALIASES[name]
    # Substring fallback for variants like "bear scenario" / "偏悲观情景"
    for alias, canonical in _CASE_NAME_ALIASES.items():
        if alias in name:
            return canonical
    return None


def _coerce_delta_path(raw: Any, n: int = 10) -> list[float]:
    """AUDIT-A5a: coerce a delta array to exactly *n* numeric floats.

    Truncated LLM responses rescued by _recovery.repair_truncated_array
    (BUG-A20) can leave short arrays (e.g. 3 elements). Downstream,
    ``zip(revenue_growth_path, delta)`` silently shortens the growth path
    and dcf_engine's ``assert len(path) == n`` kills the whole run.
    Pad with 0.0 / truncate to *n*; drop non-numeric and non-finite
    elements (numeric strings are coerced).
    """
    from aegis.core._coerce import coerce_list
    out: list[float] = []
    for v in coerce_list(raw):
        if isinstance(v, bool):
            continue
        if isinstance(v, str):
            try:
                v = float(v)
            except ValueError:
                continue
        if isinstance(v, (int, float)) and math.isfinite(v):
            out.append(float(v))
    return (out + [0.0] * n)[:n]


class ScenarioArchitect:
    """LLM-driven narrative scenario construction."""

    def __init__(self, llm_client: Any = None) -> None:
        self._llm = llm_client

    def architect(
        self,
        entity_id: str,
        entity_name: str,
        base_dcf_assumptions: dict[str, Any],
        meta_facts: dict[str, Any],
        computed_metrics: dict[str, float],
        market_data: dict[str, float] | None = None,
        sector_pack: dict[str, Any] | None = None,
        consensus_data: dict[str, Any] | None = None,
        segment_detail: dict[str, Any] | None = None,
        macro_context: dict[str, Any] | None = None,
    ) -> ScenarioBlueprint:
        """Generate narrative-driven three-scenario framework."""
        user_message = self._build_message(
            entity_id, entity_name, base_dcf_assumptions,
            meta_facts, computed_metrics, market_data,
            sector_pack, consensus_data, segment_detail,
            macro_context,
        )

        _sys_prompt = AEGIS_PROJECT_PREAMBLE + SCENARIO_ARCHITECT_SYSTEM_PROMPT
        if isinstance(macro_context, dict) and macro_context.get("language") == "zh-CN":
            _sys_prompt += (
                "\n\nLANGUAGE: This is an A-share (China) entity. Write ALL narrative "
                "fields (bear_narrative, base_narrative, bull_narrative, primary_swing_factor, "
                "and any textual output) in Simplified Chinese (简体中文). "
                "CRITICAL: All monetary amounts MUST use ¥ and '亿' units (NOT $ or B). "
                "Example: ¥62 亿债务 (NOT $6.2B debt). ¥3 亿自由现金流 (NOT $0.3B FCF). "
                "Keep JSON keys and numeric values unchanged."
            )

        raw = self._llm.call_structured(
            system_prompt=_sys_prompt,
            user_message=user_message,
            tool_schema=SCENARIO_TOOL_SCHEMA,
            tool_name="scenario_architecture",
            role="chief_analyst",
        )

        return self._parse(raw)

    def _parse(self, raw: dict[str, Any]) -> ScenarioBlueprint:
        """Parse LLM output into ScenarioBlueprint.

        AUDIT-A5a/A5b (2026-07): validate structure at the boundary.
        Delta arrays are padded/truncated to exactly 10 numeric elements
        (short arrays from truncation-repair used to zip-shorten the DCF
        growth path and crash dcf_engine's length assert). Case names are
        normalized ("Bearish " / "悲观" → "bear") so get_case() matches.
        Incomplete structure (≠ bear/base/bull, missing name, non-numeric
        probability) raises ValueError so the orchestrator's mechanical
        fallback (auto_research.py) takes over instead of receiving a
        half-broken blueprint.
        """
        # BUG-Y26 (2026-05-06): coerce list-typed fields at the boundary so
        # a JSON-encoded-string `scenarios` field doesn't get char-iterated
        # into bogus single-letter Scenario objects.
        from aegis.core._coerce import coerce_dict, coerce_list
        cases = []
        for s in coerce_list(raw.get("scenarios", [])):
            if not isinstance(s, dict):
                continue  # JSON-string element that survived coercion as-is
            # AUDIT-A5b: missing/unrecognizable name → incomplete case
            raw_name = s.get("name")
            name = _normalize_case_name(raw_name)
            if name is None:
                raise ValueError(
                    f"scenario case has missing/unrecognized name: {raw_name!r}"
                )
            # AUDIT-A5b: probability must be numeric (numeric strings coerced)
            prob = s.get("probability")
            if isinstance(prob, str):
                try:
                    prob = float(prob)
                except ValueError:
                    pass
            if (isinstance(prob, bool) or not isinstance(prob, (int, float))
                    or not math.isfinite(prob)):
                raise ValueError(
                    f"scenario '{name}' probability is not numeric: {prob!r}"
                )

            # AUDIT-A5a: pad/truncate every delta array to exactly 10
            growth_delta = _coerce_delta_path(s.get("revenue_growth_delta"))
            margin_delta = _coerce_delta_path(s.get("margin_delta"))
            # Ensure base deltas are zeros
            if name == "base":
                growth_delta = [0.0] * 10
                margin_delta = [0.0] * 10

            # Extract optional driver-specific deltas.
            # BUG-Y25 dict 版：LLM 偶发把 driver_deltas 序列化成 JSON 字符串，
            # 原 isinstance 守卫只能静默丢弃；coerce_dict 可整体救回。
            raw_driver_deltas = coerce_dict(s.get("driver_deltas", {}))
            driver_deltas: dict[str, list[float]] = {}
            for drv_name, drv_delta in raw_driver_deltas.items():
                driver_deltas[drv_name] = _coerce_delta_path(drv_delta)

            cases.append(ScenarioCase(
                name=name,
                probability=max(float(prob), 0.0),
                narrative=s.get("narrative", ""),
                key_driver=s.get("key_driver", ""),
                revenue_growth_delta=growth_delta,
                margin_delta=margin_delta,
                driver_deltas=driver_deltas,
            ))

        # AUDIT-A5b: exactly one of each canonical case, or bail to the
        # orchestrator's mechanical fallback — a missing case would leave
        # its default probability in place downstream and silently skew
        # the probability-weighted target price.
        if sorted(c.name for c in cases) != ["base", "bear", "bull"]:
            raise ValueError(
                "scenario cases incomplete: expected exactly bear/base/bull, "
                f"got {[c.name for c in cases]}"
            )

        # Normalize probabilities to sum to 1.0
        total_prob = sum(c.probability for c in cases)
        if total_prob <= 0:
            raise ValueError("scenario probabilities sum to zero")
        if abs(total_prob - 1.0) > 0.01:
            for c in cases:
                c.probability = c.probability / total_prob

        return ScenarioBlueprint(
            scenarios=cases,
            key_disagreements=coerce_list(raw.get("key_disagreements", [])),
            primary_swing_factor=raw.get("primary_swing_factor", ""),
        )

    def _build_message(
        self,
        entity_id: str,
        entity_name: str,
        base_dcf_assumptions: dict[str, Any],
        meta_facts: dict[str, Any],
        computed_metrics: dict[str, float],
        market_data: dict[str, float] | None,
        sector_pack: dict[str, Any] | None,
        consensus_data: dict[str, Any] | None,
        segment_detail: dict[str, Any] | None,
        macro_context: dict[str, Any] | None,
    ) -> str:
        parts = [
            f"=== ENTITY: {entity_name} ({entity_id}) ===",
            "",
        ]

        # Base case DCF assumptions (the starting point)
        parts.append("=== BASE CASE DCF ASSUMPTIONS (your deltas are relative to these) ===")
        growth_path = base_dcf_assumptions.get("revenue_growth_path", [])
        margin_path = base_dcf_assumptions.get("operating_margin_path", [])
        if growth_path:
            parts.append("  Revenue Growth Path (Y1-Y10):")
            for i, g in enumerate(growth_path[:10]):
                parts.append(f"    Y{i+1}: {g:.1%}")
        if margin_path:
            parts.append("  Operating Margin Path (Y1-Y10):")
            for i, m in enumerate(margin_path[:10]):
                parts.append(f"    Y{i+1}: {m:.1%}")
        wacc = base_dcf_assumptions.get("wacc")
        tgr = base_dcf_assumptions.get("terminal_growth_rate")
        if wacc:
            parts.append(f"  WACC: {wacc:.1%}")
        if tgr:
            parts.append(f"  Terminal Growth Rate: {tgr:.1%}")
        parts.append("")

        # BUG-A22: A-share entities had USD-formatted KEY FINANCIALS / MARKET
        # DATA / SEGMENTS, leading downstream LLM output to echo `$XB` for
        # ¥-denominated companies. Use the entity's display block.
        disp = resolve_display(meta_facts)
        _per_share = "/股" if disp["currency"] == "CNY" else ""

        # Key financials
        if meta_facts:
            parts.append("=== KEY FINANCIALS ===")
            key_fins = [
                ("Revenue", "revenue"), ("Net Income", "net_income"),
                ("EBITDA", "ebitda"), ("Operating Income", "operating_income"),
                ("Free Cash Flow", "free_cash_flow"),
                ("Total Debt", "total_debt"), ("Cash", "cash_and_equivalents"),
                ("SBC", "sbc"), ("R&D", "research_and_development"),
            ]
            for label, key in key_fins:
                val = meta_facts.get(key)
                if val is not None and isinstance(val, (int, float)):
                    if abs(val) < 1:
                        parts.append(f"  {label}: {val:.1%}")
                    else:
                        parts.append(f"  {label}: {fmt_money_big(val, disp)}")
            parts.append("")

        # Computed metrics
        if computed_metrics:
            parts.append("=== KEY METRICS ===")
            important = [
                "gross_margin", "operating_margin", "net_margin", "fcf_margin",
                "revenue_growth", "roic", "roe", "debt_to_equity",
                "pe_ratio", "ev_to_ebitda", "fcf_yield",
            ]
            for k in important:
                v = computed_metrics.get(k)
                if v is not None and isinstance(v, float):
                    if abs(v) < 10:
                        parts.append(f"  {k}: {v:.2%}" if abs(v) < 1 else f"  {k}: {v:.2f}")
                    else:
                        parts.append(f"  {k}: {v:,.0f}")
            parts.append("")

        # Market data
        if market_data:
            parts.append("=== MARKET DATA ===")
            # BUG-Y17 (2026-05-06): same broken magnitude dispatch as
            # research_director._build_message had — see notes there. Switch
            # to per-key heuristics so per-share fields stay tiny, aggregates
            # auto-scale, and share counts get a proper count suffix.
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
                    parts.append(f"  {k}: {fmt_money_big(v, disp)}")
            parts.append("")

        # Segment breakdown
        if segment_detail:
            parts.append("=== SEGMENT BREAKDOWN ===")
            for category, segments in segment_detail.items():
                if not segments:
                    continue
                parts.append(f"  [{category.replace('_', ' ').title()}]")
                for seg_id, data in sorted(
                    segments.items(),
                    key=lambda x: x[1].get("revenue", 0),
                    reverse=True,
                ):
                    rev = data.get("revenue", 0)
                    if rev > 0:
                        margin = data.get("operating_margin")
                        margin_str = f" (margin: {margin:.0%})" if margin else ""
                        parts.append(f"    {seg_id.replace('_', ' ').title()}: {fmt_money_big(rev, disp)}{margin_str}")
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

        # Consensus estimates
        if consensus_data:
            parts.append("=== ANALYST CONSENSUS ===")
            rev_est = consensus_data.get("revenue_estimates", {})
            eps_est = consensus_data.get("eps_estimates", {})
            if rev_est:
                for period, data in rev_est.items():
                    if isinstance(data, dict):
                        mean = data.get("mean", data.get("avg"))
                        if mean:
                            parts.append(f"  Revenue {period}: {fmt_money_big(mean, disp)} (consensus)")
            if eps_est:
                for period, data in eps_est.items():
                    if isinstance(data, dict):
                        mean = data.get("mean", data.get("avg"))
                        if mean:
                            parts.append(f"  EPS {period}: {fmt_money_small(mean, disp)}{_per_share} (consensus)")
            parts.append("")

        # Macro context
        if macro_context:
            parts.append("=== MACRO CONTEXT ===")
            cycle = macro_context.get("cycle_phase", "")
            if cycle:
                parts.append(f"  Cycle phase: {cycle}")
            for k in ["fed_rate", "us_10y", "cpi_yoy", "pmi"]:
                v = macro_context.get(k)
                if v is not None:
                    parts.append(f"  {k}: {v}")
            parts.append("")

        # Sector context
        if sector_pack:
            parts.append(f"=== SECTOR: {sector_pack.get('sector_name', '')} ===")
            val_fw = sector_pack.get("valuation_framework", {})
            margins = val_fw.get("typical_operating_margin_range", [])
            growth = val_fw.get("typical_revenue_growth_range", [])
            if margins:
                parts.append(f"  Typical OM range: {margins[0]:.0%}-{margins[1]:.0%}")
            if growth:
                parts.append(f"  Typical growth range: {growth[0]:.0%}-{growth[1]:.0%}")
            comp = sector_pack.get("competitive_dynamics", {})
            risks = comp.get("disruption_risks", [])
            if risks:
                parts.append(f"  Disruption risks: {', '.join(risks[:3])}")

            # Revenue driver decomposition (for driver-based scenarios)
            rev_drivers = sector_pack.get("revenue_drivers", {})
            decomp = rev_drivers.get("decomposition", {})
            if decomp:
                formula = decomp.get("formula", "")
                if formula:
                    parts.append("")
                    parts.append("=== REVENUE DRIVERS (use driver_deltas in your scenarios) ===")
                    parts.append(f"  Formula: {formula}")
                    for node in decomp.get("tree", []):
                        name = node.get("name", "")
                        unit = node.get("unit", "")
                        base = node.get("base_value")
                        growth = node.get("growth_driver", "")
                        base_str = f", base={base}" if base else ""
                        parts.append(f"  - {name} ({unit}{base_str}): {growth}")
                    parts.append("  When constructing scenarios, provide driver_deltas for each driver above.")
                    parts.append("  Example: \"driver_deltas\": {\"CPM\": [-0.02, -0.01, 0, 0, 0, 0, 0, 0, 0, 0]}")

            parts.append("")

        return "\n".join(parts)
