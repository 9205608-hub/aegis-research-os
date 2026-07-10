# -*- coding: utf-8 -*-
"""Rule-based agent 模板中文化回归测试（中文化铁律）。

背景：A 股 smoke/--no-llm 报告以及 LLM run 中某 agent 失败退回 rule-based
时，rule-based 模板产出的 thesis/observations/inferences/counterarguments
等曾是英文硬编码，违反 CLAUDE.md 中文化铁律。本测试对每个有 rule-based
生成路径的 agent 做双向断言：

1. zh 路径（macro_context language="zh-CN"）：所有面向报告的文本不得出现
   英文句子（启发式：剔除 ROIC/WACC 等国际缩写白名单后，不允许出现连续
   >=3 个仅由空白分隔的英文单词）。
2. en 路径（美股输入，无 language 标识）：抽取字符串锚点，确保英文模板
   逐字未变。
"""

from __future__ import annotations

import re

import pytest

from aegis.core.agents.base import AgentInput, is_zh_input
from aegis.core.agents import (
    AccountingAnalyst,
    BusinessAnalyst,
    ManagementAnalyst,
    RiskAnalyst,
    SectorContextAgent,
    ValuationAnalyst,
    VariantAnalyst,
)


# ---------------------------------------------------------------------------
# English-sentence leak heuristic
# ---------------------------------------------------------------------------

# 数据枚举值 / 国际通用缩写：这些 token 允许在中文文本中单独出现
# （如 "一致预期修正动能: positive"、"ROIC 为 15.00%"），但不允许
# 连成英文句子。
_EN_SENTENCE_RE = re.compile(
    r"[A-Za-z][A-Za-z'\-]*(?:[ \t]+[A-Za-z][A-Za-z'\-]*){2,}"
)


def english_sentences(text: str) -> list[str]:
    """Return runs of >=3 whitespace-separated English words (sentence leak)."""
    return _EN_SENTENCE_RE.findall(text)


def collect_texts(output) -> list[str]:
    """Collect every report-facing string from an AgentOutput."""
    j = output.judgment
    texts: list[str] = []
    texts += [o.text for o in j.observations]
    texts += [i.text for i in j.inferences]
    texts += [c.text for c in j.counterarguments]
    texts += [t.text for t in j.disconfirming_triggers]
    texts += list(j.self_reported_uncertainties or [])
    if j.cognitive_bias_self_check is not None:
        texts += list(j.cognitive_bias_self_check.mitigation_steps_taken or [])
    return texts


# ---------------------------------------------------------------------------
# Input fixtures — rich enough to fire most template branches
# ---------------------------------------------------------------------------

METRICS = {
    "accruals_ratio": 0.1454,          # poor（> 0.10）
    "cfo_to_net_income": 0.40,          # below floor
    "sbc_to_revenue": 0.18,             # elevated
    "dilution_rate": 0.025,
    "gross_margin": 0.55,
    "operating_margin": 0.22,
    "net_margin": 0.15,
    "nwc": 1_234_567.0,
    "roic": 0.18,
    "roe": 0.21,
    "capex_to_revenue": 0.25,           # capital-intensive
    "ev_to_ebitda": 14.2,
    "ev_to_revenue": 3.1,
    "pe_ratio": 22.5,
    "pe_ratio_ttm": 20.1,
    "enterprise_value": 8_800_000_000.0,
    "fcf_simple": 400_000_000.0,
    "net_debt_to_ebitda": 3.5,          # elevated leverage
    "current_ratio": 1.4,
    "net_debt": 2_500_000_000.0,
}


def _sector_pack(zh: bool) -> dict:
    return {
        "sector_pack_id": "cn_auto_parts" if zh else "us_tech",
        "sector_name": "汽车零部件" if zh else "US Technology",
        "key_kpis": [
            {"metric": "gross_margin", "display": "毛利率" if zh else "Gross Margin",
             "importance": "high", "healthy_range": [0.60, 0.80]},
            {"metric": "operating_margin", "display": "营业利润率" if zh else "Operating Margin",
             "importance": "high", "healthy_range": [0.30, 0.50]},
        ],
        "cycle_characteristics": {
            "cyclicality": "high",
            "primary_driver": "下游整车排产" if zh else "consumer demand",
            "leading_indicators": ["下游排产计划" if zh else "PMI new orders"],
        },
        "accounting_considerations": (
            ["政府补助计入其他收益", "应收账款账期偏长"] if zh
            else ["Revenue recognition timing", "Capitalized R&D"]
        ),
        "special_risk_factors": {
            "related_party": {"description": "关联交易占比偏高" if zh
                              else "Founder-controlled related parties"},
        },
        "competitive_dynamics": {
            "disruption_risks": ["新材料替代" if zh else "Open-source substitution"],
        },
    }


def _relationships(entity_id: str) -> list[dict]:
    return [
        {"relationship_id": "rel_1", "relationship_type": "supplier",
         "entity_a": "supplier_x", "entity_b": entity_id,
         "revenue_significance": {"b_cost_from_a_pct": 0.30}},
        {"relationship_id": "rel_2", "relationship_type": "customer",
         "entity_a": entity_id, "entity_b": "customer_y",
         "revenue_significance": {"a_revenue_from_b_pct": 0.25}},
        {"relationship_id": "rel_3", "relationship_type": "related_party_ownership",
         "entity_a": entity_id, "entity_b": "parent_group",
         "revenue_significance": {"a_revenue_from_b_pct": 0.12}},
        {"relationship_id": "rel_4", "relationship_type": "competition",
         "entity_a": entity_id, "entity_b": "rival_z"},
    ]


def _macro_context(zh: bool) -> dict:
    mc = {
        "priced_in": {
            "implied_revenue_growth": 0.30,     # aggressive branch
            "implied_terminal_growth": 0.025,
            "revision_signal": {
                "momentum": "positive",
                "breadth": "broad_upgrade",
                "acceleration": "accelerating",
                "revision_1m_pct": 0.05,
            },
            "pe_ratio_fwd": 25.0,
        },
        "scenarios": {"bear_value": 10.0, "base_value": 20.0, "bull_value": 30.0},
        "current_price": 12.0,                  # base implies +67% upside
        "cycle_phase": "中期扩张" if zh else "mid-cycle expansion",
        "disagreements": [
            {"assumption": "revenue_growth_fy26", "market_implied": "30%",
             "my_view": "18%", "this_is_the_variant": True},
        ],
        "sensitivity_rankings": [
            {"assumption": "revenue_growth", "impact_pct": 0.12},
        ],
        "insider_trading": {
            "buy_count": 4, "sell_count": 1,
            "total_buy_value": 5_000_000, "total_sell_value": 800_000,
            "net_value": 4_200_000, "sentiment": "bullish",
            "cluster_detected": True,
            "notable_transactions": [
                {"name": "张三", "title": "CEO", "type": "P",
                 "value": 2_000_000, "date": "2026-05-01"},
            ],
        },
    }
    if zh:
        mc["language"] = "zh-CN"
        mc["market_id"] = "cn"
    return mc


def make_input(zh: bool) -> AgentInput:
    entity_id = "sz_002669" if zh else "us_nvda"
    return AgentInput(
        entity_id=entity_id,
        run_id="run_test",
        question_id="q_test",
        metric_results=dict(METRICS),
        macro_context=_macro_context(zh),
        sector_pack=_sector_pack(zh),
        entity_relationships=_relationships(entity_id),
    )


def make_input_alt(zh: bool) -> AgentInput:
    """变体输入：命中另一半模板分支（弱护城河/净现金/保守隐含增速/
    逆向动能/VIE/高于健康区间/下行空间/内部人分歧等）。"""
    inp = make_input(zh)
    inp.metric_results.update({
        "accruals_ratio": 0.02,          # acceptable
        "cfo_to_net_income": 1.3,        # healthy
        "sbc_to_revenue": 0.05,          # moderate
        "gross_margin": 0.85,            # above healthy range
        "operating_margin": 0.55,        # above healthy range
        "roic": 0.04,                    # poor capital allocation / weak moat
        "capex_to_revenue": 0.05,
        "net_debt_to_ebitda": 0.5,
        "net_debt": -500_000_000.0,      # net cash
    })
    inp.macro_context["priced_in"]["implied_revenue_growth"] = 0.02   # modest
    inp.macro_context["priced_in"]["revision_signal"] = {
        "momentum": "negative",
        "breadth": "broad_downgrade",
        "acceleration": "decelerating",
        "revision_1m_pct": -0.03,
    }
    inp.macro_context["scenarios"] = {
        "bear_value": 8.0, "base_value": 9.0, "bull_value": 10.5,
    }
    inp.macro_context["current_price"] = 12.0    # base implies downside
    inp.macro_context["insider_trading"]["sentiment"] = "mixed"
    inp.macro_context["insider_trading"]["cluster_detected"] = False
    if inp.sector_pack is not None:
        inp.sector_pack["special_risk_factors"]["vie_structure"] = {
            "description": "VIE 架构" if zh else "VIE structure",
        }
        inp.sector_pack["cycle_characteristics"]["cyclicality"] = "moderate"
    return inp


ALL_AGENTS = [
    AccountingAnalyst,
    BusinessAnalyst,
    ManagementAnalyst,
    ValuationAnalyst,
    VariantAnalyst,
    RiskAnalyst,
    SectorContextAgent,
]


# ---------------------------------------------------------------------------
# is_zh_input helper
# ---------------------------------------------------------------------------

class TestIsZhInput:
    def test_zh_language_flag(self):
        assert is_zh_input(make_input(zh=True)) is True

    def test_en_default(self):
        assert is_zh_input(make_input(zh=False)) is False

    def test_market_id_cn_alone(self):
        inp = make_input(zh=False)
        inp.macro_context["market_id"] = "cn"
        assert is_zh_input(inp) is True

    def test_no_macro_context(self):
        inp = make_input(zh=False)
        inp.macro_context = None
        assert is_zh_input(inp) is False


# ---------------------------------------------------------------------------
# zh path — no English sentence may leak
# ---------------------------------------------------------------------------

class TestZhNoEnglishLeak:
    @pytest.mark.parametrize("make", [make_input, make_input_alt],
                             ids=["main", "alt-branches"])
    @pytest.mark.parametrize("agent_cls", ALL_AGENTS,
                             ids=[a.AGENT_NAME for a in ALL_AGENTS])
    def test_no_english_sentences(self, agent_cls, make):
        out = agent_cls().run(make(zh=True))
        texts = collect_texts(out)
        assert texts, f"{agent_cls.AGENT_NAME} produced no text at all"
        leaks = []
        for t in texts:
            for hit in english_sentences(t):
                leaks.append(f"{agent_cls.AGENT_NAME}: {hit!r} in {t!r}")
        assert not leaks, "English sentence leaked into zh output:\n" + "\n".join(leaks)

    @pytest.mark.parametrize("agent_cls", ALL_AGENTS,
                             ids=[a.AGENT_NAME for a in ALL_AGENTS])
    def test_has_chinese_content(self, agent_cls):
        """zh 路径每个 agent 至少产出一段含中文字符的文本。"""
        out = agent_cls().run(make_input(zh=True))
        texts = collect_texts(out)
        assert any(re.search(r"[一-鿿]", t) for t in texts), (
            f"{agent_cls.AGENT_NAME} zh output contains no Chinese characters"
        )

    def test_zh_produces_inferences(self):
        """中文关键词匹配不破坏推理链：核心 agent 在 zh 下仍产出 inference。"""
        for agent_cls in (AccountingAnalyst, BusinessAnalyst, ManagementAnalyst,
                          ValuationAnalyst, VariantAnalyst, RiskAnalyst,
                          SectorContextAgent):
            out = agent_cls().run(make_input(zh=True))
            assert out.judgment.inferences, (
                f"{agent_cls.AGENT_NAME} produced no inferences on zh path"
            )

    def test_zh_validation_passes(self):
        for agent_cls in ALL_AGENTS:
            out = agent_cls().run(make_input(zh=True))
            assert out.validation_passed, (
                f"{agent_cls.AGENT_NAME} zh: {out.validation_errors}"
            )

    def test_accounting_accrual_example(self):
        """任务书示例：应计比率句式为自然中文语序。"""
        out = AccountingAnalyst().run(make_input(zh=True))
        obs_texts = [o.text for o in out.judgment.observations]
        assert any("应计比率为 0.1454" in t and "盈利质量较差" in t for t in obs_texts)
        inf_texts = [i.text for i in out.judgment.inferences]
        assert any("盈利质量欠佳" in t for t in inf_texts)


# ---------------------------------------------------------------------------
# en path — English templates must stay verbatim
# ---------------------------------------------------------------------------

class TestEnPathUnchanged:
    def test_accounting_anchors(self):
        out = AccountingAnalyst().run(make_input(zh=False))
        obs = [o.text for o in out.judgment.observations]
        assert "Accruals ratio is 0.1454, indicating poor earnings quality" in obs
        infs = [i.text for i in out.judgment.inferences]
        assert ("Earnings quality is below par — reported earnings may overstate "
                "economic reality") in infs

    def test_business_anchors(self):
        out = BusinessAnalyst().run(make_input(zh=False))
        infs = [i.text for i in out.judgment.inferences]
        assert ("Business exhibits strong pricing power and capital efficiency — "
                "consistent with a durable moat") in infs
        obs = [o.text for o in out.judgment.observations]
        assert "Gross margin is 55.00%" in obs

    def test_management_anchors(self):
        out = ManagementAnalyst().run(make_input(zh=False))
        infs = [i.text for i in out.judgment.inferences]
        assert ("Management demonstrates strong capital allocation — "
                "ROIC exceeds cost of capital") in infs

    def test_valuation_anchors(self):
        out = ValuationAnalyst().run(make_input(zh=False))
        trigger_texts = [t.text for t in out.judgment.disconfirming_triggers]
        assert "Forward P/E moves above 95th percentile of 5-year range" in trigger_texts
        obs = [o.text for o in out.judgment.observations]
        assert "Market-implied revenue growth: 30.00%" in obs

    def test_variant_anchors(self):
        out = VariantAnalyst().run(make_input(zh=False))
        cargs = [c.text for c in out.judgment.counterarguments]
        assert any("stay irrational longer than you can stay solvent" in c for c in cargs)
        infs = [i.text for i in out.judgment.inferences]
        assert any("Consensus momentum alignment: FAVORABLE" in t for t in infs)

    def test_risk_anchors(self):
        out = RiskAnalyst().run(make_input(zh=False))
        obs = [o.text for o in out.judgment.observations]
        assert "Net Debt/EBITDA: 3.5x — leverage is elevated" in obs

    def test_sector_anchors(self):
        out = SectorContextAgent().run(make_input(zh=False))
        obs = [o.text for o in out.judgment.observations]
        assert any(o.startswith("Entity classified under sector: US Technology")
                   for o in obs)
        infs = [i.text for i in out.judgment.inferences]
        assert ("Sector is highly cyclical — current-period metrics may not "
                "represent mid-cycle earnings power") in infs
