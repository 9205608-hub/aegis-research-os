"""Shared utilities for connector modules.

Consolidates parse-boundary helpers that were previously duplicated across
akshare_connector.py and openbb_connector.py.
"""
from __future__ import annotations

import math
from typing import Any


def safe_float(val: Any) -> float | None:
    """Coerce ``val`` to float, returning None for any non-finite or invalid value.

    Rejects None, empty string, NaN, and ±inf (in both Python-float and
    string forms like ``"inf"`` / ``"infinity"``). Used at every external
    data parse boundary so downstream DCF / JSON / React layers never see
    non-finite floats — ``Infinity`` is not legal JSON and ``NaN`` is not
    portable across our serializers (BUG-Y39 / BUG-Y40).
    """
    if val is None or val == "":
        return None
    s = str(val).lower().strip()
    if s in ("nan", "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"):
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return f
