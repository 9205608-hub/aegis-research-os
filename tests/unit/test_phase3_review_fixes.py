"""Phase 3 对抗性审查确认 bug 的回归测试.

锁定 8 条经对抗性核验确认的修复，防止回归：

- #1/#5：复研瞬时失败（ok=False）**不消费**触发事件，次轮重试；
- #2：price_deviation-only 票首次观测即播种 anchor，打破死监控器死锁；
- #3：save_thesis_version 落 anchor_price → 90 天回看复盘不再恒跳过；
- #4：thesis 缺 entity_id 时 due_records 幂等检查与落盘同名（不重复生成 / 不撞车）；
- #7：--smoke 扫描落 smoke 沙箱，对生产水位线 / delta / 论点链零副作用。

全部注入假函数 + tmp_path 重定向落盘，绝不真连网络 / 真跑复研。
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import yaml

from aegis.core.acquisition.connectors.em_events_connector import (
    Announcement,
    RecentEvents,
)
from aegis.core.monitor.runner import UpdateRunResult
from aegis.core.monitor.scanner import scan_once
from aegis.core.monitor.watermark import WatermarkStore
from aegis.core.thesis.persistence import (
    build_thesis_contract,
    normalize_entity_id,
    save_thesis_version,
)


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

def _watchlist(tmp_path: Path, ticker: str, *, price_dev: bool = False,
               budget: float = 2.0) -> Path:
    data = {
        "version": 1,
        "daily_llm_budget_usd": budget,
        "announcement_lookback_days": 90,
        "tickers": [{
            "ticker": ticker, "name": ticker, "enabled": True,
            "monitors": {"announcements": True, "forecasts": True,
                         "price_deviation": price_dev},
        }],
    }
    p = tmp_path / "watchlist.yaml"
    p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return p


def _append_thesis(thesis_dir: Path, entity: str, version: int,
                   thesis: dict) -> None:
    thesis_dir.mkdir(parents=True, exist_ok=True)
    rec = {
        "version": version, "created_at": "2026-07-11T00:00:00",
        "run_id": f"run_{version}",
        "parent_version": (version - 1) if version > 1 else None,
        "thesis": thesis,
    }
    with (thesis_dir / f"{entity}.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _seed_thesis(thesis_dir: Path, ticker: str, *, price_monitor: bool = False):
    entity = normalize_entity_id(ticker)
    monitors = [{
        "description": "应收增速监控", "check_frequency": "quarterly",
        "data_source": "pit_store:receivables_vs_revenue",
    }]
    if price_monitor:
        monitors.append({
            "description": "股价偏离阈值", "check_frequency": "daily",
            "data_source": "market_price:price_deviation",
        })
    _append_thesis(thesis_dir, entity, 1, {
        "thesis_id": f"thesis_{entity}", "thesis_version": 1,
        "entity_id": entity, "core_thesis": "初始核心论点",
        "base_case_value": 10.0, "publishing_status": "published",
        "confidence_bucket": "medium", "must_monitor": monitors,
    })


def _events(anns=None, fcs=None):
    def _fn(ticker):
        return RecentEvents(stock_code=ticker, as_of="2026-07-11",
                            announcements=list(anns or []),
                            forecasts=list(fcs or []))
    return _fn


# ===========================================================================
# #1/#5：复研瞬时失败不消费触发事件 → 次轮重试
# ===========================================================================

def test_transient_update_failure_retries_next_scan(tmp_path):
    ticker = "002669"
    entity = normalize_entity_id(ticker)
    thesis_dir = tmp_path / "thesis"
    _seed_thesis(thesis_dir, ticker)
    wl = _watchlist(tmp_path, ticker)
    wm_path = tmp_path / "monitor" / "watermarks.json"

    calls: list = []

    def failing_update(t, trigger_zh, smoke, use_llm, thesis_dir_):
        calls.append(t)
        # 瞬时失败（如 LLM 超时）：ok=False，未写新版本，cost=0。
        return UpdateRunResult(ticker=t, ok=False, cost_usd=0.0,
                               error="LLM 超时（模拟）")

    ev = _events(anns=[Announcement(
        title="关于重大资产并购的提示性公告", date="2026-07-10")])

    common = dict(watchlist_path=wl, watermark_path=wm_path,
                  thesis_dir=str(thesis_dir), delta_dir=tmp_path / "deltas",
                  events_fn=ev, update_fn=failing_update)

    # 第一轮：触发 → 复研失败。
    r1 = scan_once(now_iso="2026-07-11T16:30:00", **common)
    assert calls == [ticker]
    o1 = r1.outcomes[0]
    assert o1.triggered and not o1.updated and o1.skipped_reason == "error"
    # 关键：失败后**没有**把并购公告并入 seen。
    wm = WatermarkStore(wm_path).get(ticker)
    assert not wm.seen_announcement_keys, "失败复研不该消费触发事件"

    # 第二轮：同一并购公告仍在 → 必须重新触发并重试复研。
    r2 = scan_once(now_iso="2026-07-12T16:30:00", **common)
    assert calls == [ticker, ticker], "次轮必须重试（事件未被吞）"
    assert r2.outcomes[0].triggered


# ===========================================================================
# #2：price_deviation-only 票首次观测播种 anchor → 次轮可触发（非死监控器）
# ===========================================================================

def test_price_deviation_anchor_seeded_first_scan(tmp_path):
    ticker = "600519"
    thesis_dir = tmp_path / "thesis"
    _seed_thesis(thesis_dir, ticker, price_monitor=True)
    wl = _watchlist(tmp_path, ticker, price_dev=True)
    wm_path = tmp_path / "monitor" / "watermarks.json"

    prices = {"n": 0}

    def quote_fn(t):
        prices["n"] += 1
        return 100.0 if prices["n"] == 1 else 130.0  # 第2轮 +30% > 20%

    calls: list = []

    def update_fn(t, trigger_zh, smoke, use_llm, thesis_dir_):
        calls.append(trigger_zh)
        entity = normalize_entity_id(t)
        _append_thesis(Path(thesis_dir_), entity, 2, {
            "thesis_id": f"thesis_{entity}", "thesis_version": 2,
            "entity_id": entity, "core_thesis": "v2", "base_case_value": 11.0,
            "publishing_status": "published", "confidence_bucket": "medium",
            "must_monitor": [{"description": "股价偏离阈值",
                              "check_frequency": "daily",
                              "data_source": "market_price:price_deviation"}],
        })
        return UpdateRunResult(ticker=t, ok=True, entity_id=entity,
                               cost_usd=0.01, thesis_version=2)

    common = dict(watchlist_path=wl, watermark_path=wm_path,
                  thesis_dir=str(thesis_dir), delta_dir=tmp_path / "deltas",
                  events_fn=_events(), quote_fn=quote_fn, update_fn=update_fn)

    # 第一轮：无公告事件、价格首见 → 不触发，但 anchor 被播种为 100。
    r1 = scan_once(now_iso="2026-07-11T16:30:00", **common)
    assert not r1.outcomes[0].triggered
    assert calls == []
    wm = WatermarkStore(wm_path).get(ticker)
    assert wm.anchor_price == 100.0, "首次观测应播种 anchor"

    # 第二轮：现价 130 相对 anchor 100 偏离 30% → price_deviation 触发。
    r2 = scan_once(now_iso="2026-07-12T16:30:00", **common)
    assert r2.outcomes[0].triggered and r2.outcomes[0].updated
    assert len(calls) == 1 and "偏离" in (calls[0] or "")


# ===========================================================================
# #3：save_thesis_version 落 anchor_price → 90 天回看复盘不再恒跳过
# ===========================================================================

def test_anchor_price_persisted_enables_postmortem(tmp_path, monkeypatch):
    from aegis.core.monitor import postmortem as pm_mod
    from aegis.core.monitor.postmortem import run_postmortems

    monkeypatch.setattr(pm_mod, "POSTMORTEM_DIR", tmp_path / "postmortems")

    ticker = "002669"
    thesis_dir = tmp_path / "thesis"
    created = date.today() - timedelta(days=100)  # review_date 已过期
    contract = build_thesis_contract(
        entity_id=ticker, run_id="run_20260401_090000_x",
        synthesized_thesis={"core_thesis": "康达论点", "my_variant": "差异化"},
        scenarios={"bear": 2.0, "base": 2.15, "bull": 13.6},
        created_at=created,
    )
    rec = save_thesis_version(
        ticker, contract, "run_20260401_090000_x", dir=str(thesis_dir),
        created_at=created, anchor_price=13.76,   # ← 修复：建仓价落进 record
    )
    assert rec["anchor_price"] == 13.76

    # 默认接线（不注入 price_lookup）——修复前因无锚恒生成 0 份。
    pms = run_postmortems(thesis_dir=str(thesis_dir), quote_fn=lambda t: 12.0)
    assert len(pms) == 1
    pm = pms[0]
    assert abs(pm.price_at_thesis - 13.76) < 1e-6
    assert abs(pm.total_return - (12.0 / 13.76 - 1.0)) < 1e-6


def test_save_without_anchor_price_unchanged(tmp_path):
    """向后兼容：不传 anchor_price 时 record 不含该键（旧行为逐字不变）。"""
    ticker = "301358"
    contract = build_thesis_contract(
        entity_id=ticker, run_id="run_20260701_090000_y",
        synthesized_thesis={"core_thesis": "裕能论点"},
        scenarios={"bear": 1.0, "base": 2.0, "bull": 3.0},
    )
    rec = save_thesis_version(ticker, contract, "run_20260701_090000_y",
                              dir=str(tmp_path / "thesis"))
    assert "anchor_price" not in rec


# ===========================================================================
# #4：thesis 缺 entity_id 时 due_records 幂等（同名检查=落盘）
# ===========================================================================

def test_postmortem_idempotent_when_entity_id_missing(tmp_path, monkeypatch):
    from aegis.core.monitor import postmortem as pm_mod

    monkeypatch.setattr(pm_mod, "POSTMORTEM_DIR", tmp_path / "postmortems")
    thesis_dir = tmp_path / "thesis"
    thesis_dir.mkdir(parents=True)
    past_review = (date.today() - timedelta(days=5)).isoformat()
    # 手工链：thesis **缺 entity_id**（坏链 / 旧 schema），文件名带 entity。
    rec = {
        "version": 1, "created_at": "2026-04-01T00:00:00", "run_id": "run_x",
        "parent_version": None,
        "thesis": {
            "thesis_id": "thesis_600519_sh", "thesis_version": 1,
            "core_thesis": "无 entity_id 的论点", "base_case_value": 1800.0,
            "publishing_status": "published", "confidence_bucket": "medium",
            "review_date": past_review,
            "must_monitor": [{"description": "x", "check_frequency": "quarterly",
                              "data_source": "watch_only"}],
        },
    }
    (thesis_dir / "600519_sh.jsonl").write_text(
        json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")

    r1 = pm_mod.run_postmortems(thesis_dir=str(thesis_dir),
                                quote_fn=lambda t: 1900.0,
                                price_lookup=lambda rec: 1800.0)
    assert len(r1) == 1
    # 第二轮必须幂等（同名文件已存在）——修复前 due_records 用 path.stem
    # 检查、落盘用 'unknown'，两者不一致导致重复生成。
    r2 = pm_mod.run_postmortems(thesis_dir=str(thesis_dir),
                                quote_fn=lambda t: 1900.0,
                                price_lookup=lambda rec: 1800.0)
    assert r2 == [], "第二轮应幂等无新增"
    files = list((tmp_path / "postmortems").glob("*.json"))
    assert len(files) == 1
    assert "unknown" not in files[0].name, "应按链名而非 unknown 落盘"


# ===========================================================================
# #7：--smoke 扫描落 smoke 沙箱（对生产零副作用）
# ===========================================================================

def test_smoke_cli_redirects_to_sandbox(monkeypatch):
    import scripts.scan_watchlist as cli

    captured: dict = {}

    def fake_scan_once(**kwargs):
        captured.update(kwargs)

        class _R:
            def to_markdown(self):
                return "# 冒烟"
        return _R()

    monkeypatch.setattr(cli.scanner_mod, "scan_once", fake_scan_once)
    rc = cli.main(["--smoke"])
    assert rc == 0
    # 三条落盘路径全部指向 smoke 沙箱，绝不碰生产。
    assert "smoke" in str(captured.get("watermark_path"))
    assert "smoke" in str(captured.get("delta_dir"))
    assert "smoke" in str(captured.get("thesis_dir"))
    assert captured.get("smoke") is True
    assert captured.get("use_llm") is False


def test_nonsmoke_cli_no_sandbox_redirect(monkeypatch):
    """非 smoke 不重定向（走生产默认目录）。"""
    import scripts.scan_watchlist as cli

    captured: dict = {}

    def fake_scan_once(**kwargs):
        captured.update(kwargs)

        class _R:
            def to_markdown(self):
                return "# 扫描"
        return _R()

    monkeypatch.setattr(cli.scanner_mod, "scan_once", fake_scan_once)
    cli.main(["--dry-run"])
    assert "watermark_path" not in captured or captured["watermark_path"] is None
    assert "delta_dir" not in captured or captured["delta_dir"] is None


# ===========================================================================
# #6：跨进程锁——已有扫描持锁时，另一轮非 dry_run 扫描跳过（不并发读改写）
# ===========================================================================

def test_scan_skips_when_lock_held(tmp_path):
    import fcntl
    import os as _os

    ticker = "002669"
    thesis_dir = tmp_path / "thesis"
    _seed_thesis(thesis_dir, ticker)
    wl = _watchlist(tmp_path, ticker)
    wm_path = tmp_path / "monitor" / "watermarks.json"
    monitor_root = wm_path.parent
    monitor_root.mkdir(parents=True, exist_ok=True)

    calls: list = []

    def update_fn(t, trigger_zh, smoke, use_llm, thesis_dir_):
        calls.append(t)
        return UpdateRunResult(ticker=t, ok=True, cost_usd=0.0, thesis_version=2)

    ev = _events(anns=[Announcement(title="重大合同订单公告", date="2026-07-10")])

    # 手动抢占同一把锁文件（模拟另一进程/launchd 正在扫描）。
    fd = _os.open(str(monitor_root / "scan.lock"),
                  _os.O_CREAT | _os.O_RDWR, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        report = scan_once(
            watchlist_path=wl, watermark_path=wm_path,
            thesis_dir=str(thesis_dir), delta_dir=tmp_path / "deltas",
            events_fn=ev, update_fn=update_fn, now_iso="2026-07-11T16:30:00",
        )
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        _os.close(fd)

    # 抢不到锁 → 本轮跳过：零扫描、零复研，未动水位线。
    assert report.tickers_scanned == 0
    assert calls == []
    assert not WatermarkStore(wm_path).get(ticker).seen_announcement_keys


def test_launchd_plist_valid_no_shell_wordsplit():
    """#8：plist 合法且用 WorkingDirectory + 直接解释器调用（无 bash -c 拼串）。"""
    import plistlib

    root = Path(__file__).resolve().parents[2]
    p = plistlib.load(
        (root / "configs" / "launchd" / "com.aegis.scan.plist").open("rb"))
    assert p["Label"] == "com.aegis.scan"
    assert p["StartCalendarInterval"]["Hour"] == 16
    assert p.get("WorkingDirectory")  # cwd 由 launchd 设置，不靠 shell cd
    argv = p["ProgramArguments"]
    # 直接给解释器 + 脚本，不经 bash -c（否则含空格路径会被词拆分）。
    assert argv[0].endswith("python")
    assert "bash" not in " ".join(argv)
    assert argv[1].endswith("scan_watchlist.py")


# ===========================================================================
# 复核附带修复：dry_run 对**未触发**票也不得写水位线（零副作用契约）
# ===========================================================================

def test_dry_run_no_watermark_side_effects(tmp_path):
    ticker = "002669"
    thesis_dir = tmp_path / "thesis"
    _seed_thesis(thesis_dir, ticker, price_monitor=True)
    wl = _watchlist(tmp_path, ticker, price_dev=True)
    wm_path = tmp_path / "monitor" / "watermarks.json"

    # 无事件 → 未触发；dry_run + price_deviation（会在 step3 播种 anchor）——
    # 修复前「无触发」分支排在 dry_run 早退前 → 落盘水位线（含 anchor）。
    r = scan_once(
        watchlist_path=wl, watermark_path=wm_path,
        thesis_dir=str(thesis_dir), delta_dir=tmp_path / "deltas",
        events_fn=_events(), quote_fn=lambda t: 50.0,
        dry_run=True, now_iso="2026-07-11T16:30:00",
    )
    assert not r.outcomes[0].triggered
    assert r.outcomes[0].skipped_reason == "no_change"
    # 零副作用：水位线库文件根本未被创建。
    assert not wm_path.exists(), "dry_run 不该写水位线（含未触发票 / anchor 播种）"

    # 触发态 dry_run 同样零副作用。
    from aegis.core.acquisition.connectors.em_events_connector import Announcement
    r2 = scan_once(
        watchlist_path=wl, watermark_path=wm_path,
        thesis_dir=str(thesis_dir), delta_dir=tmp_path / "deltas",
        events_fn=_events(anns=[Announcement(title="并购公告", date="2026-07-10")]),
        dry_run=True, now_iso="2026-07-11T16:30:00",
    )
    assert r2.outcomes[0].triggered and not r2.outcomes[0].updated
    assert not wm_path.exists(), "触发态 dry_run 也不该写水位线"
