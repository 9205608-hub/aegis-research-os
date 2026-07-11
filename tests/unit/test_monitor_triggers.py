"""Aegis 2.0 Phase 3 任务 B1 — 复研触发判定回归测试.

锁定的行为（DESIGN_2.0 §五.3 + Phase 3 范围2 / 设计红线 6、10）：

① 无新事件 → []（幂等前提：merge_seen 后再评估为空）；
② 有新公告（开关开）→ new_announcement；开关关 → 不触发；
③ 含关键词〔并购〕标题 → 额外 keyword_announcement（更高优先级排前）；
④ 新预告（开关开）→ new_forecast；thesis 挂 forecast_vs_consensus → 填 model_id；
⑤ 股价偏离：>20% 触发、<20% 不触发、anchor/现价缺失不触发、开关关不触发；
⑥ active_model_ids 正确反解（含 None thesis / watch_only 排除）；
⑦ 坏输入不崩、永不 raise。
"""

from __future__ import annotations

from aegis.core.acquisition.connectors.em_events_connector import (
    Announcement,
    EarningsForecast,
    RecentEvents,
)
from aegis.core.monitor.triggers import (
    Trigger,
    active_model_ids,
    evaluate_triggers,
)
from aegis.core.monitor.watchlist import MonitorSwitches, WatchlistEntry
from aegis.core.monitor.watermark import (
    TickerWatermark,
    announcement_key,
    forecast_key,
    merge_seen,
)


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

def _entry(*, announcements=True, forecasts=True, price_deviation=False):
    return WatchlistEntry(
        ticker="002669",
        name="康达新材",
        monitors=MonitorSwitches(
            announcements=announcements,
            forecasts=forecasts,
            price_deviation=price_deviation,
        ),
    )


def _announcements():
    return [
        Announcement(title="关于回购股份的进展公告", date="2026-07-02",
                     category="回购"),
        Announcement(title="2026 年半年度业绩预告", date="2026-06-28",
                     category="业绩预告"),
    ]


def _forecasts():
    return [
        EarningsForecast(
            report_period="2026-06-30", forecast_type="扭亏",
            indicator="归母净利润", value_low=1.0e8, value_high=1.2e8,
            change_pct_low=20.0, change_pct_high=44.0,
            notice_date="2026-01-21", prev_year_value=-8.3e7,
        ),
    ]


def _events(announcements=None, forecasts=None):
    return RecentEvents(
        stock_code="002669",
        as_of="2026-07-11",
        announcements=announcements if announcements is not None else [],
        forecasts=forecasts if forecasts is not None else [],
        consensus=None,
    )


def _thesis_record(*, model_ids=(), version=3):
    """构造含指定封闭目录型号 must_monitor 的链上 record。"""
    from aegis.core.thesis.monitorables import CATALOG
    must_monitor = []
    for mid in model_ids:
        entry = CATALOG[mid]
        must_monitor.append({
            "description": entry.name_zh,
            "check_frequency": entry.check_frequency,
            "data_source": f"{entry.data_source}:{mid}",
        })
    return {
        "version": version,
        "created_at": "2026-06-01T00:00:00",
        "run_id": "run-x",
        "parent_version": version - 1,
        "thesis": {"entity_id": "002669", "must_monitor": must_monitor},
    }


def _fresh_watermark():
    """空水位线（没见过任何事件）。"""
    return TickerWatermark(ticker="002669")


# ---------------------------------------------------------------------------
# active_model_ids
# ---------------------------------------------------------------------------

def test_active_model_ids_none_thesis():
    assert active_model_ids(None) == set()


def test_active_model_ids_no_thesis_key():
    assert active_model_ids({"version": 1}) == set()


def test_active_model_ids_resolves_catalog_and_drops_watch_only():
    record = _thesis_record(model_ids=("price_deviation", "forecast_vs_consensus"))
    # 混进一条 watch_only（非目录，应被排除）。
    record["thesis"]["must_monitor"].append({
        "description": "人工关注：管理层变动",
        "check_frequency": "quarterly",
        "data_source": "watch_only",
    })
    assert active_model_ids(record) == {"price_deviation", "forecast_vs_consensus"}


def test_active_model_ids_accepts_bare_thesis_dict():
    record = _thesis_record(model_ids=("leverage_trend",))
    bare = record["thesis"]
    assert active_model_ids(bare) == {"leverage_trend"}


def test_active_model_ids_bad_must_monitor_type():
    assert active_model_ids({"thesis": {"must_monitor": "oops"}}) == set()


# ---------------------------------------------------------------------------
# 无新事件 → []
# ---------------------------------------------------------------------------

def test_no_new_events_returns_empty():
    anns, fcs = _announcements(), _forecasts()
    wm = merge_seen(_fresh_watermark(), anns, fcs)  # 全部已见
    events = _events(anns, fcs)
    out = evaluate_triggers(
        entry=_entry(), thesis_record=None, events=events, watermark=wm,
    )
    assert out == []


def test_empty_events_returns_empty():
    out = evaluate_triggers(
        entry=_entry(), thesis_record=None, events=_events(),
        watermark=_fresh_watermark(),
    )
    assert out == []


# ---------------------------------------------------------------------------
# 新公告
# ---------------------------------------------------------------------------

def test_new_announcement_triggers():
    events = _events(_announcements(), [])
    out = evaluate_triggers(
        entry=_entry(), thesis_record=None, events=events,
        watermark=_fresh_watermark(),
    )
    kinds = [t.kind for t in out]
    assert "new_announcement" in kinds
    na = next(t for t in out if t.kind == "new_announcement")
    assert na.model_id is None
    assert na.detail["count"] == 2
    assert "关于回购股份的进展公告" in na.detail["titles"]


def test_announcement_switch_off_suppresses():
    events = _events(_announcements(), [])
    out = evaluate_triggers(
        entry=_entry(announcements=False), thesis_record=None, events=events,
        watermark=_fresh_watermark(),
    )
    assert [t for t in out if t.kind in ("new_announcement", "keyword_announcement")] == []


# ---------------------------------------------------------------------------
# 关键词公告
# ---------------------------------------------------------------------------

def test_keyword_announcement_extra_trigger_and_priority():
    anns = [
        Announcement(title="关于筹划重大资产并购的提示性公告", date="2026-07-05"),
        Announcement(title="关于回购股份的进展公告", date="2026-07-02"),
    ]
    events = _events(anns, [])
    out = evaluate_triggers(
        entry=_entry(), thesis_record=None, events=events,
        watermark=_fresh_watermark(),
    )
    kinds = [t.kind for t in out]
    assert "keyword_announcement" in kinds
    assert "new_announcement" in kinds
    # 关键词触发优先级更高 → 排在普通公告增量之前。
    assert kinds.index("keyword_announcement") < kinds.index("new_announcement")
    kw = next(t for t in out if t.kind == "keyword_announcement")
    assert kw.model_id == "announcement_keyword"
    assert "并购" in kw.detail["keywords"]
    assert kw.detail["armed"] is False  # thesis=None → 未武装


def test_keyword_announcement_armed_flag():
    anns = [Announcement(title="重大资产减值计提公告", date="2026-07-05")]
    events = _events(anns, [])
    record = _thesis_record(model_ids=("announcement_keyword",))
    out = evaluate_triggers(
        entry=_entry(), thesis_record=record, events=events,
        watermark=_fresh_watermark(),
    )
    kw = next(t for t in out if t.kind == "keyword_announcement")
    assert kw.detail["armed"] is True
    assert "减值" in kw.detail["keywords"]


def test_no_keyword_when_titles_clean():
    anns = [Announcement(title="关于回购股份的进展公告", date="2026-07-02")]
    events = _events(anns, [])
    out = evaluate_triggers(
        entry=_entry(), thesis_record=None, events=events,
        watermark=_fresh_watermark(),
    )
    assert [t for t in out if t.kind == "keyword_announcement"] == []


# ---------------------------------------------------------------------------
# 新预告
# ---------------------------------------------------------------------------

def test_new_forecast_triggers_without_thesis_model():
    events = _events([], _forecasts())
    out = evaluate_triggers(
        entry=_entry(), thesis_record=None, events=events,
        watermark=_fresh_watermark(),
    )
    fc = next(t for t in out if t.kind == "new_forecast")
    assert fc.model_id is None
    assert fc.detail["armed"] is False
    assert "扭亏" in fc.reason_zh
    assert "2026-01-21" in fc.reason_zh


def test_new_forecast_fills_model_id_when_armed():
    events = _events([], _forecasts())
    record = _thesis_record(model_ids=("forecast_vs_consensus",))
    out = evaluate_triggers(
        entry=_entry(), thesis_record=record, events=events,
        watermark=_fresh_watermark(),
    )
    fc = next(t for t in out if t.kind == "new_forecast")
    assert fc.model_id == "forecast_vs_consensus"
    assert fc.detail["armed"] is True


def test_forecast_switch_off_suppresses():
    events = _events([], _forecasts())
    out = evaluate_triggers(
        entry=_entry(forecasts=False), thesis_record=None, events=events,
        watermark=_fresh_watermark(),
    )
    assert [t for t in out if t.kind == "new_forecast"] == []


# ---------------------------------------------------------------------------
# 股价偏离
# ---------------------------------------------------------------------------

def _anchored_watermark(anchor=20.0):
    wm = _fresh_watermark()
    wm.anchor_price = anchor
    wm.anchor_thesis_version = 2
    return wm


def test_price_deviation_triggers_above_threshold():
    wm = _anchored_watermark(20.0)
    out = evaluate_triggers(
        entry=_entry(price_deviation=True), thesis_record=None,
        events=_events(), watermark=wm, current_price=25.30,  # +26.5%
    )
    pt = next(t for t in out if t.kind == "price_deviation")
    assert pt.model_id == "price_deviation"
    assert pt.detail["direction"] == "上涨"
    assert pt.detail["deviation_pct"] == 26.5
    assert "26.5%" in pt.reason_zh


def test_price_deviation_below_threshold_no_trigger():
    wm = _anchored_watermark(20.0)
    out = evaluate_triggers(
        entry=_entry(price_deviation=True), thesis_record=None,
        events=_events(), watermark=wm, current_price=22.00,  # +10%
    )
    assert [t for t in out if t.kind == "price_deviation"] == []


def test_price_deviation_downward_triggers():
    wm = _anchored_watermark(20.0)
    out = evaluate_triggers(
        entry=_entry(price_deviation=True), thesis_record=None,
        events=_events(), watermark=wm, current_price=15.00,  # -25%
    )
    pt = next(t for t in out if t.kind == "price_deviation")
    assert pt.detail["direction"] == "下跌"


def test_price_deviation_missing_price_no_trigger():
    wm = _anchored_watermark(20.0)
    out = evaluate_triggers(
        entry=_entry(price_deviation=True), thesis_record=None,
        events=_events(), watermark=wm, current_price=None,
    )
    assert [t for t in out if t.kind == "price_deviation"] == []


def test_price_deviation_missing_anchor_no_trigger():
    wm = _fresh_watermark()  # anchor_price=None
    out = evaluate_triggers(
        entry=_entry(price_deviation=True), thesis_record=None,
        events=_events(), watermark=wm, current_price=99.0,
    )
    assert [t for t in out if t.kind == "price_deviation"] == []


def test_price_deviation_switch_off_no_trigger():
    wm = _anchored_watermark(20.0)
    out = evaluate_triggers(
        entry=_entry(price_deviation=False), thesis_record=None,
        events=_events(), watermark=wm, current_price=99.0,  # huge move
    )
    assert [t for t in out if t.kind == "price_deviation"] == []


# ---------------------------------------------------------------------------
# 组合 + 排序 + 幂等
# ---------------------------------------------------------------------------

def test_combined_sources_sorted_by_priority():
    anns = [Announcement(title="关于订单中标的公告", date="2026-07-05")]
    wm = _anchored_watermark(20.0)
    events = _events(anns, _forecasts())
    out = evaluate_triggers(
        entry=_entry(price_deviation=True),
        thesis_record=_thesis_record(model_ids=("forecast_vs_consensus",)),
        events=events, watermark=wm, current_price=30.0,  # +50%
    )
    kinds = [t.kind for t in out]
    # 四类都在。
    assert set(kinds) == {
        "keyword_announcement", "price_deviation", "new_forecast", "new_announcement",
    }
    # 优先级排序：keyword < price < forecast < announcement。
    assert kinds == [
        "keyword_announcement", "price_deviation", "new_forecast", "new_announcement",
    ]


def test_idempotent_after_merge_seen():
    anns, fcs = _announcements(), _forecasts()
    events = _events(anns, fcs)
    wm0 = _fresh_watermark()
    first = evaluate_triggers(
        entry=_entry(), thesis_record=None, events=events, watermark=wm0,
    )
    assert first  # 首轮有触发
    wm1 = merge_seen(wm0, anns, fcs)
    second = evaluate_triggers(
        entry=_entry(), thesis_record=None, events=events, watermark=wm1,
    )
    assert second == []  # 合并水位线后同一批事件不再触发


def test_partial_new_events_only_new_ones_trigger():
    old = Announcement(title="关于回购股份的进展公告", date="2026-07-02")
    new = Announcement(title="关于对外投资的公告", date="2026-07-08")
    wm = merge_seen(_fresh_watermark(), [old], [])
    events = _events([new, old], [])
    out = evaluate_triggers(
        entry=_entry(), thesis_record=None, events=events, watermark=wm,
    )
    na = next(t for t in out if t.kind == "new_announcement")
    assert na.detail["count"] == 1
    assert na.detail["titles"] == ["关于对外投资的公告"]


# ---------------------------------------------------------------------------
# 容错：坏输入不崩
# ---------------------------------------------------------------------------

def test_never_raises_on_garbage_inputs():
    # events / entry / watermark 全传 None / 坏类型，必须返回 list 不崩。
    out = evaluate_triggers(
        entry=None, thesis_record="not-a-dict", events=None,
        watermark=None, current_price="oops",
    )
    assert out == []


def test_dict_form_events():
    # JSON 往返后事件切片是 dict —— key 计算与对象一致，应正常触发。
    ann = Announcement(title="关于订单中标的公告", date="2026-07-05")
    events = {
        "stock_code": "002669", "as_of": "2026-07-11",
        "announcements": [{"title": ann.title, "date": ann.date, "category": "",
                           "source": "eastmoney"}],
        "forecasts": [], "consensus": None,
    }
    wm = _fresh_watermark()
    out = evaluate_triggers(
        entry=_entry(), thesis_record=None, events=events, watermark=wm,
    )
    assert any(t.kind == "keyword_announcement" for t in out)
    assert any(t.kind == "new_announcement" for t in out)


def test_trigger_is_dataclass_with_expected_fields():
    t = Trigger(kind="new_announcement", model_id=None, reason_zh="x", detail={})
    assert t.kind == "new_announcement"
    assert t.model_id is None
    assert t.reason_zh == "x"
    assert t.detail == {}
