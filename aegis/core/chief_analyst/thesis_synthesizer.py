"""Thesis Synthesizer — Chief Analyst post-agent layer.

Runs AFTER all 7 specialist agents to:
1. Read all agent judgments holistically
2. Synthesize a coherent, opinionated thesis
3. Resolve or explicitly expose conflicts between agents
4. Generate the fields that DecisionEngine used to keyword-match

This REPLACES the keyword-matching approach in DecisionEngine._extract_summaries()
with genuine LLM synthesis — like a senior analyst reading 7 specialist memos
and writing the investment conclusion.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from aegis.core.chief_analyst.preamble import (
    AEGIS_PROJECT_PREAMBLE, resolve_display, fmt_money_small,
)


# Fields that may contain narrative dollar-value claims and must be checked
# against the model's DCF scenarios for consistency.
_VALUE_CLAIM_FIELDS = (
    "core_thesis",
    "my_variant",
    "variant_magnitude",
    "variant_decomposition_narrative",
    "why_market_is_wrong",
    "market_implied_story",
)

# Match dollar amounts in narrative text. We capture broadly here and then
# filter out aggregate-magnitude values (B/M/K/T/%/billion/million/...) in
# code, because regex-only filtering produces false positives like "$750 bull"
# being treated as if it had a B suffix.
#
# The (?!\d|\.\d) lookahead is essential — it stops digit runs from being
# split mid-number (e.g. matching "$30" out of "$30000").
_DOLLAR_RE = re.compile(
    r"\$([0-9][0-9,]*(?:\.[0-9]+)?)(?!\d|\.\d)"
    r"(?:\s*[-–—]\s*\$?([0-9][0-9,]*(?:\.[0-9]+)?)(?!\d|\.\d))?"
)

# Suffix patterns that indicate the matched dollar value is an AGGREGATE
# (revenue, income, market cap, share count) rather than a per-share fair
# value. We check these as a positive match against the text immediately
# following the dollar value.
_UNIT_SUFFIX_RE = re.compile(
    r"^\s*("
    r"[BMKTbmkt]\b"          # B, M, K, T as standalone unit (word boundary)
    r"|bn\b|mm\b|bps?\b"     # bn, mm, bp, bps
    r"|billion|million|thousand|trillion"
    r"|basis\s+points?"
    r"|%"
    r")"
)

# BUG-A25 (2026-05-06): A-share equivalent of _DOLLAR_RE. Synthesizer
# previously skipped scrub entirely for ¥-denominated reports because
# `if "$" not in text: continue`. So `公允价值¥1.50/股` claims that
# disagree with DCF scenarios (e.g. bear ¥-0.66 / base ¥-0.32 / bull
# ¥0.31) sailed through. Now we also scan ¥ patterns and skip aggregates
# tagged with 亿 / 万 / 百万 / % / 倍.
_YUAN_RE = re.compile(
    r"¥([0-9][0-9,]*(?:\.[0-9]+)?)(?!\d|\.\d)"
    r"(?:\s*[-–—]\s*¥?([0-9][0-9,]*(?:\.[0-9]+)?)(?!\d|\.\d))?"
)
_CN_UNIT_SUFFIX_RE = re.compile(
    r"^\s*("
    r"亿|万|千万|百万|千|百"   # 中文 magnitude tags
    r"|元/?亿|元/?万"          # rare e.g. "¥X 元/亿"
    r"|%|倍"                   # ratio / multiple suffixes
    r")"
)


# BUG-Y26 (2026-05-06): `_coerce_list` was promoted to `aegis.core._coerce`
# so chief_analyst components, agents, and any future LLM-touching parser
# can share a single hardened implementation. Kept the name here as a thin
# re-export for back-compat — existing imports keep working.
from aegis.core._coerce import coerce_list as _coerce_list  # noqa: F401


# BUG-A15 (2026-05-04): % return claims in headlines and ledes — patterns
# like "下行 81-89%", "下行约 50%", "implies -45% downside", "+30% upside".
# Editor frequently invents alternate-framework returns (cash-floor anchor,
# restructure ceiling) that don't match DCF-vs-price. We scrub those that
# diverge >10pt from any sanctioned scenario return.
_PCT_RANGE_RE = re.compile(
    r"(?:[-−–]?\s*([0-9]{1,3}(?:\.[0-9]+)?)\s*[-~–至]\s*([0-9]{1,3}(?:\.[0-9]+)?)\s*%)"
    r"|(?:([+\-−]?\s*[0-9]{1,3}(?:\.[0-9]+)?)\s*%)"
)
# BUG-Y48 (2026-05-06): keyword sets were sparse on Chinese — only 3 ZH
# downside cues, 1 ZH upside cue. CN narratives use a much wider vocabulary
# for valuation gap framing (重估 / 估值修复 / 估值回归 / 价值发现 ...).
# Without these the scrub couldn't reliably classify a quoted percentage as
# directional → false negatives on real return claims AND false positives on
# margin/growth percentages that lacked clear English direction cues.
_DOWNSIDE_KEYWORDS = (
    "下行", "下行空间", "下行风险", "下跌", "下挫",
    "回调", "回归", "回落", "调整",
    "估值压缩", "估值回落", "估值下修",
    "downside", "downside risk", "implied downside", "valuation gap",
)
_UPSIDE_KEYWORDS = (
    "上行", "上行空间", "上升空间", "重估空间",
    "估值修复", "估值回归", "估值重估", "价值发现",
    "反弹", "修复",
    "upside", "implied upside", "potential upside", "valuation re-rating",
)
# BUG-Y21 (2026-05-06): "上涨" is too ambiguous — fires on both "股价上涨"
# (return) AND "营收上涨" / "毛利率上涨" (growth/margin). Removed from
# upside keywords; instead we explicitly look for downside/upside framing
# pairs ("DCF下行/隐含上行/价格上涨X% vs DCF") elsewhere.

# BUG-Y21: growth-context indicators that should EXCLUDE a percentage from
# the return-claim scrubber. "营收同比+453%" is a YoY growth claim, NOT a
# DCF-vs-price return assertion. Skip when these keywords sit near the %.
_GROWTH_CONTEXT_KEYWORDS = (
    "增长", "增速", "同比", "环比", "yoy", "qoq", "growth",
    "营收", "revenue", "净利润", "净利", "earnings",
    # AUDIT (2026-07): generic "利润率" + "ebitda" added — "调整后EBITDA
    # 利润率18%" is a margin metric, not a return claim, but neither
    # 毛利率 nor 净利率 substring-matched it.
    "毛利率", "净利率", "利润率", "ebitda", "净息差", "yield", "margin",
    "市占率", "market share",
    "cagr",
)

# AUDIT follow-up (2026-07-10, 康达新材 LLM run): context words that mark a
# bare currency figure as a fair-value / price-target claim. A ¥/$ figure
# with none of these nearby is illustrative prose (unit economics, cash-burn
# ratios like "每确认¥1账面利润，实际要烧掉近¥10现金") and must NOT be
# scrubbed — the old behaviour rewrote it into mid-sentence garbage.
_FAIR_VALUE_CONTEXT = (
    "目标价", "公允", "合理价", "内在价值", "每股价值", "每股", "估值",
    "定价", "价值区间", "对应股价", "股价应", "看至", "上看", "应达",
    "fair value", "price target", "target price", "intrinsic",
    "worth", "per share", "per-share", "valuation", "should trade",
)


# AUDIT (2026-07, thesis_synthesizer:106/:112): Y48 introduced substring
# collisions across the two direction sets — "回归"(down) ⊂ "估值回归"(up),
# and "调整"(down) fires on the adjusted-metric prefix "调整后". The old
# `any(k in ctx)` checks let downside win whenever both sets hit, so an
# upside re-rating claim ("盈利驱动估值回归，隐含35%空间") got signed
# NEGATIVE and tripped a false "% RETURN CONSISTENCY OVERRIDE" warning.
# Fix: longest-match-first — scan both sets together ordered by keyword
# length; the longest hit wins. Equal-length hits in BOTH directions are
# undecidable → direction 0, and the caller skips that % (宁漏勿假).
_DIRECTION_KEYWORDS: list[tuple[str, int]] = sorted(
    [(_k, -1) for _k in _DOWNSIDE_KEYWORDS]
    + [(_k, +1) for _k in _UPSIDE_KEYWORDS],
    key=lambda kv: len(kv[0]),
    reverse=True,
)


def _kw_hits(window: str, kw: str) -> bool:
    if kw == "调整":
        # "调整后EBITDA" / "调整后净利润" are adjusted-metric prefixes,
        # not downside cues — only count "调整" NOT followed by "后".
        return bool(re.search(r"调整(?!后)", window))
    return kw in window


def _direction_of(window: str) -> int:
    """Longest-match direction of a context window.

    Returns -1 (downside), +1 (upside), or 0 (no cue / undecidable tie).
    """
    best_len = 0
    best_dir = 0
    tie = False
    for kw, d in _DIRECTION_KEYWORDS:
        if len(kw) < best_len:
            break  # list is length-sorted desc — nothing longer remains
        if not _kw_hits(window, kw):
            continue
        if len(kw) > best_len:
            best_len, best_dir, tie = len(kw), d, False
        elif d != best_dir:
            tie = True  # equal-length hit in the opposite direction
    return 0 if tie else best_dir


# ═══ Aegis 2.0 Phase 0 — 预期前沿 prompt 渲染 + scrubber 白名单 ═══════
#
# DESIGN_2.0 §三.A / 设计红线 2：反解输出必须条件化（「若利润率 X 则需
# 增速 Y」），禁止单点「市场隐含增速 Z%」。设计红线 9：前沿数字面世必须
# 同步注册 scrubber/critic 白名单——frontier_sanctioned_growth_pcts 就是
# 该白名单的生成器，synthesize()/ReportEditor.edit() 把它接进
# _scrub_fair_value_claims 的 % 一致性检查。

_FRONTIER_CCY_SYMBOLS = {"CNY": "¥", "USD": "$", "HKD": "HK$"}


def _fmt_growth(g: float) -> str:
    return f"{g:+.1%}"


def frontier_prompt_lines(frontier: dict[str, Any] | None, lang: str = "zh") -> list[str]:
    """把 ExpectationsFrontier.to_dict() 渲染成条件化句式行（zh/en 双语）。

    每档利润率情景一行：「若利润率维持 X%，现价需要 Y% 增速支撑
    （WACC±1%: lo ~ hi）」；无解档引用引擎的结构化诊断文本
    （diagnostic_zh / diagnostic_en，由渲染语言选择——中文化铁律）。
    """
    if not isinstance(frontier, dict) or not frontier.get("scenarios"):
        return []
    zh = lang == "zh"
    sym = _FRONTIER_CCY_SYMBOLS.get(str(frontier.get("currency", "")), "")
    try:
        price = float(frontier.get("market_price") or 0.0)
    except (TypeError, ValueError):
        price = 0.0

    lines: list[str] = []
    for scen in frontier["scenarios"]:
        if not isinstance(scen, dict):
            continue
        label = str(scen.get("label", ""))
        try:
            margin = float(scen.get("target_margin") or 0.0)
        except (TypeError, ValueError):
            margin = 0.0
        cols = [c for c in (scen.get("wacc_columns") or []) if isinstance(c, dict)]
        def _delta_of(col: dict) -> float:
            v = col.get("wacc_delta")
            try:
                return float(v) if v is not None else 1.0
            except (TypeError, ValueError):
                return 1.0

        base_col = next((c for c in cols if abs(_delta_of(c)) < 1e-9), None)
        all_growths = [
            float(s["implied_growth"])
            for c in cols for s in (c.get("solutions") or [])
            if isinstance(s, dict) and s.get("implied_growth") is not None
        ]
        extreme = any(
            s.get("extreme_expectation")
            for c in cols for s in (c.get("solutions") or [])
            if isinstance(s, dict)
        )
        base_growths = [
            float(s["implied_growth"])
            for s in ((base_col or {}).get("solutions") or [])
            if isinstance(s, dict) and s.get("implied_growth") is not None
        ]
        diag_key = "diagnostic_zh" if zh else "diagnostic_en"
        diag = str((base_col or (cols[0] if cols else {})).get(diag_key) or "")

        if base_growths:
            joiner = "、" if zh else ", "
            base_txt = joiner.join(_fmt_growth(g) for g in base_growths)
            lo, hi = min(all_growths), max(all_growths)
            if zh:
                line = (
                    f"若终年营业利润率为 {margin:.1%}（{label}），"
                    f"现价 {sym}{price:.2f} 需要约 {base_txt} 的年营收增速支撑"
                    f"（WACC±1% 区间: {_fmt_growth(lo)} ~ {_fmt_growth(hi)}）"
                )
                if len(base_growths) > 1:
                    line += "；价格-增速曲线非单调，存在多个隐含增速解，须结合验证点解读"
                if extreme:
                    line += "〔极端预期：隐含终年累计营收 scale 超 30 倍〕"
            else:
                line = (
                    f"At a terminal operating margin of {margin:.1%} ({label}), "
                    f"the current price {sym}{price:.2f} requires roughly "
                    f"{base_txt} annual revenue growth "
                    f"(WACC±1% range: {_fmt_growth(lo)} ~ {_fmt_growth(hi)})"
                )
                if len(base_growths) > 1:
                    line += (
                        "; the price-vs-growth curve is non-monotonic "
                        "(multiple implied-growth solutions)"
                    )
                if extreme:
                    line += " [extreme expectation: >30x cumulative revenue scale]"
        elif all_growths:
            # 基准 WACC 档无解，但 ±1% 档有解——如实给区间 + 基准档诊断。
            lo, hi = min(all_growths), max(all_growths)
            if zh:
                line = (
                    f"若终年营业利润率为 {margin:.1%}（{label}）：基准 WACC 档无解"
                    f"（{diag or '见诊断'}），WACC±1% 档隐含增速 "
                    f"{_fmt_growth(lo)} ~ {_fmt_growth(hi)}"
                )
            else:
                line = (
                    f"At a terminal operating margin of {margin:.1%} ({label}): "
                    f"no solution at base WACC ({diag or 'see diagnostic'}); "
                    f"WACC±1% columns imply {_fmt_growth(lo)} ~ {_fmt_growth(hi)}"
                )
        else:
            if zh:
                line = (
                    f"若终年营业利润率为 {margin:.1%}（{label}）："
                    f"{diag or '网格内无解'}"
                )
            else:
                line = (
                    f"At a terminal operating margin of {margin:.1%} ({label}): "
                    f"{diag or 'no solution within the grid'}"
                )
        lines.append(line)
    return lines


def frontier_sanctioned_growth_pcts(frontier: dict[str, Any] | None) -> list[float]:
    """设计红线 9：前沿表全部隐含增速百分数（含 WACC±1% 列的解）+ margin
    档百分数 → % 一致性 scrubber 的 sanctioned 白名单（百分数幅值）。"""
    if not isinstance(frontier, dict):
        return []
    out: set[float] = set()
    for scen in frontier.get("scenarios") or []:
        if not isinstance(scen, dict):
            continue
        try:
            out.add(round(abs(float(scen.get("target_margin"))) * 100.0, 1))
        except (TypeError, ValueError):
            pass
        for col in scen.get("wacc_columns") or []:
            if not isinstance(col, dict):
                continue
            for s in col.get("solutions") or []:
                if not isinstance(s, dict):
                    continue
                try:
                    out.add(round(abs(float(s.get("implied_growth"))) * 100.0, 1))
                except (TypeError, ValueError):
                    pass
    return sorted(out)


def _scrub_fair_value_claims(
    raw: dict[str, Any],
    scenarios: dict[str, float],
    market_data: dict[str, float] | None = None,
    fields: tuple[str, ...] | None = None,
    extra_sanctioned_pcts: Sequence[float] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Detect and rewrite per-share fair-value claims that contradict scenarios.

    A claim is considered inconsistent if a cited dollar value (≥10) does
    NOT match any of bear/base/bull within ±15% AND is not the current market
    price. This catches "$520-580" when scenarios are bear $120 / base $750 /
    bull $1126 — neither endpoint matches a sanctioned scenario value.

    This is a backstop for the prompt-level constraint in
    THESIS_SYNTHESIZER_SYSTEM_PROMPT. The LLM is told not to invent fair-value
    numbers; this catches the cases where it does anyway and rewrites the
    offending text in-place.

    BUG-A15 (2026-05-04): also scrub percentage return claims (e.g. "下行
    81-89%") that don't match any DCF-vs-price gap. Editors sometimes
    quote alternate-framework returns next to DCF numbers, mixing
    incompatible anchors and misleading readers.
    """
    bear = scenarios.get("bear_value")
    base = scenarios.get("base_value")
    bull = scenarios.get("bull_value")
    if not all(isinstance(x, (int, float)) for x in (bear, base, bull)):
        return raw, []
    if not (bear and base and bull):
        return raw, []

    # BUG-Y32 (2026-05-06): include probability-weighted value in the
    # sanctioned set. Editor frequently quotes prob-weighted return (e.g.
    # 茅台 v6: target ¥4014.77 vs price ¥1375 → +192%) which is a legitimate
    # DCF-derived return but used to be missing from the sanctioned set,
    # causing false-positive RETURN CONSISTENCY OVERRIDE warnings.
    sanctioned = [float(bear), float(base), float(bull)]
    _pw = scenarios.get("probability_weighted_value")
    if isinstance(_pw, (int, float)) and _pw > 0:
        sanctioned.append(float(_pw))
    market_price = None
    if market_data:
        mp = market_data.get("current_price") or market_data.get("price")
        if isinstance(mp, (int, float)) and mp > 0:
            market_price = float(mp)

    def _matches_scenario(v: float) -> bool:
        # Within ±15% of any sanctioned scenario value
        for s in sanctioned:
            if s > 0 and abs(v - s) / s <= 0.15:
                return True
        # Within ±5% of current market price (narratives often quote the
        # market price for reference, that's not a fair-value invention).
        if market_price and abs(v - market_price) / market_price <= 0.05:
            return True
        return False

    warnings: list[str] = []
    out = dict(raw)
    field_list = fields if fields is not None else _VALUE_CLAIM_FIELDS

    # BUG-A25: branch by currency. A-share scenarios are CNY (small per-share
    # values, magnitudes typically <¥10/股), US scenarios are USD (per-share
    # often $50-300). The thresholds and aggregate-suffix patterns differ.
    is_cny = bool(scenarios.get("currency") == "CNY")
    money_re = _YUAN_RE if is_cny else _DOLLAR_RE
    unit_re = _CN_UNIT_SUFFIX_RE if is_cny else _UNIT_SUFFIX_RE
    sigil = "¥" if is_cny else "$"
    # CNY per-share values can be very small (Run #3 600568: bear -0.66 /
    # base -0.32 / bull 0.31). USD per-share rarely meaningful below $10.
    min_value_threshold = 0.5 if is_cny else 10.0

    for field_name in field_list:
        text = out.get(field_name) or ""
        if not isinstance(text, str) or sigil not in text:
            continue
        bad: list[tuple[str, float]] = []
        for m in money_re.finditer(text):
            try:
                v1 = float(m.group(1).replace(",", ""))
            except ValueError:
                continue
            v2 = None
            if m.group(2):
                try:
                    v2 = float(m.group(2).replace(",", ""))
                except ValueError:
                    v2 = None
            # Skip very small numbers — typically EPS, dividend per share,
            # or per-unit prices, not fair-value claims. Threshold differs
            # by currency (BUG-A25): CNY per-share scenarios can be ¥0.30,
            # USD per-share rarely meaningful below $10.
            if v1 < min_value_threshold:
                continue
            # Skip aggregate-magnitude values: anything followed directly by
            # a unit suffix (B / M / K / T / 亿 / 万 / %, billion, ...).
            # This handles both single values ($215.9B / ¥53亿) and ranges.
            tail = text[m.end():m.end() + 16]
            if unit_re.match(tail):
                continue
            # For the optional second number in a range, also check what
            # follows IT for a unit suffix — e.g. "$10-15B" / "¥10-53亿"
            # where m.end() is right after the second number.
            if v2 is not None:
                # For ranges, the regex's m.end() is after v2, so the tail
                # check above already handles aggregate suffixes correctly.
                pass
            # Detect dash-form aggregate ranges where only the leading number
            # was captured: "$10-15B" / "¥10-53亿" (no sigil before 15/53).
            if v2 is None:
                lookahead = text[m.end():m.end() + 16]
                _agg_dash = (
                    r"\s*[-–—]\s*[0-9][0-9,]*(?:\.[0-9]+)?\s*(亿|万|千万|百万)"
                    if is_cny
                    else r"\s*[-–—]\s*[0-9][0-9,]*(?:\.[0-9]+)?\s*"
                         r"([BMKTbmkt]\b|billion|million|thousand|trillion)"
                )
                if re.match(_agg_dash, lookahead):
                    continue
            # AUDIT follow-up (2026-07-10, 康达新材 run): bare currency
            # figures in ratio/illustration prose are not fair-value claims
            # ("每确认¥1账面利润，实际要烧掉近¥10现金" was rewritten into
            # mid-sentence garbage). Only scrutinise a figure when nearby
            # context talks about value / price targets — otherwise skip
            # (宁漏勿假, same principle as the Y48 direction-word fix).
            _win = text[max(0, m.start() - 40):m.end() + 40].lower()
            if not any(k in _win for k in _FAIR_VALUE_CONTEXT):
                continue
            # A claim is bad if neither endpoint matches a sanctioned scenario.
            if v2 is not None:
                if not _matches_scenario(v1) and not _matches_scenario(v2):
                    bad.append((m.group(0), (v1 + v2) / 2))
            else:
                if not _matches_scenario(v1):
                    bad.append((m.group(0), v1))
        if not bad:
            continue
        # Rewrite the offending dollar tokens with a [see scenarios] tag.
        # 中文化铁律: the tag itself must be Chinese in CN reports — the
        # English tag was leaking into A-share ledes mid-sentence.
        _tag = "〔详见DCF情景估值〕" if is_cny else "[see DCF scenarios]"
        new_text = text
        for token, _ in bad:
            new_text = new_text.replace(token, _tag, 1)
        out[field_name] = new_text
        bad_str = ", ".join(t for t, _ in bad[:3])
        warnings.append(
            f"VALUATION CONSISTENCY OVERRIDE — synthesizer cited fair-value "
            f"figure(s) {bad_str} in '{field_name}' that do not match any sanctioned "
            f"scenario (bear {sigil}{bear:.2f} / base {sigil}{base:.2f} / bull {sigil}{bull:.2f}). "
            f"The narrative was rewritten to point readers back to the DCF scenarios. "
            f"This indicates the LLM disagreed with the model's valuation — review "
            f"the variant_analyst output and DCF inputs."
        )

    # BUG-A15: percentage-claim scrubbing. Compute the sanctioned set of
    # implied returns from DCF-vs-price (bear / base / bull) and flag any
    # cited percentage in downside/upside context that diverges >10pt from
    # all sanctioned values. This catches Editor headlines like "下行
    # 81-89% vs 上行仅13%" when the actual DCF-implied range is -97% to
    # -99% — the % numbers come from a different framework (cash floor /
    # restructure ceiling) and the conflation is what misleads readers.
    if market_price and market_price > 0:
        # 设计红线 9：预期前沿的隐含增速 / margin 档百分数是 sanctioned
        # numbers——它们会以「若利润率 X% 需 Y% 增速」句式出现在叙事里，
        # 不得被 % RETURN CONSISTENCY 检查误判为 return 主张。
        _extra_pcts = tuple(extra_sanctioned_pcts or ())

        def _is_extra_sanctioned(val: float) -> bool:
            return any(abs(abs(val) - p) <= 0.5 for p in _extra_pcts)

        sanctioned_returns = []
        for s in sanctioned:
            if s != 0:
                sanctioned_returns.append((s / market_price - 1) * 100)
        # Only enforce when DCF gives MEANINGFUL returns. For loss-making
        # companies all three scenarios are tiny near-zero values yielding
        # ~-99% returns; the alternate-framework % is actually more useful
        # and shouldn't be scrubbed. Heuristic: skip when |min_return| > 90
        # (DCF is essentially saying "company is worthless" and headlines
        # quoting alternate frameworks are doing a service).
        if sanctioned_returns and min(abs(r) for r in sanctioned_returns) < 90:
            for field_name in field_list:
                text = out.get(field_name) or ""
                if not isinstance(text, str) or "%" not in text:
                    continue
                # Only scrub when we can identify the % is in downside /
                # upside framing (vs e.g. "毛利率17.9%" which is a margin).
                _has_dir = any(k in text for k in (_DOWNSIDE_KEYWORDS + _UPSIDE_KEYWORDS))
                if not _has_dir:
                    continue
                bad_pct: list[str] = []
                for m in _PCT_RANGE_RE.finditer(text):
                    # Look at 24 chars before AND after for downside/upside
                    # context, since either order is common ("下行 81%",
                    # "implied 45% downside"). Prefer leading context.
                    ctx_before = text[max(0, m.start() - 24):m.start()]
                    ctx_after = text[m.end():m.end() + 24]
                    # BUG-Y21: skip growth-context percentages — "营收同比
                    # +453%" / "毛利率上涨 5pp" are NOT return claims and
                    # should not be matched against DCF-vs-price sanctioned
                    # returns. Use a wider window (40 chars) since growth
                    # phrases tend to lead the metric noun by a few words.
                    _ctx_growth = text[max(0, m.start() - 40):m.end() + 40].lower()
                    if any(g in _ctx_growth for g in _GROWTH_CONTEXT_KEYWORDS):
                        continue
                    # AUDIT (2026-07): longest-match direction resolution,
                    # leading context preferred (either order is common:
                    # "下行 81%", "implied 45% downside"). A tie (both
                    # directions at equal keyword length) yields 0 → the
                    # sign checks below skip this % instead of guessing.
                    _dir = _direction_of(ctx_before) or _direction_of(ctx_after)
                    is_down = _dir < 0
                    is_up = _dir > 0
                    # Range form (group 1 + group 2)
                    if m.group(1) and m.group(2):
                        try:
                            v1, v2 = float(m.group(1)), float(m.group(2))
                        except ValueError:
                            continue
                        sign = -1 if is_down else (+1 if is_up else 0)
                        if sign == 0:
                            continue
                        # 设计红线 9：两端都命中前沿白名单 → 不是 return 主张
                        if _is_extra_sanctioned(v1) and _is_extra_sanctioned(v2):
                            continue
                        cited = sorted([sign * v1, sign * v2])
                        if not any(cited[0] - 10 <= r <= cited[1] + 10 for r in sanctioned_returns):
                            bad_pct.append(m.group(0))
                    # Single-value form (group 3) — already-signed
                    elif m.group(3):
                        try:
                            tok = m.group(3).replace(" ", "").replace("−", "-")
                            v = float(tok)
                        except ValueError:
                            continue
                        # If the token itself is unsigned, infer sign from context.
                        if "-" not in tok and "+" not in tok:
                            if is_down:
                                v = -v
                            elif is_up:
                                v = +v
                            else:
                                continue
                        # Skip very small numbers — typically margin / yield
                        # / probability mentions, not return claims, unless
                        # there's directional context.
                        if abs(v) < 5 and not (is_down or is_up):
                            continue
                        # 设计红线 9：命中前沿隐含增速/margin 白名单 → 放行
                        if _is_extra_sanctioned(v):
                            continue
                        if not any(abs(v - r) <= 10 for r in sanctioned_returns):
                            bad_pct.append(m.group(0))
                if bad_pct:
                    warnings.append(
                        f"% RETURN CONSISTENCY OVERRIDE — '{field_name}' cites "
                        f"{', '.join(bad_pct[:3])} but sanctioned DCF-vs-price "
                        f"returns are {[f'{r:+.0f}%' for r in sanctioned_returns]}. "
                        f"Either the headline conflates DCF with an alternate "
                        f"valuation framework, or the % is a hallucination."
                    )
    return out, warnings


@dataclass
class SynthesizedThesis:
    """The synthesized thesis — replaces keyword-extracted summaries."""

    # Core thesis: the ONE most important thing about this investment
    core_thesis: str

    # Variant: where we disagree with the market
    my_variant: str
    variant_magnitude: str  # Quantified if possible
    variant_decomposition_narrative: str  # How the variant breaks down

    # Why now
    why_now: str

    # Market's story vs ours
    market_implied_story: str
    key_assumption_disagreement: str

    # Counter-thesis: the strongest case against us
    counter_thesis: str

    # Edge articulation
    why_market_is_wrong: str
    what_would_change_my_mind: str
    edge_source: str
    edge_durability: str  # "short_term", "medium_term", "long_term"

    # Key conflicts the synthesizer identified
    unresolved_tensions: list[str] = field(default_factory=list)

    # Management quality summary (synthesized, not keyword-matched)
    management_quality_summary: str = ""
    capital_allocation_assessment: str = ""

    # Conviction level based on evidence strength
    conviction_narrative: str = ""

    # Hypothesis validation (did the agents confirm or refute the Director's initial read?)
    hypothesis_validated: bool = True
    hypothesis_evolution: str = ""  # How the thesis changed from initial hypothesis
    biggest_surprise: str = ""  # What the agents found that was most unexpected
    agents_that_challenged: list[str] = field(default_factory=list)  # Which agents contradicted the hypothesis

    # Open research questions that agents raised but couldn't be answered from available data
    open_questions: list[dict[str, str]] = field(default_factory=list)


SYNTHESIS_TOOL_SCHEMA = {
    "type": "object",
    "required": [
        "core_thesis", "my_variant", "variant_magnitude",
        "variant_decomposition_narrative", "why_now",
        "market_implied_story", "key_assumption_disagreement",
        "counter_thesis", "why_market_is_wrong", "what_would_change_my_mind",
        "edge_source", "edge_durability",
    ],
    "properties": {
        "core_thesis": {
            "type": "string",
            "description": "The single most important statement about this investment — the kind of sentence a PM remembers. Must be concrete, specific, and grounded in this entity's actual financials (revenue, margin, valuation multiples from the data provided). CRITICAL: reference ONLY this specific entity. Do not mention Meta, Apple, or any other company by name unless they appear as actual competitors in the data.",
        },
        "my_variant": {
            "type": "string",
            "description": "Where SPECIFICALLY we disagree with consensus. Not vague ('we think growth is higher') but precise ('we believe X metric will reach Y% within Z quarters, vs market-implied W%'). Use THIS entity's actual segments and products. Do not reference other companies by name.",
        },
        "variant_magnitude": {
            "type": "string",
            "description": "Quantified variant size. e.g. '$85 per share upside if our thesis is correct, representing 15% from current levels'. Use the DCF scenarios and sensitivity data to anchor this.",
        },
        "variant_decomposition_narrative": {
            "type": "string",
            "description": "How the total variant breaks down across drivers. e.g. '60% of the variant comes from higher revenue growth (our 14% vs implied 10%), 25% from margin expansion, 15% from lower effective tax rate.'",
        },
        "why_now": {
            "type": "string",
            "description": "Why this thesis is actionable NOW, not 6 months ago or 6 months from now. What catalyst or inflection point makes timing relevant?",
        },
        "market_implied_story": {
            "type": "string",
            "description": "What the current price implies about the market's beliefs. Be specific about THIS entity's implied growth, margin, and terminal assumptions. Use the reverse DCF and sensitivity data provided. Do not reference other companies by name.",
        },
        "key_assumption_disagreement": {
            "type": "string",
            "description": "The ONE assumption where we most disagree with the market. This is the crux of the variant.",
        },
        "counter_thesis": {
            "type": "string",
            "description": "The strongest possible argument AGAINST our thesis. Written as if by our smartest opponent. Must be genuinely threatening, not a strawman.",
        },
        "why_market_is_wrong": {
            "type": "string",
            "description": "Our specific evidence for why the market's implied narrative is incorrect. Must reference actual data points from the agent analyses.",
        },
        "what_would_change_my_mind": {
            "type": "string",
            "description": "Specific, observable conditions that would invalidate this thesis — concrete quarterly metrics or events specific to THIS entity. Do not reference other companies by name.",
        },
        "edge_source": {
            "type": "string",
            "description": "Where our information/analytical edge comes from. Be honest about edge quality.",
        },
        "edge_durability": {
            "type": "string",
            "enum": ["short_term", "medium_term", "long_term"],
        },
        "unresolved_tensions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Genuine tensions between agent analyses that you CANNOT fully resolve. Intellectual honesty matters — flag what's genuinely uncertain.",
        },
        "management_quality_summary": {
            "type": "string",
            "description": "Synthesized view of management quality based on management analyst's findings. One paragraph.",
        },
        "capital_allocation_assessment": {
            "type": "string",
            "description": "Synthesized view of capital allocation quality. One paragraph.",
        },
        "conviction_narrative": {
            "type": "string",
            "description": "Your honest assessment of conviction level and why. Reference specific evidence strength from the agent analyses for THIS entity. Do not reference other companies by name.",
        },
        "hypothesis_validated": {
            "type": "boolean",
            "description": "Did the specialist agents' findings broadly SUPPORT the Research Director's initial hypothesis? true = hypothesis holds (possibly refined), false = hypothesis was wrong or needs major revision.",
        },
        "hypothesis_evolution": {
            "type": "string",
            "description": "How did the thesis evolve from the initial hypothesis? Be specific about what changed or what was strengthened, using THIS entity's segments and drivers. If hypothesis was confirmed, explain what evidence strengthened it. Do not reference other companies by name.",
        },
        "biggest_surprise": {
            "type": "string",
            "description": "What was the most unexpected finding from the agent analyses? The thing the Research Director did NOT anticipate. This is often where the most valuable insight lives.",
        },
        "agents_that_challenged": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Which agents produced findings that contradicted or significantly modified the initial hypothesis? List agent names. Empty array if all agents confirmed.",
        },
    },
}


THESIS_SYNTHESIZER_SYSTEM_PROMPT = """You are the Chief Research Analyst synthesizing the work of 7 specialist analysts into a coherent investment thesis.

YOU HAVE JUST READ:
- The Research Director's initial hypothesis and research directive
- Accounting Analyst's judgment (earnings quality, SBC, tax, red flags)
- Business Analyst's judgment (moat, segments, driver tree, competitive position)
- Management Analyst's judgment (track record, capital allocation, governance)
- Valuation Analyst's judgment (DCF sensitivity, implied assumptions, peer multiples)
- Variant Analyst's judgment (market vs our view, value gap decomposition, catalysts)
- Risk Analyst's judgment (downside tree, kill criteria, tail risks)
- Sector Context Agent's judgment (benchmarks, cycle position, sector quirks)

YOUR JOB: Synthesize ALL of this into ONE coherent thesis, AND validate whether the initial hypothesis survived contact with evidence.

HOW A TOP ANALYST SYNTHESIZES:
1. Don't just concatenate — INTEGRATE. Find the narrative thread that connects the pieces.
2. Identify where agents AGREE (that's your high-conviction core) and where they CONFLICT (that's your uncertainty).
3. The core_thesis should be the ONE sentence that captures the entire investment case.
4. The variant must be SPECIFIC and TESTABLE — not "we think it's cheap" but exactly HOW and WHY.
5. The counter_thesis must be genuinely threatening — if it doesn't make you uncomfortable, it's a strawman.

CRITICAL: HYPOTHESIS VALIDATION
The Research Director formed an initial hypothesis BEFORE any specialist analysis. Now that you've seen all the evidence:
- Did the hypothesis survive? Was it confirmed, refined, or refuted?
- Which agents challenged it? What did they find that was unexpected?
- What's the BIGGEST SURPRISE from the specialist analyses?
- How did the thesis EVOLVE from the initial read to the final synthesis?

This is not a formality. A top analyst always tracks how their view changed through the research process. If your final thesis is identical to the initial hypothesis, either the initial read was perfect (rare) or you're not integrating the evidence properly.

KEY PRINCIPLES:
- You have the Research Director's initial hypothesis. Your job is to UPGRADE it with evidence, not just repeat it.
- If the specialist agents found evidence that CONTRADICTS the initial hypothesis, say so clearly.
- Quantify everything you can using the provided data.
- Be intellectually honest about what you don't know.

WHAT MAKES A GREAT THESIS:
- Specific enough that someone could disagree with the exact claim
- Grounded in at least 3 data points from the agent analyses
- Acknowledges the strongest counter-argument honestly
- Makes clear what would falsify it
- Has a "why now" that creates urgency

HARD CONSTRAINTS:
- Do NOT invent numbers not present in any agent's analysis
- Do NOT water down the thesis to avoid being wrong — be specific
- Do NOT ignore genuine conflicts between agents — expose them
- Every claim must be traceable to at least one agent's observation or inference

CRITICAL — VALUATION ANCHORING (zero tolerance):
- The DCF scenarios provided in MARKET & VALUATION CONTEXT (bear / base / bull)
  are the ONLY sanctioned per-share fair-value numbers. They are computed by
  the model with consistent assumptions.
- When stating a "fair value", "intrinsic value", "target", or any per-share
  $-figure, you MUST use one of: the bear value, the base value, the bull value,
  or a probability-weighted average of those three. You may NOT invent a new
  range like "$520-580" that does not appear in the scenarios.
- If you genuinely disagree with the model's base, do not invent a substitute
  number. Instead: (a) name the specific assumption you reject, (b) point to
  the agent observation that contradicts it, (c) state your view as a directional
  claim ("base is too low — we expect closer to bull case"). Never as a fresh number.
- Violating this rule produces internally inconsistent reports where the narrative
  cites one fair value and the scenario table shows another. This is the single
  most damaging failure mode in our reports.

CRITICAL — EXPECTATIONS-FIRST FRAMING (Aegis 2.0 methodology):
The report's primary conclusion answers "what expectations does the current
price embed, and are those expectations compatible with verifiable facts?" —
NOT "the model says fair value is X so the market is wrong by Y%". Follow
this three-step arc in core_thesis / market_implied_story / why_market_is_wrong:
1. WHAT THE PRICE IMPLIES — quote the MARKET-IMPLIED EXPECTATIONS FRONTIER
   when provided, using its conditional form ("at margin X%, the current
   price requires ~Y% growth"). NEVER assert a single-point "market implies
   Z% growth" — one price cannot identify both growth and margin.
2. EXPECTATIONS vs VERIFIABLE FACTS — test those implied expectations
   against disclosed facts: the RECENT DISCLOSED EVENTS block (the ONLY
   sanctioned catalyst source) and the agents' financial evidence.
3. VERIFICATION / FALSIFICATION SIGNALS — state which observable events
   (预告/公告/季报 metrics) would confirm or kill the priced-in expectations.
Framing rules:
- Do NOT phrase the headline claim as a bare "XX% downside/upside". Express
  the conclusion as an expectations judgment ("the price embeds expectations
  that verifiable fundamentals do not yet support; key checkpoints below").
  The DCF scenarios and the DCF-vs-price gap stay fully quotable as
  supporting evidence — never suppress them (the gap itself is information).
- When a PRICING REGIME assessment is provided, adopt its narrative frame
  (steady / growth / turnaround / story / mixed) to interpret the gap and
  pick verification points. The regime NEVER justifies hiding the gap."""


class ThesisSynthesizer:
    """Post-agent Chief Analyst layer that synthesizes all judgments."""

    def __init__(self, llm_client: Any = None) -> None:
        self._llm = llm_client

    def synthesize(
        self,
        entity_id: str,
        entity_name: str,
        directive: Any,  # ResearchDirective
        judgments: list[Any],  # list[JudgmentContract]
        computed_metrics: dict[str, float],
        market_data: dict[str, float],
        scenarios: dict[str, float],
        implied_growth: float,
        sensitivity_rankings: list[dict],
        meta_facts: dict[str, Any] | None = None,
        narrative_supplements: dict[str, str] | None = None,  # agent_name → narrative
        open_questions: list[dict[str, Any]] | None = None,
    ) -> SynthesizedThesis:
        """Synthesize all agent judgments into a coherent thesis."""
        user_message = self._build_message(
            entity_id, entity_name, directive, judgments,
            computed_metrics, market_data, scenarios,
            implied_growth, sensitivity_rankings, meta_facts,
            narrative_supplements, open_questions,
        )

        _sys_prompt = AEGIS_PROJECT_PREAMBLE + THESIS_SYNTHESIZER_SYSTEM_PROMPT
        if isinstance(scenarios, dict) and scenarios.get("currency") == "CNY":
            _sys_prompt += (
                "\n\nLANGUAGE: This is an A-share (China) entity. Write ALL natural-language "
                "fields (core_thesis, variant_view, edge assessment text, unresolved_tensions, "
                "biggest_surprise, hypothesis_evolution, revised_thesis, and all narrative output) "
                "in Simplified Chinese (简体中文). Keep JSON keys and enum values in English. "
                "Currency in ¥ with '亿' as magnitude unit."
            )

        raw = self._llm.call_structured(
            system_prompt=_sys_prompt,
            user_message=user_message,
            tool_schema=SYNTHESIS_TOOL_SCHEMA,
            tool_name="synthesized_thesis",
            role="planner",
        )

        # ── Post-validation: scrub off-scenario fair-value claims ──
        # Even with the prompt constraint, LLMs occasionally invent a fair
        # value range that contradicts the model's scenarios (e.g. narrative
        # says "$520-580" while scenarios show base=$750, bull=$1126).
        # We rewrite the offending field in-place and append a CONSISTENCY
        # warning that propagates into the unresolved_tensions list so the
        # report shows the override.
        # 设计红线 9：前沿隐含增速/margin 百分数注册进 % 白名单，防止
        # 「若利润率 X% 需 Y% 增速」句式被误判成 return 主张。
        raw, valuation_warnings = _scrub_fair_value_claims(
            raw, scenarios, market_data,
            extra_sanctioned_pcts=frontier_sanctioned_growth_pcts(
                (meta_facts or {}).get("__expectations_frontier")
            ),
        )
        if valuation_warnings:
            existing = list(raw.get("unresolved_tensions", []) or [])
            existing.extend(valuation_warnings)
            raw["unresolved_tensions"] = existing

        return SynthesizedThesis(
            core_thesis=raw.get("core_thesis", ""),
            my_variant=raw.get("my_variant", ""),
            variant_magnitude=raw.get("variant_magnitude", ""),
            variant_decomposition_narrative=raw.get("variant_decomposition_narrative", ""),
            why_now=raw.get("why_now", ""),
            market_implied_story=raw.get("market_implied_story", ""),
            key_assumption_disagreement=raw.get("key_assumption_disagreement", ""),
            counter_thesis=raw.get("counter_thesis", ""),
            why_market_is_wrong=raw.get("why_market_is_wrong", ""),
            what_would_change_my_mind=raw.get("what_would_change_my_mind", ""),
            edge_source=raw.get("edge_source", ""),
            edge_durability=raw.get("edge_durability", "medium_term"),
            unresolved_tensions=_coerce_list(raw.get("unresolved_tensions")),
            management_quality_summary=raw.get("management_quality_summary", ""),
            capital_allocation_assessment=raw.get("capital_allocation_assessment", ""),
            conviction_narrative=raw.get("conviction_narrative", ""),
            hypothesis_validated=raw.get("hypothesis_validated", True),
            hypothesis_evolution=raw.get("hypothesis_evolution", ""),
            biggest_surprise=raw.get("biggest_surprise", ""),
            agents_that_challenged=_coerce_list(raw.get("agents_that_challenged")),
            open_questions=open_questions or [],
        )

    def _build_message(
        self,
        entity_id: str,
        entity_name: str,
        directive: Any,
        judgments: list[Any],
        computed_metrics: dict[str, float],
        market_data: dict[str, float],
        scenarios: dict[str, float],
        implied_growth: float,
        sensitivity_rankings: list[dict],
        meta_facts: dict[str, Any] | None,
        narrative_supplements: dict[str, str] | None = None,
        open_questions: list[dict[str, Any]] | None = None,
    ) -> str:
        parts = [
            f"=== SYNTHESIS TASK: {entity_name} ({entity_id}) ===",
            "",
        ]

        # Research Director's initial hypothesis
        if directive:
            parts.append("=== RESEARCH DIRECTOR'S INITIAL DIRECTIVE ===")
            parts.append(f"Initial Hypothesis: {directive.initial_hypothesis}")
            parts.append(f"Hypothesis Type: {directive.hypothesis_type}")
            parts.append(f"Key Controversy: {directive.key_controversy}")
            parts.append(f"Consensus Likely Believes: {directive.what_consensus_likely_believes}")
            parts.append(f"Key Variables: {', '.join(directive.key_variables)}")
            parts.append(f"Opening Angle: {directive.opening_angle}")
            parts.append(f"What Could Flip: {directive.what_could_flip_my_view}")
            parts.append("")

        # Market context
        # BUG-A22: A-share entities had USD-formatted DCF/price inputs and
        # the LLM faithfully echoed `$2.66` for `¥2.66/股`. Use the entity's
        # display block.
        disp = resolve_display(meta_facts)
        parts.append("=== MARKET & VALUATION CONTEXT ===")
        price = market_data.get("current_price", 0)
        _per_share_suffix = "/股" if disp["currency"] == "CNY" else ""
        parts.append(f"Current Price: {fmt_money_small(float(price), disp)}{_per_share_suffix}")
        for k, v in scenarios.items():
            if isinstance(v, (int, float)):
                parts.append(f"DCF {k}: {fmt_money_small(float(v), disp)}{_per_share_suffix}")
        # BUG-Y20 (2026-05-06): if ReverseDCFSolver marked the implied
        # growth unreliable (bisection boundary-hit), don't feed the LLM a
        # fake-clean number. The synthesizer would otherwise build narrative
        # like "市场隐含 50% 增速" which is meaningless when the solver
        # actually didn't converge.
        if meta_facts.get("__implied_growth_unreliable"):
            _bnd = meta_facts.get("__implied_growth_boundary_hit", "?")
            parts.append(
                f"Market-Implied Revenue Growth: n/a (reverse-DCF non-monotonic; "
                f"bisection hit {_bnd} bound — likely non-monotonic price/growth "
                f"curve for loss-making/high-capex profile)"
            )
        else:
            try:
                parts.append(f"Market-Implied Revenue Growth: {float(implied_growth):.1%}")
            except (ValueError, TypeError):
                parts.append(f"Market-Implied Revenue Growth: {implied_growth}")
        if sensitivity_rankings:
            parts.append("Top Sensitivities:")
            for sr in sensitivity_rankings[:5]:
                parts.append(f"  {sr.get('assumption', '')}: {float(sr.get('impact_pct', 0)):.1f}% impact")
        parts.append("")

        # ── Aegis 2.0 Phase 0：预期前沿 / 定价体制 / 近事件事实块 ──
        _lang = "zh" if disp["currency"] == "CNY" else "en"
        _frontier_lines = frontier_prompt_lines(
            (meta_facts or {}).get("__expectations_frontier"), _lang,
        )
        if _frontier_lines:
            parts.append("=== MARKET-IMPLIED EXPECTATIONS FRONTIER (conditional reverse-DCF) ===")
            parts.append(
                "(One margin scenario per line. Quote these in conditional form only; "
                "a single-point 'market implies Z% growth' is prohibited.)"
            )
            for _ln in _frontier_lines:
                parts.append(f"  - {_ln}")
            parts.append("")

        _regime = (meta_facts or {}).get("__pricing_regime")
        if isinstance(_regime, dict) and _regime.get("weights"):
            parts.append("=== PRICING REGIME (narrative frame only — never suppress the DCF gap) ===")
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
            for _vf in _regime.get("verification_focus") or []:
                parts.append(f"  verify: {_vf}")
            parts.append("")

        _events_block = (meta_facts or {}).get("__recent_events_prompt")
        if isinstance(_events_block, str) and _events_block.strip():
            parts.append("=== RECENT DISCLOSED EVENTS (the ONLY sanctioned catalyst source) ===")
            parts.append(_events_block)
            parts.append("")

        # Key metrics
        parts.append("=== KEY METRICS ===")
        highlight_metrics = [
            "gross_margin", "operating_margin", "net_margin", "roic", "roe",
            "sbc_to_revenue", "capex_to_revenue", "pe_ratio", "ev_to_ebitda",
            "ev_to_revenue", "current_ratio", "revenue_cagr",
        ]
        for m in highlight_metrics:
            val = computed_metrics.get(m)
            if val is not None:
                if abs(val) < 10:
                    parts.append(f"  {m}: {val:.4f}" if abs(val) < 1 else f"  {m}: {val:.2f}")
                else:
                    parts.append(f"  {m}: {val:,.0f}")
        parts.append("")

        # All agent judgments
        for j in judgments:
            agent = getattr(j, "agent_name", "unknown")
            parts.append(f"=== {agent.upper().replace('_', ' ')} JUDGMENT ===")

            # Observations
            obs = getattr(j, "observations", [])
            if obs:
                parts.append("Observations:")
                for o in obs:
                    parts.append(f"  - {o.text} [sources: {', '.join(o.source_ids[:3])}]")

            # Inferences
            infs = getattr(j, "inferences", [])
            if infs:
                parts.append("Inferences:")
                for inf in infs:
                    conf = getattr(inf, "confidence", "medium")
                    parts.append(f"  [{conf}] {inf.text}")

            # Counterarguments
            cas = getattr(j, "counterarguments", [])
            if cas:
                parts.append("Counterarguments:")
                for ca in cas:
                    parts.append(f"  [{ca.strength}] {ca.text}")

            # Disconfirming triggers
            dts = getattr(j, "disconfirming_triggers", [])
            if dts:
                parts.append("Kill Criteria:")
                for dt in dts:
                    parts.append(f"  - {dt.text}")

            # Uncertainties
            uncs = getattr(j, "self_reported_uncertainties", [])
            if uncs:
                parts.append("Self-Reported Uncertainties:")
                for u in uncs:
                    parts.append(f"  - {u}")

            parts.append("")

        # Narrative supplements from deep-mode agents
        if narrative_supplements:
            parts.append("=== DEEP-MODE ANALYST MEMOS ===")
            parts.append("(These are free-form analytical narratives from agents that ran in DEEP mode.")
            parts.append(" They contain insights that go beyond the structured observations/inferences.)")
            parts.append("")
            for agent_name, narrative in narrative_supplements.items():
                if narrative:
                    label = agent_name.upper().replace("_", " ")
                    parts.append(f"--- {label} MEMO ---")
                    parts.append(narrative)
                    parts.append("")

        if open_questions:
            parts.append("=== OPEN RESEARCH QUESTIONS (unanswered by available data) ===")
            parts.append("(Agents raised these questions but the system couldn't answer them automatically.)")
            parts.append(" Consider how these gaps affect your thesis confidence.)")
            for oq in open_questions:
                agent_label = oq.get("agent", "unknown").replace("_", " ").title()
                parts.append(f"  [{oq.get('priority', 'medium').upper()}] {agent_label}: {oq.get('question', '')}")
            parts.append("")

        parts.append("Synthesize all the above into a coherent, opinionated investment thesis.")
        return "\n".join(parts)
