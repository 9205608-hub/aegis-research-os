"""Unit tests for the production HTML renderer — html_report_v2.

AUDIT-E3 (2026-07): `html_report_v2.generate_html_report` is the sole
production entry point (every report since the v2 cutover), yet it had
zero direct test coverage — the legacy tests import re-exported helpers
from the `html_report` shim that hard-route to html_report_legacy dead
code. These tests import the v2 module directly.

Covers:
- _derive_rating thresholds (both languages, zero-price guard)
- publishing_status branching in build_report_dict:
  published / blocked / needs_review / downgraded (AUDIT-C2 regression)
- _sanitize_floats on inf / -inf / nan in nested containers (BUG-Y39)
- generate_html_report end-to-end: injected window.REPORT JSON parses,
  contains no Infinity/NaN tokens, and no literal "</" that would
  terminate the <script> block early (AUDIT </script> escape)
- Chinese headline fallback uses abs() — no "-23.8%下行空间" double
  negative (AUDIT headline regression)
- US long-form unit suffix reads "USD billions", not "usd bs"
- Staleness banner month math (month granularity + real period end)
"""

import json
import math
from datetime import datetime
from types import SimpleNamespace

import pytest

from aegis.core.reports import html_report_v2 as v2


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """Keep tests hermetic — skip sparkline / quote-meta network fetches."""
    monkeypatch.setenv("AEGIS_SKIP_SPARKLINE", "1")


# ─────────────────────────────────────────────────────────────────
# Builders
# ─────────────────────────────────────────────────────────────────

def _decision(**over):
    base = dict(
        entity_id="600519",
        publishing_status="published",
        confidence_bucket="medium",
        bias_check_status="通过",
        run_id="run_test",
        period="FY2025",
        open_questions=[],
        dcf_output=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


_CN_SCENARIOS = {
    "currency": "CNY",
    "base_value": 150.0,
    "probability_weighted_value": 150.0,
    "bear_value": 100.0, "bear_probability": 0.25, "bear_narrative": "悲观情形",
    "base_probability": 0.50, "base_narrative": "基准情形",
    "bull_value": 200.0, "bull_probability": 0.25, "bull_narrative": "乐观情形",
}

_CN_META = {
    "ebitda": 5e9, "operating_income": 4e9, "revenue": 2e10,
    "net_income": 3e9, "total_equity": 1e10, "shares_outstanding": 1e9,
}


def _build_cn(**over):
    """build_report_dict with a minimal-but-valid CN (A-share) input set."""
    kw = dict(
        decision=_decision(),
        market_data={"current_price": 100.0},
        meta_facts=dict(_CN_META),
        scenarios=dict(_CN_SCENARIOS),
        entity_id="600519",
        entity_name="贵州茅台",
        entity_name_clean="贵州茅台",
        period="FY2025",
    )
    kw.update(over)
    return v2.build_report_dict(**kw)


def _generate(decision=None, **over):
    """generate_html_report with the same minimal CN defaults."""
    kw = dict(
        computed_metrics={},
        market_data={"current_price": 100.0},
        agent_judgments=[],
        critic_results=[],
        meta_facts=dict(_CN_META),
        scenarios=dict(_CN_SCENARIOS),
        entity_name="贵州茅台",
        entity_name_clean="贵州茅台",
        period="FY2025",
    )
    kw.update(over)
    return v2.generate_html_report(decision or _decision(), **kw)


def _extract_report_json(html: str) -> str:
    """Pull the inlined window.REPORT JSON out of the rendered page."""
    marker = "window.REPORT = "
    start = html.index(marker) + len(marker)
    end = html.index(";</script>", start)
    return html[start:end]


# ─────────────────────────────────────────────────────────────────
# _derive_rating
# ─────────────────────────────────────────────────────────────────

class TestDeriveRating:

    def test_thresholds_en(self):
        assert v2._derive_rating(130, 100, False) == ("Buy", "buy")
        assert v2._derive_rating(110, 100, False) == ("Overweight", "buy")
        assert v2._derive_rating(100, 100, False) == ("Hold", "hold")
        assert v2._derive_rating(92, 100, False) == ("Reduce", "avoid")
        assert v2._derive_rating(80, 100, False) == ("Avoid", "avoid")

    def test_thresholds_zh(self):
        assert v2._derive_rating(130, 100, True) == ("买入", "buy")
        assert v2._derive_rating(110, 100, True) == ("增持", "buy")
        assert v2._derive_rating(100, 100, True) == ("持有", "hold")
        assert v2._derive_rating(92, 100, True) == ("减持", "avoid")
        assert v2._derive_rating(80, 100, True) == ("回避", "avoid")

    def test_zero_price_falls_back_to_hold(self):
        assert v2._derive_rating(150, 0, True) == ("持有", "hold")
        assert v2._derive_rating(150, -1, False) == ("Hold", "hold")


# ─────────────────────────────────────────────────────────────────
# publishing_status × rating (BUG-Y22 + AUDIT-C2)
# ─────────────────────────────────────────────────────────────────

class TestPublishingStatusRating:

    def test_published_derives_rating_from_target(self):
        rep = _build_cn()  # target 150 vs price 100 → +50% → 买入
        assert rep["rating"]["word"] == "买入"
        assert rep["rating"]["tone"] == "buy"
        assert rep["rating"]["weighted"] == "概率加权"
        assert rep["rating"]["downgraded"] is False

    def test_blocked_shows_not_rated(self):
        # Aegis 2.0 Phase 0（评级语义）：blocked 文案改造为「预期无法验证」
        # 语境——不是模型没意见，而是现价隐含预期无法用可验证事实核验。
        rep = _build_cn(decision=_decision(publishing_status="blocked"))
        assert rep["rating"]["word"] == "预期无法验证 · 暂不评级"
        assert rep["rating"]["tone"] == "hold"

    def test_blocked_shows_not_rated_en(self):
        rep = v2.build_report_dict(
            decision=_decision(entity_id="NVDA", publishing_status="blocked"),
            market_data={"current_price": 100.0},
            meta_facts={"ebitda": 5e9, "operating_income": 4e9},
            scenarios={"currency": "USD", "base_value": 150.0,
                       "probability_weighted_value": 150.0},
            entity_id="NVDA",
            entity_name="NVIDIA",
            entity_name_clean="NVIDIA",
            period="FY2026",
        )
        assert rep["rating"]["word"] == "Expectations unverifiable · Not Rated"
        assert rep["rating"]["tone"] == "hold"

    def test_needs_review_shows_under_review(self):
        rep = _build_cn(decision=_decision(publishing_status="needs_review"))
        assert rep["rating"]["word"] == "审核中"
        assert rep["rating"]["tone"] == "hold"

    def test_downgraded_keeps_rating_with_caveat_zh(self):
        # AUDIT-C2: downgraded used to fall through to the plain rating
        # path and render an unqualified "买入".
        rep = _build_cn(decision=_decision(publishing_status="downgraded"))
        assert rep["rating"]["word"] == "买入"  # rating retained
        assert rep["rating"]["downgraded"] is True
        assert rep["rating"]["weighted"] == "评级已降级 · 存在未解决分歧"

    def test_downgraded_keeps_rating_with_caveat_en(self):
        rep = v2.build_report_dict(
            decision=_decision(entity_id="NVDA", publishing_status="downgraded"),
            market_data={"current_price": 100.0},
            meta_facts={"ebitda": 5e9, "operating_income": 4e9},
            scenarios={"currency": "USD", "base_value": 150.0,
                       "probability_weighted_value": 150.0},
            entity_id="NVDA",
            entity_name="NVIDIA",
            entity_name_clean="NVIDIA",
            period="FY2026",
        )
        assert rep["rating"]["word"] == "Buy"
        assert rep["rating"]["downgraded"] is True
        assert rep["rating"]["weighted"] == "Downgraded · unresolved conflicts"


# ─────────────────────────────────────────────────────────────────
# _sanitize_floats (BUG-Y39)
# ─────────────────────────────────────────────────────────────────

class TestSanitizeFloats:

    def test_non_finite_become_none(self):
        assert v2._sanitize_floats(float("inf")) is None
        assert v2._sanitize_floats(float("-inf")) is None
        assert v2._sanitize_floats(float("nan")) is None

    def test_finite_values_pass_through(self):
        assert v2._sanitize_floats(1.5) == 1.5
        assert v2._sanitize_floats(0.0) == 0.0
        assert v2._sanitize_floats(42) == 42          # int untouched
        assert v2._sanitize_floats("nan") == "nan"    # strings untouched

    def test_nested_containers(self):
        dirty = {
            "a": [1.0, float("inf"), {"b": float("nan")}],
            "c": (float("-inf"), "ok"),
        }
        clean = v2._sanitize_floats(dirty)
        assert clean == {"a": [1.0, None, {"b": None}], "c": (None, "ok")}


# ─────────────────────────────────────────────────────────────────
# generate_html_report — injected JSON integrity
# ─────────────────────────────────────────────────────────────────

class TestGeneratedJsonIntegrity:

    def test_inf_nan_inputs_produce_parseable_json(self):
        sc = dict(_CN_SCENARIOS)
        sc["bear_value"] = float("inf")        # → scenarios[0].px
        sc["base_probability"] = float("nan")  # → scenarios[1].prob
        html = _generate(scenarios=sc)
        raw = _extract_report_json(html)
        assert "Infinity" not in raw
        assert "NaN" not in raw
        rep = json.loads(raw)  # must parse cleanly
        bear = next(s for s in rep["scenarios"] if s["key"] == "bear")
        base = next(s for s in rep["scenarios"] if s["key"] == "base")
        assert bear["px"] is None
        assert base["prob"] is None

    def test_script_terminator_in_llm_text_is_escaped(self):
        # AUDIT: a literal "</script>" inside any LLM string used to
        # terminate the inline <script> block and white-screen the report.
        evil = "ROIC<WACC，价值毁灭</script><script>alert(1)</script><!--"
        judgment = {
            "agent_name": "business_analyst",
            "inferences": [{"text": evil, "confidence": "medium"}],
            "observations": [{"text": "观察一"}],
            "counterarguments": [],
        }
        html = _generate(agent_judgments=[judgment])
        raw = _extract_report_json(html)
        assert "</" not in raw          # every "</" escaped as "<\/"
        assert "<!--" not in raw        # comment-open escaped too
        rep = json.loads(raw)           # escapes are JSON-equivalent…
        assert rep["agents"][0]["thesis"] == evil  # …and round-trip

    def test_chinese_labels_spot_check(self):
        html = _generate()
        rep = json.loads(_extract_report_json(html))
        assert rep["rating"]["weighted"] == "概率加权"
        assert rep["rating"]["timeHorizon"] == "12 个月"
        assert rep["dcf"]["title"] == "十年现金流桥接与企业价值拆解"
        assert rep["scenarios"][0]["tag"] == "悲观情景"


# ─────────────────────────────────────────────────────────────────
# Chinese headline fallback (BUG-Y34 + AUDIT abs() regression)
# ─────────────────────────────────────────────────────────────────

class TestChineseHeadlineFallback:

    def test_downside_uses_expectations_framing(self):
        # Aegis 2.0 Phase 0（第 6 项）：target 76.2 vs price 100（隐含
        # -23.8%）——中文 fallback 不再输出裸「X% 下行空间」主张，改为
        # 预期框架句式。DCF 差值仍在情景区块/核心判断完整展示（红线 1）。
        sc = dict(_CN_SCENARIOS)
        sc["base_value"] = 76.2
        sc["probability_weighted_value"] = 76.2
        rep = _build_cn(scenarios=sc)
        assert "现价隐含预期显著高于可验证基本面" in rep["headline"]
        assert "下行空间" not in rep["headline"]
        assert "%" not in rep["headline"]  # 裸百分比主张禁止出现在 headline

    def test_upside_uses_expectations_framing(self):
        rep = _build_cn()  # target 150 vs 100 → +50%（市场隐含预期偏保守）
        assert "现价隐含预期低于模型可验证基本面" in rep["headline"]
        assert "上行空间" not in rep["headline"]
        assert "%" not in rep["headline"]
        assert "贵州茅台" in rep["headline"]

    def test_near_par_uses_compatible_framing(self):
        sc = dict(_CN_SCENARIOS)
        sc["base_value"] = 102.0
        sc["probability_weighted_value"] = 102.0
        rep = _build_cn(scenarios=sc)
        assert "大体相容" in rep["headline"]

    def test_en_fallback_unchanged(self):
        # 铁律：rule-based 模板 en 分支逐字不动。
        rep = v2.build_report_dict(
            decision=_decision(entity_id="NVDA"),
            market_data={"current_price": 100.0},
            meta_facts={"ebitda": 5e9, "operating_income": 4e9},
            scenarios={"currency": "USD", "base_value": 76.2,
                       "probability_weighted_value": 76.2},
            entity_id="NVDA",
            entity_name="NVIDIA",
            entity_name_clean="NVIDIA",
            period="FY2026",
        )
        assert rep["headline"] == "NVIDIA: rule-based DCF implies 23.8% downside"

    def test_editor_headline_takes_precedence(self):
        rep = _build_cn(edited_report={"headline": "编辑器标题", "lede": "导语"})
        assert rep["headline"] == "编辑器标题"


# ─────────────────────────────────────────────────────────────────
# Editor front_page_numbers 渲染接线（2026-08-01）
# ─────────────────────────────────────────────────────────────────

def _fpn_entries(n):
    return [
        {"label": f"指标{i}", "value": f"{i * 10}%", "context": f"背景说明{i}"}
        for i in range(1, n + 1)
    ]


class TestFrontPageNumbers:
    """Editor front_page_numbers → REPORT.frontPageNumbers 映射。

    数据侧清洗（report_editor._scrub_front_page_numbers）已有独立回归
    （test_front_page_scrub.py）；这里只测渲染装配：有数据渲染 / 空列表
    零占位 / 超限截断 / 半空条目过滤 / dict 与 dataclass 双形态 / 中文
    label 原样透传。
    """

    def test_entries_pass_through_with_chinese_labels(self):
        rep = _build_cn(edited_report=SimpleNamespace(
            headline="编辑器标题", front_page_numbers=_fpn_entries(4)))
        fpn = rep["frontPageNumbers"]
        assert len(fpn) == 4
        assert fpn[0] == {"label": "指标1", "value": "10%",
                          "context": "背景说明1"}

    def test_no_editor_yields_empty_list(self):
        # 空列表 = 模板零占位（report.jsx FrontPageNumbers 返回 null）
        rep = _build_cn(edited_report=None)
        assert rep["frontPageNumbers"] == []

    def test_empty_field_yields_empty_list(self):
        rep = _build_cn(edited_report=SimpleNamespace(front_page_numbers=[]))
        assert rep["frontPageNumbers"] == []

    def test_editor_without_field_yields_empty_list(self):
        # 旧缓存的 EditedReport 可能压根没有该字段
        rep = _build_cn(edited_report=SimpleNamespace(headline="标题"))
        assert rep["frontPageNumbers"] == []

    def test_truncated_to_five(self):
        # Editor schema maxItems=6；渲染端兜底截到 5
        rep = _build_cn(edited_report=SimpleNamespace(
            front_page_numbers=_fpn_entries(6)))
        assert len(rep["frontPageNumbers"]) == 5
        assert rep["frontPageNumbers"][-1]["label"] == "指标5"

    def test_half_empty_entries_filtered_before_cap(self):
        # label/value 缺一即跳过，且坏条目不挤占 5 条名额
        entries = [
            {"label": "", "value": "12%", "context": ""},
            {"label": "只有标签", "value": "", "context": ""},
        ] + _fpn_entries(5)
        rep = _build_cn(edited_report=SimpleNamespace(
            front_page_numbers=entries))
        fpn = rep["frontPageNumbers"]
        assert len(fpn) == 5
        assert fpn[0]["label"] == "指标1"

    def test_dict_shaped_edited_report_from_replay_cache(self):
        # replay 缓存里 edited_report 是 dict 而非 dataclass（_g 双形态）
        rep = _build_cn(edited_report={"front_page_numbers": _fpn_entries(2)})
        assert len(rep["frontPageNumbers"]) == 2

    def test_generated_html_carries_entries(self):
        html = _generate(edited_report=SimpleNamespace(
            headline="编辑器标题", opening_paragraph="开篇",
            front_page_numbers=_fpn_entries(3)))
        rep = json.loads(_extract_report_json(html))
        assert len(rep["frontPageNumbers"]) == 3
        assert rep["frontPageNumbers"][0]["label"] == "指标1"


# ─────────────────────────────────────────────────────────────────
# US long-form unit suffix ("usd bs" regression)
# ─────────────────────────────────────────────────────────────────

class TestUnitSuffix:

    def test_us_unit_reads_usd_billions(self):
        rep = v2.build_report_dict(
            decision=_decision(entity_id="NVDA"),
            market_data={"current_price": 100.0},
            meta_facts={"ebitda": 5e9, "operating_income": 4e9, "revenue": 6e10},
            scenarios={"currency": "USD", "base_value": 150.0,
                       "probability_weighted_value": 150.0},
            entity_id="NVDA",
            entity_name="NVIDIA",
            entity_name_clean="NVIDIA",
            period="FY2026",
        )
        assert rep["dcf"]["unit"] == "USD billions"
        assert rep["financials"]["revTitle"].endswith("USD billions")
        assert "usd bs" not in json.dumps(rep, default=str)

    def test_cn_unit_unchanged(self):
        rep = _build_cn()
        assert rep["dcf"]["unit"] == "亿元人民币"


# ─────────────────────────────────────────────────────────────────
# Staleness banner month math
# ─────────────────────────────────────────────────────────────────

class TestStaleBanner:

    def test_gap_is_month_granular(self):
        # FY three years back always trips the 15-month threshold; the
        # expected month count uses the same calendar-FY-end approximation
        # the renderer falls back to (fy_year-12).
        now = datetime.now()
        fy = now.year - 3
        gap = (now.year * 12 + now.month) - (fy * 12 + 12)
        rep = _build_cn(period=f"FY{fy}", decision=_decision(period=f"FY{fy}"))
        banner = rep["staleBanner"]
        assert banner is not None
        assert f"约 {gap} 个月" in banner
        assert f"{fy}-12-31" in banner

    def test_real_period_end_preferred(self):
        # A non-calendar fiscal year end (e.g. NVDA-style late January)
        # must drive both the month math and the displayed date.
        now = datetime.now()
        pe_year = now.year - 2
        gap = (now.year * 12 + now.month) - (pe_year * 12 + 1)
        meta = dict(_CN_META)
        meta["fiscal_period_end"] = f"{pe_year}-01-31"
        rep = _build_cn(
            meta_facts=meta,
            period=f"FY{pe_year}",
            decision=_decision(period=f"FY{pe_year}"),
        )
        banner = rep["staleBanner"]
        assert banner is not None
        assert f"{pe_year}-01-31" in banner
        assert f"约 {gap} 个月" in banner

    def test_fresh_period_has_no_banner(self):
        now = datetime.now()
        rep = _build_cn(period=f"FY{now.year}",
                        decision=_decision(period=f"FY{now.year}"))
        assert rep["staleBanner"] is None


# ─────────────────────────────────────────────────────────────────
# 审计处方（2026-08-28，300502 对抗性审计回归锁）
# ─────────────────────────────────────────────────────────────────

class TestObservationFrameworkVerdict:
    """处方一 1：产品形态=观察框架时抑制卖方包装。"""

    @staticmethod
    def _obs_decision(**over):
        # downgraded + 待解问题 ≥ 8 → derive_product_form 判观察框架
        base = dict(
            publishing_status="downgraded",
            open_questions=[{"question": f"q{i}"} for i in range(8)],
        )
        base.update(over)
        return _decision(**base)

    def test_observation_ticket_suppresses_sellside_packaging(self):
        rep = _build_cn(decision=self._obs_decision())
        assert rep["productForm"] is not None  # 横幅在
        # 评级：不再给方向性评级（旧行为：downgraded 保留 买入/回避）
        assert rep["rating"]["word"] == "观察框架 · 不评级"
        assert rep["rating"]["tone"] == "hold"
        # 目标价数字保留，但语义改「参照系」
        assert rep["rating"]["target"] == 150.0
        assert rep["rating"]["weighted"] == "DCF 参照系 · 非目标价"
        # 期限/风险等级的卖方包装去掉
        assert rep["rating"]["observationFramework"] is True
        assert rep["rating"]["timeHorizon"] is None
        assert rep["rating"]["riskLevel"] is None
        # 核心判断改参照系措辞，不再有「估值回归空间」的方向性预判
        assert "参照系" in rep["coreCalloutHtml"]
        assert "估值回归空间" not in rep["coreCalloutHtml"]

    def test_investment_thesis_keeps_packaging(self):
        rep = _build_cn()  # published、无缺口 → 投资论点
        assert rep["productForm"] is None
        assert rep["rating"]["observationFramework"] is False
        assert rep["rating"]["timeHorizon"] == "12 个月"
        assert rep["rating"]["riskLevel"] == "中高"
        assert "估值回归空间" in rep["coreCalloutHtml"]

    def test_mismatch_priority_over_observation_label(self):
        # 失配票（本身也是观察框架）目标价被扣留优先于参照系标签
        scen = dict(_CN_SCENARIOS)
        scen["valuation_sanity"] = {"mismatch": True, "ratio": 11.6}
        rep = _build_cn(decision=self._obs_decision(), scenarios=scen)
        assert rep["rating"]["target"] is None
        assert rep["rating"]["weighted"] == "估值失配 · 不提供目标价"

    def test_blocked_wording_not_overridden(self):
        # blocked 的既有文案本就不是评级——观察框架覆盖不动它
        rep = _build_cn(decision=self._obs_decision(publishing_status="blocked"))
        assert rep["rating"]["word"] == "预期无法验证 · 暂不评级"
        assert rep["rating"]["observationFramework"] is True


class TestTTMLabelHonesty:
    """处方二 2：TTM 标签与数值口径一致（quick 卡 + rail）。"""

    MD = {"current_price": 100.0, "market_cap": 1.6e11}

    def test_quick_pe_derived_from_ttm_engine(self):
        meta = dict(_CN_META, ttm_net_income=4e9, ttm_revenue=2.4e10)
        rep = _build_cn(market_data=dict(self.MD), meta_facts=meta)
        pe_card = [c for c in rep["quick"] if c["lbl"].startswith("市盈率")][0]
        assert pe_card["lbl"] == "市盈率 (TTM)"
        assert pe_card["val"] == "40.0×"          # 1600亿 / 40亿 TTM 归母
        assert "每股收益(TTM) 4.00" in pe_card["sub"]  # 与主值同口径同股本
        rail = {kv["k"]: kv["v"] for kv in rep["rail"]["marketKvs"]}
        assert rail["营收 (TTM)"] == "¥240.0 亿"   # 真 TTM，非 FY 值

    def test_quick_pe_fy_fallback_label_honest(self):
        # 无 TTM 源 → 回退 FY 静态值，但标签不再冒充 TTM
        rep = _build_cn(
            market_data=dict(self.MD),
            computed_metrics={"pe_ratio": 53.3},
        )
        pe_card = [c for c in rep["quick"] if c["lbl"].startswith("市盈率")][0]
        assert pe_card["lbl"] == "市盈率 (静态)"
        assert pe_card["val"] == "53.3×"
        assert "每股收益 3.00" in pe_card["sub"]  # FY 归母 30亿 / 10亿股
        rail = {kv["k"]: kv["v"] for kv in rep["rail"]["marketKvs"]}
        assert "营收 (TTM)" not in rail
        assert rail["营收 (FY2025)"] == "¥200.0 亿"

    def test_canonical_pe_ratio_ttm_key_still_first(self):
        # orchestrator 的 canonical 键优先于 TTM 引擎现算
        meta = dict(_CN_META, ttm_net_income=4e9)
        rep = _build_cn(
            market_data=dict(self.MD), meta_facts=meta,
            computed_metrics={"pe_ratio_ttm": 41.7},
        )
        pe_card = [c for c in rep["quick"] if c["lbl"].startswith("市盈率")][0]
        assert pe_card["lbl"] == "市盈率 (TTM)"
        assert pe_card["val"] == "41.7×"


class TestMonitoringContractBlock:
    """处方三 2：监控合约区块（kill 条件 + monitorables + H2 兑现锚）。"""

    KILLS = [
        {"description": "H2 归母净利润兑现失败", "threshold": "低于¥100亿",
         "check_frequency": "quarterly"},
        {"description": "激励考核门槛显著低于预期", "threshold": "低于¥170亿",
         "check_frequency": "event-driven"},
    ]
    MONS = [
        {"description": "光互联产品毛利率环比", "check_frequency": "quarterly",
         "source_agent": "valuation_analyst"},
        {"description": "大客户集中度变化", "check_frequency": "annually",
         "source_agent": "risk_analyst"},
        {"description": "两融余额占比", "check_frequency": "monthly",
         "source_agent": "risk_analyst"},
    ]

    def test_monitoring_block_renders_kills_and_monitorables(self):
        dec = _decision(kill_criteria=list(self.KILLS),
                        monitorables=list(self.MONS))
        rep = _build_cn(decision=dec)
        mon = rep["monitoring"]
        assert mon is not None
        assert len(mon["kills"]) == 2
        assert mon["kills"][0]["threshold"] == "低于¥100亿"
        assert mon["kills"][0]["frequency"] == "季度"
        assert mon["kills"][1]["frequency"] == "事件驱动"
        assert len(mon["monitorables"]) == 3
        assert mon["monitorables"][0]["agent"] == "估值分析师"
        assert mon["monitorables"][1]["agent"] == "风险分析师"

    def test_monitoring_none_when_no_structured_data(self):
        rep = _build_cn()
        assert rep["monitoring"] is None

    def test_anchor_note_derived_from_verification_evidence(self):
        # canonical H2 阈值 = 同年全年一致预期归母 − H1 实际归母（处方二 1
        # 指定推导），两块 evidence 齐备才出锚行
        meta = dict(_CN_META)
        meta["__verification"] = [
            {"check_id": "cfo_to_net_income", "status": "fail",
             "evidence": {"period": "2026-06-30", "net_income": 7.529e9}},
            {"check_id": "forecast_vs_consensus", "status": "insufficient",
             "evidence": {"year": 2026, "consensus_net_profit": 1.9757e10}},
        ]
        dec = _decision(kill_criteria=list(self.KILLS))
        rep = _build_cn(decision=dec, meta_facts=meta)
        note = rep["monitoring"]["anchorNote"]
        assert note is not None
        assert "¥122.3亿" in note          # 197.57 − 75.29 ≈ 122.3
        assert "唯一推导口径" in note

    def test_anchor_note_absent_when_evidence_incomplete(self):
        meta = dict(_CN_META)
        meta["__verification"] = [
            {"check_id": "cfo_to_net_income", "status": "pass",
             "evidence": {"period": "2025-12-31", "net_income": 9.5e9}},
        ]
        dec = _decision(kill_criteria=list(self.KILLS))
        rep = _build_cn(decision=dec, meta_facts=meta)
        assert rep["monitoring"]["anchorNote"] is None


class TestAuditRoundTwoNotes:
    """审计补丁二轮（2026-08-28）：kills 表头注记 / 激励口径注 / 前瞻 PE 脚注。"""

    GROWTH_KILL = [{"description": "H2归母净利润兑现失败，增长路径断档",
                    "threshold": "低于¥100亿", "check_frequency": "quarterly"}]
    GENERIC_KILL = [{"description": "信用评级下调", "threshold": "评级下调一档",
                     "check_frequency": "event-driven"}]
    INCENTIVE_KILL = [{"description": "管理层激励计划考核门槛显著低于市场预期",
                       "threshold": "考核门槛低于预期", "check_frequency": "event-driven"}]

    def test_kills_note_growth_wording(self):
        rep = _build_cn(decision=_decision(kill_criteria=list(self.GROWTH_KILL)))
        note = rep["monitoring"]["killsNote"]
        assert "增长（多头）假说" in note
        assert "双向监控对" in note

    def test_kills_note_generic_wording(self):
        rep = _build_cn(decision=_decision(kill_criteria=list(self.GENERIC_KILL)))
        note = rep["monitoring"]["killsNote"]
        assert "核心假设" in note and "增长（多头）假说" not in note

    def test_incentive_note_present_only_with_incentive_entries(self):
        rep = _build_cn(decision=_decision(kill_criteria=list(self.INCENTIVE_KILL)))
        assert "待公告核准" in rep["monitoring"]["incentiveNote"]
        rep2 = _build_cn(decision=_decision(kill_criteria=list(self.GENERIC_KILL)))
        assert rep2["monitoring"]["incentiveNote"] is None

    @staticmethod
    def _meta_with_consensus(eps, net_profit):
        meta = dict(_CN_META)
        meta["__recent_events"] = {
            "consensus": {
                "org_count": 17, "insufficient_coverage": False,
                "predictions": [
                    {"year": 2026, "eps": eps, "net_profit": net_profit},
                ],
            },
        }
        return meta

    def test_agents_footnote_on_share_basis_divergence(self):
        # 一致预期 EPS 隐含股本 20亿 vs 当前 10亿（市值 1000亿/现价 100）
        # → 两组前瞻 PE 分别为 100/5=20.0× 与 1000/100=10.0×，脚注调和
        meta = self._meta_with_consensus(eps=5.0, net_profit=1.0e10)
        rep = _build_cn(
            market_data={"current_price": 100.0, "market_cap": 1.0e11},
            meta_facts=meta,
        )
        note = rep["agentsFootnote"]
        assert note is not None
        assert "2026E≈20.0×" in note and "2026E≈10.0×" in note
        assert "股本基准" in note

    def test_agents_footnote_absent_when_bases_agree(self):
        # 隐含股本 = 当前股本（10亿）→ 无分歧，不出脚注
        meta = self._meta_with_consensus(eps=10.0, net_profit=1.0e10)
        rep = _build_cn(
            market_data={"current_price": 100.0, "market_cap": 1.0e11},
            meta_facts=meta,
        )
        assert rep["agentsFootnote"] is None

    def test_agents_footnote_absent_without_consensus(self):
        rep = _build_cn()
        assert rep["agentsFootnote"] is None
