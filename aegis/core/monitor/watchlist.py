"""Watchlist 配置加载 — Aegis 2.0 Phase 3 任务 A1.

事件循环的入口清单：从 ``configs/watchlist.yaml`` 读出「监控哪些票、每票
开哪些监控器、全局预算与公告回溯窗口」。

设计取舍（红线 10）：纯标准库 + 已装 ``pyyaml``；配置即数据（dataclass），
无状态机。所有解析路径**永不 raise 到调用方**——文件缺失回退内置默认票池，
单条目缺字段 / 坏值一律容错取默认，坏 YAML 记日志后回退默认。

型号与阈值不在此定义——监控器开关只是布尔位，真正的可执行型号走
:mod:`aegis.core.thesis.monitorables` 的封闭目录（红线 6）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

__all__ = [
    "MonitorSwitches",
    "WatchlistEntry",
    "Watchlist",
    "DEFAULT_WATCHLIST_PATH",
    "load_watchlist",
]

#: 默认配置文件路径（相对项目根）。
DEFAULT_WATCHLIST_PATH = Path("configs/watchlist.yaml")

# 全局字段的兜底默认值（坏值回退时复用，单一事实源）。
_DEFAULT_VERSION = 1
_DEFAULT_DAILY_BUDGET_USD = 2.0
_DEFAULT_LOOKBACK_DAYS = 90


@dataclass
class MonitorSwitches:
    """单票的监控器开关位。"""

    announcements: bool = True
    forecasts: bool = True
    price_deviation: bool = False   # 需要实时行情，默认关


@dataclass
class WatchlistEntry:
    """票池中的一支标的。"""

    ticker: str
    name: str = ""
    enabled: bool = True
    monitors: MonitorSwitches = field(default_factory=MonitorSwitches)


@dataclass
class Watchlist:
    """整份监控票池配置。"""

    version: int = _DEFAULT_VERSION
    daily_llm_budget_usd: float = _DEFAULT_DAILY_BUDGET_USD
    announcement_lookback_days: int = _DEFAULT_LOOKBACK_DAYS
    entries: list[WatchlistEntry] = field(default_factory=list)

    def enabled_entries(self) -> list[WatchlistEntry]:
        """仅返回 ``enabled=True`` 的条目（禁用票不参与本轮扫描）。"""
        return [e for e in self.entries if e.enabled]


# ── 内置默认票池（文件缺失 / 坏 YAML 时回退，保证监控回路不空转）─────────
def _default_watchlist() -> Watchlist:
    """内置默认票池（至少含康达新材、贵州茅台，均 enabled）。"""
    return Watchlist(
        entries=[
            WatchlistEntry(ticker="002669", name="康达新材"),
            WatchlistEntry(ticker="600519", name="贵州茅台"),
        ]
    )


# ── 容错取值原语（永不 raise）──────────────────────────────────────────
def _as_bool(val: Any, default: bool) -> bool:
    """宽松布尔解析：真/假的常见字符串写法都认，坏值回退默认。"""
    if isinstance(val, bool):
        return val
    if val is None:
        return default
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return bool(val)
    s = str(val).strip().lower()
    if s in ("true", "1", "yes", "y", "on", "是", "开"):
        return True
    if s in ("false", "0", "no", "n", "off", "否", "关"):
        return False
    return default


def _as_int(val: Any, default: int) -> int:
    try:
        if isinstance(val, bool):  # bool 是 int 子类，单独挡掉
            return default
        return int(val)
    except (TypeError, ValueError):
        return default


def _as_float(val: Any, default: float) -> float:
    try:
        if isinstance(val, bool):
            return default
        return float(val)
    except (TypeError, ValueError):
        return default


def _as_str(val: Any, default: str = "") -> str:
    if val is None:
        return default
    return str(val).strip()


def _parse_switches(raw: Any) -> MonitorSwitches:
    """monitors 子 dict → MonitorSwitches；缺键 / 非 dict 取型号默认。"""
    default = MonitorSwitches()
    if not isinstance(raw, dict):
        return default
    return MonitorSwitches(
        announcements=_as_bool(raw.get("announcements"), default.announcements),
        forecasts=_as_bool(raw.get("forecasts"), default.forecasts),
        price_deviation=_as_bool(
            raw.get("price_deviation"), default.price_deviation
        ),
    )


def _parse_entry(raw: Any) -> WatchlistEntry | None:
    """单条目 dict → WatchlistEntry；ticker 缺失返回 None（调用方跳过）。"""
    if not isinstance(raw, dict):
        return None
    ticker = _as_str(raw.get("ticker") or raw.get("code"))
    if not ticker:
        return None
    return WatchlistEntry(
        ticker=ticker,
        name=_as_str(raw.get("name")),
        enabled=_as_bool(raw.get("enabled"), True),
        monitors=_parse_switches(raw.get("monitors")),
    )


def load_watchlist(path: Path | str | None = None) -> Watchlist:
    """读 YAML → :class:`Watchlist`。文件缺失时回退内置默认票池（不 raise）。

    容错策略：
      - 文件不存在 / 读不了 / 坏 YAML / 顶层非映射 → 内置默认票池；
      - 全局数值字段坏值 → 回退各自默认；
      - ``tickers`` 列表里 ticker 缺失的条目跳过，monitors 子 dict 缺键
        取 :class:`MonitorSwitches` 默认。
    """
    p = Path(path) if path is not None else DEFAULT_WATCHLIST_PATH

    if not p.exists():
        logger.info("watchlist 配置不存在（%s），回退内置默认票池", p)
        return _default_watchlist()

    try:
        raw_text = p.read_text(encoding="utf-8")
        data = yaml.safe_load(raw_text)
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("watchlist 配置读取 / 解析失败（%s）：%s，回退默认票池", p, exc)
        return _default_watchlist()

    if not isinstance(data, dict):
        logger.warning("watchlist 配置顶层非映射（%s），回退默认票池", p)
        return _default_watchlist()

    entries: list[WatchlistEntry] = []
    raw_tickers = data.get("tickers")
    if isinstance(raw_tickers, list):
        for raw_entry in raw_tickers:
            entry = _parse_entry(raw_entry)
            if entry is None:
                logger.debug("watchlist 跳过缺 ticker 的条目：%r", raw_entry)
                continue
            entries.append(entry)
    elif raw_tickers is not None:
        logger.warning("watchlist 的 tickers 字段非列表（%s），忽略", p)

    if not entries:
        logger.info("watchlist 配置未含有效条目（%s），回退内置默认票池", p)
        return _default_watchlist()

    return Watchlist(
        version=_as_int(data.get("version"), _DEFAULT_VERSION),
        daily_llm_budget_usd=_as_float(
            data.get("daily_llm_budget_usd"), _DEFAULT_DAILY_BUDGET_USD
        ),
        announcement_lookback_days=_as_int(
            data.get("announcement_lookback_days"), _DEFAULT_LOOKBACK_DAYS
        ),
        entries=entries,
    )
