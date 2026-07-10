"""Narrative-vs-Fact Critic (v3).

Cross-checks numbers that LLM agents cite in their narratives against
the ground-truth data tables (meta_facts, computed_metrics, etc.).

Unlike NumericConsistencyCritic (v1/v2) which only catches self-
contradictory equations *within* a narrative, this critic catches a
fundamentally harder class of errors: **numbers that look plausible
but are factually wrong**, because the LLM hallucinated or misquoted
a source value.

Currently checks:

1. **CAGR window mislabel**: agent writes "N-year CAGR" with value X%,
   but actual N-year CAGR computed from historical revenue ≠ X%.
   Root cause: LLM rephrases "4-year" as "three years" but keeps the
   4-year number. (AAPL BUG — 3.3% is 4yr, not 3yr.)

2. **Key metric mismatch**: agent cites a specific value for a known
   metric (revenue, net income, operating margin, P/E, EPS, FCF, D&A,
   ROIC) and the value doesn't match meta_facts/computed_metrics within
   tolerance.

3. **Revenue/growth figure mismatch**: agent cites FY20XX revenue or
   YoY growth that doesn't match __historical_revenue / __historical_growth.

Design principles:
- CONSERVATIVE: only flag high-confidence mismatches (tight regex + known
  metric names + >10% relative error for dollar amounts, >1.5pp for ratios)
- No LLM in the loop: pure regex + arithmetic
- severity="warn" by default, "block" for CAGR window mislabel (the most
  dangerous class because it directly misleads growth projections)
"""

from __future__ import annotations

import math
import re
from typing import Any

from aegis.core.critics.base import CriticBase
from aegis.data_contracts.critic_result_schema import CriticIssue, CriticResult
from aegis.data_contracts.judgment_schema import JudgmentContract


# ── Tolerances ───────────────────────────────────────────────────────

# Dollar amounts (revenue, net_income, etc.): 10% relative
DOLLAR_REL_TOL = 0.10
# Ratios/percentages (margin, CAGR, growth): 1.0 percentage points absolute
# Intentionally tight — CAGR errors of 1.5pp (e.g. 3.3% vs 1.8%) are material
# because they compound over 10-year DCF projections.
RATIO_ABS_TOL = 0.010
# Multiples (P/E, EV/EBITDA): 15% relative
MULTIPLE_REL_TOL = 0.15


def _close_dollar(claimed: float, actual: float) -> bool:
    if actual == 0:
        return abs(claimed) < 1e6
    return abs(claimed - actual) / abs(actual) <= DOLLAR_REL_TOL


def _close_ratio(claimed: float, actual: float) -> bool:
    """Both in decimal form (0.033 not 3.3%)."""
    return abs(claimed - actual) <= RATIO_ABS_TOL


def _close_multiple(claimed: float, actual: float) -> bool:
    if actual == 0:
        return abs(claimed) < 1.0
    return abs(claimed - actual) / abs(actual) <= MULTIPLE_REL_TOL


# ── Regex patterns ───────────────────────────────────────────────────

# CAGR pattern: "N-year CAGR" or "CAGR over N years" + a percentage
# Also matches Chinese: "N年CAGR"
_YEAR_NUM = r"(\d+|one|two|three|four|five|six|seven|eight|nine|ten)"

_CAGR_PATTERN = re.compile(
    rf"(?:{_YEAR_NUM}-year\s+(?:revenue\s+)?CAGR|"              # "3-year CAGR"
    rf"CAGR\s+(?:of\s+)?(?:over\s+)?(?:the\s+)?(?:past\s+|last\s+)?{_YEAR_NUM}\s+years?|"  # "CAGR over 3 years"
    # AUDIT (2026-07): Chinese CAGR aliases — LLM narratives usually write
    # "复合增长率" / "年均复合增长率" instead of the literal "CAGR", so the
    # most dangerous block-level check (CAGR window mislabel) was dead for
    # natural Chinese phrasing. Longer aliases listed first.
    rf"(\d+)年\s*(?:营收\s*|收入\s*)?"                             # "3年CAGR" / "3年复合增长率"
    r"(?:年均复合增长率|复合年均增长率|年均复合增速|复合年均增速|"
    r"年复合增长率|年复合增速|复合增长率|复合增速|CAGR)|"
    rf"(?:past|last|trailing|over\s+the\s+(?:past|last))\s+{_YEAR_NUM}\s+years?[^0-9]{{0,60}}?CAGR"  # "past three years...CAGR"
    r")"
    r"[^0-9]{0,40}?"  # filler (up to 40 chars)
    r"(?:of\s+(?:only\s+)?|is\s+|at\s+|was\s+|=\s*|：\s*)?"
    r"(-?\d+(?:\.\d+)?)\s*%",
    re.IGNORECASE,
)

# Pattern: "CAGR of X%...over/past N years" or "CAGR...X% annually over the past N years"
_CAGR_TRAILING = re.compile(
    r"CAGR[^0-9]{0,20}?"
    r"(?:of\s+(?:only\s+)?|is\s+|at\s+|was\s+|=\s*|：\s*)?"
    r"(-?\d+(?:\.\d+)?)\s*%"
    r"[^0-9]{0,60}?"
    rf"(?:over|past|last|trailing)\s+(?:the\s+)?(?:past\s+|last\s+)?{_YEAR_NUM}\s+years?",
    re.IGNORECASE,
)

# Also match reverse order: "CAGR of X%...over N years"
_CAGR_REVERSE = re.compile(
    r"(?:revenue\s+)?CAGR[^0-9]{0,15}?"
    r"(?:of\s+|is\s+|at\s+|was\s+|=\s*|：\s*)?"
    r"(-?\d+(?:\.\d+)?)\s*%"
    r"[^0-9]{0,40}?"
    r"(?:over|past|last|trailing)\s+(?:the\s+)?(\d+)\s+years?",
    re.IGNORECASE,
)

# Key metric patterns — "revenue of $XXX B/billion" etc.
_DOLLAR_MAG = {
    "B": 1e9, "billion": 1e9, "bn": 1e9,
    "M": 1e6, "million": 1e6, "mn": 1e6,
    "T": 1e12, "trillion": 1e12,
    "亿": 1e8, "万亿": 1e12, "万": 1e4,
}

# Only match TOTAL/COMPANY-LEVEL revenue — NOT segment revenue or forward
# projections. We require "total revenue" or standalone "revenue" preceded by
# a company-level context word, or Chinese equivalents. Segment names like
# "iPhone revenue $209B" or "cloud revenue" should NOT match.
_DOLLAR_METRIC_PATTERNS: list[tuple[str, re.Pattern, str]] = []
for _metric_name, _keys in [
    # "revenue" only when preceded by "total" or start-of-sentence / company-level context
    # We exclude segment-qualified revenue (iPhone/Services/Cloud/YouTube/etc.)
    ("revenue", ["total revenue", "总营收", "总收入"]),
    ("net_income", ["net income", "net profit", "净利润", "归母净利润"]),
    ("operating_income", ["operating income", "operating profit", "营业利润"]),
    ("ebitda", ["EBITDA"]),
    ("free_cash_flow", ["free cash flow", "FCF", "自由现金流"]),
    ("depreciation_amortization", ["D&A", "depreciation and amortization",
                                     "depreciation & amortization", "折旧摊销", "折旧与摊销"]),
]:
    for _kw in _keys:
        _esc = re.escape(_kw)
        # AUDIT (2026-07, narrative_fact_critic:140): the connector group
        # used \s+ — Chinese has no space before 为/达到/约, so the most
        # common CN phrasings ("总营收为65.0亿" / "净利润达到10.5亿") never
        # matched and the whole check was dead for them. \s* keeps the
        # English forms working while letting CN connectors bind directly.
        _pat = re.compile(
            rf"(?<![a-zA-Z]){_esc}(?:\s*(?:of|is|was|at|=|was approximately|approximately|约为|约|达到|为))?\s*"
            rf"(?:[\$¥€£]|HK\$|US\$|RMB)?\s*(-?\d+(?:\.\d+)?)\s*"
            rf"(万亿|亿|万|B(?![a-zA-Z])|billion|bn|M(?![a-zA-Z])|million|mn|T(?![a-zA-Z])|trillion)",
            re.IGNORECASE,
        )
        _DOLLAR_METRIC_PATTERNS.append((_metric_name, _pat, _kw))

# Ratio metric patterns — only match company-level ratios.
# We skip segment-specific margins (e.g. "Services operating margin 70%")
# by requiring the keyword NOT be preceded by a segment-like qualifier.
# This is conservative: only flag when we're confident it's company-level.
_RATIO_METRIC_PATTERNS: list[tuple[str, re.Pattern]] = []
for _metric_name, _keys in [
    # Only check ROIC/ROE which are always company-level.
    # Operating/gross/net margins are too often segment-specific to safely match.
    ("roic", ["ROIC", "投资回报率"]),
    ("roe", ["ROE", "净资产收益率"]),
]:
    for _kw in _keys:
        _esc = re.escape(_kw)
        _pat = re.compile(
            rf"(?<![a-zA-Z]){_esc}[^0-9]{{0,20}}?(-?\d+(?:\.\d+)?)\s*%",
            re.IGNORECASE,
        )
        _RATIO_METRIC_PATTERNS.append((_metric_name, _pat))

# Historical revenue: "FY2025 revenue of $416.2B"
_FY_REVENUE_PATTERN = re.compile(
    r"FY\s*(\d{4})\s+(?:revenue|营收|营业收入)[^0-9]{0,20}?"
    r"(?:[\$¥€£]|HK\$|US\$|RMB)?\s*(-?\d+(?:\.\d+)?)\s*"
    r"(万亿|亿|万|B(?![a-zA-Z])|billion|bn|M(?![a-zA-Z])|million|mn)",
    re.IGNORECASE,
)

# YoY growth: "FY2025 revenue growth of 6.4%"
_FY_GROWTH_PATTERN = re.compile(
    r"FY\s*(\d{4})\s+(?:revenue\s+)?(?:growth|增长|增速)[^0-9]{0,20}?"
    r"(-?\d+(?:\.\d+)?)\s*%",
    re.IGNORECASE,
)


_WORD_TO_NUM = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}


def _parse_year_count(s: str) -> int | None:
    """Parse '3' or 'three' → 3."""
    s = s.strip().lower()
    if s.isdigit():
        return int(s)
    return _WORD_TO_NUM.get(s)


def _parse_dollar(num_str: str, unit_str: str) -> float | None:
    """Parse '416.2' + 'B' → 416.2e9."""
    try:
        val = float(num_str)
    except (TypeError, ValueError):
        return None
    unit_clean = unit_str.strip().rstrip(".")
    # AUDIT-C3 (2026-07): was `re.search(suffix + "$")` over dict insertion
    # order — "万亿" ends in "亿", so the 1e8 entry matched first and
    # "2.4万亿" parsed as 2.4e8 (10,000× low). Every correct trillion-CNY
    # citation (工商银行/中石油/平安…) then tripped METRIC_MISMATCH /
    # FY_REVENUE_MISMATCH with absurd warning text. fullmatch kills the
    # suffix aliasing — the regex capture is always an exact unit token.
    for suffix, mult in _DOLLAR_MAG.items():
        if re.fullmatch(re.escape(suffix), unit_clean, re.IGNORECASE):
            return val * mult
    return None


def _format_money(amount: float, meta_facts: dict | None) -> str:
    """BUG-Y12 (2026-05-06): format an absolute monetary amount in the
    currency implied by `meta_facts.__display`. Default `$X.XB`; CNY uses
    `¥X.X亿`. Used in critic warning text so an A-share LLM that quotes
    `总营收 65亿` doesn't get a confusing `$6.5B` mismatch warning that
    mixes US sigil with Chinese keyword.
    """
    display = (meta_facts or {}).get("__display") or {}
    currency = (display.get("currency") or (meta_facts or {}).get("__currency") or "USD").upper()
    is_cny = currency in ("CNY", "RMB", "¥") or display.get("symbol") == "¥"
    if is_cny:
        return f"¥{amount/1e8:.1f}亿"
    return f"${amount/1e9:.1f}B"


class NarrativeFactCritic(CriticBase):
    """Cross-checks agent narrative numbers against ground-truth data."""

    CRITIC_TYPE = "narrative_fact_critic"

    def review(
        self,
        judgments: list[JudgmentContract],
        context: dict | None = None,
    ) -> CriticResult:
        issues: list[CriticIssue] = []

        meta_facts = (context or {}).get("meta_facts", {})
        computed_metrics = (context or {}).get("computed_metrics", {})
        market_data = (context or {}).get("market_data", {})

        # Build ground truth tables
        hist_rev = meta_facts.get("__historical_revenue", {})
        hist_growth = meta_facts.get("__historical_growth", {})

        for j in judgments:
            issues.extend(self._check_judgment(
                j, meta_facts, computed_metrics, market_data,
                hist_rev, hist_growth,
            ))

        return CriticResult(
            critic_id=f"critic_narrative_fact_{id(self)}",
            critic_type=self.CRITIC_TYPE,
            issues=issues,
            block_publish=self._any_block(issues),
            overall_risk=self._overall_risk(issues),
        )

    def _extract_texts(self, j: JudgmentContract) -> list[tuple[str, str]]:
        texts = []
        for i, obs in enumerate(j.observations):
            texts.append((f"obs[{i}]", obs.text))
        for i, inf in enumerate(j.inferences):
            texts.append((f"inf[{i}]", inf.text))
        # Also check narrative supplement if present
        narrative = getattr(j, "narrative", None)
        if narrative and isinstance(narrative, str):
            texts.append(("narrative", narrative))
        return texts

    def _check_judgment(
        self,
        j: JudgmentContract,
        meta_facts: dict,
        computed_metrics: dict,
        market_data: dict,
        hist_rev: dict,
        hist_growth: dict,
    ) -> list[CriticIssue]:
        issues: list[CriticIssue] = []

        for source, text in self._extract_texts(j):
            # 1. CAGR window checks
            issues.extend(self._check_cagr(j, source, text, hist_rev, meta_facts))

            # 2. Dollar metric checks
            issues.extend(self._check_dollar_metrics(
                j, source, text, meta_facts, computed_metrics))

            # 3. Ratio metric checks
            issues.extend(self._check_ratio_metrics(
                j, source, text, computed_metrics))

            # 4. Historical revenue / growth checks
            issues.extend(self._check_historical(
                j, source, text, hist_rev, hist_growth, meta_facts))

        return issues

    def _check_cagr(
        self,
        j: JudgmentContract,
        source: str,
        text: str,
        hist_rev: dict[int, float],
        meta_facts: dict | None = None,
    ) -> list[CriticIssue]:
        """Check CAGR window + value consistency."""
        issues = []
        if not hist_rev:
            return issues

        sorted_years = sorted(hist_rev.keys())
        if len(sorted_years) < 2:
            return issues

        latest_year = sorted_years[-1]

        # Find CAGR claims with explicit N-year window
        cagr_claims: list[tuple[int, float]] = []  # (n_years, claimed_pct)

        for m in _CAGR_PATTERN.finditer(text):
            raw_n = m.group(1) or m.group(2) or m.group(3) or m.group(4)
            n_years = _parse_year_count(raw_n)
            if n_years is None:
                continue
            claimed_pct = float(m.group(5))
            cagr_claims.append((n_years, claimed_pct))

        for m in _CAGR_REVERSE.finditer(text):
            claimed_pct = float(m.group(1))
            n_years = _parse_year_count(m.group(2))
            if n_years is None:
                continue
            cagr_claims.append((n_years, claimed_pct))

        for m in _CAGR_TRAILING.finditer(text):
            claimed_pct = float(m.group(1))
            n_years = _parse_year_count(m.group(2))
            if n_years is None:
                continue
            cagr_claims.append((n_years, claimed_pct))

        for n_years, claimed_pct in cagr_claims:
            base_year = latest_year - n_years
            if base_year not in hist_rev:
                continue

            base_rev = hist_rev[base_year]
            end_rev = hist_rev[latest_year]
            if base_rev <= 0 or n_years <= 0:
                continue

            actual_cagr = (end_rev / base_rev) ** (1 / n_years) - 1
            claimed_decimal = claimed_pct / 100

            if not _close_ratio(claimed_decimal, actual_cagr):
                actual_pct = actual_cagr * 100
                issues.append(self._make_issue(
                    code="CAGR_WINDOW_MISMATCH",
                    severity="block",
                    message=(
                        f"{j.agent_name} {source}: claims {n_years}-year revenue "
                        f"CAGR is {claimed_pct:.1f}%, but actual "
                        f"FY{base_year}→FY{latest_year} CAGR = {actual_pct:.1f}% "
                        f"({_format_money(base_rev, meta_facts)} → "
                        f"{_format_money(end_rev, meta_facts)}). "
                        f"Off by {abs(claimed_pct - actual_pct):.1f}pp."
                    ),
                    judgment_ids=[j.judgment_id],
                    action=(
                        f"Use the correct {n_years}-year CAGR of {actual_pct:.1f}%, "
                        f"or change the window label to match the value."
                    ),
                ))

        return issues

    def _check_dollar_metrics(
        self,
        j: JudgmentContract,
        source: str,
        text: str,
        meta_facts: dict,
        computed_metrics: dict,
    ) -> list[CriticIssue]:
        """Check dollar-denominated metrics (revenue, net income, etc.)."""
        issues = []

        for metric_key, pattern, keyword in _DOLLAR_METRIC_PATTERNS:
            actual = meta_facts.get(metric_key) or computed_metrics.get(metric_key)
            if actual is None or actual == 0:
                continue

            for m in pattern.finditer(text):
                claimed = _parse_dollar(m.group(1), m.group(2))
                if claimed is None:
                    continue

                if not _close_dollar(claimed, actual):
                    pct_off = abs(claimed - actual) / abs(actual) * 100
                    issues.append(self._make_issue(
                        code="METRIC_MISMATCH",
                        severity="warn",
                        message=(
                            f"{j.agent_name} {source}: cites {keyword} as "
                            f"{_format_money(claimed, meta_facts)}, but meta_facts has "
                            f"{_format_money(actual, meta_facts)} (off by {pct_off:.0f}%)."
                        ),
                        judgment_ids=[j.judgment_id],
                        action=f"Verify {metric_key} value against source data.",
                    ))

        return issues

    def _check_ratio_metrics(
        self,
        j: JudgmentContract,
        source: str,
        text: str,
        computed_metrics: dict,
    ) -> list[CriticIssue]:
        """Check ratio metrics (margins, ROIC, ROE)."""
        issues = []

        for metric_key, pattern in _RATIO_METRIC_PATTERNS:
            actual = computed_metrics.get(metric_key)
            if actual is None:
                continue

            for m in pattern.finditer(text):
                try:
                    claimed_pct = float(m.group(1))
                except (TypeError, ValueError):
                    continue
                claimed_decimal = claimed_pct / 100

                # actual is already decimal (0.32 for 32%)
                if not _close_ratio(claimed_decimal, actual):
                    actual_pct = actual * 100
                    issues.append(self._make_issue(
                        code="RATIO_MISMATCH",
                        severity="warn",
                        message=(
                            f"{j.agent_name} {source}: cites {metric_key} as "
                            f"{claimed_pct:.1f}%, but computed_metrics has "
                            f"{actual_pct:.1f}% (off by "
                            f"{abs(claimed_pct - actual_pct):.1f}pp)."
                        ),
                        judgment_ids=[j.judgment_id],
                        action=f"Verify {metric_key} against computed_metrics.",
                    ))

        return issues

    def _check_historical(
        self,
        j: JudgmentContract,
        source: str,
        text: str,
        hist_rev: dict[int, float],
        hist_growth: dict[int, float],
        meta_facts: dict | None = None,
    ) -> list[CriticIssue]:
        """Check FY-specific revenue and growth claims."""
        issues = []

        # FY20XX revenue claims
        for m in _FY_REVENUE_PATTERN.finditer(text):
            fy = int(m.group(1))
            claimed = _parse_dollar(m.group(2), m.group(3))
            if claimed is None:
                continue
            actual = hist_rev.get(fy)
            if actual is None:
                continue
            if not _close_dollar(claimed, actual):
                pct_off = abs(claimed - actual) / abs(actual) * 100
                issues.append(self._make_issue(
                    code="FY_REVENUE_MISMATCH",
                    severity="warn",
                    message=(
                        f"{j.agent_name} {source}: cites FY{fy} revenue as "
                        f"{_format_money(claimed, meta_facts)}, but historical data has "
                        f"{_format_money(actual, meta_facts)} (off by {pct_off:.0f}%)."
                    ),
                    judgment_ids=[j.judgment_id],
                    action="Use historical revenue data from meta_facts.",
                ))

        # FY20XX growth claims
        for m in _FY_GROWTH_PATTERN.finditer(text):
            fy = int(m.group(1))
            claimed_pct = float(m.group(2))
            actual = hist_growth.get(fy)
            if actual is None:
                continue
            claimed_decimal = claimed_pct / 100
            if not _close_ratio(claimed_decimal, actual):
                actual_pct = actual * 100
                issues.append(self._make_issue(
                    code="FY_GROWTH_MISMATCH",
                    severity="warn",
                    message=(
                        f"{j.agent_name} {source}: cites FY{fy} growth as "
                        f"{claimed_pct:.1f}%, but actual is {actual_pct:.1f}% "
                        f"(off by {abs(claimed_pct - actual_pct):.1f}pp)."
                    ),
                    judgment_ids=[j.judgment_id],
                    action="Use YoY growth data from __historical_growth.",
                ))

        return issues
