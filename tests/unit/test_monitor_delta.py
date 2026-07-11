"""Aegis 2.0 Phase 3 — 论点 Delta 简报（纯函数）回归测试.

锁定行为（任务 A3 / 设计红线 10）：

① 无变化 → summary 明确写「本次复核论点无实质变化」；
② 文本字段（核心论点…）严格不等即算变、被捕获；
③ 数值字段（每股价值）变化 > 1% 捕获、≤ 1% 忽略（浮点噪声门）；
④ 发布状态 blocked → publishable 被捕获（枚举串变化，label 中文、值保原串）；
⑤ 监控点按 description 集合求 added / removed；
⑥ 首版（prev=None）→ from_version=None、changes 空、summary「首次建立论点」；
⑦ to_dict / to_markdown 不崩且中文；
⑧ 坏 payload 容错，永不 raise。

纯内存、无 I/O、不连网络。
"""

from __future__ import annotations

from aegis.core.monitor.delta import (
    DeltaBriefing,
    FieldChange,
    diff_theses,
    summarize_change,
)


# ---------------------------------------------------------------------------
# 夹具：thesis payload（record["thesis"]，即 ThesisContract.model_dump 形态切片）
# ---------------------------------------------------------------------------

def _thesis(**overrides) -> dict:
    """构造一个 thesis payload dict；overrides 覆盖单个字段。"""
    base = {
        "entity_id": "002669",
        "core_thesis": "股价隐含的困境反转预期与三张报表交叉验证背离。",
        "my_variant": "市场信扭亏是经营拐点，我们看到的是并表与赊销撑起的会计利润。",
        "counter_thesis": "定增落地 + 军工订单放量可能显著改善基本面。",
        "market_implied_story": "现价隐含 22.6% 的营收复合增速预期。",
        "sector_cycle_position": "定价体制（mixed）：题材叙事与困境反转之间。",
        "publishing_status": "blocked",
        "confidence_bucket": "medium",
        "bear_case_value": 1.10,
        "base_case_value": 2.36,
        "bull_case_value": 4.30,
        "must_monitor": [
            {"description": "经营现金流/归母净利润 低于 0.5",
             "check_frequency": "quarterly",
             "data_source": "pit_store:cfo_to_net_income"},
            {"description": "资产负债率同比抬升超 5pp",
             "check_frequency": "quarterly",
             "data_source": "pit_store:leverage_trend"},
        ],
    }
    base.update(overrides)
    return base


def _record(version: int, **thesis_overrides) -> dict:
    """构造一条链 record（含 version / thesis）。"""
    return {
        "version": version,
        "created_at": "2026-07-10T13:12:11",
        "run_id": f"run_v{version}",
        "parent_version": version - 1 if version > 1 else None,
        "thesis": _thesis(**thesis_overrides),
    }


# ---------------------------------------------------------------------------
# ① 无变化
# ---------------------------------------------------------------------------

class TestNoChange:
    def test_identical_theses_report_no_substantial_change(self):
        b = diff_theses(_thesis(), _thesis(), entity_id="002669",
                        from_version=1, to_version=2)
        assert b.changes == []
        assert b.monitorables_added == []
        assert b.monitorables_removed == []
        assert "本次复核论点无实质变化" in b.summary_zh

    def test_no_change_summary_appends_trigger(self):
        b = diff_theses(_thesis(), _thesis(), trigger_zh="季度定期报告到期")
        assert "本次复核论点无实质变化" in b.summary_zh
        assert "季度定期报告到期" in b.summary_zh


# ---------------------------------------------------------------------------
# ② 文本字段变化
# ---------------------------------------------------------------------------

class TestTextFieldChange:
    def test_core_thesis_change_captured(self):
        prev = _thesis()
        new = _thesis(core_thesis="半年报证伪反转，论点由质疑转为确认下行。")
        b = diff_theses(prev, new, from_version=1, to_version=2)
        fields = {c.field for c in b.changes}
        assert "core_thesis" in fields
        chg = next(c for c in b.changes if c.field == "core_thesis")
        assert chg.label_zh == "核心论点"
        assert chg.before == prev["core_thesis"]
        assert chg.after == new["core_thesis"]

    def test_whitespace_only_difference_is_not_a_change(self):
        prev = _thesis(my_variant="观点甲")
        new = _thesis(my_variant="  观点甲  ")
        b = diff_theses(prev, new)
        assert all(c.field != "my_variant" for c in b.changes)

    def test_multiple_text_fields_change(self):
        prev = _thesis()
        new = _thesis(my_variant="改了差异化观点", counter_thesis="改了反方论点")
        b = diff_theses(prev, new)
        fields = {c.field for c in b.changes}
        assert {"my_variant", "counter_thesis"} <= fields


# ---------------------------------------------------------------------------
# ③ 数值字段变化：>1% 捕获、≤1% 忽略
# ---------------------------------------------------------------------------

class TestNumericFieldChange:
    def test_base_case_value_10_to_13_captured(self):
        prev = _thesis(base_case_value=10.0)
        new = _thesis(base_case_value=13.0)
        b = diff_theses(prev, new)
        chg = next((c for c in b.changes if c.field == "base_case_value"), None)
        assert chg is not None
        assert chg.label_zh == "基准每股价值"
        assert chg.before == 10.0 and chg.after == 13.0

    def test_base_case_value_tiny_move_ignored(self):
        prev = _thesis(base_case_value=10.0)
        new = _thesis(base_case_value=10.05)   # +0.5% < 1% 门槛
        b = diff_theses(prev, new)
        assert all(c.field != "base_case_value" for c in b.changes)

    def test_base_case_value_exactly_over_threshold_captured(self):
        prev = _thesis(base_case_value=100.0)
        new = _thesis(base_case_value=101.5)   # +1.5% > 1%
        b = diff_theses(prev, new)
        assert any(c.field == "base_case_value" for c in b.changes)

    def test_none_to_value_is_a_change(self):
        prev = _thesis(bull_case_value=None)
        new = _thesis(bull_case_value=4.30)
        b = diff_theses(prev, new)
        assert any(c.field == "bull_case_value" for c in b.changes)

    def test_value_to_none_is_a_change(self):
        prev = _thesis(bear_case_value=1.1)
        new = _thesis(bear_case_value=None)
        b = diff_theses(prev, new)
        assert any(c.field == "bear_case_value" for c in b.changes)

    def test_none_to_none_is_not_a_change(self):
        prev = _thesis(bull_case_value=None)
        new = _thesis(bull_case_value=None)
        b = diff_theses(prev, new)
        assert all(c.field != "bull_case_value" for c in b.changes)

    def test_zero_baseline_to_nonzero_is_a_change(self):
        prev = _thesis(base_case_value=0.0)
        new = _thesis(base_case_value=2.0)
        b = diff_theses(prev, new)
        assert any(c.field == "base_case_value" for c in b.changes)


# ---------------------------------------------------------------------------
# ④ 发布状态 / 置信度枚举串变化
# ---------------------------------------------------------------------------

class TestEnumStringChange:
    def test_publishing_status_blocked_to_publishable(self):
        prev = _thesis(publishing_status="blocked")
        new = _thesis(publishing_status="publishable")
        b = diff_theses(prev, new)
        chg = next((c for c in b.changes if c.field == "publishing_status"), None)
        assert chg is not None
        assert chg.label_zh == "发布状态"        # label 中文
        assert chg.before == "blocked"           # 值保留原串
        assert chg.after == "publishable"

    def test_confidence_bucket_change_captured(self):
        prev = _thesis(confidence_bucket="medium")
        new = _thesis(confidence_bucket="high")
        b = diff_theses(prev, new)
        assert any(c.field == "confidence_bucket" for c in b.changes)


# ---------------------------------------------------------------------------
# ⑤ 监控点增删
# ---------------------------------------------------------------------------

class TestMonitorablesDiff:
    def test_added_and_removed_by_description(self):
        prev = _thesis(must_monitor=[
            {"description": "监控点A", "data_source": "pit_store:x"},
            {"description": "监控点B", "data_source": "pit_store:y"},
        ])
        new = _thesis(must_monitor=[
            {"description": "监控点B", "data_source": "pit_store:y"},
            {"description": "监控点C", "data_source": "pit_store:z"},
        ])
        b = diff_theses(prev, new)
        assert b.monitorables_added == ["监控点C"]
        assert b.monitorables_removed == ["监控点A"]

    def test_same_monitorables_no_diff(self):
        b = diff_theses(_thesis(), _thesis())
        assert b.monitorables_added == []
        assert b.monitorables_removed == []

    def test_monitorable_only_change_still_summarized(self):
        prev = _thesis(must_monitor=[{"description": "监控点A"}])
        new = _thesis(must_monitor=[{"description": "监控点A"},
                                    {"description": "监控点D"}])
        b = diff_theses(prev, new)
        assert b.changes == []                    # 主体字段没动
        assert b.monitorables_added == ["监控点D"]
        assert "本次复核论点无实质变化" not in b.summary_zh
        assert "监控点" in b.summary_zh


# ---------------------------------------------------------------------------
# summary 内容
# ---------------------------------------------------------------------------

class TestSummary:
    def test_summary_counts_and_names_most_critical(self):
        prev = _thesis(publishing_status="blocked", base_case_value=10.0)
        new = _thesis(publishing_status="publishable", base_case_value=13.0)
        b = diff_theses(prev, new)
        # 发布状态优先级最高，应被点名为最关键
        assert "发布状态" in b.summary_zh
        assert "处论点变化" in b.summary_zh

    def test_summary_includes_trigger_when_given(self):
        prev = _thesis()
        new = _thesis(core_thesis="改了")
        b = diff_theses(prev, new, trigger_zh="经营现金流/归母净利润 红旗触发")
        assert "经营现金流/归母净利润 红旗触发" in b.summary_zh


# ---------------------------------------------------------------------------
# ⑥ summarize_change：首版 + 便捷封装
# ---------------------------------------------------------------------------

class TestSummarizeChange:
    def test_first_version_prev_none(self):
        new = _record(1)
        b = summarize_change(None, new)
        assert b.from_version is None
        assert b.to_version == 1
        assert b.changes == []
        assert "首次建立论点" in b.summary_zh
        assert b.entity_id == "002669"

    def test_first_version_counts_monitorables(self):
        b = summarize_change(None, _record(1))
        assert "首次建立论点" in b.summary_zh
        assert "2 个监控点" in b.summary_zh

    def test_second_version_delegates_to_diff(self):
        prev = _record(1)
        new = _record(2, base_case_value=13.0)   # v1 base=2.36 → v2 13.0（大变）
        b = summarize_change(prev, new)
        assert b.from_version == 1
        assert b.to_version == 2
        assert any(c.field == "base_case_value" for c in b.changes)

    def test_summarize_change_passes_trigger(self):
        b = summarize_change(_record(1), _record(2, publishing_status="publishable"),
                             trigger_zh="业绩预告发布")
        assert b.trigger_zh == "业绩预告发布"
        assert "业绩预告发布" in b.summary_zh


# ---------------------------------------------------------------------------
# ⑦ to_dict / to_markdown 不崩且中文
# ---------------------------------------------------------------------------

class TestSerialization:
    def test_to_dict_shape(self):
        prev = _thesis()
        new = _thesis(core_thesis="改了核心论点", base_case_value=13.0)
        d = diff_theses(prev, new, entity_id="002669",
                        from_version=1, to_version=2).to_dict()
        assert d["entity_id"] == "002669"
        assert d["from_version"] == 1
        assert d["to_version"] == 2
        assert isinstance(d["changes"], list)
        assert d["changes"][0]["label_zh"]   # 中文标签在
        assert "summary_zh" in d

    def test_to_markdown_is_chinese_and_does_not_crash(self):
        prev = _thesis(publishing_status="blocked", base_case_value=10.0,
                       must_monitor=[{"description": "监控点A"}])
        new = _thesis(publishing_status="publishable", base_case_value=13.0,
                      must_monitor=[{"description": "监控点B"}])
        md = diff_theses(prev, new, entity_id="002669",
                         from_version=1, to_version=2).to_markdown()
        assert "论点 Delta 简报" in md
        assert "触发来源" in md
        assert "变更清单" in md
        assert "影响总结" in md
        assert "发布状态" in md
        assert "¥13.00" in md               # 数值中文格式化
        assert "监控点A" in md and "监控点B" in md

    def test_to_markdown_first_version(self):
        md = summarize_change(None, _record(1)).to_markdown()
        assert "首版" in md
        assert "首次建立论点" in md

    def test_to_markdown_no_change(self):
        md = diff_theses(_thesis(), _thesis(), from_version=1, to_version=2).to_markdown()
        assert "无变化" in md or "无增减" in md
        assert "本次复核论点无实质变化" in md


# ---------------------------------------------------------------------------
# ⑧ 容错：坏 payload 永不 raise
# ---------------------------------------------------------------------------

class TestFaultTolerance:
    def test_non_dict_payloads_do_not_raise(self):
        b = diff_theses("not-a-dict", 42, entity_id="x", to_version=2)
        assert isinstance(b, DeltaBriefing)
        assert "本次复核论点无实质变化" in b.summary_zh

    def test_bad_must_monitor_shapes_tolerated(self):
        prev = _thesis(must_monitor="not-a-list")
        new = _thesis(must_monitor=[{"no_desc": 1}, {"description": ""}, None])
        b = diff_theses(prev, new)
        # 坏监控点项被跳过，不产出虚假 added/removed，不 raise
        assert b.monitorables_added == []
        assert isinstance(b, DeltaBriefing)

    def test_summarize_change_bad_records_tolerated(self):
        b = summarize_change({"version": "bad", "thesis": None},
                             {"version": None, "thesis": "junk"})
        assert isinstance(b, DeltaBriefing)
        assert b.to_version == 0

    def test_missing_entity_id_defaults_gracefully(self):
        prev = {"core_thesis": "a"}
        new = {"core_thesis": "b", "entity_id": "600519_sh"}
        b = diff_theses(prev, new)
        assert b.entity_id == "600519_sh"    # 从 payload 兜底
        # entity_id 缺省也不崩
        md = diff_theses({}, {}).to_markdown()
        assert "未知标的" in md


# ---------------------------------------------------------------------------
# FieldChange dataclass 基本形态
# ---------------------------------------------------------------------------

def test_field_change_dataclass():
    c = FieldChange(field="core_thesis", label_zh="核心论点", before="a", after="b")
    assert c.field == "core_thesis"
    assert c.label_zh == "核心论点"
    assert (c.before, c.after) == ("a", "b")
