"""Aegis 2.0 Phase 3 任务 B2 — scanner 一轮扫描编排回归测试.

全部注入假函数（events_fn / triggers_fn / update_fn），tmp_path 重定向所有落盘
目录，**绝不真连网络、绝不真跑复研**。锁定的行为：

① 无新事件 → no_change、水位线更新、无 delta 文件；
② 有新公告 → 调 update_fn、生成 delta .md/.json、budget 被 charge、水位线重锚；
③ 幂等：同批事件第二轮 → no_change（水位线已见）、update_fn 不再被调；
④ 预算耗尽 → budget_exhausted、不调 update_fn；
⑤ dry_run → 不调 update_fn、零副作用（不写水位线 / delta）；
⑥ 无 thesis → no_thesis、但水位线仍更新；
⑦ 单票 events_fn 抛异常 → 该票 error、其他票正常、整轮不崩；
⑧ ScanReport.to_markdown / to_dict 中文不崩。

触发判定用一个**水位线感知**的假 triggers_fn（复用真实 diff_new），因此幂等性
由「扫描器每轮 merge_seen」真实驱动，而非依赖尚未落地的 B1 triggers 模块。
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from aegis.core.acquisition.connectors.em_events_connector import (
    Announcement,
    EarningsForecast,
    RecentEvents,
)
from aegis.core.monitor.budget import DailyBudget
from aegis.core.monitor.runner import UpdateRunResult
from aegis.core.monitor.scanner import ScanReport, TickerScanOutcome, scan_once
from aegis.core.monitor.watermark import WatermarkStore, diff_new
from aegis.core.thesis.persistence import normalize_entity_id


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

def _write_watchlist(tmp_path: Path, tickers, *, budget: float = 2.0,
                     price_dev: bool = False) -> Path:
    data = {
        "version": 1,
        "daily_llm_budget_usd": budget,
        "announcement_lookback_days": 90,
        "tickers": [
            {
                "ticker": t, "name": t, "enabled": True,
                "monitors": {
                    "announcements": True, "forecasts": True,
                    "price_deviation": price_dev,
                },
            }
            for t in tickers
        ],
    }
    p = tmp_path / "watchlist.yaml"
    p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return p


def _append_thesis(thesis_dir: Path, entity: str, version: int,
                   thesis: dict) -> None:
    thesis_dir.mkdir(parents=True, exist_ok=True)
    rec = {
        "version": version,
        "created_at": "2026-07-11T00:00:00",
        "run_id": f"run_{version}",
        "parent_version": (version - 1) if version > 1 else None,
        "thesis": thesis,
    }
    with (thesis_dir / f"{entity}.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _seed_thesis_v1(thesis_dir: Path, ticker: str) -> None:
    entity = normalize_entity_id(ticker)
    _append_thesis(thesis_dir, entity, 1, {
        "thesis_id": f"thesis_{entity}", "thesis_version": 1,
        "entity_id": entity, "core_thesis": "初始核心论点",
        "base_case_value": 10.0, "publishing_status": "published",
        "confidence_bucket": "medium",
        "must_monitor": [{
            "description": "应收增速监控", "check_frequency": "quarterly",
            "data_source": "pit_store:receivables_vs_revenue",
        }],
    })


def _events_fn(anns=None, fcs=None):
    def _fn(ticker: str):
        return RecentEvents(
            stock_code=ticker, as_of="2026-07-11",
            announcements=list(anns or []), forecasts=list(fcs or []),
        )
    return _fn


class _FakeTrigger:
    def __init__(self, kind, model_id, reason_zh, detail=""):
        self.kind = kind
        self.model_id = model_id
        self.reason_zh = reason_zh
        self.detail = detail


def _triggers_fn(*, entry, thesis_record, events, watermark,
                 current_price=None):
    """水位线感知：只对「新增」公告 / 预告产触发（→ 天然幂等）。"""
    anns = list(getattr(events, "announcements", []) or []) if events else []
    fcs = list(getattr(events, "forecasts", []) or []) if events else []
    new_a, new_f = diff_new(watermark, anns, fcs)
    out = []
    for a in new_a:
        out.append(_FakeTrigger(
            "announcement", "announcement_keyword",
            f"新公告：{a.title}", a.date))
    for f in new_f:
        out.append(_FakeTrigger(
            "forecast", "forecast_vs_consensus",
            f"新预告：{f.report_period}", f.notice_date))
    return out


def _make_update_fn(cost: float = 0.02):
    """假复研：读链自增一版写回 thesis_dir，返回 ok。记录被调次数。"""
    calls: list[tuple[str, str | None]] = []

    def _fn(ticker, trigger_zh, smoke, use_llm, thesis_dir):
        calls.append((ticker, trigger_zh))
        entity = normalize_entity_id(ticker)
        from aegis.core.thesis.persistence import history
        recs = history(ticker, dir=thesis_dir)
        new_version = (recs[-1]["version"] + 1) if recs else 1
        _append_thesis(Path(thesis_dir), entity, new_version, {
            "thesis_id": f"thesis_{entity}", "thesis_version": new_version,
            "entity_id": entity,
            "core_thesis": f"复研后的核心论点 v{new_version}",
            "base_case_value": 10.0 + new_version,
            "publishing_status": "published", "confidence_bucket": "high",
            "must_monitor": [{
                "description": "应收增速监控", "check_frequency": "quarterly",
                "data_source": "pit_store:receivables_vs_revenue",
            }],
        })
        return UpdateRunResult(
            ticker=ticker, ok=True, run_id=f"run_{new_version}",
            entity_id=entity, cost_usd=cost, thesis_version=new_version,
        )

    _fn.calls = calls  # type: ignore[attr-defined]
    return _fn


def _paths(tmp_path: Path):
    return {
        "watermark_path": tmp_path / "monitor" / "watermarks.json",
        "thesis_dir": tmp_path / "thesis",
        "delta_dir": tmp_path / "deltas",
        "spend_dir": tmp_path / "monitor" / "spend",
    }


# ---------------------------------------------------------------------------
# ① 无新事件 → no_change
# ---------------------------------------------------------------------------

def test_no_new_events_no_change(tmp_path):
    wl = _write_watchlist(tmp_path, ["002669"])
    p = _paths(tmp_path)
    update_fn = _make_update_fn()

    report = scan_once(
        watchlist_path=wl, watermark_path=p["watermark_path"],
        thesis_dir=str(p["thesis_dir"]), delta_dir=p["delta_dir"],
        events_fn=_events_fn(anns=[]), triggers_fn=_triggers_fn,
        update_fn=update_fn, now_iso="2026-07-11T09:00:00",
    )

    assert report.tickers_scanned == 1
    assert report.tickers_triggered == 0
    assert report.tickers_updated == 0
    o = report.outcomes[0]
    assert o.triggered is False and o.updated is False
    assert o.skipped_reason == "no_change"
    assert update_fn.calls == []
    # 水位线已更新（last_scan_at）。
    store = WatermarkStore(p["watermark_path"])
    assert store.get("002669").last_scan_at == "2026-07-11T09:00:00"
    # 无 delta 文件。
    assert not p["delta_dir"].exists() or list(p["delta_dir"].glob("*")) == []


# ---------------------------------------------------------------------------
# ② 有新公告 → 复研 + delta + 计费 + 重锚
# ---------------------------------------------------------------------------

def test_new_announcement_triggers_update(tmp_path):
    wl = _write_watchlist(tmp_path, ["002669"])
    p = _paths(tmp_path)
    _seed_thesis_v1(p["thesis_dir"], "002669")
    update_fn = _make_update_fn(cost=0.02)
    ann = Announcement(title="关于对外投资的公告", date="2026-07-05",
                       category="对外投资")

    report = scan_once(
        watchlist_path=wl, watermark_path=p["watermark_path"],
        thesis_dir=str(p["thesis_dir"]), delta_dir=p["delta_dir"],
        events_fn=_events_fn(anns=[ann]), triggers_fn=_triggers_fn,
        update_fn=update_fn, now_iso="2026-07-11T09:00:00",
    )

    o = report.outcomes[0]
    assert o.triggered is True and o.updated is True
    assert o.skipped_reason is None
    assert o.cost_usd == 0.02
    # update_fn 被调，trigger_zh 含中文触发原因。
    assert len(update_fn.calls) == 1
    assert "新公告" in (update_fn.calls[0][1] or "")
    # delta .md + .json 落盘（v2）。
    assert (p["delta_dir"] / "002669_v2.md").exists()
    assert (p["delta_dir"] / "002669_v2.json").exists()
    delta_json = json.loads(
        (p["delta_dir"] / "002669_v2.json").read_text(encoding="utf-8"))
    assert delta_json["to_version"] == 2
    assert delta_json["from_version"] == 1
    # budget 被 charge。
    spent = DailyBudget(2.0, dir=p["spend_dir"], today="2026-07-11").spent_today()
    assert abs(spent - 0.02) < 1e-9
    # 水位线重锚到新版 + 公告已见。
    wm = WatermarkStore(p["watermark_path"]).get("002669")
    assert wm.anchor_thesis_version == 2
    assert len(wm.seen_announcement_keys) == 1


# ---------------------------------------------------------------------------
# ③ 幂等：同批事件第二轮 → no_change
# ---------------------------------------------------------------------------

def test_idempotent_second_scan(tmp_path):
    wl = _write_watchlist(tmp_path, ["002669"])
    p = _paths(tmp_path)
    _seed_thesis_v1(p["thesis_dir"], "002669")
    update_fn = _make_update_fn()
    ann = Announcement(title="关于对外投资的公告", date="2026-07-05")
    common = dict(
        watchlist_path=wl, watermark_path=p["watermark_path"],
        thesis_dir=str(p["thesis_dir"]), delta_dir=p["delta_dir"],
        events_fn=_events_fn(anns=[ann]), triggers_fn=_triggers_fn,
        update_fn=update_fn,
    )

    r1 = scan_once(now_iso="2026-07-11T09:00:00", **common)
    assert r1.outcomes[0].updated is True
    assert len(update_fn.calls) == 1

    # 第二轮：同一公告已在水位线 → 无新增 → no_change，update_fn 不再被调。
    r2 = scan_once(now_iso="2026-07-11T10:00:00", **common)
    assert r2.outcomes[0].triggered is False
    assert r2.outcomes[0].skipped_reason == "no_change"
    assert len(update_fn.calls) == 1


# ---------------------------------------------------------------------------
# ④ 预算耗尽 → budget_exhausted、不调 update_fn
# ---------------------------------------------------------------------------

def test_budget_exhausted_skips_update(tmp_path):
    wl = _write_watchlist(tmp_path, ["002669"], budget=0.01)
    p = _paths(tmp_path)
    _seed_thesis_v1(p["thesis_dir"], "002669")
    # 预先把当日预算花超。
    DailyBudget(0.01, dir=p["spend_dir"], today="2026-07-11").charge("seed", 0.05)
    update_fn = _make_update_fn()
    ann = Announcement(title="关于重大资产减值的公告", date="2026-07-05")

    report = scan_once(
        watchlist_path=wl, watermark_path=p["watermark_path"],
        thesis_dir=str(p["thesis_dir"]), delta_dir=p["delta_dir"],
        events_fn=_events_fn(anns=[ann]), triggers_fn=_triggers_fn,
        update_fn=update_fn, now_iso="2026-07-11T09:00:00",
    )

    o = report.outcomes[0]
    assert o.triggered is True and o.updated is False
    assert o.skipped_reason == "budget_exhausted"
    assert update_fn.calls == []
    # 无 delta 文件。
    assert not p["delta_dir"].exists() or list(p["delta_dir"].glob("*")) == []
    # 水位线仍并入（避免下轮重复触发同一公告）。
    wm = WatermarkStore(p["watermark_path"]).get("002669")
    assert len(wm.seen_announcement_keys) == 1


# ---------------------------------------------------------------------------
# ⑤ dry_run → 不调 update_fn、零副作用
# ---------------------------------------------------------------------------

def test_dry_run_no_side_effects(tmp_path):
    wl = _write_watchlist(tmp_path, ["002669"])
    p = _paths(tmp_path)
    _seed_thesis_v1(p["thesis_dir"], "002669")
    update_fn = _make_update_fn()
    ann = Announcement(title="关于对外投资的公告", date="2026-07-05")

    report = scan_once(
        watchlist_path=wl, watermark_path=p["watermark_path"],
        thesis_dir=str(p["thesis_dir"]), delta_dir=p["delta_dir"],
        events_fn=_events_fn(anns=[ann]), triggers_fn=_triggers_fn,
        update_fn=update_fn, dry_run=True, now_iso="2026-07-11T09:00:00",
    )

    o = report.outcomes[0]
    assert o.triggered is True and o.updated is False
    assert update_fn.calls == []
    # 零副作用：不写水位线库、不落 delta。
    assert not p["watermark_path"].exists()
    assert not p["delta_dir"].exists() or list(p["delta_dir"].glob("*")) == []


# ---------------------------------------------------------------------------
# ⑥ 无 thesis → no_thesis、但水位线仍更新
# ---------------------------------------------------------------------------

def test_no_thesis_marks_watermark(tmp_path):
    wl = _write_watchlist(tmp_path, ["002669"])
    p = _paths(tmp_path)
    # 不 seed thesis。
    update_fn = _make_update_fn()
    ann = Announcement(title="关于对外投资的公告", date="2026-07-05")

    report = scan_once(
        watchlist_path=wl, watermark_path=p["watermark_path"],
        thesis_dir=str(p["thesis_dir"]), delta_dir=p["delta_dir"],
        events_fn=_events_fn(anns=[ann]), triggers_fn=_triggers_fn,
        update_fn=update_fn, now_iso="2026-07-11T09:00:00",
    )

    o = report.outcomes[0]
    assert o.triggered is True and o.updated is False
    assert o.skipped_reason == "no_thesis"
    assert update_fn.calls == []
    # 水位线仍更新（seen + last_scan_at）。
    wm = WatermarkStore(p["watermark_path"]).get("002669")
    assert len(wm.seen_announcement_keys) == 1
    assert wm.last_scan_at == "2026-07-11T09:00:00"


# ---------------------------------------------------------------------------
# ⑦ 单票 events_fn 抛异常 → 该票 error、其他票正常、整轮不崩
# ---------------------------------------------------------------------------

def test_one_ticker_error_isolated(tmp_path):
    wl = _write_watchlist(tmp_path, ["002669", "600519"])
    p = _paths(tmp_path)
    update_fn = _make_update_fn()

    def _events(ticker: str):
        if ticker == "002669":
            raise RuntimeError("事件源炸了")
        return RecentEvents(stock_code=ticker, as_of="2026-07-11",
                            announcements=[], forecasts=[])

    report = scan_once(
        watchlist_path=wl, watermark_path=p["watermark_path"],
        thesis_dir=str(p["thesis_dir"]), delta_dir=p["delta_dir"],
        events_fn=_events, triggers_fn=_triggers_fn,
        update_fn=update_fn, now_iso="2026-07-11T09:00:00",
    )

    assert report.tickers_scanned == 2
    by_ticker = {o.ticker: o for o in report.outcomes}
    assert by_ticker["002669"].skipped_reason == "error"
    assert by_ticker["002669"].error is not None
    assert by_ticker["600519"].skipped_reason == "no_change"


# ---------------------------------------------------------------------------
# ⑧ ScanReport.to_markdown / to_dict 中文不崩
# ---------------------------------------------------------------------------

def test_scan_report_serialization(tmp_path):
    wl = _write_watchlist(tmp_path, ["002669"])
    p = _paths(tmp_path)
    _seed_thesis_v1(p["thesis_dir"], "002669")
    update_fn = _make_update_fn(cost=0.02)
    ann = Announcement(title="关于对外投资的公告", date="2026-07-05")

    report = scan_once(
        watchlist_path=wl, watermark_path=p["watermark_path"],
        thesis_dir=str(p["thesis_dir"]), delta_dir=p["delta_dir"],
        events_fn=_events_fn(anns=[ann]), triggers_fn=_triggers_fn,
        update_fn=update_fn, now_iso="2026-07-11T09:00:00",
    )

    md = report.to_markdown()
    assert isinstance(md, str)
    assert "监控扫描报告" in md
    assert "002669" in md
    d = report.to_dict()
    assert d["tickers_scanned"] == 1
    assert d["tickers_updated"] == 1
    assert d["outcomes"][0]["delta"]["to_version"] == 2
    # 报告文件已落盘。
    scans_dir = p["watermark_path"].parent / "scans"
    assert list(scans_dir.glob("*.md"))


def test_scan_report_empty_watchlist_markdown(tmp_path):
    # 空票池（全 disabled）→ to_markdown 不崩。
    data = {"version": 1, "tickers": [
        {"ticker": "002669", "enabled": False}]}
    wl = tmp_path / "wl.yaml"
    wl.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    p = _paths(tmp_path)

    report = scan_once(
        watchlist_path=wl, watermark_path=p["watermark_path"],
        thesis_dir=str(p["thesis_dir"]), delta_dir=p["delta_dir"],
        events_fn=_events_fn(anns=[]), triggers_fn=_triggers_fn,
        update_fn=_make_update_fn(), now_iso="2026-07-11T09:00:00",
    )
    assert report.tickers_scanned == 0
    assert "监控扫描报告" in report.to_markdown()
