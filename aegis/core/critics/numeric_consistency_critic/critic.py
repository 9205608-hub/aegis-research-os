"""Numeric Consistency Critic.

Scans narrative text in `Observation.text` and `Inference.text` for
fabricated arithmetic — equations the LLM stated that don't actually
hold. Examples from real reports:

  - "净负债 47 亿 = 总债务 75 亿 - 现金 15 亿"   (75-15=60, not 47)
  - "OpEx growth 23% = $4.2B vs $3.4B"           (4.2/3.4-1 ≈ 23.5%, OK)
  - "FCF margin 12% = $0.6B / $5B"               (0.6/5 = 12%, OK)

Strategy: regex the text for explicit equations of the form
`X (unit) = Y (unit) ± Z (unit)` and `X% = Y / Z`. Parse the operands,
evaluate the right-hand side, and flag any mismatch beyond a 5%
relative tolerance.

This is intentionally CONSERVATIVE — we only flag equations the LLM
*explicitly stated*, not numbers that happen to disagree across
sentences. The goal is to catch self-contradictory math, not enforce
cross-paragraph consistency (which is a separate problem).

Severity: `warn` only. We surface the contradiction in the report's
critic-issues list but do not block publish — the false-positive risk
of regex-based math parsing is too high to justify a hard gate. If the
warn count is high we should tighten the prompt instead.
"""

from __future__ import annotations

import re

from aegis.core.critics.base import CriticBase
from aegis.data_contracts.critic_result_schema import CriticIssue, CriticResult
from aegis.data_contracts.judgment_schema import JudgmentContract


# ── Number parsing ────────────────────────────────────────────────────
#
# We accept signed decimals optionally followed by a magnitude suffix.
# "亿" = 1e8, "万亿" = 1e12, "万" = 1e4, "B" = 1e9, "M" = 1e6, "K" = 1e3.
# A bare number (no suffix) is taken at face value. Currency prefixes
# ($, ¥, etc.) are stripped before parsing.
#
# We DO NOT try to normalize across magnitudes inside one equation —
# instead we require all three operands of a single equation to share
# the same magnitude class (or both be unitless). Mixing 亿 and 万 in
# a single equation is uncommon in practice and would produce too many
# false positives.

_NUM_RE = r"(-?\d+(?:\.\d+)?)"
_CCY_PREFIX = r"(?:[\$¥€£]|HK\$|US\$|RMB)?"

# Magnitude suffixes — order matters: 万亿 must be tried before 万.
# For English suffixes (B/M/K) we use a lookahead instead of \b
# because \bB\b doesn't match "50B" (digit→letter has no word
# boundary). Trailing lookahead `(?![a-zA-Z])` blocks matches inside
# words like "Bond" or "Million" while still matching "50B".
_MAG_PATTERNS = [
    (r"万亿", 1e12),
    (r"亿", 1e8),
    (r"万", 1e4),
    (r"B(?![a-zA-Z])", 1e9),
    (r"M(?![a-zA-Z])", 1e6),
    (r"K(?![a-zA-Z])", 1e3),
    (r"bn(?![a-zA-Z])", 1e9),
    (r"mn(?![a-zA-Z])", 1e6),
    (r"trillion", 1e12),
    (r"billion", 1e9),
    (r"million", 1e6),
]


def _parse_amount(token: str) -> tuple[float | None, float | None]:
    """Parse `'¥75 亿'` → `(75.0, 1e8)`. Returns (value, magnitude)."""
    if token is None:
        return None, None
    s = token.strip()
    # Strip currency prefix.
    s = re.sub(r"^(?:[\$¥€£]|HK\$|US\$|RMB)\s*", "", s)
    m = re.match(_NUM_RE + r"\s*(.*)$", s)
    if not m:
        return None, None
    val = float(m.group(1))
    rest = m.group(2)
    for pat, mult in _MAG_PATTERNS:
        if re.search(pat, rest):
            return val, mult
    return val, 1.0


REL_TOL = 0.05  # 5% relative tolerance for additive equations
RATIO_ABS_TOL = 0.01  # 1 percentage point absolute tolerance for ratio claims


def _within_rel_tol(lhs: float, rhs: float) -> bool:
    """5% relative tolerance — for additive equations on dollar amounts.
    Floors denom at 1.0 to handle near-zero LHS gracefully (e.g. an
    LHS of 0.5 with RHS of 0.6 is still "close enough" in absolute terms)."""
    denom = max(abs(lhs), 1.0)
    return abs(lhs - rhs) / denom <= REL_TOL


def _within_ratio_tol(lhs: float, rhs: float) -> bool:
    """For ratio/percentage comparisons, use ABSOLUTE tolerance of 1
    percentage point. A claim of "12%" vs actual 11.5% is acceptable
    rounding; "12%" vs 7.5% is not. Relative tolerance with a 1.0
    floor would let the 7.5% case through unflagged."""
    return abs(lhs - rhs) <= RATIO_ABS_TOL


def _within_unitless_tol(lhs: float, rhs: float) -> bool:
    """For unitless multiples (P/E, EV/EBITDA), 5% relative tolerance
    measured against the LHS itself with a floor of 0.5 — multiples
    are usually >5 so the floor only kicks in for unusual claims."""
    denom = max(abs(lhs), 0.5)
    return abs(lhs - rhs) / denom <= REL_TOL


def _eval_equation_three(lhs: str, op: str, a: str, b: str) -> tuple[bool, float, float] | None:
    """Evaluate `lhs = a op b` for additive ops. Returns
    (is_consistent, lhs_val, rhs_val) in normalized magnitude. None if
    any operand fails to parse or the operands have inconsistent
    magnitudes."""
    lhs_v, lhs_mag = _parse_amount(lhs)
    a_v, a_mag = _parse_amount(a)
    b_v, b_mag = _parse_amount(b)
    if None in (lhs_v, a_v, b_v) or None in (lhs_mag, a_mag, b_mag):
        return None
    # All three must share magnitude (or all be unitless == 1.0).
    if not (lhs_mag == a_mag == b_mag):
        return None
    if op == "+":
        rhs = a_v + b_v
    elif op in ("-", "−", "–"):
        rhs = a_v - b_v
    else:
        return None
    return _within_rel_tol(lhs_v, rhs), lhs_v, rhs


def _eval_ratio_pct(lhs_pct: str, a: str, b: str) -> tuple[bool, float, float] | None:
    """Evaluate `lhs% = a / b`. LHS is a bare percentage; A and B must
    share magnitude (so dividing them gives a dimensionless ratio).
    Returns (consistent, lhs_as_decimal, computed_ratio)."""
    try:
        lhs_v = float(lhs_pct) / 100.0
    except (TypeError, ValueError):
        return None
    a_v, a_mag = _parse_amount(a)
    b_v, b_mag = _parse_amount(b)
    if None in (a_v, b_v) or None in (a_mag, b_mag):
        return None
    if a_mag != b_mag:
        return None
    if b_v == 0:
        return None
    rhs = a_v / b_v
    return _within_ratio_tol(lhs_v, rhs), lhs_v, rhs


def _eval_unitless_ratio(lhs: str, a: str, b: str) -> tuple[bool, float, float] | None:
    """Evaluate `lhs = a / b` where all three are bare numbers (no unit
    suffix). This catches P/E and multiple claims like `25 = 100 / 4`."""
    try:
        lhs_v = float(lhs)
        a_v = float(a)
        b_v = float(b)
    except (TypeError, ValueError):
        return None
    if b_v == 0:
        return None
    rhs = a_v / b_v
    return _within_unitless_tol(lhs_v, rhs), lhs_v, rhs


# ── Equation patterns ─────────────────────────────────────────────────
#
# Pattern 1: `<num1>(unit) = <num2>(unit) [-+] <num3>(unit)`
# Anchors: each operand starts with optional currency prefix and ends
# with a magnitude suffix (亿/B/...) so we don't accidentally match
# percentages or year numbers like "2024".
#
# We use a single magnitude class per equation (Chinese 亿/万 OR
# English B/M/K) — mixing causes too many false positives.

_OPERAND = (
    r"(?:[\$¥€£]|HK\$|US\$|RMB)?\s*-?\d+(?:\.\d+)?\s*"
    r"(?:万亿|亿|万|B(?![a-zA-Z])|M(?![a-zA-Z])|K(?![a-zA-Z])"
    r"|bn(?![a-zA-Z])|mn(?![a-zA-Z])|trillion|billion|million)"
)

# Allow up to 12 chars of non-digit, non-operator filler between an `=`
# or `+/-` and the next operand, so we catch real narratives like
# `净负债 47 亿 = 总债务 75 亿 − 现金 15 亿` where nouns like "总债务"
# and "现金" sit between the operator and the number.
_FILLER = r"[^\d=≈+\-−–]{0,12}"

# Pattern A: A = B ± C (additive, all units must match)
_EQ_THREE_PATTERN = re.compile(
    rf"({_OPERAND})\s*[=≈]{_FILLER}({_OPERAND})\s*([-+−–]){_FILLER}({_OPERAND})",
)

# AUDIT (2026-07, numeric critic:147-171): Chinese implicit equations.
# "净负债47亿元，即总债务75亿减现金15亿" carries the exact same broken
# arithmetic as the literal-`=` form but contains neither '=' nor '−', so
# it bypassed _EQ_THREE_PATTERN entirely. Accept Chinese copulas
# (即/等于/为/是/合计...) as the equality marker — but ONLY paired with a
# Chinese word operator (减/加/扣除...): "为营收50亿-60亿" is a RANGE, not
# an equation, so hyphen-style operators stay exclusive to '='/'≈'.
_CN_EQ_MARK = r"(?:即为|即|等于|也就是|亦即|合计为|合计|为|是)"
_CN_OP_WORD = r"(减去|扣除|扣减|减|加上|加)"
# Tolerates 元/人民币/，between the LHS operand and the copula. Excludes
# digits and operator chars so it can't swallow a neighbouring operand.
_CN_EQ_GAP = r"[^\d=≈+\-−–%]{0,6}?"
_EQ_THREE_CN_PATTERN = re.compile(
    rf"({_OPERAND}){_CN_EQ_GAP}{_CN_EQ_MARK}{_FILLER}"
    rf"({_OPERAND}){_CN_EQ_GAP}{_CN_OP_WORD}{_FILLER}({_OPERAND})",
)
_CN_MINUS_WORDS = ("减去", "扣除", "扣减", "减")

# Pattern B: X% = A / B (ratio percent — A and B share unit, LHS is %)
# We use a tighter filler around `/` because explicit ratio statements
# usually format the operands tightly (e.g. "12% = $0.6B / $5B").
_BARE_NUM = r"-?\d+(?:\.\d+)?"
_RATIO_PCT_PATTERN = re.compile(
    rf"({_BARE_NUM})\s*%\s*[=≈]{_FILLER}({_OPERAND})\s*[/÷]\s*{_FILLER}({_OPERAND})",
)

# Pattern C: X = A / B (unitless ratio — all three are bare numbers,
# typical for P/E, EV/EBITDA, or multiple claims). LHS often has a
# trailing 'x' to mark "multiple"; we accept and discard it.
# We require explicit `/` or `÷` between A and B.
_UNITLESS_RATIO_PATTERN = re.compile(
    rf"({_BARE_NUM})x?\s*[=≈]{_FILLER}({_BARE_NUM})\s*[/÷]\s*{_FILLER}({_BARE_NUM})(?![.\d])",
)


def _find_equations(text: str) -> list[tuple]:
    """Return list of equations found in `text`, tagged by kind:

    - ("additive", lhs, a, op, b)        for  A = B ± C
    - ("ratio_pct", lhs_pct, a, b)       for  X% = A / B
    - ("unitless_ratio", lhs, a, b)      for  X = A / B
    """
    out: list[tuple] = []
    for m in _EQ_THREE_PATTERN.finditer(text):
        out.append(("additive", m.group(1), m.group(2), m.group(3), m.group(4)))
    for m in _EQ_THREE_CN_PATTERN.finditer(text):
        op = "-" if m.group(3) in _CN_MINUS_WORDS else "+"
        out.append(("additive", m.group(1), m.group(2), op, m.group(4)))
    for m in _RATIO_PCT_PATTERN.finditer(text):
        out.append(("ratio_pct", m.group(1), m.group(2), m.group(3)))
    for m in _UNITLESS_RATIO_PATTERN.finditer(text):
        out.append(("unitless_ratio", m.group(1), m.group(2), m.group(3)))
    return out


class NumericConsistencyCritic(CriticBase):
    """Reviews agent narratives for self-contradictory arithmetic."""

    CRITIC_TYPE = "numeric_consistency_critic"

    # Tolerance for declaring an equation "broken".
    REL_TOLERANCE = 0.05

    def review(
        self,
        judgments: list[JudgmentContract],
        context: dict | None = None,
    ) -> CriticResult:
        issues: list[CriticIssue] = []

        for j in judgments:
            issues.extend(self._scan_judgment(j))

        return CriticResult(
            critic_id=f"critic_numeric_{id(self)}",
            critic_type=self.CRITIC_TYPE,
            issues=issues,
            block_publish=False,  # Warn-only in v1
            overall_risk=self._overall_risk(issues),
        )

    def _scan_judgment(self, j: JudgmentContract) -> list[CriticIssue]:
        issues: list[CriticIssue] = []

        texts: list[tuple[str, str]] = []
        for i, obs in enumerate(j.observations):
            texts.append((f"obs[{i}]", obs.text))
        for i, inf in enumerate(j.inferences):
            texts.append((f"inf[{i}]", inf.text))

        for source, text in texts:
            for eq in _find_equations(text):
                kind = eq[0]
                if kind == "additive":
                    _, lhs, a, op, b = eq
                    result = _eval_equation_three(lhs, op, a, b)
                    if result is None or result[0]:
                        continue
                    _, lhs_v, rhs_v = result
                    expr = f"{lhs} = {a} {op} {b}"
                elif kind == "ratio_pct":
                    _, lhs_pct, a, b = eq
                    result = _eval_ratio_pct(lhs_pct, a, b)
                    if result is None or result[0]:
                        continue
                    _, lhs_v, rhs_v = result
                    expr = f"{lhs_pct}% = {a} / {b}"
                elif kind == "unitless_ratio":
                    _, lhs, a, b = eq
                    result = _eval_unitless_ratio(lhs, a, b)
                    if result is None or result[0]:
                        continue
                    _, lhs_v, rhs_v = result
                    expr = f"{lhs} = {a} / {b}"
                else:
                    continue

                off = abs(lhs_v - rhs_v) / max(abs(lhs_v), 1e-9)
                msg = (
                    f"Arithmetic contradiction in {j.agent_name} {source}: "
                    f"agent wrote `{expr}` but actual RHS ≈ {rhs_v:.4g}, "
                    f"not {lhs_v:.4g} (off by {off:.0%})"
                )
                issues.append(self._make_issue(
                    code="NUMERIC_BROKEN_EQUATION",
                    severity="warn",
                    message=msg,
                    judgment_ids=[j.judgment_id],
                    action="Recompute the equation in the narrative, or remove the explicit arithmetic claim",
                ))

        return issues
