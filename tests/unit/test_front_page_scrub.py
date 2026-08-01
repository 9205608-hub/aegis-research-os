"""AUDIT 遗留清偿回归（2026-08-01）— Editor front_page_numbers 接入清洗。

HANDOFF 待办："Editor front_page_numbers 无 scrub"。首页大字数字是报告
最显眼的位置，此前却是 Editor 输出里唯一不经 _scrub_fair_value_claims
的数字通道——LLM 编造的目标价/回报% 可以直通报告头版。

覆盖：
- 编造目标价 / 编造回报% 的 entry 被整条剔除（不留空壳）
- sanctioned 情景值 / 市价 / 聚合额（亿）/ 增长% / 无方向 margin% 存活
- 白名单存活（设计红线 9）：分部占比、客户集中度两位小数披露值
  （±0.5pp 容差）在方向性上下文里也不被误杀，strict 票同样存活
- strict（估值失配）票：连 sanctioned 情景值也整条剔除
- 仅 context 违规 → entry 保留、context 原位替换（中文占位符）
- label 无数字的 entry 不受影响，label 原样保留
- ReportEditor.edit() 全链路接线（清洗发生在 try 块内，静默失效会被抓）
- 清洗后为空列表时 HTML 渲染优雅降级（v2 渲染器不消费该字段，零区块）
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from aegis.core.chief_analyst.report_editor import (
    ReportEditor,
    _scrub_front_page_numbers,
)

MKT = {"current_price": 349.0}

# 正常口径：三档围绕市价 ±50% 内（同 test_confidence_overhaul.SANE_SCEN）
SANE_SCEN = {
    "currency": "CNY",
    "bear_value": 250.0,
    "base_value": 380.0,
    "bull_value": 520.0,
    "probability_weighted_value": 390.0,
}

# 宁德时代式失配：DCF 三档全部在市价 8-15× 之外 → strict 票
MISMATCH_SCEN = {
    "currency": "CNY",
    "bear_value": 2800.0,
    "base_value": 4000.0,
    "bull_value": 5200.0,
    "probability_weighted_value": 4100.0,
}


def _entry(label, value, context):
    return {"label": label, "value": value, "context": context}


class TestFabricatedDropped:
    """编造数字整条剔除，不留空壳。"""

    def test_fabricated_target_price_dropped(self):
        kept, warns = _scrub_front_page_numbers(
            [_entry("目标价", "¥1640", "我们认为合理目标价为¥1640")],
            SANE_SCEN, MKT,
        )
        assert kept == []
        assert any("FRONT PAGE NUMBER DROPPED" in w for w in warns)

    def test_fabricated_return_pct_dropped(self):
        # sanctioned 回报为 -28%/+9%/+49%/+12%——90% 上行不属于任何口径
        kept, _ = _scrub_front_page_numbers(
            [_entry("潜在回报", "+90%", "较现价有90%上行空间")],
            SANE_SCEN, MKT,
        )
        assert kept == []

    def test_no_placeholder_shell_left(self):
        # 剔除而非留 "〔详见DCF情景估值〕" 空壳
        kept, _ = _scrub_front_page_numbers(
            [_entry("目标价", "¥1640", "合理目标价¥1640")],
            SANE_SCEN, MKT,
        )
        assert all("〔" not in (e.get("value") or "") for e in kept)


class TestSanctionedSurvival:
    """真实披露数据 / sanctioned 值必须存活（设计红线 9）。"""

    def test_scenario_value_survives_normal_mode(self):
        kept, warns = _scrub_front_page_numbers(
            [_entry("DCF基准估值", "¥380", "基准情景公允价值¥380/股")],
            SANE_SCEN, MKT,
        )
        assert len(kept) == 1
        assert kept[0]["value"] == "¥380"
        assert warns == []

    def test_market_price_survives(self):
        kept, _ = _scrub_front_page_numbers(
            [_entry("现价", "¥349.00", "当前市价¥349.00隐含较高估值预期")],
            SANE_SCEN, MKT,
        )
        assert len(kept) == 1

    def test_aggregate_and_growth_and_margin_survive(self):
        entries = [
            _entry("净现金", "¥53亿", "资产负债表净现金¥53亿，提供下行保护"),
            _entry("营收同比增速", "+45.2%", "2025年营收同比增长45.2%"),
            _entry("毛利率", "23.5%", "毛利率23.5%，显著高于同业"),
        ]
        kept, warns = _scrub_front_page_numbers(entries, SANE_SCEN, MKT)
        assert [e["label"] for e in kept] == ["净现金", "营收同比增速", "毛利率"]
        assert warns == []

    def test_segment_pct_whitelist_survives_directional_context(self):
        # L1 Wave 1 白名单：方向性上下文（"估值修复"命中 upside 关键词）
        # 里的分部占比 % 若无白名单会被 % 一致性检查扫到。
        entry = _entry(
            "智能装备业务占比", "62.4%",
            "该分部收入占比62.4%，估值修复空间取决于其毛利率走势",
        )
        kept, _ = _scrub_front_page_numbers(
            [entry], SANE_SCEN, MKT, extra_sanctioned_pcts=[62.4],
        )
        assert len(kept) == 1
        assert kept[0]["value"] == "62.4%"

    def test_customer_concentration_two_decimal_survives_strict(self):
        # L1 Wave 2 白名单：两位小数披露值（38.27%）+ ±0.5pp 容差，
        # strict 票下也不被误杀。
        entries = [
            _entry("前五大客户集中度", "38.27%",
                   "前五大客户贡献38.27%营收，估值修复空间受此制约"),
            _entry("第一大客户占比", "38.5%",  # 与 38.27 差 0.23pp，容差内
                   "第一大客户占比38.5%，估值修复弹性受限"),
        ]
        kept, _ = _scrub_front_page_numbers(
            entries, MISMATCH_SCEN, MKT,
            extra_sanctioned_pcts=[38.27], strict=True,
        )
        assert len(kept) == 2


class TestStrictTicket:
    """strict（估值失配）票：连 sanctioned 情景值也不可引用 → 整条剔除。"""

    def test_scenario_value_dropped_in_strict(self):
        kept, warns = _scrub_front_page_numbers(
            [_entry("DCF基准", "¥4000", "DCF基准公允价值¥4000/股")],
            MISMATCH_SCEN, MKT, strict=True,
        )
        assert kept == []
        assert any("DROPPED" in w for w in warns)

    def test_directional_pct_dropped_in_strict(self):
        # 失配票 sanctioned 回报集为空——任何方向性 % 都被剔除
        kept, _ = _scrub_front_page_numbers(
            [_entry("下行空间", "70%", "较现价存在70%下行空间")],
            MISMATCH_SCEN, MKT, strict=True,
        )
        assert kept == []

    def test_nondirectional_margin_survives_strict(self):
        # 无方向上下文的 margin% 不是回报主张，strict 票也保留
        kept, _ = _scrub_front_page_numbers(
            [_entry("毛利率", "23.5%", "毛利率23.5%")],
            MISMATCH_SCEN, MKT, strict=True,
        )
        assert len(kept) == 1


class TestContextOnlyRewrite:
    """仅 context 违规：entry 保留、context 原位替换（与正文字段一致）。"""

    def test_context_rewritten_value_intact(self):
        entry = _entry(
            "DCF基准估值", "¥380",
            "该股较现价有90%上行空间，基准情景公允价值¥380/股",
        )
        kept, warns = _scrub_front_page_numbers([entry], SANE_SCEN, MKT)
        assert len(kept) == 1
        assert kept[0]["value"] == "¥380"          # 大字数字原封不动
        assert "90%" not in kept[0]["context"]      # 违规 % 被替换
        assert "〔回报口径详见DCF情景〕" in kept[0]["context"]  # 中文占位符
        assert any("CONTEXT REWRITTEN" in w for w in warns)


class TestLabelUntouched:
    """label 无数字的 entry 不受影响，label 原样保留。"""

    def test_clean_entries_pass_through_unchanged(self):
        entries = [
            _entry("自由现金流转化率", "68%", "经营现金流对净利润的覆盖为68%"),
            _entry("研发费用率", "12.3%", "研发投入占营收12.3%"),
        ]
        kept, warns = _scrub_front_page_numbers(entries, SANE_SCEN, MKT)
        assert kept == entries
        assert warns == []

    def test_label_preserved_when_context_rewritten(self):
        entry = _entry(
            "估值锚", "¥380",
            "较现价有90%上行空间〔基准情景¥380〕",
        )
        kept, _ = _scrub_front_page_numbers([entry], SANE_SCEN, MKT)
        assert len(kept) == 1
        assert kept[0]["label"] == "估值锚"


class TestInputBoundaries:
    """BUG-Y26 家族边界：JSON 字符串 / 空值 / 非 dict 元素。"""

    def test_json_string_entries_coerced_and_scrubbed(self):
        raw = json.dumps([
            {"label": "目标价", "value": "¥1640", "context": "合理目标价¥1640"},
            {"label": "毛利率", "value": "23.5%", "context": "毛利率23.5%"},
        ], ensure_ascii=False)
        kept, _ = _scrub_front_page_numbers(raw, SANE_SCEN, MKT)
        assert [e["label"] for e in kept] == ["毛利率"]

    def test_empty_and_none_inputs(self):
        assert _scrub_front_page_numbers([], SANE_SCEN, MKT) == ([], [])
        assert _scrub_front_page_numbers(None, SANE_SCEN, MKT) == ([], [])

    def test_non_dict_elements_filtered(self):
        kept, _ = _scrub_front_page_numbers(
            ["not a dict", 42, _entry("毛利率", "23.5%", "毛利率23.5%")],
            SANE_SCEN, MKT,
        )
        assert len(kept) == 1

    def test_no_scenarios_passes_through(self):
        # 情景缺失时清洗器 no-op——entry 原样保留（smoke / rule-based 路径）
        kept, warns = _scrub_front_page_numbers(
            [_entry("目标价", "¥1640", "合理目标价¥1640")], {}, MKT,
        )
        assert len(kept) == 1
        assert warns == []


class TestEditorWiring:
    """ReportEditor.edit() 全链路：清洗在 try 块内，接线错误会静默失效。"""

    def _edit(self, fpn, scenarios, meta_facts=None):
        e = ReportEditor()
        e._llm = MagicMock()
        e._llm.call_structured.return_value = {
            "headline": "标题",
            "front_page_numbers": fpn,
        }
        return e.edit(
            entity_name="宁德时代",
            synthesized_thesis=MagicMock(unresolved_tensions=[]),
            directive=None,
            computed_metrics={}, market_data=dict(MKT),
            scenarios=dict(scenarios),
            meta_facts=meta_facts or {}, segment_detail=None,
        )

    def test_fabricated_dropped_sanctioned_kept_end_to_end(self):
        edited = self._edit(
            [
                {"label": "目标价", "value": "¥1640", "context": "合理目标价¥1640"},
                {"label": "DCF基准估值", "value": "¥380",
                 "context": "基准情景公允价值¥380/股"},
            ],
            SANE_SCEN,
        )
        assert [n["label"] for n in edited.front_page_numbers] == ["DCF基准估值"]

    def test_strict_ticket_drops_all_dcf_numbers_end_to_end(self):
        # 失配票（base 4000 vs 市价 349）→ strict：情景值也整条剔除；
        # 白名单披露值（客户集中度）存活。
        edited = self._edit(
            [
                {"label": "DCF基准", "value": "¥4000",
                 "context": "DCF基准公允价值¥4000/股"},
                {"label": "前五大客户集中度", "value": "38.27%",
                 "context": "前五大客户贡献38.27%营收，估值修复空间受此制约"},
            ],
            MISMATCH_SCEN,
            meta_facts={
                "__customer_concentration": {"sanctioned_pcts": [38.27]},
            },
        )
        assert [n["label"] for n in edited.front_page_numbers] == ["前五大客户集中度"]

    def test_segment_whitelist_wired_from_meta_facts(self):
        # L1 Wave 1 白名单经 meta_facts.__segment_composition 透传到位
        edited = self._edit(
            [
                {"label": "智能装备业务占比", "value": "62.4%",
                 "context": "该分部收入占比62.4%，估值修复空间取决于其毛利率走势"},
            ],
            SANE_SCEN,
            meta_facts={
                "__segment_composition": {"sanctioned_pcts": [62.4]},
            },
        )
        assert len(edited.front_page_numbers) == 1

    def test_all_dropped_yields_empty_list_not_shells(self):
        edited = self._edit(
            [
                {"label": "目标价", "value": "¥1640", "context": "合理目标价¥1640"},
                {"label": "潜在回报", "value": "+90%",
                 "context": "较现价有90%上行空间"},
            ],
            SANE_SCEN,
        )
        assert edited.front_page_numbers == []


class TestEmptyRenderDegrade:
    """清洗后为空列表 → HTML 渲染优雅降级。

    v2 渲染器（html_report.py 别名 → html_report_v2）只消费 edited_report
    的 headline / opening_paragraph，front_page_numbers 没有独立渲染区块
    ——空列表不产生任何空壳区块，且整页渲染不受影响。
    """

    @pytest.fixture(autouse=True)
    def _offline(self, monkeypatch):
        monkeypatch.setenv("AEGIS_SKIP_SPARKLINE", "1")

    def test_render_with_empty_front_page_numbers(self):
        from aegis.core.reports import html_report_v2 as v2

        decision = SimpleNamespace(
            entity_id="600519",
            publishing_status="published",
            confidence_bucket="medium",
            bias_check_status="通过",
            run_id="run_test",
            period="FY2025",
            open_questions=[],
            dcf_output=None,
        )
        edited = SimpleNamespace(
            headline="测试标题：首页数字全部被清洗后仍可渲染",
            executive_summary="摘要。",
            front_page_numbers=[],   # 清洗后为空
            section_order=[], section_emphasis={}, key_exhibits=[],
            opening_paragraph="开篇段落。", closing_paragraph="收尾段落。",
            risk_summary="风险摘要。", de_emphasized=[],
        )
        html = v2.generate_html_report(
            decision,
            computed_metrics={},
            market_data={"current_price": 100.0},
            agent_judgments=[],
            critic_results=[],
            meta_facts={
                "ebitda": 5e9, "operating_income": 4e9, "revenue": 2e10,
                "net_income": 3e9, "total_equity": 1e10,
                "shares_outstanding": 1e9,
            },
            scenarios={
                "currency": "CNY",
                "base_value": 150.0, "probability_weighted_value": 150.0,
                "bear_value": 100.0, "bear_probability": 0.25,
                "base_probability": 0.50, "bull_value": 200.0,
                "bull_probability": 0.25,
            },
            entity_name="贵州茅台",
            entity_name_clean="贵州茅台",
            period="FY2025",
            edited_report=edited,
        )
        # 页面渲染成功，headline 注入，且注入的 window.REPORT JSON 可解析
        assert "测试标题" in html
        marker = "window.REPORT = "
        start = html.index(marker) + len(marker)
        end = html.index(";</script>", start)
        json.loads(html[start:end])
