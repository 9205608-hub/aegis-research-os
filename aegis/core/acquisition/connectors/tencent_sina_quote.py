"""A-share realtime quote fallback via Tencent and Sina Finance.

yfinance's .SS/.SZ support is unreliable under the user's Clash Verge proxy,
and akshare's push2.eastmoney endpoint is blocked entirely in that setup
(see HANDOFF 2026-04-18). As a last-resort fallback for current_price, hit
the two endpoints that reliably return:

  - https://qt.gtimg.cn/q=sh600089   — Tencent finance tick
  - https://hq.sinajs.cn/list=sh600089 — Sina finance tick (needs Referer)

Both are public, return GB-2312 encoded delimited strings, no key required,
no proxy needed (they bypass Clash Verge cleanly in practice).

Usage:
    q = fetch_cn_quote("600089")
    if q:
        price = q.current_price   # e.g. 27.68
        name  = q.name            # "特变电工"

Returns None on any failure — caller should keep falling through the chain.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import re
import urllib.request as _ur

logger = logging.getLogger(__name__)


@dataclass
class CNQuote:
    code: str            # "600089"
    name: str            # "特变电工"
    current_price: float
    prev_close: float
    open_price: float
    high: float
    low: float
    volume: int
    source: str          # "tencent" | "sina"
    market_cap: float = 0.0      # CNY, absolute (not 亿). Zero = unknown.
    shares_outstanding: float = 0.0  # Absolute share count. Zero = unknown.


def _prefix_for(code: str) -> str:
    """Map a bare 6-digit A-share code to the exchange prefix Tencent/Sina expect.

    6xxxxx → Shanghai (sh), 0xxxxx / 3xxxxx → Shenzhen (sz).
    Accepts already-prefixed inputs (sh600089, SH600089) as well.
    """
    c = code.strip().lower()
    for p in ("sh", "sz"):
        if c.startswith(p):
            return c  # already prefixed
    # Strip any .SS / .SZ yfinance suffix
    c = c.replace(".ss", "").replace(".sz", "")
    if not c.isdigit() or len(c) != 6:
        raise ValueError(f"not a bare A-share code: {code!r}")
    if c.startswith("6"):
        return f"sh{c}"
    if c.startswith(("0", "3")):
        return f"sz{c}"
    raise ValueError(f"unknown A-share prefix for {code!r}")


def _fetch_tencent(prefixed: str, timeout: float = 6.0) -> CNQuote | None:
    """v_sh600089="1~特变电工~600089~27.68~27.49~27.60~1929719~..."""
    url = f"https://qt.gtimg.cn/q={prefixed}"
    try:
        req = _ur.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with _ur.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("gbk", errors="replace")
    except Exception as e:
        logger.debug(f"tencent quote fetch failed for {prefixed}: {type(e).__name__}: {e}")
        return None

    m = re.search(r'="([^"]+)"', raw)
    if not m:
        return None
    parts = m.group(1).split("~")
    # Layout (common prefix): 1~name~code~price~prev_close~open~volume(hands)~...
    # Observed 2026-04-23 for sh600089:
    #   parts[33] = day high, parts[34] = day low
    #   parts[44] = total market cap (单位：亿 ¥), parts[45] = circulating市值
    # Values × 1e8 to convert 亿 → absolute CNY.
    if len(parts) < 7:
        return None
    try:
        def _float_or_zero(v: str) -> float:
            try:
                return float(v)
            except (ValueError, TypeError):
                return 0.0

        total_cap_yi = _float_or_zero(parts[45]) if len(parts) > 45 else 0.0
        # Circulating cap (parts[44]) is a proxy if total is missing
        if total_cap_yi <= 0 and len(parts) > 44:
            total_cap_yi = _float_or_zero(parts[44])
        market_cap = total_cap_yi * 1e8  # 亿 → CNY absolute

        # Tencent also exposes total shares (position 37/38 depending on field
        # count). We derive shares from market_cap / price instead — same
        # source, always internally consistent.
        price = float(parts[3])
        shares = market_cap / price if price > 0 and market_cap > 0 else 0.0

        return CNQuote(
            code=parts[2],
            name=parts[1],
            current_price=price,
            prev_close=float(parts[4]),
            open_price=float(parts[5]),
            high=float(parts[33]) if len(parts) > 33 else 0.0,
            low=float(parts[34]) if len(parts) > 34 else 0.0,
            volume=int(float(parts[6])) * 100,  # 手 → 股
            source="tencent",
            market_cap=market_cap,
            shares_outstanding=shares,
        )
    except (ValueError, IndexError):
        return None


def _fetch_sina(prefixed: str, timeout: float = 6.0) -> CNQuote | None:
    """var hq_str_sh600089="特变电工,27.600,27.490,27.680,28.270,27.350,...","""
    url = f"https://hq.sinajs.cn/list={prefixed}"
    try:
        req = _ur.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://finance.sina.com.cn",
        })
        with _ur.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("gbk", errors="replace")
    except Exception as e:
        logger.debug(f"sina quote fetch failed for {prefixed}: {type(e).__name__}: {e}")
        return None

    m = re.search(r'="([^"]+)"', raw)
    if not m:
        return None
    fields = m.group(1).split(",")
    # Layout: name, open, prev_close, price, high, low, ...
    if len(fields) < 10:
        return None
    try:
        return CNQuote(
            code=prefixed[2:],  # strip sh/sz
            name=fields[0],
            current_price=float(fields[3]),
            prev_close=float(fields[2]),
            open_price=float(fields[1]),
            high=float(fields[4]),
            low=float(fields[5]),
            volume=int(float(fields[8])) if fields[8] else 0,
            source="sina",
        )
    except (ValueError, IndexError):
        return None


def fetch_cn_quote(code: str) -> CNQuote | None:
    """Fetch A-share real-time quote, Tencent first then Sina.

    Returns None only when BOTH sources fail or the code is malformed.
    Empty/zero current_price is treated as failure and falls through.
    """
    try:
        prefixed = _prefix_for(code)
    except ValueError as e:
        logger.debug(f"fetch_cn_quote: bad code: {e}")
        return None

    for fetch in (_fetch_tencent, _fetch_sina):
        q = fetch(prefixed)
        if q is not None and q.current_price > 0:
            return q

    return None
