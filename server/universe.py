"""Ticker universe — static catalog for the search page.

Intentionally small: the search input is free-text, so the universe list
only drives autocomplete suggestions. A local CSV at
`data/universe.csv` overrides this default when present (one row per
ticker: `tck,ex,name,sector`).
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any


@dataclass
class UniverseEntry:
    tck: str       # ticker symbol ("NVDA", "301358")
    ex: str        # exchange code ("NASDAQ", "SZ", "SH")
    name: str      # display name (中英混合允许)
    sector: str    # free-text sector/industry
    px: float = 0  # optional static reference price (0 = fetch at run time)
    chg: float = 0 # optional static day change pct (0 = unknown)


# Seeded defaults. Covers the handful of tickers the demo report cache has
# plus major US + A-share names. Expand via data/universe.csv.
_DEFAULTS: list[UniverseEntry] = [
    # US Tech — have cached state
    UniverseEntry("NVDA", "NASDAQ", "NVIDIA 英伟达", "半导体 · AI 基础设施"),
    UniverseEntry("META", "NASDAQ", "Meta Platforms", "广告 · 社交"),
    UniverseEntry("AAPL", "NASDAQ", "Apple 苹果", "消费电子"),
    UniverseEntry("GOOG", "NASDAQ", "Alphabet 谷歌", "广告 · 云服务"),
    UniverseEntry("TSLA", "NASDAQ", "Tesla 特斯拉", "电动车"),
    UniverseEntry("MSFT", "NASDAQ", "Microsoft 微软", "云计算 · 企业软件"),
    UniverseEntry("AMZN", "NASDAQ", "Amazon 亚马逊", "电商 · 云服务"),
    # US chips / AI-adjacent
    UniverseEntry("AMD",  "NASDAQ", "AMD 超微半导体", "半导体"),
    UniverseEntry("AVGO", "NASDAQ", "Broadcom 博通", "半导体 · 网络"),
    UniverseEntry("ASML", "NASDAQ", "ASML 阿斯麦", "半导体设备"),
    UniverseEntry("ARM",  "NASDAQ", "Arm Holdings", "半导体 IP"),
    UniverseEntry("PLTR", "NASDAQ", "Palantir", "企业 AI"),
    # A-share
    UniverseEntry("301358", "SZ", "湖南裕能",  "磷酸铁锂正极 · 新能源"),
    UniverseEntry("600519", "SH", "贵州茅台",  "消费白酒"),
    UniverseEntry("300750", "SZ", "宁德时代",  "动力电池"),
    UniverseEntry("002415", "SZ", "海康威视",  "安防视频监控"),
    UniverseEntry("000858", "SZ", "五粮液",    "消费白酒"),
    UniverseEntry("600036", "SH", "招商银行",  "股份制银行"),
    UniverseEntry("601318", "SH", "中国平安",  "综合金融"),
]


def _load_csv_overrides(project_root: Path) -> list[UniverseEntry] | None:
    """Load optional `data/universe.csv`. Missing file → None."""
    csv_path = project_root / "data" / "universe.csv"
    if not csv_path.exists():
        return None

    out: list[UniverseEntry] = []
    with csv_path.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                out.append(UniverseEntry(
                    tck=row["tck"].strip(),
                    ex=row["ex"].strip(),
                    name=row["name"].strip(),
                    sector=row.get("sector", "").strip(),
                ))
            except KeyError:
                continue
    return out or None


def get_universe(project_root: Path) -> list[dict[str, Any]]:
    """Return the ticker universe as a list of dicts (JSON-ready)."""
    entries = _load_csv_overrides(project_root) or _DEFAULTS
    return [asdict(e) for e in entries]
