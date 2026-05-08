"""HTML report entry — thin shim routing to v2 by default.

History:
  - `html_report_legacy.py` is the original 2700-line Python HTML
    string builder. Kept for fallback and A/B comparison.
  - `html_report_v2.py` is the Claude Design template-injection renderer
    (feeds `window.REPORT` into `web/report.jsx`).

Set `AEGIS_LEGACY_REPORT=1` to route back to the legacy renderer
without editing code. Useful when debugging new-template regressions.
The shim filters v2-only kwargs so the legacy function doesn't reject them.
"""
from __future__ import annotations

import os
from typing import Any

# Extra kwargs the v2 renderer accepts but the legacy renderer doesn't know about.
# The shim drops these when routing to legacy so callers can pass them freely.
_V2_ONLY_KWARGS = {"period", "dcf_output", "pipeline_duration", "model_name", "macro_snapshot",
                   "entity_name_clean", "risk_warning_prefix"}


def generate_html_report(*args: Any, **kwargs: Any) -> str:
    if os.environ.get("AEGIS_LEGACY_REPORT") == "1":
        from .html_report_legacy import generate_html_report as _legacy
        filtered = {k: v for k, v in kwargs.items() if k not in _V2_ONLY_KWARGS}
        return _legacy(*args, **filtered)
    from .html_report_v2 import generate_html_report as _v2
    return _v2(*args, **kwargs)


def _build_valuation_chart_js(*args: Any, **kwargs: Any) -> str:
    from .html_report_legacy import _build_valuation_chart_js as _legacy

    return _legacy(*args, **kwargs)


def _build_insider_trading_card(*args: Any, **kwargs: Any) -> str:
    from .html_report_legacy import _build_insider_trading_card as _legacy

    return _legacy(*args, **kwargs)


def _build_news_sentiment_card(*args: Any, **kwargs: Any) -> str:
    from .html_report_legacy import _build_news_sentiment_card as _legacy

    return _legacy(*args, **kwargs)


__all__ = [
    "generate_html_report",
    "_build_valuation_chart_js",
    "_build_insider_trading_card",
    "_build_news_sentiment_card",
]
