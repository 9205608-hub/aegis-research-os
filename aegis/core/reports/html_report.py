"""HTML report entry — direct alias to v2 renderer.

The pre-2026-05 legacy Python HTML string assembler (html_report_legacy.py,
2711 lines) was removed; v2 (Claude Design template injection feeding
window.REPORT into web/report.jsx) is the only renderer.
"""
from __future__ import annotations

from .html_report_v2 import generate_html_report

__all__ = ["generate_html_report"]
