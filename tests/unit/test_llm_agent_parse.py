"""Regression tests for LLMAgentBase parse-boundary hardening (AUDIT-B4/B5/B6/B7).

The parse section of LLMAgentBase.run() sits OUTSIDE the LLM try/except —
any ValidationError there escapes run(), gets swallowed by the
orchestrator's broad except (auto_research.py:2311) and silently swaps the
WHOLE agent for the rule-based template, skipping the quality-gate LLM
retry. These tests feed minimal raw dicts through run() via a stub LLM
client and assert that the 2026-07 audit's proven crash inputs now parse.

Covers:
  - AUDIT-B4: Counterargument.strength normalization ("medium"/"very
    strong"/"STRONG" all used to ValidationError the agent)
  - AUDIT-B5: source_ids/evidence_ids as strings, scalar
    based_on_observation_indices, bias_check as JSON string, per-item drop
    of malformed elements
  - AUDIT-B6: _strip_sensitive CJK support (the 3 audited sentences that
    came back UNCHANGED must now change)
  - AUDIT-B7: _is_content_filter_error no longer misclassifies bare 400s
"""

import json

import pytest

from aegis.core.agents.base import AgentInput, AgentOutput
from aegis.core.agents.llm_agent_base import (
    LLMAgentBase,
    _is_content_filter_error,
    _strip_sensitive,
)
from aegis.core.llm.deepseek_client import DeepSeekContentFilterError


# ---------------------------------------------------------------------------
# Harness: stub LLM + minimal agent that bypasses real LLM config
# ---------------------------------------------------------------------------

class _StubLLM:
    """Returns a canned raw dict from call_structured()."""

    def __init__(self, raw):
        self.raw = raw
        self.calls = []

    def call_structured(self, **kwargs):
        self.calls.append(kwargs)
        return self.raw


class _ParseAgent(LLMAgentBase):
    AGENT_NAME = "parse_test_agent"
    AGENT_VERSION = "0.0.1"
    SYSTEM_PROMPT = "You are a test agent."

    def __init__(self, llm):  # bypass LLMAgentBase.__init__ (no env config)
        self._llm = llm


def _raw(**overrides):
    """Minimal well-formed raw dict; override single fields per test.

    Keeps observations < 4 so the 8/0 auto-rescue path never triggers.
    """
    base = {
        "observations": [
            {"text": "营收同比增长 30%", "source_ids": ["m_revenue"]},
            {"text": "毛利率维持 45%", "source_ids": ["m_gross_margin"]},
        ],
        "inferences": [
            {"text": "增长动能可持续", "based_on_observation_indices": [0], "confidence": "high"},
            {"text": "盈利质量稳定", "based_on_observation_indices": [1], "confidence": "medium"},
        ],
        "counterarguments": [
            {"text": "行业景气度可能见顶", "strength": "moderate", "evidence_ids": []},
        ],
        "disconfirming_triggers": [
            {"text": "连续两季营收增速跌破 10%"},
        ],
        "cognitive_bias_self_check": {
            "anchoring_risk": "low",
            "confirmation_bias_risk": "medium",
            "recency_bias_risk": "medium",
            "narrative_fallacy_risk": "low",
            "mitigation_steps_taken": ["复核了反面证据"],
        },
        "self_reported_uncertainties": ["宏观需求不确定"],
    }
    base.update(overrides)
    return base


def _run(raw) -> AgentOutput:
    agent = _ParseAgent(_StubLLM(raw))
    inp = AgentInput(entity_id="e_test", run_id="r_test", question_id="q_test")
    return agent.run(inp)


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------

class TestBaseline:
    def test_clean_raw_parses(self):
        out = _run(_raw())
        assert isinstance(out, AgentOutput)
        assert not out.is_llm_fallback
        assert len(out.judgment.observations) == 2
        assert len(out.judgment.inferences) == 2
        assert out.judgment.counterarguments[0].strength == "moderate"


# ---------------------------------------------------------------------------
# AUDIT-B5 ①: nested list[str] fields as strings
# ---------------------------------------------------------------------------

class TestNestedStringForList:
    def test_source_ids_string_coerced(self):
        # 审计实测崩溃点: source_ids='m_revenue' → ValidationError → 整 agent 丢弃
        out = _run(_raw(observations=[
            {"text": "营收同比增长 30%", "source_ids": "m_revenue"},
            {"text": "毛利率维持 45%", "source_ids": ["m_gross_margin"]},
        ]))
        assert not out.is_llm_fallback
        assert out.judgment.observations[0].source_ids == ["m_revenue"]

    def test_source_ids_json_string_coerced(self):
        out = _run(_raw(observations=[
            {"text": "营收同比增长 30%", "source_ids": '["m_revenue", "m_growth"]'},
            {"text": "毛利率维持 45%", "source_ids": ["m_gross_margin"]},
        ]))
        assert out.judgment.observations[0].source_ids == ["m_revenue", "m_growth"]

    def test_evidence_ids_string_coerced(self):
        out = _run(_raw(counterarguments=[
            {"text": "行业景气度可能见顶", "strength": "moderate", "evidence_ids": "ev_1"},
        ]))
        assert out.judgment.counterarguments[0].evidence_ids == ["ev_1"]


# ---------------------------------------------------------------------------
# AUDIT-B5 ②: scalar / missing based_on_observation_indices
# ---------------------------------------------------------------------------

class TestInferenceIndices:
    def test_scalar_index_coerced_to_list(self):
        # 审计实测崩溃点: indices=1 (标量) → TypeError: 'int' object is not iterable
        out = _run(_raw(inferences=[
            {"text": "增长动能可持续", "based_on_observation_indices": 1, "confidence": "high"},
            {"text": "盈利质量稳定", "based_on_observation_indices": [0], "confidence": "medium"},
        ]))
        assert not out.is_llm_fallback
        assert out.judgment.inferences[0].based_on_observation_indices == [1]

    def test_out_of_range_scalar_defaults_to_zero(self):
        out = _run(_raw(inferences=[
            {"text": "增长动能可持续", "based_on_observation_indices": 7, "confidence": "high"},
            {"text": "盈利质量稳定", "based_on_observation_indices": [0], "confidence": "medium"},
        ]))
        assert out.judgment.inferences[0].based_on_observation_indices == [0]

    def test_missing_indices_defaults_to_zero(self):
        out = _run(_raw(inferences=[
            {"text": "增长动能可持续", "confidence": "high"},
            {"text": "盈利质量稳定", "based_on_observation_indices": [0], "confidence": "medium"},
        ]))
        assert out.judgment.inferences[0].based_on_observation_indices == [0]

    def test_inference_dropped_when_zero_observations(self):
        # obs_count==0 时 indices 全清空 → 不能默认 [0] (会撞 min_length=1
        # 之后再撞 constraint 校验)，直接丢弃该 inference 而不是炸掉 run()
        out = _run(_raw(
            observations=[],
            inferences=[
                {"text": "凭空推论", "based_on_observation_indices": [0], "confidence": "high"},
            ],
        ))
        assert isinstance(out, AgentOutput)  # 没炸
        assert out.judgment.inferences == []

    def test_confidence_medium_high_normalized(self):
        # BUG-Y24 回归护栏（归一化搬家到 _coerce 后行为不变）
        out = _run(_raw(inferences=[
            {"text": "增长动能可持续", "based_on_observation_indices": [0], "confidence": "medium_high"},
            {"text": "盈利质量稳定", "based_on_observation_indices": [1], "confidence": "medium"},
        ]))
        assert out.judgment.inferences[0].confidence == "high"


# ---------------------------------------------------------------------------
# AUDIT-B5 ③: cognitive_bias_self_check as JSON string / garbage
# ---------------------------------------------------------------------------

class TestBiasCheckCoercion:
    def test_bias_check_json_string_rescued(self):
        # 审计实测崩溃点: bias_data 是 JSON 字符串 → .get() AttributeError
        out = _run(_raw(cognitive_bias_self_check=json.dumps({
            "anchoring_risk": "medium_high",
            "confirmation_bias_risk": "low",
            "recency_bias_risk": "medium",
            "narrative_fallacy_risk": "high",
        })))
        assert not out.is_llm_fallback
        bc = out.judgment.cognitive_bias_self_check
        assert bc.anchoring_risk == "high"  # medium_high 归一化
        assert bc.confirmation_bias_risk == "low"

    def test_bias_check_garbage_string_defaults(self):
        out = _run(_raw(cognitive_bias_self_check="not a json object"))
        bc = out.judgment.cognitive_bias_self_check
        assert bc.anchoring_risk == "medium"
        assert bc.mitigation_steps_taken == []

    def test_bias_check_non_dict_scalar_defaults(self):
        out = _run(_raw(cognitive_bias_self_check=42))
        assert out.judgment.cognitive_bias_self_check.anchoring_risk == "medium"


# ---------------------------------------------------------------------------
# AUDIT-B4: Counterargument.strength normalization
# ---------------------------------------------------------------------------

class TestStrengthNormalization:
    def test_strength_medium_normalized_to_moderate(self):
        # 审计实测: strength='medium' → ValidationError → 整 agent 退 rule-based
        out = _run(_raw(counterarguments=[
            {"text": "行业景气度可能见顶", "strength": "medium"},
        ]))
        assert not out.is_llm_fallback
        assert out.judgment.counterarguments[0].strength == "moderate"

    def test_strength_very_strong_and_upper_case(self):
        out = _run(_raw(counterarguments=[
            {"text": "反驳一", "strength": "very strong"},
            {"text": "反驳二", "strength": "STRONG"},
        ]))
        assert out.judgment.counterarguments[0].strength == "strong"
        assert out.judgment.counterarguments[1].strength == "strong"

    def test_strength_missing_defaults_to_moderate(self):
        out = _run(_raw(counterarguments=[
            {"text": "行业景气度可能见顶"},
        ]))
        assert out.judgment.counterarguments[0].strength == "moderate"


# ---------------------------------------------------------------------------
# AUDIT-B5 ④: per-item drop instead of whole-agent abort
# ---------------------------------------------------------------------------

class TestPerItemDrop:
    def test_malformed_observation_dropped(self):
        out = _run(_raw(observations=[
            {"text": "营收同比增长 30%", "source_ids": ["m_revenue"]},
            {"text": "", "source_ids": ["m_x"]},  # min_length=1 违规 → 丢弃
        ]))
        assert not out.is_llm_fallback
        assert len(out.judgment.observations) == 1

    def test_malformed_counterargument_dropped(self):
        out = _run(_raw(counterarguments=[
            {"strength": "strong"},  # 缺 text → 丢弃
            {"text": "行业景气度可能见顶", "strength": "moderate"},
        ]))
        assert len(out.judgment.counterarguments) == 1

    def test_combined_llm_quirks_survive(self):
        # 全套 quirk 一起上，agent 仍产出真实内容
        out = _run(_raw(
            observations=[
                {"text": "营收同比增长 30%", "source_ids": "m_revenue"},
                {"text": "毛利率维持 45%", "source_ids": ["m_gross_margin"]},
            ],
            inferences=[
                {"text": "增长动能可持续", "based_on_observation_indices": 1, "confidence": "medium_high"},
                {"text": "盈利质量稳定", "based_on_observation_indices": ["0"], "confidence": "medium"},
            ],
            counterarguments=[
                {"text": "行业景气度可能见顶", "strength": "medium", "evidence_ids": "ev_1"},
            ],
            cognitive_bias_self_check=json.dumps({
                "anchoring_risk": "medium_high",
                "confirmation_bias_risk": "low",
                "recency_bias_risk": "medium",
                "narrative_fallacy_risk": "high",
            }),
            self_reported_uncertainties=["宏观需求不确定", 3],
        ))
        assert not out.is_llm_fallback
        j = out.judgment
        assert j.observations[0].source_ids == ["m_revenue"]
        assert j.inferences[0].based_on_observation_indices == [1]
        assert j.inferences[0].confidence == "high"
        assert j.inferences[1].based_on_observation_indices == [0]
        assert j.counterarguments[0].strength == "moderate"
        assert j.counterarguments[0].evidence_ids == ["ev_1"]
        assert j.cognitive_bias_self_check.anchoring_risk == "high"
        assert j.self_reported_uncertainties == ["宏观需求不确定", "3"]


# ---------------------------------------------------------------------------
# AUDIT-B6: _strip_sensitive must handle CJK / mixed-script text
# ---------------------------------------------------------------------------

class TestStripSensitiveCJK:
    """审计实测三组 UNCHANGED 的句子必须变化。"""

    def test_chinese_export_control_and_entity_list(self):
        src = "美国出口管制与实体清单限制了公司"
        out = _strip_sensitive(src)
        assert out != src
        assert "出口管制" not in out
        assert "实体清单" not in out
        assert "贸易限制" in out
        assert "受限名单" in out

    def test_english_brand_adjacent_to_cjk(self):
        src = "公司与Huawei在昇腾生态竞争"
        out = _strip_sensitive(src)
        assert out != src
        assert "Huawei" not in out
        assert "a regional competitor" in out

    def test_taiwan_adjacent_to_cjk(self):
        src = "受Taiwan供应链影响"
        out = _strip_sensitive(src)
        assert out != src
        assert "Taiwan" not in out
        assert "the region" in out

    def test_chinese_huawei_and_taiwan(self):
        out = _strip_sensitive("华为与台湾供应链")
        assert "华为" not in out
        assert "某区域竞争对手" in out
        assert "该地区" in out

    def test_tsmc_full_names_protected(self):
        # 别误伤台积电全称（中英文）
        src = "台湾积体电路制造 与 Taiwan Semiconductor 合作"
        out = _strip_sensitive(src)
        assert "台湾积体电路制造" in out
        assert "Taiwan Semiconductor" in out

    def test_military_terms(self):
        out = _strip_sensitive("产品涉军用与军工板块，军方采购占比低")
        assert "军用" not in out
        assert "军工" not in out
        assert "军方" not in out
        assert "受限用途" in out

    def test_plain_english_still_stripped(self):
        # 原有英文路径不回归
        out = _strip_sensitive("Export controls and sanctions hit Huawei.")
        assert "Export control" not in out
        assert "sanction" not in out.lower() or "trade restrictions" in out
        assert "Huawei" not in out

    def test_sanction_inside_word_untouched(self):
        # (?<![A-Za-z]) 边界不误伤词中片段
        assert _strip_sensitive("sanctioned") == "sanctioned"


class TestStripRetryIntegration:
    """content_filter 重试端到端：第二次调用的 prompt 必须真的被脱敏。"""

    def test_retry_uses_stripped_chinese_prompt(self):
        raw = _raw()

        class _FilterOnceLLM(_StubLLM):
            def call_structured(self, **kwargs):
                self.calls.append(kwargs)
                if len(self.calls) == 1:
                    raise DeepSeekContentFilterError(
                        "Error code: 400 - high risk content_filter"
                    )
                return self.raw

        agent = _ParseAgent(_FilterOnceLLM(raw))
        agent.SYSTEM_PROMPT = "分析这家公司：与华为竞争，受出口管制影响。"
        inp = AgentInput(entity_id="e_test", run_id="r_test", question_id="q_test")
        out = agent.run(inp)

        assert not out.is_llm_fallback  # 重试成功，没有退 mock
        assert len(agent._llm.calls) == 2
        first, second = agent._llm.calls
        assert "华为" in first["system_prompt"]
        # AUDIT-B6 修复前：第二次 prompt 与第一次逐字相同（strip 是 no-op）
        assert second["system_prompt"] != first["system_prompt"]
        assert "华为" not in second["system_prompt"]
        assert "某区域竞争对手" in second["system_prompt"]
        assert "出口管制" not in second["system_prompt"]


# ---------------------------------------------------------------------------
# AUDIT-B7: _is_content_filter_error classification
# ---------------------------------------------------------------------------

class TestContentFilterClassification:
    def test_typed_deepseek_error_is_content_filter(self):
        # 也覆盖 Grok：GrokClient 继承 DeepSeekClient，抛同一个类型
        assert _is_content_filter_error(
            DeepSeekContentFilterError("Error code: 400 - rejected")
        )

    def test_semantic_keywords_match(self):
        assert _is_content_filter_error(RuntimeError("upstream content_filter triggered"))
        assert _is_content_filter_error(RuntimeError("Error code: 400 - high risk input"))

    def test_bare_400_schema_error_is_not_content_filter(self):
        # 修复前: '400' in msg[:30] → schema 400 也走脱敏重试 + 元数据撒谎
        e = RuntimeError(
            "Error code: 400 - {'error': {'message': 'Invalid request: "
            "tools[0].function.parameters is not a valid JSON Schema'}}"
        )
        assert not _is_content_filter_error(e)

    def test_400_with_filter_body_is_content_filter(self):
        e = RuntimeError("Error code: 400 - request blocked by safety filter")
        assert _is_content_filter_error(e)

    def test_unrelated_error_is_not_content_filter(self):
        assert not _is_content_filter_error(RuntimeError("connection timeout"))
