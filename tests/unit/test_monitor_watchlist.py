"""Watchlist 加载器单测 — Aegis 2.0 Phase 3 任务 A1.

覆盖：内置默认票池、真实 configs/watchlist.yaml 可解析、缺字段容错、
enabled_entries 过滤、坏 YAML / 缺文件不崩、坏值回退默认。绝不连网络。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aegis.core.monitor.watchlist import (
    DEFAULT_WATCHLIST_PATH,
    MonitorSwitches,
    Watchlist,
    WatchlistEntry,
    load_watchlist,
)

# 项目根 = 本文件上溯 tests/unit/ 两级。
REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_CONFIG = REPO_ROOT / "configs" / "watchlist.yaml"


# ── 内置默认票池 ─────────────────────────────────────────────────────
def test_missing_file_returns_default(tmp_path: Path) -> None:
    """文件不存在 → 回退内置默认票池（含 002669 / 600519，均 enabled）。"""
    wl = load_watchlist(tmp_path / "does_not_exist.yaml")
    assert isinstance(wl, Watchlist)
    assert wl.version == 1
    assert wl.daily_llm_budget_usd == 2.0
    assert wl.announcement_lookback_days == 90
    tickers = {e.ticker for e in wl.entries}
    assert {"002669", "600519"} <= tickers
    assert all(e.enabled for e in wl.entries)


def test_default_switches_values() -> None:
    """MonitorSwitches 默认：公告 / 预告开，价格偏离关。"""
    sw = MonitorSwitches()
    assert sw.announcements is True
    assert sw.forecasts is True
    assert sw.price_deviation is False


# ── 真实配置文件 ─────────────────────────────────────────────────────
def test_real_config_parses() -> None:
    """真实 configs/watchlist.yaml 能被解析，含三支目标票。"""
    assert REAL_CONFIG.exists(), f"缺配置文件：{REAL_CONFIG}"
    wl = load_watchlist(REAL_CONFIG)
    tickers = {e.ticker for e in wl.entries}
    assert {"002669", "600519", "301358"} <= tickers
    assert wl.version >= 1
    assert wl.daily_llm_budget_usd > 0
    assert wl.announcement_lookback_days > 0
    # price_deviation 默认应为关（需实时行情）。
    for e in wl.entries:
        assert e.monitors.price_deviation is False


def test_default_path_constant() -> None:
    assert DEFAULT_WATCHLIST_PATH == Path("configs/watchlist.yaml")


# ── enabled_entries 过滤 ─────────────────────────────────────────────
def test_enabled_entries_filters(tmp_path: Path) -> None:
    cfg = tmp_path / "wl.yaml"
    cfg.write_text(
        "tickers:\n"
        "  - ticker: '000001'\n"
        "    enabled: true\n"
        "  - ticker: '000002'\n"
        "    enabled: false\n"
        "  - ticker: '000003'\n",  # enabled 缺失 → 默认 true
        encoding="utf-8",
    )
    wl = load_watchlist(cfg)
    assert len(wl.entries) == 3
    enabled = {e.ticker for e in wl.enabled_entries()}
    assert enabled == {"000001", "000003"}


# ── 缺字段 / 容错 ────────────────────────────────────────────────────
def test_entry_missing_ticker_skipped(tmp_path: Path) -> None:
    cfg = tmp_path / "wl.yaml"
    cfg.write_text(
        "tickers:\n"
        "  - name: 无代码条目\n"       # 缺 ticker → 跳过
        "  - ticker: '600000'\n"
        "    name: 浦发银行\n",
        encoding="utf-8",
    )
    wl = load_watchlist(cfg)
    assert [e.ticker for e in wl.entries] == ["600000"]


def test_partial_monitors_take_defaults(tmp_path: Path) -> None:
    """monitors 子 dict 缺键 → 取 MonitorSwitches 默认。"""
    cfg = tmp_path / "wl.yaml"
    cfg.write_text(
        "tickers:\n"
        "  - ticker: '002669'\n"
        "    monitors:\n"
        "      price_deviation: true\n",  # 只给一个键
        encoding="utf-8",
    )
    wl = load_watchlist(cfg)
    e = wl.entries[0]
    assert e.monitors.price_deviation is True   # 显式给的
    assert e.monitors.announcements is True      # 缺 → 默认
    assert e.monitors.forecasts is True          # 缺 → 默认
    assert e.name == ""                          # 缺 name → 默认空串


def test_bad_global_values_fall_back(tmp_path: Path) -> None:
    """全局数值字段坏值 → 各自回退默认。"""
    cfg = tmp_path / "wl.yaml"
    cfg.write_text(
        "version: not_an_int\n"
        "daily_llm_budget_usd: abc\n"
        "announcement_lookback_days: []\n"
        "tickers:\n"
        "  - ticker: '002669'\n",
        encoding="utf-8",
    )
    wl = load_watchlist(cfg)
    assert wl.version == 1
    assert wl.daily_llm_budget_usd == 2.0
    assert wl.announcement_lookback_days == 90
    assert wl.entries[0].ticker == "002669"


def test_string_switch_values_coerced(tmp_path: Path) -> None:
    """monitors 开关的字符串写法（是/否、on/off）被宽松解析。"""
    cfg = tmp_path / "wl.yaml"
    cfg.write_text(
        "tickers:\n"
        "  - ticker: '002669'\n"
        "    monitors:\n"
        "      announcements: '否'\n"
        "      forecasts: off\n"
        "      price_deviation: 'yes'\n",
        encoding="utf-8",
    )
    wl = load_watchlist(cfg)
    e = wl.entries[0]
    assert e.monitors.announcements is False
    assert e.monitors.forecasts is False
    assert e.monitors.price_deviation is True


# ── 坏输入不崩 ───────────────────────────────────────────────────────
def test_malformed_yaml_returns_default(tmp_path: Path) -> None:
    cfg = tmp_path / "bad.yaml"
    cfg.write_text("tickers: [unclosed\n  - ::::\n", encoding="utf-8")
    wl = load_watchlist(cfg)   # 不 raise
    assert isinstance(wl, Watchlist)
    assert {e.ticker for e in wl.entries} >= {"002669", "600519"}


def test_top_level_not_mapping_returns_default(tmp_path: Path) -> None:
    cfg = tmp_path / "list.yaml"
    cfg.write_text("- just\n- a\n- list\n", encoding="utf-8")
    wl = load_watchlist(cfg)
    assert isinstance(wl, Watchlist)
    assert {e.ticker for e in wl.entries} >= {"002669", "600519"}


def test_empty_file_returns_default(tmp_path: Path) -> None:
    cfg = tmp_path / "empty.yaml"
    cfg.write_text("", encoding="utf-8")
    wl = load_watchlist(cfg)
    assert isinstance(wl, Watchlist)
    assert {e.ticker for e in wl.entries} >= {"002669", "600519"}


def test_tickers_not_a_list_returns_default(tmp_path: Path) -> None:
    """tickers 非列表 → 无有效条目 → 回退默认票池。"""
    cfg = tmp_path / "wl.yaml"
    cfg.write_text("tickers: 002669\n", encoding="utf-8")
    wl = load_watchlist(cfg)
    assert {e.ticker for e in wl.entries} >= {"002669", "600519"}


def test_string_path_accepted(tmp_path: Path) -> None:
    """path 传字符串也可（Path | str | None）。"""
    cfg = tmp_path / "wl.yaml"
    cfg.write_text("tickers:\n  - ticker: '002669'\n", encoding="utf-8")
    wl = load_watchlist(str(cfg))
    assert wl.entries[0].ticker == "002669"


def test_none_path_uses_default_constant(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """path=None → 用 DEFAULT_WATCHLIST_PATH；此处指向不存在处验证回退。"""
    import aegis.core.monitor.watchlist as mod

    monkeypatch.setattr(mod, "DEFAULT_WATCHLIST_PATH", tmp_path / "nope.yaml")
    wl = load_watchlist(None)
    assert {e.ticker for e in wl.entries} >= {"002669", "600519"}


def test_code_field_alias_for_ticker(tmp_path: Path) -> None:
    """条目用 code 代替 ticker 也能被接受。"""
    cfg = tmp_path / "wl.yaml"
    cfg.write_text("tickers:\n  - code: '600519'\n    name: 贵州茅台\n", encoding="utf-8")
    wl = load_watchlist(cfg)
    assert wl.entries[0].ticker == "600519"
    assert wl.entries[0].name == "贵州茅台"


def test_watchlist_entry_defaults() -> None:
    """WatchlistEntry 每票默认 monitors 独立（default_factory，非共享）。"""
    a = WatchlistEntry(ticker="002669")
    b = WatchlistEntry(ticker="600519")
    a.monitors.announcements = False
    assert b.monitors.announcements is True   # 不被 a 污染
