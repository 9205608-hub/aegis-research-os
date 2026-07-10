"""Aegis 2.0 Phase 2 任务 B — Thesis 持久化 + monitorables 封闭目录回归测试.

锁定的行为（DESIGN_2.0 §三.C / Phase 2 / 设计红线 6、10）：

① monitorables 封闭目录 = verification.py 6 检查器型号 + 2 价格/事件型；
② 核验未通过项自动成为监控点（阈值取自检查器参数常量）；
③ LLM 乱写型号名的容错归一（别名 / 大小写 / 连字符 / 中文关键词）；
④ 目录映射不上的自由文本观察点降级 watch_only（「人工关注」）；
⑤ ThesisContract 构建：market_implied_story 从预期前沿摘要生成、
   regime 摘要进 sector_cycle_position、缺字段容错「未提供」；
⑥ append-only JSONL 版本链：version 自增 / parent_version / 坏行跳过；
⑦ JSON 全序列化往返（合同 → JSONL → 合同，无损）。
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from aegis.core.thesis.monitorables import (
    ANNOUNCEMENT_KEYWORDS,
    CATALOG,
    WATCH_ONLY_SOURCE,
    build_monitorables,
    monitorable_model_id,
    normalize_model_id,
)
from aegis.core.thesis.persistence import (
    PLACEHOLDER,
    build_thesis_contract,
    history,
    load_latest,
    normalize_entity_id,
    run_created_at,
    save_thesis_version,
)
from aegis.core.truth.verification import (
    CHECK_NAMES_ZH,
    VerificationResult,
)
from aegis.data_contracts.thesis_schema import Monitorable, ThesisContract


# ---------------------------------------------------------------------------
# 夹具：002669 康达真实形态的 run 产物切片
# ---------------------------------------------------------------------------

def _verification_dicts() -> list[dict]:
    """meta_facts["__verification"] 的真实形态（002669 快照缩样）。"""
    return [
        {
            "check_id": "receivables_vs_revenue",
            "name_zh": "应收增速 vs 营收增速",
            "status": "pass",
            "detail_zh": "2026-03-31 期应收账款同比 +27.7% vs 营收同比 +31.8%（缺口 -4pp，阈值内）",
            "evidence": {"gap_pp": -4.1},
        },
        {
            "check_id": "cfo_to_net_income",
            "name_zh": "经营现金流 / 归母净利润",
            "status": "fail",
            "detail_zh": "2025-12-31 期经营现金流 -11.99亿元 / 归母净利润 1.25亿元 = -9.56，低于 0.5 红旗线，账面利润未获现金流支撑",
            "evidence": {"ratio": -9.56},
        },
        {
            "check_id": "leverage_trend",
            "name_zh": "资产负债率趋势",
            "status": "fail",
            "detail_zh": "2026-03-31 期资产负债率 69.0%，较上年同期 57.1% 上升 11.9pp（超 5pp 阈值）",
            "evidence": {"delta_pp": 11.9},
        },
        {
            "check_id": "forecast_vs_consensus",
            "name_zh": "业绩预告 vs 一致预期",
            "status": "insufficient",
            "detail_zh": "无有效一致预期（近6个月覆盖机构 0 家，未达使用门槛），缺口无法核验",
            "evidence": {},
        },
    ]


def _regime_dict() -> dict:
    return {
        "dominant": "mixed",
        "narrative_frame_zh": "市场定价框架处于「题材叙事」与「困境反转」之间的混合状态。",
        "verification_focus": [
            "现金消耗速度与再融资依赖程度",
            "题材催化剂的可证伪时间点（公告、订单、政策节点）",
        ],
    }


def _frontier_dict() -> dict:
    """ExpectationsFrontier.to_dict() 的最小真实形态（单情景单列有解）。"""
    return {
        "market_price": 13.54,
        "currency": "CNY",
        "base_wacc": 0.095,
        "horizon_years": 10,
        "growth_grid_low": -0.5,
        "growth_grid_high": 0.8,
        "growth_grid_step": 0.01,
        "scenarios": [{
            "label": "维持当前",
            "target_margin": 0.029,
            "starting_margin": 0.029,
            "margin_path": [0.029],
            "wacc_columns": [{
                "wacc": 0.095, "wacc_delta": 0.0, "status": "solved",
                "solutions": [{
                    "implied_growth": 0.226,
                    "cumulative_revenue_scale": 7.7,
                    "extreme_expectation": False,
                }],
                "multiple_solutions": False,
                "diagnostic_code": "", "diagnostic_zh": "", "diagnostic_en": "",
                "grid_price_min": 1.0, "grid_price_max": 40.0,
                "valid_grid_points": 130,
            }],
        }],
    }


def _synthesized_dict() -> dict:
    """SynthesizedThesis 的 dict 形态（合同映射需要的字段子集）。"""
    return {
        "core_thesis": "当前股价隐含的困境反转预期与三张报表交叉验证结果背离。",
        "my_variant": "市场相信扭亏是经营拐点，我们看到的是并表与赊销撑起的会计利润。",
        "variant_magnitude": "DCF 概率加权 ¥2.36/股 vs 市价 ¥13.54。",
        "why_now": "2026 年半年报是反转叙事的第一个证伪窗口。",
        "market_implied_story": "（LLM 版本：应被预期前沿摘要取代）",
        "key_assumption_disagreement": "2025 年归母净利润是否可持续。",
        "counter_thesis": "定增落地 + 军工订单放量可能使基本面显著改善。",
        "what_would_change_my_mind": "半年报经营现金流转正且 CFO/NI 回到 0.5 以上。",
        "edge_source": "对盈利-现金流-资产负债表三角关系的系统性交叉验证。",
        "edge_durability": "short_term",
        "unresolved_tensions": ["定增最终条款未定", "军品订单节奏不可见"],
        "management_quality_summary": "资本配置存在系统性缺陷。",
        "capital_allocation_assessment": "ROIC 与 WACC 长期负缺口。",
        "open_questions": [
            {"agent": "accounting_analyst", "question": "营收增量中并表贡献占比？"},
        ],
    }


# ---------------------------------------------------------------------------
# ① 封闭目录本身
# ---------------------------------------------------------------------------

class TestCatalog:
    def test_catalog_covers_all_verification_checks_plus_two_price_models(self):
        # 6 个数据规则型号与 verification.py 封闭目录一比一
        for check_id in CHECK_NAMES_ZH:
            assert check_id in CATALOG, f"verification 型号 {check_id} 不在目录"
        # + 2 个价格/事件型
        assert "price_deviation" in CATALOG
        assert "announcement_keyword" in CATALOG
        assert len(CATALOG) == len(CHECK_NAMES_ZH) + 2

    def test_catalog_thresholds_derive_from_verification_constants(self):
        # 阈值文案由 verification 常量派生（单一事实源，不许硬编码漂移）
        assert "0.5" in CATALOG["cfo_to_net_income"].threshold_zh
        assert "20pp" in CATALOG["receivables_vs_revenue"].threshold_zh
        assert "25pp" in CATALOG["inventory_vs_revenue"].threshold_zh
        assert "5pp" in CATALOG["leverage_trend"].threshold_zh
        assert "20%" in CATALOG["forecast_vs_consensus"].threshold_zh
        for kw in ANNOUNCEMENT_KEYWORDS:
            assert kw in CATALOG["announcement_keyword"].threshold_zh

    def test_catalog_entries_are_zh_and_frequency_typed(self):
        for entry in CATALOG.values():
            assert entry.name_zh  # 中文化铁律：目录名必须有中文名
            assert entry.check_frequency in ("daily", "weekly", "quarterly")


# ---------------------------------------------------------------------------
# ③ 型号名容错归一（LLM 乱写）
# ---------------------------------------------------------------------------

class TestNormalizeModelId:
    @pytest.mark.parametrize("raw,expected", [
        # 精确
        ("cfo_to_net_income", "cfo_to_net_income"),
        # 大小写 / 连字符 / 空格（_coerce 同风格容错）
        ("CFO-to-NI", "cfo_to_net_income"),
        ("  Leverage Trend ", "leverage_trend"),
        # 别名
        ("cfo_ni", "cfo_to_net_income"),
        ("debt_ratio", "leverage_trend"),
        ("price_alert", "price_deviation"),
        ("公告关键词", "announcement_keyword"),
        # 中文关键词兜底
        ("应收增速异常", "receivables_vs_revenue"),
        ("存货周转恶化", "inventory_vs_revenue"),
        ("业绩预告与一致预期缺口", "forecast_vs_consensus"),
        ("扣非利润成色", "deducted_to_attributable"),
    ])
    def test_sloppy_model_names_normalize(self, raw, expected):
        assert normalize_model_id(raw) == expected

    def test_unmappable_returns_none(self):
        assert normalize_model_id("行业渗透率跟踪") is None
        assert normalize_model_id("") is None
        assert normalize_model_id(None) is None


# ---------------------------------------------------------------------------
# ②④ build_monitorables
# ---------------------------------------------------------------------------

class TestBuildMonitorables:
    def test_verification_failures_auto_become_monitorables(self):
        ms = build_monitorables(verification_results=_verification_dicts())
        by_model = {monitorable_model_id(m): m for m in ms}
        # 未通过项（fail）自动进监控
        assert "cfo_to_net_income" in by_model
        assert "leverage_trend" in by_model
        # 通过 / 数据不足项不自动进监控
        assert "receivables_vs_revenue" not in by_model
        assert "forecast_vs_consensus" not in by_model
        # 阈值取自检查器参数常量；核验依据进描述
        cfo = by_model["cfo_to_net_income"]
        assert "0.5" in cfo.description
        assert "-9.56" in cfo.description  # detail_zh 依据保留
        assert cfo.check_frequency == "quarterly"
        assert cfo.data_source == "pit_store:cfo_to_net_income"

    def test_verification_result_objects_also_accepted(self):
        results = [VerificationResult(
            check_id="leverage_trend",
            name_zh="资产负债率趋势",
            status="fail",
            detail_zh="上升 11.9pp（超 5pp 阈值）",
        )]
        ms = build_monitorables(verification_results=results)
        assert any(monitorable_model_id(m) == "leverage_trend" for m in ms)

    def test_llm_dict_with_sloppy_model_name_maps_into_catalog(self):
        st = {"monitorables": [
            {"model": "CFO-to-NI", "threshold": "0.6", "description": "现金流覆盖持续跟踪"},
        ]}
        ms = build_monitorables(synthesized_thesis=st)
        hit = [m for m in ms if monitorable_model_id(m) == "cfo_to_net_income"]
        assert len(hit) == 1
        assert "现金流覆盖持续跟踪" in hit[0].description
        assert "0.6" in hit[0].description  # LLM 填的阈值保留

    def test_llm_free_text_keyword_maps_into_catalog(self):
        st = {"follow_ups": ["跟踪存货周转与营收的背离", "关注股价是否偏离建仓价"]}
        ms = build_monitorables(synthesized_thesis=st)
        models = {monitorable_model_id(m) for m in ms}
        assert "inventory_vs_revenue" in models
        assert "price_deviation" in models

    def test_unmappable_text_degrades_to_watch_only(self):
        st = {"monitorables": ["军工行业渗透率与竞品格局变化"]}
        ms = build_monitorables(synthesized_thesis=st)
        watch = [m for m in ms if m.data_source == WATCH_ONLY_SOURCE]
        assert len(watch) == 1
        assert watch[0].description.startswith("人工关注：")
        assert "军工行业渗透率" in watch[0].description
        assert monitorable_model_id(watch[0]) is None  # 不做可执行承诺

    def test_verification_fail_wins_over_llm_same_model(self):
        """核验版（带依据数值）优先，LLM 同型号观察点不覆盖。"""
        st = {"monitorables": [{"model": "leverage_trend", "description": "留意负债"}]}
        ms = build_monitorables(
            synthesized_thesis=st, verification_results=_verification_dicts())
        lev = [m for m in ms if monitorable_model_id(m) == "leverage_trend"]
        assert len(lev) == 1
        assert "11.9pp" in lev[0].description  # 核验依据而非 LLM 文案

    def test_regime_focus_maps_via_keywords(self):
        ms = build_monitorables(regime=_regime_dict())
        models = {monitorable_model_id(m) for m in ms}
        assert "cfo_to_net_income" in models      # 「现金消耗速度」
        assert "announcement_keyword" in models   # 「公告、订单」
        # 体制验证点未命中的不降级 watch_only（清单已在报告展示）
        assert not any(m.data_source == WATCH_ONLY_SOURCE for m in ms)

    def test_open_questions_feed_watch_candidates(self):
        st = {"open_questions": [
            {"agent": "accounting_analyst", "question": "应收账款集中度是否恶化？"},
        ]}
        ms = build_monitorables(synthesized_thesis=st)
        assert any(
            monitorable_model_id(m) == "receivables_vs_revenue" for m in ms)

    def test_empty_inputs_yield_nonempty_fallback(self):
        """合同 must_monitor 要求 min_length=1 —— 空输入如实兜底人工关注。"""
        ms = build_monitorables()
        assert len(ms) == 1
        assert ms[0].data_source == WATCH_ONLY_SOURCE
        assert ms[0].description.startswith("人工关注：")

    def test_never_raises_on_garbage(self):
        ms = build_monitorables(
            synthesized_thesis={"monitorables": '["a", "b"]'},  # JSON 字符串列表
            verification_results="not-a-list",
            regime=42,
        )
        assert ms  # 永不 raise，且保证非空

    def test_all_outputs_are_valid_contract_monitorables(self):
        ms = build_monitorables(
            synthesized_thesis=_synthesized_dict(),
            verification_results=_verification_dicts(),
            regime=_regime_dict(),
        )
        for m in ms:
            assert isinstance(m, Monitorable)  # 沉睡合同复活，schema 严格校验


# ---------------------------------------------------------------------------
# ⑤ build_thesis_contract
# ---------------------------------------------------------------------------

class TestBuildThesisContract:
    def _build(self, **kw) -> ThesisContract:
        base = dict(
            entity_id="002669",
            run_id="run_20260710_131211_aae3324b",
            synthesized_thesis=_synthesized_dict(),
            frontier=_frontier_dict(),
            regime=_regime_dict(),
            verification_results=_verification_dicts(),
            kill_criteria=[{
                "description": "半年报 CFO/NI 仍低于 0.5",
                "threshold": "0.5",
                "check_frequency": "quarterly",
            }],
            scenarios={"bear": 1.1, "base": {"per_share": 2.36}, "bull": 4.30},
        )
        base.update(kw)
        return build_thesis_contract(**base)

    def test_market_implied_story_generated_from_frontier(self):
        c = self._build()
        # 预期前沿摘要（条件化句式）取代 LLM 同名字段
        assert "13.54" in c.market_implied_story
        assert "2.9%" in c.market_implied_story
        assert "22.6%" in c.market_implied_story
        assert "LLM 版本" not in c.market_implied_story

    def test_no_frontier_falls_back_to_synthesized_field(self):
        c = self._build(frontier=None)
        assert "LLM 版本" in c.market_implied_story

    def test_regime_summary_lands_in_sector_cycle_position(self):
        c = self._build()
        assert "定价体制" in c.sector_cycle_position
        assert "mixed" in c.sector_cycle_position
        assert "题材叙事" in c.sector_cycle_position

    def test_narrative_fields_map_from_synthesized_thesis(self):
        c = self._build()
        st = _synthesized_dict()
        assert c.my_variant == st["my_variant"]
        assert c.counter_thesis == st["counter_thesis"]
        assert c.core_thesis == st["core_thesis"]
        assert c.edge_classification.edge_source == st["edge_source"]
        assert c.edge_classification.edge_durability.value == "short_term"
        assert c.fragility_points == st["unresolved_tensions"]
        assert c.open_questions == ["营收增量中并表贡献占比？"]

    def test_kill_criteria_passthrough(self):
        c = self._build()
        assert len(c.kill_criteria) == 1
        assert c.kill_criteria[0].threshold == "0.5"

    def test_scenarios_and_monitorables_wired(self):
        c = self._build()
        assert c.bear_case_value == 1.1
        assert c.base_case_value == 2.36  # 嵌套 dict 形态
        assert c.bull_case_value == 4.30
        # must_monitor 来自 B1：核验未通过项在列
        assert any(
            monitorable_model_id(m) == "cfo_to_net_income"
            for m in c.must_monitor)

    def test_missing_everything_tolerated_with_zh_placeholders(self):
        c = build_thesis_contract(entity_id="600519", run_id="run_x")
        assert c.core_thesis == PLACEHOLDER
        assert c.my_variant == PLACEHOLDER
        assert c.market_implied_story == PLACEHOLDER
        assert c.sector_cycle_position == PLACEHOLDER
        assert c.kill_criteria  # 如实占位，不为空
        assert "人工判断" in c.kill_criteria[0].threshold
        assert c.must_monitor  # B1 兜底
        assert c.bear_case_value is None

    def test_entity_id_normalized_to_schema_pattern(self):
        assert build_thesis_contract(
            entity_id="NVDA", run_id="r1").entity_id == "nvda"
        assert build_thesis_contract(
            entity_id="600519.SH", run_id="r1").entity_id == "600519_sh"
        assert normalize_entity_id("") == "unknown"

    def test_review_date_is_created_plus_90d(self):
        """postmortem 90 天回看：review_date = run 产物时间 + 90 天。"""
        c = self._build()
        assert c.review_date == date(2026, 7, 10) + timedelta(days=90)

    def test_enum_tolerance(self):
        c = self._build(
            publishing_status="不是合法状态",
            confidence="medium-high",   # BUG-Y24 同族复合值
            market_id="火星",
        )
        assert c.publishing_status.value == "draft"
        assert c.confidence_bucket.value == "high"
        assert c.markets_covered[0].value == "cn"

    def test_json_roundtrip(self):
        """JSON 全序列化往返：合同 → dict → 字符串 → 合同，无损。"""
        c = self._build()
        payload = json.loads(json.dumps(c.model_dump(mode="json"), ensure_ascii=False))
        restored = ThesisContract.model_validate(payload)
        assert restored == c


# ---------------------------------------------------------------------------
# ⑥⑦ append-only JSONL 版本链
# ---------------------------------------------------------------------------

class TestVersionChain:
    def _contract(self, run_id: str) -> ThesisContract:
        return build_thesis_contract(
            entity_id="002669",
            run_id=run_id,
            synthesized_thesis=_synthesized_dict(),
            verification_results=_verification_dicts(),
        )

    def test_append_and_read_chain(self, tmp_path):
        r1 = save_thesis_version(
            "002669", self._contract("run_20260710_131211_a"),
            "run_20260710_131211_a", dir=tmp_path)
        r2 = save_thesis_version(
            "002669", self._contract("run_20260801_090000_b"),
            "run_20260801_090000_b", dir=tmp_path)
        assert (r1["version"], r1["parent_version"]) == (1, None)
        assert (r2["version"], r2["parent_version"]) == (2, 1)
        # created_at 取 run 产物时间（从 run_id 时间戳解析）
        assert r1["created_at"].startswith("2026-07-10T13:12:11")
        assert r2["created_at"].startswith("2026-08-01T09:00:00")

        chain = history("002669", dir=tmp_path)
        assert [rec["version"] for rec in chain] == [1, 2]
        latest = load_latest("002669", dir=tmp_path)
        assert latest is not None and latest["run_id"] == "run_20260801_090000_b"
        # 合同内版本号与链上位置对齐；v2 的 parent_thesis_id 指向 v1
        assert latest["thesis"]["thesis_version"] == 2
        assert latest["thesis"]["parent_thesis_id"] == "thesis_002669"
        assert chain[0]["thesis"]["parent_thesis_id"] is None

    def test_file_is_append_only_jsonl(self, tmp_path):
        save_thesis_version("002669", self._contract("r1"), "r1", dir=tmp_path)
        save_thesis_version("002669", self._contract("r2"), "r2", dir=tmp_path)
        lines = (tmp_path / "002669.jsonl").read_text(
            encoding="utf-8").strip().splitlines()
        assert len(lines) == 2  # 一版一行，旧行不改写（不建状态机）
        assert json.loads(lines[0])["version"] == 1

    def test_chain_roundtrip_restores_contract(self, tmp_path):
        original = self._contract("run_20260710_131211_a")
        save_thesis_version(
            "002669", original, "run_20260710_131211_a", dir=tmp_path)
        latest = load_latest("002669", dir=tmp_path)
        restored = ThesisContract.model_validate(latest["thesis"])
        # 落盘前 thesis_version/parent 已对齐链位置——其余字段必须无损
        assert restored == original.model_copy(
            update={"thesis_version": 1, "parent_thesis_id": None})

    def test_corrupted_line_skipped_and_versioning_continues(self, tmp_path):
        save_thesis_version("002669", self._contract("r1"), "r1", dir=tmp_path)
        with (tmp_path / "002669.jsonl").open("a", encoding="utf-8") as f:
            f.write("{corrupted json!!\n")
        assert [r["version"] for r in history("002669", dir=tmp_path)] == [1]
        r = save_thesis_version("002669", self._contract("r2"), "r2", dir=tmp_path)
        assert r["version"] == 2  # 坏行不打断链，版本继续自增

    def test_missing_entity_returns_empty(self, tmp_path):
        assert history("999999", dir=tmp_path) == []
        assert load_latest("999999", dir=tmp_path) is None

    def test_entity_filename_normalized(self, tmp_path):
        save_thesis_version(
            "NVDA", build_thesis_contract(entity_id="NVDA", run_id="r1"),
            "r1", dir=tmp_path)
        assert (tmp_path / "nvda.jsonl").exists()
        assert load_latest("nvda", dir=tmp_path) is not None

    def test_run_created_at_parser(self):
        assert run_created_at("run_20260710_131211_aae3324b") is not None
        assert run_created_at("garbage") is None
        assert run_created_at(None) is None
