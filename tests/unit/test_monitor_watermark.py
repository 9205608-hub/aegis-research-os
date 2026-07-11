"""Aegis 2.0 Phase 3 任务 A2 — 事件水位线库回归测试.

锁定的行为（DESIGN_2.0 §五.3 + Phase 3 范围2 / 设计红线 10）：

① 空库 get 返回默认 TickerWatermark（不写盘）；
② put → save → 重开新 store 能读回（跨实例持久化）；
③ diff_new 找出 key 不在已见集合里的新增事件；
④ 幂等：merge_seen 后再 diff_new 同一批为空；
⑤ MAX_SEEN_KEYS 截断（保最近）；
⑥ 坏 JSON / 缺文件 / 结构异常 → 空库不崩；
⑦ dict 与 dataclass 两种输入算出的 key 一致（JSON 往返稳定）。
"""

from __future__ import annotations

import json

import pytest

from aegis.core.acquisition.connectors.em_events_connector import (
    Announcement,
    EarningsForecast,
)
from aegis.core.monitor.watermark import (
    MAX_SEEN_KEYS,
    TickerWatermark,
    WatermarkStore,
    announcement_key,
    diff_new,
    forecast_key,
    merge_seen,
)


# ---------------------------------------------------------------------------
# 夹具：康达（002669）真实形态的事件切片
# ---------------------------------------------------------------------------

def _announcements() -> list[Announcement]:
    return [
        Announcement(title="关于回购股份的进展公告", date="2026-07-02",
                     category="回购"),
        Announcement(title="2026 年半年度业绩预告", date="2026-06-28",
                     category="业绩预告"),
        Announcement(title="关于对外投资的公告", date="2026-05-15",
                     category="对外投资"),
    ]


def _forecasts() -> list[EarningsForecast]:
    return [
        EarningsForecast(
            report_period="2026-06-30", forecast_type="预增",
            indicator="归母净利润", value_low=1.0e8, value_high=1.2e8,
            change_pct_low=20.0, change_pct_high=44.0,
            notice_date="2026-06-28", prev_year_value=8.3e7,
        ),
    ]


# ---------------------------------------------------------------------------
# key 稳定性（dict 与对象一致）
# ---------------------------------------------------------------------------

def test_announcement_key_object_and_dict_agree():
    a = _announcements()[0]
    d = {"title": a.title, "date": a.date, "category": a.category,
         "source": a.source}
    assert announcement_key(a) == announcement_key(d)
    assert announcement_key(a) == "2026-07-02|关于回购股份的进展公告"


def test_forecast_key_object_and_dict_agree():
    f = _forecasts()[0]
    d = {"report_period": f.report_period, "forecast_type": f.forecast_type,
         "notice_date": f.notice_date, "indicator": f.indicator}
    assert forecast_key(f) == forecast_key(d)
    assert forecast_key(f) == "2026-06-30|预增|2026-06-28"


def test_key_handles_missing_fields():
    # 缺字段不崩，None 归一为空串。
    assert announcement_key({}) == "|"
    assert announcement_key({"date": "2026-07-02"}) == "2026-07-02|"
    assert forecast_key({}) == "||"


# ---------------------------------------------------------------------------
# 空库 / 持久化
# ---------------------------------------------------------------------------

def test_empty_store_get_returns_default_and_no_write(tmp_path):
    path = tmp_path / "watermarks.json"
    store = WatermarkStore(path=path)
    wm = store.get("002669")
    assert wm == TickerWatermark(ticker="002669")
    assert wm.seen_announcement_keys == []
    assert wm.anchor_price is None
    # get 不写盘。
    assert not path.exists()


def test_put_save_reopen_roundtrip(tmp_path):
    path = tmp_path / "watermarks.json"
    store = WatermarkStore(path=path)
    wm = TickerWatermark(
        ticker="002669",
        seen_announcement_keys=["2026-07-02|A", "2026-06-28|B"],
        seen_forecast_keys=["2026-06-30|预增|2026-06-28"],
        last_scan_at="2026-07-11T09:00:00",
        anchor_price=12.34,
        anchor_thesis_version=3,
        last_seen_announcement_date="2026-07-02",
    )
    store.put(wm)
    assert path.exists()

    # 重开新实例读回，字段无损。
    reopened = WatermarkStore(path=path)
    got = reopened.get("002669")
    assert got == wm


def test_get_returns_copy_not_shared_reference(tmp_path):
    path = tmp_path / "watermarks.json"
    store = WatermarkStore(path=path)
    store.put(TickerWatermark(ticker="600519",
                              seen_announcement_keys=["k1"]))
    got = store.get("600519")
    got.seen_announcement_keys.append("mutated")
    # 内存库不被外部原地改动污染。
    assert store.get("600519").seen_announcement_keys == ["k1"]


def test_multiple_tickers_persist_independently(tmp_path):
    path = tmp_path / "watermarks.json"
    store = WatermarkStore(path=path)
    store.put(TickerWatermark(ticker="002669", anchor_price=10.0))
    store.put(TickerWatermark(ticker="600519", anchor_price=1700.0))
    reopened = WatermarkStore(path=path)
    assert reopened.get("002669").anchor_price == 10.0
    assert reopened.get("600519").anchor_price == 1700.0
    assert reopened.get("000001") == TickerWatermark(ticker="000001")


# ---------------------------------------------------------------------------
# diff_new / merge_seen / 幂等
# ---------------------------------------------------------------------------

def test_diff_new_finds_only_unseen():
    anns = _announcements()
    fcs = _forecasts()
    wm = TickerWatermark(
        ticker="002669",
        # 已见第一条公告 + 唯一那条预告。
        seen_announcement_keys=[announcement_key(anns[0])],
        seen_forecast_keys=[forecast_key(fcs[0])],
    )
    new_a, new_f = diff_new(wm, anns, fcs)
    assert [a.title for a in new_a] == [anns[1].title, anns[2].title]
    assert new_f == []


def test_diff_new_all_new_on_empty_watermark():
    anns = _announcements()
    fcs = _forecasts()
    wm = TickerWatermark(ticker="002669")
    new_a, new_f = diff_new(wm, anns, fcs)
    assert len(new_a) == 3
    assert len(new_f) == 1


def test_diff_new_none_inputs_safe():
    wm = TickerWatermark(ticker="002669")
    assert diff_new(wm, None, None) == ([], [])


def test_merge_then_diff_is_idempotent():
    anns = _announcements()
    fcs = _forecasts()
    wm = TickerWatermark(ticker="002669")

    # 首扫：全部新增。
    new_a, new_f = diff_new(wm, anns, fcs)
    assert len(new_a) == 3 and len(new_f) == 1

    # 合并回写后再扫同一批 → 无新增（幂等）。
    wm2 = merge_seen(wm, anns, fcs)
    again_a, again_f = diff_new(wm2, anns, fcs)
    assert again_a == [] and again_f == []


def test_merge_seen_does_not_mutate_input():
    anns = _announcements()
    wm = TickerWatermark(ticker="002669")
    merge_seen(wm, anns, [])
    # 原对象不被原地改动。
    assert wm.seen_announcement_keys == []
    assert wm.last_seen_announcement_date is None


def test_merge_seen_updates_last_seen_date():
    anns = _announcements()  # 最新日期 2026-07-02
    wm = TickerWatermark(ticker="002669",
                         last_seen_announcement_date="2026-01-01")
    wm2 = merge_seen(wm, anns, [])
    assert wm2.last_seen_announcement_date == "2026-07-02"
    # 已有更新的水位线不被回退。
    wm3 = merge_seen(
        TickerWatermark(ticker="002669",
                        last_seen_announcement_date="2026-12-31"),
        anns, [])
    assert wm3.last_seen_announcement_date == "2026-12-31"


def test_merge_seen_dedupes_within_batch_and_across_calls():
    a = Announcement(title="同一条", date="2026-07-02")
    wm = TickerWatermark(ticker="002669")
    # 同一批含重复 + 两次合并同一条 → seen 里只保留一个 key。
    wm2 = merge_seen(wm, [a, a], [])
    wm3 = merge_seen(wm2, [a], [])
    assert wm3.seen_announcement_keys == [announcement_key(a)]


def test_max_seen_keys_truncation_keeps_recent():
    # 造 MAX_SEEN_KEYS + 50 条公告，索引升序 = 时序（越大越新）。
    total = MAX_SEEN_KEYS + 50
    anns = [Announcement(title=f"t{i:04d}", date="2026-07-02")
            for i in range(total)]
    wm = merge_seen(TickerWatermark(ticker="002669"), anns, [])
    assert len(wm.seen_announcement_keys) == MAX_SEEN_KEYS
    # 保留末尾（最近）的 MAX_SEEN_KEYS 个。
    expected_tail = [announcement_key(a) for a in anns[-MAX_SEEN_KEYS:]]
    assert wm.seen_announcement_keys == expected_tail
    # 最早的那批已被截掉。
    assert announcement_key(anns[0]) not in wm.seen_announcement_keys


def test_forecast_key_dict_roundtrip_matches_via_store(tmp_path):
    # 预告 dataclass → merge → 落盘 → 读回（dict 形态）→ diff 仍幂等。
    path = tmp_path / "watermarks.json"
    store = WatermarkStore(path=path)
    fcs = _forecasts()
    wm = merge_seen(store.get("002669"), [], fcs)
    store.put(wm)

    reopened = WatermarkStore(path=path)
    got = reopened.get("002669")
    # 读回后 seen 集合仍能把同一批预告全过滤掉。
    _, new_f = diff_new(got, [], fcs)
    assert new_f == []


# ---------------------------------------------------------------------------
# 容错：坏文件 / 缺文件 / 结构异常
# ---------------------------------------------------------------------------

def test_corrupt_json_does_not_crash(tmp_path):
    path = tmp_path / "watermarks.json"
    path.write_text("{ this is not json ]", encoding="utf-8")
    store = WatermarkStore(path=path)  # 不崩
    assert store.get("002669") == TickerWatermark(ticker="002669")


def test_non_dict_root_does_not_crash(tmp_path):
    path = tmp_path / "watermarks.json"
    path.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    store = WatermarkStore(path=path)
    assert store.get("002669") == TickerWatermark(ticker="002669")


def test_missing_file_starts_empty(tmp_path):
    path = tmp_path / "sub" / "dir" / "watermarks.json"
    store = WatermarkStore(path=path)  # 父目录都不存在也不崩
    assert store.get("002669") == TickerWatermark(ticker="002669")
    # put 时才 mkdir 落盘。
    store.put(TickerWatermark(ticker="002669", anchor_price=5.0))
    assert path.exists()


def test_partial_and_bad_typed_record_tolerated(tmp_path):
    path = tmp_path / "watermarks.json"
    path.write_text(json.dumps({
        "002669": {
            "seen_announcement_keys": ["k1", 42, None, "k2"],  # 混类型
            "anchor_price": "not-a-number",                    # 坏类型 → None
            "anchor_thesis_version": 2,
            "last_scan_at": "",                                # 空串 → None
        },
        "600519": "not-a-dict",                                # 整条坏 → 空水位线
    }, ensure_ascii=False), encoding="utf-8")

    store = WatermarkStore(path=path)
    wm = store.get("002669")
    # None 被过滤，其余转字符串。
    assert wm.seen_announcement_keys == ["k1", "42", "k2"]
    assert wm.anchor_price is None
    assert wm.anchor_thesis_version == 2
    assert wm.last_scan_at is None

    bad = store.get("600519")
    assert bad == TickerWatermark(ticker="600519")


def test_put_overwrites_existing_ticker(tmp_path):
    path = tmp_path / "watermarks.json"
    store = WatermarkStore(path=path)
    store.put(TickerWatermark(ticker="002669", anchor_price=10.0))
    store.put(TickerWatermark(ticker="002669", anchor_price=11.0,
                              anchor_thesis_version=2))
    reopened = WatermarkStore(path=path)
    got = reopened.get("002669")
    assert got.anchor_price == 11.0
    assert got.anchor_thesis_version == 2
