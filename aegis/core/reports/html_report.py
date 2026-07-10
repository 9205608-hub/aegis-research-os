"""HTML report entry — thin alias for the v2 renderer.

History:
  - ``html_report_legacy.py`` (the original 2711-line Python HTML string
    builder) was deleted in Phase 0 of DESIGN_2.0 (2026-07). Every report
    since the v2 cutover has rendered through ``html_report_v2``, so the
    legacy branch and its ``AEGIS_LEGACY_REPORT`` escape hatch were dead
    code locked in place only by their own tests.
  - ``html_report_v2.py`` is the Claude Design template-injection renderer
    (feeds ``window.REPORT`` into ``web/report.jsx``).

This module stays as the stable import path for callers
(``auto_research.py``, ``replay_from_cache.py``).
"""
from __future__ import annotations

from .html_report_v2 import generate_html_report

__all__ = ["generate_html_report"]
