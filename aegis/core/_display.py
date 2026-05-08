"""Shared currency / unit formatting helpers.

Refactor 2 (会话5 续 2) unified rendering layers via `meta_facts["__display"]`.
BUG-A22 (会话6) fixed chief_analyst LLM user_message formatting.
BUG-A26 (会话6 续) extends the same helpers to specialist agents'
`_build_user_message` so A-share entities don't get `$X.XB` inputs.

Lives at `aegis/core/_display.py` (leading underscore = internal utility)
because both `agents/` and `chief_analyst/` need it. Originally lived in
`chief_analyst/preamble.py`; moved here once a second consumer appeared.
The chief_analyst version re-exports from this module to keep the old
import path working — feel free to delete that re-export once everything
imports from here.
"""

from __future__ import annotations

from typing import Any


# Default display context — used as fallback when meta_facts has no
# __display block (legacy cache, US fallback path, mock path, etc).
_DEFAULT_DISPLAY = {
    "symbol": "$", "scale": 1e9, "unit": "B",
    "big_scale": 1e9, "big_unit": "B", "currency": "USD",
}


def resolve_display(meta_facts: dict[str, Any] | None) -> dict[str, Any]:
    """Single source of truth for currency / unit formatting in LLM
    user_messages and log lines.
    """
    if not meta_facts:
        return dict(_DEFAULT_DISPLAY)
    disp = meta_facts.get("__display") or {}
    out = dict(_DEFAULT_DISPLAY)
    out.update({k: v for k, v in disp.items() if v is not None})
    return out


def fmt_money_big(val: float, disp: dict[str, Any]) -> str:
    """Format a large monetary value like '¥53.0亿' / '$5.3B' / '¥1.2万亿' / '$2.5T'.

    BUG-Y16 (2026-05-06): previously this ALWAYS used `big_scale` / `big_unit`
    (`万亿` / `T`), so an A-share company with ¥65亿 revenue rendered as
    `¥0.0万亿` (rounded to zero). The intended behaviour: when the value
    crosses the `big_scale` threshold, switch from `scale`+`unit` to
    `big_scale`+`big_unit`. This keeps small-magnitude values (¥X亿 / $XB)
    legible while still gracefully escalating to `万亿` / `T` for the rare
    multi-trillion case (Apple market cap, central bank balance sheets).

    Falls back gracefully when `disp` is missing fields (legacy callers
    that only set `scale`/`unit`): treat `big_scale` as identical to
    `scale` so the value still scales sensibly.
    """
    if val is None:
        return ""
    scale = disp.get("scale") or 1e9
    unit = disp.get("unit") or "B"
    big_scale = disp.get("big_scale") or scale
    big_unit = disp.get("big_unit") or unit
    symbol = disp.get("symbol") or "$"
    if abs(val) >= big_scale and big_scale > scale:
        return f"{symbol}{val / big_scale:.1f}{big_unit}"
    return f"{symbol}{val / scale:.1f}{unit}"


def fmt_money_small(val: float, disp: dict[str, Any]) -> str:
    """Format a per-share / small monetary value like '¥2.66' / '$2.66'."""
    if val is None:
        return ""
    return f"{disp['symbol']}{val:.2f}"
