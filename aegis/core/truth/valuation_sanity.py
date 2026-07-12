"""Valuation sanity — two-sided DCF-vs-price magnitude check.

AUDIT 2026-07-12 (Grok 20-audit sweep, HANDOFF「大整改」): a DCF base of
10× market price (宁德时代 ¥4000+ vs ¥349) passed every existing check,
because the consistency logging only fired on base < 0.20×price and the
terminal-value / capex gates only key on high-capex / negative-FCF
profiles. This module is the single shared rule used by the orchestrator
(scenario assembly), the thesis synthesizer (strict scrub + magnitude
degradation), and the publish gate (valuation_sanity_gate):

    base/price outside [1/ratio, ratio]  →  mismatch

On mismatch the system must treat the situation as *model bug > market
bug* until DCF inputs (share count / net debt / revenue path / terminal
assumptions) are re-audited: no price targets, no up/downside %, narrative
degraded to direction-only. The DCF figures themselves remain visible in
the scenario section as a model diagnostic — honesty over suppression.
"""

from __future__ import annotations

from typing import Any

# Beyond this ratio the model and the market are not in the same magnitude
# regime. 3× is deliberately loose: a genuine deep-value / deep-short call
# can defend 2-3×, but nothing research-grade lives beyond it.
MISMATCH_RATIO = 3.0


def check_valuation_sanity(
    base_value: Any,
    market_price: Any,
    ratio_threshold: float = MISMATCH_RATIO,
) -> dict[str, Any] | None:
    """Return a sanity verdict dict, or None when inputs are unusable.

    Verdict keys: ``mismatch`` (bool), ``ratio`` (base/price, 0.0 when
    base <= 0), ``base_value``, ``market_price``, ``ratio_threshold``.

    ``base_value <= 0`` with a positive market price is also a mismatch —
    a non-positive per-share DCF cannot anchor magnitude conclusions any
    more than a 10× one can.
    """
    if not isinstance(base_value, (int, float)) or not isinstance(market_price, (int, float)):
        return None
    if market_price <= 0:
        return None
    if base_value <= 0:
        return {
            "mismatch": True,
            "ratio": 0.0,
            "base_value": float(base_value),
            "market_price": float(market_price),
            "ratio_threshold": float(ratio_threshold),
        }
    ratio = float(base_value) / float(market_price)
    return {
        "mismatch": ratio > ratio_threshold or ratio < 1.0 / ratio_threshold,
        "ratio": ratio,
        "base_value": float(base_value),
        "market_price": float(market_price),
        "ratio_threshold": float(ratio_threshold),
    }
