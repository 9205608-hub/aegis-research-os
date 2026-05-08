"""Scan `demos/` for completed reports and extract card-ready metadata.

Every v2 report embeds `window.REPORT = {...JSON...}` in an inline
`<script>` tag. We grep that out and reduce it to the fields the search
page's "近期研报" grid needs.

If a report predates v2 (legacy HTML renderer) the JSON isn't there;
such files are skipped silently so the card list stays clean.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

# Matches the opening brace through the balanced closing brace + semicolon.
# We rely on JSON not containing an unescaped `};</script>` — true for our
# emitter (html_report_v2.render_report_html).
_REPORT_JSON_RE = re.compile(
    r"window\.REPORT\s*=\s*(\{.*?\});\s*</script>",
    re.DOTALL,
)

_FILENAME_RE = re.compile(r"^(?P<tck>[\w]+)_(?P<period>fy\d{4})_auto_report\.html$", re.IGNORECASE)


@dataclass
class RecentCard:
    tck: str
    ex: str
    name: str
    fy: str
    when: str       # relative time string: "2 小时前", "昨天", "4/16"
    rating: str     # 买入/持有/回避 …
    verdict: str    # tone class: "buy" | "hold" | "sell"
    thesis: str     # short headline / thesis snippet (under 180 chars)
    px: float
    target: float
    file: str       # relative URL path: "report/{id}" — resolved by app.py
    market: str     # "US" | "CN" — drives local --up/--down color swap


def _parse_report_json(html: str) -> dict[str, Any] | None:
    m = _REPORT_JSON_RE.search(html)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def _relative_time(mtime: float, now: float) -> str:
    """Render a file mtime as a short Chinese relative-time string."""
    import datetime as _dt
    delta = now - mtime
    if delta < 3600:
        return f"{int(delta // 60)} 分钟前"
    if delta < 3600 * 24:
        return f"{int(delta // 3600)} 小时前"
    if delta < 3600 * 48:
        return "昨天"
    # Older than 2 days — use absolute M/D
    return _dt.datetime.fromtimestamp(mtime).strftime("%-m/%-d")


def _tone_to_verdict(tone: str) -> str:
    """Map rating.tone from REPORT dict to the CSS class used by recent-card."""
    return {
        "buy": "buy",
        "hold": "hold",
        "avoid": "sell",
    }.get(tone, "hold")


def scan_demos(demos_dir: Path, limit: int = 12) -> list[dict[str, Any]]:
    """Return recent reports sorted newest-first as JSON-ready dicts."""
    import time

    if not demos_dir.exists():
        return []

    now = time.time()
    cards: list[tuple[float, RecentCard]] = []

    for f in demos_dir.glob("*_auto_report.html"):
        m = _FILENAME_RE.match(f.name)
        if not m:
            continue

        try:
            data = _parse_report_json(f.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if not data:
            # Legacy (pre-v2) report — skip
            continue

        # Prefer the filename's ticker (clean symbol like "META") over the
        # REPORT.code field (which may contain a messy entity_id like
        # "meta_platforms"). Fall back to code only if filename is ambiguous.
        tck = m.group("tck").upper() or data.get("code") or ""
        card = RecentCard(
            tck=tck,
            ex=data.get("exchange") or "",
            name=data.get("company") or tck,
            fy=m.group("period").upper(),
            when=_relative_time(f.stat().st_mtime, now),
            rating=(data.get("rating") or {}).get("word", "—"),
            verdict=_tone_to_verdict((data.get("rating") or {}).get("tone", "hold")),
            thesis=(data.get("headline") or data.get("lede") or "")[:200],
            px=(data.get("price") or {}).get("last", 0) or 0,
            target=(data.get("rating") or {}).get("target", 0) or 0,
            file=f"/report/{f.stem}",
            market=(data.get("price") or {}).get("market", "US") or "US",
        )
        cards.append((f.stat().st_mtime, card))

    # Newest first, cap at `limit`
    cards.sort(key=lambda t: t[0], reverse=True)
    return [asdict(c) for _, c in cards[:limit]]


def read_report_html(demos_dir: Path, slug: str) -> str | None:
    """Read a single report HTML by slug (filename without extension)."""
    path = demos_dir / f"{slug}.html"
    if not path.exists() or not path.is_file():
        return None
    # Defensive: ensure the slug doesn't traverse out of demos/
    resolved = path.resolve()
    if not str(resolved).startswith(str(demos_dir.resolve())):
        return None
    return resolved.read_text(encoding="utf-8", errors="replace")
