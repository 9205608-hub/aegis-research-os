"""Aegis 2.0 Phase 0 接线回归 — 预期前沿 / 近事件 / 定价体制成为报告主轴.

覆盖（任务清单第 7 项）：
1. orchestrator 纯函数辅助：margin 档构造（sector pack 字段 / 8% 缺省 /
   去重）、pricing_regime 特征映射（含 pw≤0 极端哨兵、NI≤0 口径失效）。
2. 全管线（offline META fixture）：meta_facts["__expectations_frontier"] /
   ["__pricing_regime"] 落库，dataclass 树可 JSON 序列化。
3. frontier_prompt_lines zh/en 双语句式（设计红线 2 条件化）+
   frontier_sanctioned_growth_pcts 白名单生成器（设计红线 9）。
4. scrubber：前沿隐含增速 % 在方向性语境下不再误报（白名单命中）；
   不在白名单时照旧报警（控制组）；margin % 走 growth-context 豁免。
5. numeric_consistency_critic 对「若利润率 X% 需 Y% 增速」句式不误判。
6. valuation / variant 规则模板：前沿在场→条件化观察（zh/en），
   前沿缺席→legacy 单点观察，A9 unreliable gate 语义保留。
7. chief-analyst 三件套 prompt 注入：前沿表 / 体制 narrative / 事件块。
8. 渲染层「市场在定价什么」区块：中文 label、无解/极端标注、验证点
   「未核验」、事件摘要；缺数据时 pricedIn=None（显示「暂无」而非烂值）。
9. blocked 评级新文案（预期无法验证 · 暂不评级）在 test_html_report_v2
   迁移断言之外，这里锁 end-to-end JSON 路径。
"""

from types import SimpleNamespace
from unittest.mock import patch

import json

import pytest

from aegis.core.agents.valuation_analyst.agent import ValuationAnalyst
from aegis.core.agents.variant_analyst.agent import VariantAnalyst
from aegis.core.agents.base import AgentInput
from aegis.core.chief_analyst.report_editor import ReportEditor
from aegis.core.chief_analyst.research_director import ResearchDirector
from aegis.core.chief_analyst.thesis_synthesizer import (
    ThesisSynthesizer,
    _scrub_fair_value_claims,
    frontier_prompt_lines,
    frontier_sanctioned_growth_pcts,
)
from aegis.core.critics.numeric_consistency_critic.critic import (
    NumericConsistencyCritic,
)
from aegis.core.orchestrator.auto_research import (
    AutoResearchOrchestrator,
    ResearchConfig,
    build_margin_scenarios,
    compute_pricing_regime_inputs,
    DEFAULT_SECTOR_TYPICAL_MARGIN,
)
from aegis.core.reports import html_report_v2 as v2
from aegis.data_contracts.judgment_schema import (
    CognitiveBiasSelfCheck,
    Inference,
    JudgmentContract,
    Observation,
)

from test_auto_research import (
    MOCK_XBRL_FACTS,
    _FakeCatalystCalendar,
    _FakeForm4Connector,
    _FakeMarketDataConnector,
    _make_mock_packet,
)


# ─────────────────────────────────────────────────────────────────
# 共享 fixture：一棵手工构造的前沿 dict（与 ExpectationsFrontier.to_dict()
# 同构），含 solved / 多解 / 无解 / 极端 四种单元。
# ─────────────────────────────────────────────────────────────────

def _col(delta, growths=(), status="solved", diag_zh="", diag_en="",
         extreme=False):
    sols = [
        {"implied_growth": g, "cumulative_revenue_scale": (1 + g) ** 10,
         "extreme_expectation": extreme}
        for g in growths
    ]
    return {
        "wacc": 0.09 + delta, "wacc_delta": delta,
        "status": status if growths else ("no_solution" if status == "solved" else status),
        "solutions": sols,
        "multiple_solutions": len(sols) > 1,
        "diagnostic_code": "" if growths else "value_below_price_everywhere",
        "diagnostic_zh": diag_zh, "diagnostic_en": diag_en,
        "grid_price_min": 1.0, "grid_price_max": 5.0,
        "valid_grid_points": 200,
    }


MOCK_FRONTIER = {
    "market_price": 13.76,
    "currency": "CNY",
    "base_wacc": 0.09,
    "horizon_years": 10,
    "growth_grid_low": -0.20,
    "growth_grid_high": 0.80,
    "growth_grid_step": 0.005,
    "scenarios": [
        {   # 维持现状档：全列无解（康达型）
            "label": "维持现状", "target_margin": 0.029,
            "starting_margin": 0.029, "margin_path": [0.029] * 10,
            "wacc_columns": [
                _col(-0.01, diag_zh="即使增速高达 +80.0%，该利润率档也撑不起现价",
                     diag_en="Even at +80.0% growth this scenario cannot reach the price"),
                _col(0.0, diag_zh="即使增速高达 +80.0%，该利润率档也撑不起现价",
                     diag_en="Even at +80.0% growth this scenario cannot reach the price"),
                _col(0.01, diag_zh="即使增速高达 +80.0%，该利润率档也撑不起现价",
                     diag_en="Even at +80.0% growth this scenario cannot reach the price"),
            ],
        },
        {   # 行业中位档：三列可解，基准 +42%
            "label": "行业中位", "target_margin": 0.08,
            "starting_margin": 0.029, "margin_path": [0.08] * 10,
            "wacc_columns": [
                _col(-0.01, growths=(0.385,), extreme=True),
                _col(0.0, growths=(0.42,), extreme=True),
                _col(0.01, growths=(0.455,), extreme=True),
            ],
        },
        {   # 中点档：基准列多解（非单调）
            "label": "两者中点", "target_margin": 0.055,
            "starting_margin": 0.029, "margin_path": [0.055] * 10,
            "wacc_columns": [
                _col(-0.01, growths=(0.55,)),
                _col(0.0, growths=(0.12, 0.58)),
                _col(0.01, growths=(0.60,)),
            ],
        },
    ],
}

MOCK_REGIME = {
    "weights": {"steady": 0.08, "growth": 0.12, "turnaround": 0.45, "story": 0.35},
    "dominant": "mixed",
    "top_two": ("turnaround", "story"),
    "scores": {"steady": -1.0, "growth": 0.0, "turnaround": 1.5, "story": 1.0},
    "features": {"dcf_gap": 5.4},
    "narrative_frame_zh": "市场定价框架处于「困境反转」与「题材叙事」之间的混合状态。",
    "narrative_frame_en": "The pricing regime is mixed between turnaround and story.",
    "verification_focus": [
        "盈利质量修复信号（CFO/净利比、应计项目占比回落）",
        "重组/并购整合的落地证据（公告、订单、业绩承诺兑现）",
    ],
}

MOCK_EVENTS = {
    "stock_code": "002669",
    "as_of": "2026-07-10",
    "announcements": [
        {"title": "第五届董事会第十二次会议决议公告", "date": "2026-07-02",
         "category": "董事会", "source": "eastmoney"},
        {"title": "关于全资子公司完成工商变更登记的公告", "date": "2026-06-20",
         "category": "", "source": "eastmoney"},
    ],
    "forecasts": [
        {"report_period": "2025-12-31", "forecast_type": "扭亏",
         "indicator": "每股收益", "value_low": 0.06, "value_high": 0.07,
         "change_pct_low": None, "change_pct_high": None,
         "notice_date": "2026-01-21", "prev_year_value": None},
        {"report_period": "2025-12-31", "forecast_type": "扭亏",
         "indicator": "归属于上市公司股东的净利润", "value_low": 1.25e8,
         "value_high": 1.35e8, "change_pct_low": 110.0,
         "change_pct_high": 120.0, "notice_date": "2026-01-21",
         "prev_year_value": None},
    ],
    "consensus": {
        "org_count": 1, "latest_report_date": None,
        "insufficient_coverage": True, "predictions": [],
    },
}

MOCK_EVENTS_PROMPT = (
    "以下为公开披露事实（截至 2026-07-10），分析必须以此为准，"
    "禁止引用未在此列出的催化剂或传闻。\n"
    "■ 业绩预告（东方财富）\n"
    "- 报告期 2025-12-31 | 类型: 扭亏 | 指标: 每股收益 | 预告区间: 0.06元 ~ 0.07元\n"
    "■ 一致预期（东方财富，旁证口径）\n"
    "- 无有效一致预期：近6个月覆盖机构 1 家，最近研报日期 无\n"
    "■ 近90天公告标题\n"
    "- 2026-07-02 第五届董事会第十二次会议决议公告"
)


def _meta_facts_with_phase0(**over):
    mf = {
        "ebitda": 5e8, "operating_income": 4e8, "revenue": 5e9,
        "net_income": 3e8, "total_equity": 2e9, "shares_outstanding": 4e8,
        "__expectations_frontier": MOCK_FRONTIER,
        "__pricing_regime": MOCK_REGIME,
        "__recent_events": MOCK_EVENTS,
        "__recent_events_prompt": MOCK_EVENTS_PROMPT,
    }
    mf.update(over)
    return mf


# ═════════════════════════════════════════════════════════════════
# 1. orchestrator 纯函数辅助
# ═════════════════════════════════════════════════════════════════

class TestBuildMarginScenarios:

    def test_three_tiers_from_valuation_framework(self):
        pack = {"valuation_framework": {"typical_operating_margin_range": [0.15, 0.25]}}
        scens = build_margin_scenarios(0.029, pack, zh=True)
        assert [s[0] for s in scens] == ["维持现状", "行业中位", "两者中点"]
        assert scens[0][1] == pytest.approx(0.029)
        assert scens[1][1] == pytest.approx(0.20)       # 区间中点
        assert scens[2][1] == pytest.approx((0.029 + 0.20) / 2, abs=1e-4)

    def test_benchmarks_fallback_field(self):
        pack = {"benchmarks": {"typical_margins": {"operating_margin": [0.10, 0.20]}}}
        scens = build_margin_scenarios(0.30, pack, zh=False)
        assert [s[0] for s in scens] == ["Hold current margin", "Sector median", "Midpoint"]
        assert scens[1][1] == pytest.approx(0.15)

    def test_default_8pct_when_pack_lacks_margin(self):
        scens = build_margin_scenarios(0.029, {"sector_name": "Generic"}, zh=True)
        assert scens[1][1] == pytest.approx(DEFAULT_SECTOR_TYPICAL_MARGIN)

    def test_dedupe_when_current_equals_sector(self):
        pack = {"valuation_framework": {"typical_operating_margin_range": [0.06, 0.10]}}
        scens = build_margin_scenarios(0.08, pack, zh=True)
        assert len(scens) == 1
        assert scens[0] == ("维持现状", 0.08)


class TestComputePricingRegimeInputs:

    def test_basic_mapping(self):
        kw = compute_pricing_regime_inputs(
            meta_facts={"net_income": 1e8, "ebitda": 2e8, "free_cash_flow": 5e7,
                        "__historical_growth": {2023: 0.05, 2024: 0.30},
                        "__revenue_cagr": 0.10},
            computed_metrics={"accruals_ratio": 0.05, "cfo_to_net_income": 1.2,
                              "net_debt": 1e8},
            market_price=15.0, pw_value=12.0,
            terminal_value_gate_triggered=True,
        )
        assert kw["dcf_gap"] == pytest.approx(0.25)
        assert kw["fcf_positive"] is True
        assert kw["cfo_to_ni"] == pytest.approx(1.2)
        assert kw["growth_regime_break"] == pytest.approx(0.20)
        assert kw["net_debt_to_ebitda"] == pytest.approx(0.5)
        assert kw["terminal_value_gate_triggered"] is True

    def test_nonpositive_pw_maps_to_extreme_gap(self):
        kw = compute_pricing_regime_inputs({}, {}, 13.76, -0.5, False)
        assert kw["dcf_gap"] == pytest.approx(10.0)  # 极端溢价哨兵（clip 上限）

    def test_negative_ni_disables_cfo_ratio(self):
        kw = compute_pricing_regime_inputs(
            {"net_income": -1e8}, {"cfo_to_net_income": -3.0}, 10.0, 5.0, False)
        assert kw["cfo_to_ni"] is None  # 口径失效不猜

    def test_unreliable_cagr_disables_break(self):
        kw = compute_pricing_regime_inputs(
            {"__historical_growth": {2024: 0.5}, "__revenue_cagr": 0.1,
             "__revenue_cagr_unreliable": True},
            {}, 10.0, 5.0, False)
        assert kw["growth_regime_break"] is False

    def test_nonpositive_ebitda_disables_leverage(self):
        kw = compute_pricing_regime_inputs(
            {"ebitda": -1e8, "net_debt": 5e8}, {}, 10.0, 5.0, False)
        assert kw["net_debt_to_ebitda"] is None


# ═════════════════════════════════════════════════════════════════
# 2. 全管线（offline）：前沿 + 体制落进 meta_facts
# ═════════════════════════════════════════════════════════════════

class TestOrchestratorWiring:

    @patch("aegis.core.acquisition.connectors.edgar_connector.SECEDGARConnector.fetch")
    def test_frontier_and_regime_in_meta_facts(self, mock_fetch):
        mock_fetch.return_value = _make_mock_packet(MOCK_XBRL_FACTS)
        orch = AutoResearchOrchestrator(
            market_data_connector_factory=_FakeMarketDataConnector,
            catalyst_calendar_factory=_FakeCatalystCalendar,
            form4_connector_factory=_FakeForm4Connector,
        )
        result = orch.run(ResearchConfig(
            ticker="META", period="FY2024", current_price=585.0,
            market_cap=1_510_000_000_000, generate_html=False,
            enable_openbb=False, enable_news_sentiment=False,
            enable_recent_events=False,
        ))

        frontier = result.meta_facts.get("__expectations_frontier")
        assert isinstance(frontier, dict)
        assert frontier["market_price"] == pytest.approx(585.0)
        assert frontier["scenarios"], "至少一档利润率情景"
        for scen in frontier["scenarios"]:
            assert len(scen["wacc_columns"]) == 3  # WACC±1% 三列（设计红线 2）
        json.dumps(frontier)  # 可序列化（replay / 渲染前提）

        regime = result.meta_facts.get("__pricing_regime")
        assert isinstance(regime, dict)
        assert set(regime["weights"]) == {"steady", "growth", "turnaround", "story"}
        assert sum(regime["weights"].values()) == pytest.approx(1.0)
        assert regime["dominant"] in {"steady", "growth", "turnaround", "story", "mixed"}
        assert regime["narrative_frame_zh"] and regime["narrative_frame_en"]
        assert regime["verification_focus"]
        json.dumps(regime, default=list)

        # US 路径不拉 A 股事件切片
        assert "__recent_events" not in result.meta_facts

    def test_config_has_recent_events_flag(self):
        assert ResearchConfig(ticker="X").enable_recent_events is True


# ═════════════════════════════════════════════════════════════════
# 3. frontier prompt 行渲染 + 白名单生成器
# ═════════════════════════════════════════════════════════════════

class TestFrontierPromptLines:

    def test_zh_conditional_sentence(self):
        lines = frontier_prompt_lines(MOCK_FRONTIER, "zh")
        assert len(lines) == 3
        solved = lines[1]
        assert "若终年营业利润率为 8.0%（行业中位）" in solved
        assert "¥13.76" in solved
        assert "+42.0%" in solved
        assert "WACC±1% 区间: +38.5% ~ +45.5%" in solved
        assert "极端预期" in solved

    def test_zh_no_solution_uses_engine_diagnostic(self):
        lines = frontier_prompt_lines(MOCK_FRONTIER, "zh")
        assert "维持现状" in lines[0]
        assert "撑不起现价" in lines[0]   # 引擎 diagnostic_zh 原文

    def test_zh_multiple_solutions_flagged(self):
        lines = frontier_prompt_lines(MOCK_FRONTIER, "zh")
        assert "+12.0%、+58.0%" in lines[2]
        assert "非单调" in lines[2]

    def test_en_conditional_sentence(self):
        lines = frontier_prompt_lines(MOCK_FRONTIER, "en")
        assert "At a terminal operating margin of 8.0%" in lines[1]
        assert "requires roughly +42.0% annual revenue growth" in lines[1]
        assert "cannot reach the price" in lines[0]  # diagnostic_en 原文

    def test_empty_or_none_frontier(self):
        assert frontier_prompt_lines(None, "zh") == []
        assert frontier_prompt_lines({}, "zh") == []

    def test_sanctioned_pcts_include_all_solutions_and_margins(self):
        pcts = frontier_sanctioned_growth_pcts(MOCK_FRONTIER)
        # WACC±1% 列的解全部入册（设计红线 9）
        for expected in (38.5, 42.0, 45.5, 12.0, 58.0, 55.0, 60.0):
            assert expected in pcts
        # margin 档百分数也入册
        for expected in (2.9, 8.0, 5.5):
            assert expected in pcts
        assert frontier_sanctioned_growth_pcts(None) == []


# ═════════════════════════════════════════════════════════════════
# 4. scrubber 白名单（设计红线 9）
# ═════════════════════════════════════════════════════════════════

# sanctioned DCF-vs-price returns = −30% / −20% / −10%（远离 42% 与 83%，
# 避免与被测百分数撞进 ±10pt 容差——83% 下行对康达实盘情景反而是合法的）。
_SCRUB_SCENARIOS = {
    "currency": "CNY",
    "bear_value": 70.0, "base_value": 80.0, "bull_value": 90.0,
    "probability_weighted_value": 80.0,
}
_SCRUB_MKT = {"current_price": 100.0}


class TestScrubberFrontierWhitelist:

    # 控制/实验组共用文本：方向词「下行」在场，且 % 附近刻意不带
    # 增速/营收等 growth-context 关键词——只有前沿白名单能放行它。
    _TEXT = "现价存在下行风险：42.0% 的预期缺乏事实支撑。"

    def test_frontier_growth_pct_in_direction_context_not_flagged(self):
        # 42% 与任何 DCF-vs-price return（−30/−20/−10）都不匹配 →
        # 旧逻辑必报警；命中前沿白名单后放行（设计红线 9）。
        pcts = frontier_sanctioned_growth_pcts(MOCK_FRONTIER)
        _, warns = _scrub_fair_value_claims(
            {"core_thesis": self._TEXT}, _SCRUB_SCENARIOS, _SCRUB_MKT,
            fields=("core_thesis",), extra_sanctioned_pcts=pcts,
        )
        assert not any("% RETURN CONSISTENCY" in w for w in warns)

    def test_same_pct_without_whitelist_still_flagged(self):
        # 控制组：不传白名单时照旧报警——证明放行确实来自白名单。
        _, warns = _scrub_fair_value_claims(
            {"core_thesis": self._TEXT}, _SCRUB_SCENARIOS, _SCRUB_MKT,
            fields=("core_thesis",),
        )
        assert any("% RETURN CONSISTENCY" in w for w in warns)

    def test_frontier_sentence_exempt_via_growth_context(self):
        # 标准前沿句式带「利润率/增速」——growth-context 豁免路径直接放行
        # （margin 档 2.9%/8% 百分数也在此路径下天然安全）。
        raw = {"market_implied_story": "存在下行风险。若利润率维持 2.9%，"
                                       "现价需要 42.0% 增速支撑；若达行业中位 "
                                       "8.0%，需要 12.0% 增速。"}
        _, warns = _scrub_fair_value_claims(
            raw, _SCRUB_SCENARIOS, _SCRUB_MKT, fields=("market_implied_story",),
        )
        assert not any("% RETURN CONSISTENCY" in w for w in warns)

    def test_true_return_hallucination_still_flagged_with_whitelist(self):
        # 白名单不放行真正的 return 幻觉（83% 不在前沿解集内）。
        raw = {"core_thesis": "较现价存在 83% 下行空间。"}
        pcts = frontier_sanctioned_growth_pcts(MOCK_FRONTIER)
        _, warns = _scrub_fair_value_claims(
            raw, _SCRUB_SCENARIOS, _SCRUB_MKT, fields=("core_thesis",),
            extra_sanctioned_pcts=pcts,
        )
        assert any("% RETURN CONSISTENCY" in w for w in warns)


# ═════════════════════════════════════════════════════════════════
# 5. numeric_consistency_critic 不误判前沿句式
# ═════════════════════════════════════════════════════════════════

def _judgment(obs_texts):
    return JudgmentContract(
        judgment_id="j_phase0",
        agent_name="valuation_analyst",
        agent_version="v1_test",
        question_id="q_test",
        run_id="run_test",
        judgment_status="complete",
        observations=[Observation(text=t, source_ids=["fact:test"]) for t in obs_texts],
        inferences=[Inference(text="中性结论。", confidence="medium",
                              based_on_observation_indices=[0])],
        cognitive_bias_self_check=CognitiveBiasSelfCheck(
            anchoring_risk="low", confirmation_bias_risk="low",
            recency_bias_risk="low", narrative_fallacy_risk="low",
        ),
    )


class TestNumericCriticFrontierSentence:

    def test_conditional_frontier_sentence_not_flagged(self):
        j = _judgment([
            "市场隐含预期（条件化反解）: 若终年营业利润率为 8.0%（行业中位），"
            "现价 ¥13.76 需要约 +42.0% 的年营收增速支撑"
            "（WACC±1% 区间: +38.5% ~ +45.5%）",
            "若利润率维持 2.9%，现价需要 42.0% 增速支撑。",
        ])
        result = NumericConsistencyCritic().review([j])
        assert result.issues == []


# ═════════════════════════════════════════════════════════════════
# 6. valuation / variant 规则模板双语句式
# ═════════════════════════════════════════════════════════════════

def _agent_input(zh: bool, frontier_lines=None, legacy_growth=None,
                 unreliable=False):
    priced_in = {
        "implied_terminal_growth": 0.025,
        "implied_growth_unreliable": unreliable,
        "implied_revenue_growth": legacy_growth,
    }
    if frontier_lines is not None:
        priced_in["expectations_frontier"] = {
            "market_price": 13.76, "currency": "CNY", "base_wacc": 0.09,
            "lines": frontier_lines,
        }
    mc = {
        "priced_in": priced_in,
        "scenarios": {"bear_value": 1.5, "base_value": 2.15, "bull_value": 3.0},
        "current_price": 13.76,
        "cycle_phase": "中期扩张" if zh else "mid-cycle",
    }
    if zh:
        mc["language"] = "zh-CN"
        mc["market_id"] = "cn"
    return AgentInput(
        entity_id="sz_002669" if zh else "us_test",
        run_id="run_test", question_id="q_test",
        metric_results={"pe_ratio": 25.0}, macro_context=mc,
    )


_ZH_LINE = ("若终年营业利润率为 8.0%（行业中位），现价 ¥13.76 需要约 "
            "+42.0% 的年营收增速支撑（WACC±1% 区间: +38.5% ~ +45.5%）")
_EN_LINE = ("At a terminal operating margin of 8.0% (sector median), the "
            "current price ¥13.76 requires roughly +42.0% annual revenue "
            "growth (WACC±1% range: +38.5% ~ +45.5%)")


class TestValuationAnalystFrontier:

    def test_zh_frontier_observation(self):
        out = ValuationAnalyst().run(_agent_input(True, [_ZH_LINE]))
        obs = [o.text for o in out.judgment.observations]
        assert any(o.startswith("市场隐含预期（条件化反解）: 若终年营业利润率")
                   for o in obs)
        # 设计红线 2：前沿在场时不再输出单点隐含增速
        assert not any("市场隐含营收增速" in o for o in obs)

    def test_en_frontier_observation(self):
        out = ValuationAnalyst().run(_agent_input(False, [_EN_LINE]))
        obs = [o.text for o in out.judgment.observations]
        assert any(o.startswith("Market-implied expectation (conditional):")
                   for o in obs)
        assert not any("Market-implied revenue growth:" in o for o in obs)

    def test_zh_frontier_inference_conditional_framing(self):
        out = ValuationAnalyst().run(_agent_input(True, [_ZH_LINE]))
        infs = [i.text for i in out.judgment.inferences]
        assert any("若利润率 X%，需 Y% 增速支撑" in t for t in infs)

    def test_legacy_single_point_when_frontier_absent(self):
        out = ValuationAnalyst().run(_agent_input(True, None, legacy_growth=0.30))
        obs = [o.text for o in out.judgment.observations]
        assert "市场隐含营收增速: 30.00%" in obs

    def test_a9_unreliable_gate_preserved(self):
        # 前沿缺席 + unreliable 标志 → 单点观察也不输出（AUDIT-A9 语义）
        out = ValuationAnalyst().run(
            _agent_input(True, None, legacy_growth=0.50, unreliable=True))
        obs = [o.text for o in out.judgment.observations]
        assert not any("市场隐含营收增速" in o for o in obs)


class TestVariantAnalystFrontier:

    def test_zh_frontier_observation_and_inference(self):
        out = VariantAnalyst().run(_agent_input(True, [_ZH_LINE]))
        obs = [o.text for o in out.judgment.observations]
        assert any("市场隐含预期（条件化反解）" in o for o in obs)
        assert not any("市场隐含营收增速" in o for o in obs)
        infs = [i.text for i in out.judgment.inferences]
        assert any("支撑不了的部分即为预期差" in t for t in infs)

    def test_en_frontier_observation(self):
        out = VariantAnalyst().run(_agent_input(False, [_EN_LINE]))
        obs = [o.text for o in out.judgment.observations]
        assert any("Market-implied expectation (conditional):" in o for o in obs)
        infs = [i.text for i in out.judgment.inferences]
        assert any("expectations gap" in t for t in infs)

    def test_legacy_fallback_when_frontier_absent(self):
        out = VariantAnalyst().run(_agent_input(True, None, legacy_growth=0.30))
        obs = [o.text for o in out.judgment.observations]
        assert "市场隐含营收增速: 30.00%" in obs


# ═════════════════════════════════════════════════════════════════
# 7. chief-analyst prompt 注入
# ═════════════════════════════════════════════════════════════════

_CN_SCENARIOS = {
    "currency": "CNY", "bear_value": 1.5, "base_value": 2.15,
    "bull_value": 3.0, "probability_weighted_value": 2.15,
}
_CN_META_DISPLAY = {"__currency": "CNY", "__display": {
    "currency": "CNY", "symbol": "¥", "scale": 1e8, "unit": "亿",
    "big_scale": 1e12, "big_unit": "万亿",
}}


def _thesis_ns():
    return SimpleNamespace(
        core_thesis="核心论点", my_variant="变体", variant_magnitude="幅度",
        why_now="现在", market_implied_story="市场故事",
        key_assumption_disagreement="分歧", counter_thesis="反论",
        why_market_is_wrong="错因", what_would_change_my_mind="改观",
        conviction_narrative="信念", unresolved_tensions=[],
    )


class TestChiefAnalystPromptInjection:

    def test_synthesizer_message_contains_phase0_blocks(self):
        mf = _meta_facts_with_phase0(**_CN_META_DISPLAY)
        msg = ThesisSynthesizer()._build_message(
            "002669", "康达新材", None, [], {}, {"current_price": 13.76},
            dict(_CN_SCENARIOS), 0.0, [], mf, None, None,
        )
        assert "MARKET-IMPLIED EXPECTATIONS FRONTIER" in msg
        assert "若终年营业利润率为 8.0%（行业中位）" in msg
        assert "PRICING REGIME" in msg
        assert "困境反转" in msg  # narrative_frame_zh
        assert "RECENT DISCLOSED EVENTS" in msg
        assert "禁止引用未在此列出的催化剂" in msg

    def test_synthesizer_message_no_blocks_when_absent(self):
        msg = ThesisSynthesizer()._build_message(
            "META", "Meta", None, [], {}, {"current_price": 585.0},
            {"currency": "USD", "base_value": 500.0}, 0.1, [],
            {"__currency": "USD"}, None, None,
        )
        assert "EXPECTATIONS FRONTIER" not in msg
        assert "RECENT DISCLOSED EVENTS" not in msg

    def test_director_message_reads_agent_macro(self):
        macro = {
            "cycle_phase": "late_expansion",
            "priced_in": {"expectations_frontier": {
                "market_price": 13.76, "currency": "CNY", "base_wacc": 0.09,
                "lines": [_ZH_LINE],
            }},
            "pricing_regime": {
                "dominant": "mixed", "top_two": ["turnaround", "story"],
                "weights": {"steady": 0.08, "growth": 0.12,
                            "turnaround": 0.45, "story": 0.35},
                "narrative_frame": "市场定价框架处于混合状态。",
                "verification_focus": ["盈利质量修复信号"],
            },
            "recent_events": MOCK_EVENTS_PROMPT,
        }
        msg = ResearchDirector()._build_message(
            "002669", "康达新材", dict(_CN_META_DISPLAY), {}, macro, {},
            None, {"current_price": 13.76}, None, None, None,
            dict(_CN_SCENARIOS), None, None,
        )
        assert "MARKET-IMPLIED EXPECTATIONS FRONTIER" in msg
        assert _ZH_LINE in msg
        assert "PRICING REGIME" in msg
        assert "dominant: mixed" in msg
        assert "RECENT DISCLOSED EVENTS" in msg
        assert "业绩预告" in msg

    def test_editor_message_contains_phase0_blocks(self):
        mf = _meta_facts_with_phase0(**_CN_META_DISPLAY)
        msg = ReportEditor()._build_message(
            "康达新材", _thesis_ns(), None, {}, {"current_price": 13.76},
            dict(_CN_SCENARIOS), mf, None,
        )
        assert "MARKET-IMPLIED EXPECTATIONS FRONTIER" in msg
        assert "PRICING REGIME" in msg
        assert "RECENT DISCLOSED EVENTS" in msg

    def test_prompts_carry_expectations_first_rules(self):
        from aegis.core.chief_analyst.thesis_synthesizer import (
            THESIS_SYNTHESIZER_SYSTEM_PROMPT,
        )
        from aegis.core.chief_analyst.research_director import (
            RESEARCH_DIRECTOR_SYSTEM_PROMPT,
        )
        from aegis.core.chief_analyst.report_editor import (
            REPORT_EDITOR_SYSTEM_PROMPT,
        )
        for prompt in (THESIS_SYNTHESIZER_SYSTEM_PROMPT,
                       RESEARCH_DIRECTOR_SYSTEM_PROMPT,
                       REPORT_EDITOR_SYSTEM_PROMPT):
            assert "EXPECTATIONS" in prompt
        # 禁止裸 ±% headline 主张（Editor 是最后防线）
        assert "PROHIBITED as the headline claim" in REPORT_EDITOR_SYSTEM_PROMPT
        # 设计红线 1：体制只改叙事，不得隐藏估值差
        assert "never justifies omitting the valuation gap" in REPORT_EDITOR_SYSTEM_PROMPT


# ═════════════════════════════════════════════════════════════════
# 8. 渲染层「市场在定价什么」区块
# ═════════════════════════════════════════════════════════════════

def _decision(**over):
    base = dict(
        entity_id="002669", publishing_status="published",
        confidence_bucket="medium", bias_check_status="通过",
        run_id="run_test", period="FY2025", open_questions=[],
        dcf_output=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _build_cn_report(**meta_over):
    return v2.build_report_dict(
        decision=_decision(),
        market_data={"current_price": 13.76},
        meta_facts=_meta_facts_with_phase0(**meta_over),
        scenarios=dict(_CN_SCENARIOS),
        entity_id="002669",
        entity_name="康达新材",
        entity_name_clean="康达新材",
        period="FY2025",
    )


class TestPricedInBlock:

    def test_block_present_with_chinese_labels(self):
        rep = _build_cn_report()
        p = rep["pricedIn"]
        assert p is not None
        assert p["title"] == "市场在定价什么"

    def test_frontier_table_rows_and_cells(self):
        p = _build_cn_report()["pricedIn"]
        f = p["frontier"]
        assert f["waccCols"] == ["WACC−1%", "基准", "WACC+1%"]
        assert "¥13.76" in f["priceLine"]
        rows = {r["label"]: r for r in f["rows"]}
        # 无解档：中文标注 + 引擎诊断进 tooltip
        no_sol = rows["维持现状"]["cells"][1]
        assert no_sol["text"] == "无解"
        assert "撑不起现价" in no_sol["diag"]
        # 可解档：+42.0% + 极端标注
        solved = rows["行业中位"]["cells"][1]
        assert solved["text"] == "+42.0%"
        assert "extreme" in solved["flags"]
        # 多解档
        multi = rows["两者中点"]["cells"][1]
        assert "multiple" in multi["flags"]
        assert "+12.0%" in multi["text"] and "+58.0%" in multi["text"]

    def test_regime_block_mixed_label_and_weights(self):
        p = _build_cn_report()["pricedIn"]
        r = p["regime"]
        assert r["mixed"] is True
        assert r["dominantLabel"] == "混合（困境反转 × 题材叙事）"
        assert r["weights"][0]["key"] == "turnaround"  # 权重降序
        assert r["weights"][0]["pct"] == pytest.approx(45.0)
        assert "混合状态" in r["narrative"]

    def test_verification_marked_unverified(self):
        p = _build_cn_report()["pricedIn"]
        assert p["verification"], "验证点清单非空"
        assert all(v["status"] == "未核验" for v in p["verification"])

    def test_events_summary_chinese(self):
        p = _build_cn_report()["pricedIn"]
        ev = p["events"]
        assert ev["asOf"] == "2026-07-10"
        # 每股收益 0.06 元不得被压成「0元」（em_events 实测坑）
        eps_row = next(f for f in ev["forecasts"] if f["indicator"] == "每股收益")
        assert eps_row["range"] == "0.06元 ~ 0.07元"
        npf = next(f for f in ev["forecasts"] if "净利润" in f["indicator"])
        assert npf["range"] == "1.25亿元 ~ 1.35亿元"
        assert ev["announcements"][0]["title"].startswith("第五届董事会")
        assert "无有效一致预期" in ev["consensusLine"]

    def test_missing_data_yields_none_block(self):
        rep = v2.build_report_dict(
            decision=_decision(),
            market_data={"current_price": 13.76},
            meta_facts={"ebitda": 5e8, "operating_income": 4e8},
            scenarios=dict(_CN_SCENARIOS),
            entity_id="002669", entity_name="康达新材",
            entity_name_clean="康达新材", period="FY2025",
        )
        assert rep["pricedIn"] is None  # 前端据此显示「暂无」/隐藏区块

    def test_partial_data_frontier_only(self):
        rep = v2.build_report_dict(
            decision=_decision(),
            market_data={"current_price": 13.76},
            meta_facts={"ebitda": 5e8, "__expectations_frontier": MOCK_FRONTIER},
            scenarios=dict(_CN_SCENARIOS),
            entity_id="002669", entity_name="康达新材",
            entity_name_clean="康达新材", period="FY2025",
        )
        p = rep["pricedIn"]
        assert p["frontier"] is not None
        assert p["regime"] is None
        assert p["events"] is None

    def test_dcf_scenarios_still_rendered(self):
        # 设计红线 1：预期区块在场时，DCF 情景与差值照旧展示。
        rep = _build_cn_report()
        assert len(rep["scenarios"]) == 3
        assert rep["rating"]["target"] is not None

    def test_end_to_end_html_contains_block(self, monkeypatch):
        monkeypatch.setenv("AEGIS_SKIP_SPARKLINE", "1")
        html = v2.generate_html_report(
            decision=_decision(publishing_status="blocked"),
            computed_metrics={},
            market_data={"current_price": 13.76},
            agent_judgments=[], critic_results=[],
            meta_facts=_meta_facts_with_phase0(),
            scenarios=dict(_CN_SCENARIOS),
            entity_name="康达新材", entity_name_clean="康达新材",
            period="FY2025",
        )
        marker = "window.REPORT = "
        start = html.index(marker) + len(marker)
        end = html.index(";</script>", start)
        rep = json.loads(html[start:end].replace("<\\/", "</"))
        assert rep["pricedIn"]["title"] == "市场在定价什么"
        # blocked 新文案 end-to-end（评级语义改造）
        assert rep["rating"]["word"] == "预期无法验证 · 暂不评级"
        # jsx 已内联新区块
        assert "sec-pricedin" in html
        assert "市场在定价什么" in html
