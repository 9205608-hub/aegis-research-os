"""Mock LLM Client — for local development without API key.

Returns rule-based responses that match the JudgmentContract schema,
reusing the existing rule-based agent logic.
"""

from __future__ import annotations

from typing import Any

from aegis.core.llm.config import CostTracker, UsageRecord


class MockLLMClient:
    """Mock LLM client for development and testing.

    Returns structured data that passes JudgmentContract validation.
    Simulates token usage for cost tracking testing.
    """

    def __init__(self) -> None:
        self.cost_tracker = CostTracker()
        self._call_count = 0

    def call_structured(
        self,
        system_prompt: str,
        user_message: str,
        tool_schema: dict[str, Any],
        tool_name: str = "output",
        role: str = "specialist_agent",
        language: str = "en",
        fallback_reason: str = "",
        **kwargs,
    ) -> dict[str, Any]:
        """Return mock structured output based on the tool schema.

        Generates plausible responses based on the agent role and input context.
        `language` controls fallback locale ("en" or "zh-CN") so A-share runs
        get Chinese rule-based text and don't break CLAUDE.md's 中文化铁律.

        BUG-A13 (2026-05-05): `fallback_reason` is the underlying failure
        category from llm_agent_base. Categorise it so the on-card label
        reflects the actual cause (timeout / parse-fail / content-filter)
        rather than the always-the-same '[LLM 不可用]'.
        """
        self._call_count += 1

        # Simulate token usage
        usage = UsageRecord(
            model_id=f"mock-{role}",
            input_tokens=len(system_prompt.split()) + len(user_message.split()),
            output_tokens=500,
        )
        self.cost_tracker.record(usage)

        # Generate role-appropriate mock response
        return self._generate_mock_judgment(
            role, user_message, tool_schema, language, fallback_reason,
        )

    def call_text(
        self,
        system_prompt: str,
        user_message: str,
        role: str = "specialist_agent",
    ) -> str:
        """Return mock text response."""
        self._call_count += 1
        usage = UsageRecord(
            model_id=f"mock-{role}",
            input_tokens=len(system_prompt.split()) + len(user_message.split()),
            output_tokens=200,
        )
        self.cost_tracker.record(usage)
        return f"[Mock {role} response to: {user_message[:100]}...]"

    @staticmethod
    def _classify_fallback_reason(reason: str) -> tuple[str, str]:
        """Categorise an LLM failure string for the on-card label.

        Returns (en_tag, zh_tag) — short phrases suitable for the FB prefix.
        """
        s = (reason or "").lower()
        if "content_filter" in s or "high risk" in s or "rejected" in s:
            return ("content filter", "内容过滤拒答")
        if "timeout" in s or "timed out" in s:
            return ("timed out", "调用超时")
        if "rate" in s and ("limit" in s or "429" in s):
            return ("rate-limited", "限流退避失败")
        if "json" in s or "parseable" in s or "tool call" in s or "empty" in s:
            return ("output unparseable", "输出无法解析")
        if "401" in s or "auth" in s or "api_key" in s:
            return ("auth failed", "鉴权失败")
        if "connection" in s or "network" in s or "remote" in s:
            return ("network error", "网络异常")
        if reason:
            return ("call failed", "调用失败")
        return ("LLM unavailable", "LLM 不可用")

    def _generate_mock_judgment(
        self, role: str, context: str, schema: dict, language: str = "en",
        fallback_reason: str = "",
    ) -> dict[str, Any]:
        """Generate a plausible mock judgment based on role."""
        if language == "zh-CN":
            return self._generate_mock_judgment_zh(role, fallback_reason)
        # BUG-A13: classify the underlying cause so on-card text reflects
        # the actual reason rather than the always-the-same "LLM unavailable".
        en_tag, _ = self._classify_fallback_reason(fallback_reason)
        # Base template that passes JudgmentContract validation
        base = {
            "observations": [
                {
                    "text": f"[{role}] Key observation based on provided financial data",
                    "source_ids": ["metric:primary_metric"],
                },
                {
                    "text": f"[{role}] Secondary observation from evidence review",
                    "source_ids": ["evidence:primary_source"],
                },
            ],
            "inferences": [
                {
                    "text": f"Management demonstrates strong capital allocation — ROIC exceeds cost of capital [rule-based fallback: {en_tag} for {role}]",
                    "based_on_observation_indices": [0, 1],
                    "confidence": "medium",
                },
            ],
            "counterarguments": [
                {
                    "text": f"Historical metrics may not reflect forward conditions — current cycle position and structural changes could alter the outlook [rule-based fallback: {en_tag} for {role}]",
                    "strength": "moderate",
                    "evidence_ids": [],
                },
            ],
            "disconfirming_triggers": [
                {
                    "text": f"[{role}] Primary disconfirming trigger to monitor",
                    "monitorable": True,
                    "check_frequency": "quarterly",
                },
            ],
            "self_reported_uncertainties": [
                f"[{role}] Key uncertainty in this analysis",
            ],
            "cognitive_bias_self_check": {
                "anchoring_risk": "medium",
                "confirmation_bias_risk": "medium",
                "recency_bias_risk": "low",
                "narrative_fallacy_risk": "medium",
                "mitigation_steps_taken": [
                    "Used multiple data sources for cross-validation",
                    "Explicitly sought disconfirming evidence",
                ],
            },
        }

        # Role-specific enhancements — GENERIC templates (no company-specific text).
        # These are fallback placeholders when the LLM is unavailable.
        role_details = {
            "accounting_analyst": {
                "observations": [
                    {"text": "Accruals ratio is within acceptable range — earnings quality appears sound [rule-based fallback]", "source_ids": ["metric:accruals_ratio"]},
                    {"text": "CFO/NI ratio exceeds 1.0 — strong cash conversion [rule-based fallback]", "source_ids": ["metric:cfo_to_net_income"]},
                    {"text": "SBC as a percentage of revenue should be monitored for dilution impact [rule-based fallback]", "source_ids": ["metric:sbc_to_revenue"]},
                ],
                "inferences": [
                    {"text": "Earnings quality requires further LLM analysis — rule-based assessment suggests cash conversion is adequate but SBC dilution warrants monitoring [rule-based fallback]", "based_on_observation_indices": [0, 1, 2], "confidence": "low"},
                ],
            },
            "business_analyst": {
                "observations": [
                    {"text": "Gross margin level indicates pricing power and competitive positioning [rule-based fallback]", "source_ids": ["metric:gross_margin"]},
                    {"text": "ROIC level reflects capital allocation efficiency [rule-based fallback]", "source_ids": ["metric:roic"]},
                ],
                "inferences": [
                    {"text": "Business quality assessment requires LLM analysis to evaluate competitive moat durability and reinvestment runway [rule-based fallback]", "based_on_observation_indices": [0, 1], "confidence": "low"},
                ],
            },
            "valuation_analyst": {
                "observations": [
                    {"text": "Current valuation multiples suggest the market is pricing in forward earnings expectations [rule-based fallback]", "source_ids": ["metric:pe_ratio"]},
                    {"text": "Enterprise value relative to earnings reflects profitability trajectory expectations [rule-based fallback]", "source_ids": ["metric:ev_to_ebitda"]},
                ],
                "inferences": [
                    {"text": "DCF sensitivity analysis shows terminal growth rate and WACC are the dominant value drivers — detailed assessment requires LLM analysis [rule-based fallback]", "based_on_observation_indices": [0, 1], "confidence": "low"},
                ],
                "counterarguments": [
                    {"text": "Valuation multiples may reflect structural business changes rather than cyclical overvaluation [rule-based fallback]", "strength": "moderate", "evidence_ids": []},
                ],
            },
            "variant_analyst": {
                "observations": [
                    {"text": "Market-implied growth rate from reverse DCF should be compared against base case estimates [rule-based fallback]", "source_ids": ["reverse_dcf:implied_growth"]},
                    {"text": "Consensus revision momentum and breadth provide context for market expectations [rule-based fallback]", "source_ids": ["consensus:revision_momentum"]},
                ],
                "inferences": [
                    {"text": "Variant thesis identification requires LLM analysis to assess where our view diverges from consensus [rule-based fallback]", "based_on_observation_indices": [0, 1], "confidence": "low"},
                ],
                "counterarguments": [
                    {"text": "Market consensus may be incorporating information not yet reflected in our model — counter-thesis analysis requires LLM [rule-based fallback]", "strength": "moderate", "evidence_ids": []},
                ],
            },
            "risk_analyst": {
                "observations": [
                    {"text": "Balance sheet leverage and net debt position affect financial risk profile [rule-based fallback]", "source_ids": ["metric:net_debt"]},
                    {"text": "Capital expenditure intensity relative to revenue indicates reinvestment requirements [rule-based fallback]", "source_ids": ["metric:capex_to_revenue"]},
                ],
                "inferences": [
                    {"text": "Key risk identification requires LLM analysis — rule-based assessment flags balance sheet and capex as areas to monitor [rule-based fallback]", "based_on_observation_indices": [0, 1], "confidence": "low"},
                ],
            },
            "management_analyst": {
                "observations": [
                    {"text": "Historical ROIC trajectory reflects management capital allocation quality [rule-based fallback]", "source_ids": ["metric:roic"]},
                    {"text": "Capital allocation decisions and investment cycle should be evaluated against strategic objectives [rule-based fallback]", "source_ids": ["metric:capex_to_revenue"]},
                ],
                "inferences": [
                    {"text": "Management quality assessment requires LLM analysis to evaluate strategic execution track record and forward investment thesis [rule-based fallback]", "based_on_observation_indices": [0, 1], "confidence": "low"},
                ],
            },
            "sector_context_agent": {
                "observations": [
                    {"text": "Entity sector classification determines relevant peer group and cyclicality profile [rule-based fallback]", "source_ids": ["sector_pack:classification"]},
                    {"text": "Macro cycle positioning affects sector-level tailwinds and headwinds [rule-based fallback]", "source_ids": ["sector_pack:cycle"]},
                ],
                "inferences": [
                    {"text": "Sector context analysis requires LLM to evaluate competitive dynamics and macro sensitivity for this specific entity [rule-based fallback]", "based_on_observation_indices": [0, 1], "confidence": "low"},
                ],
            },
        }

        # Merge role-specific data into base template
        role_key = role.replace("_agent", "").replace("_context", "")
        if role_key in role_details:
            for key, value in role_details[role_key].items():
                base[key] = value

        return base

    def _generate_mock_judgment_zh(
        self, role: str, fallback_reason: str = "",
    ) -> dict[str, Any]:
        """Chinese-locale mock judgment for A-share fallback (BUG-34).

        BUG-A13 (2026-05-05): the `[LLM 不可用]` label always used to ship
        regardless of the actual failure mode. Now categorises the reason
        (timeout / parse-fail / content-filter / network …) so the user
        sees an accurate cause without diving into stderr logs.
        """
        _, zh_tag = self._classify_fallback_reason(fallback_reason)
        FB = f"[规则模板兜底·{zh_tag}]"
        base = {
            "observations": [
                {"text": f"{FB} 基于财务数据的关键观察待补", "source_ids": ["metric:primary_metric"]},
                {"text": f"{FB} 来自二级证据的补充观察待补", "source_ids": ["evidence:primary_source"]},
            ],
            "inferences": [
                {
                    "text": f"{FB} 完整推断需要 LLM 二次分析；规则层暂以中性结论占位",
                    "based_on_observation_indices": [0, 1],
                    "confidence": "low",
                },
            ],
            "counterarguments": [
                {
                    "text": f"{FB} 历史指标未必反映前瞻条件，周期位置与结构性变化可能改变结论",
                    "strength": "moderate",
                    "evidence_ids": [],
                },
            ],
            "disconfirming_triggers": [
                {
                    "text": f"{FB} 关键证伪信号待 LLM 补全",
                    "monitorable": True,
                    "check_frequency": "quarterly",
                },
            ],
            "self_reported_uncertainties": [f"{FB} 本环节关键不确定性待 LLM 补全"],
            "cognitive_bias_self_check": {
                "anchoring_risk": "medium",
                "confirmation_bias_risk": "medium",
                "recency_bias_risk": "low",
                "narrative_fallacy_risk": "medium",
                "mitigation_steps_taken": ["跨源交叉验证", "主动寻找证伪证据"],
            },
        }

        role_details = {
            "accounting_analyst": {
                "observations": [
                    {"text": f"{FB} 应计项目占比处于正常区间，盈利质量初判稳健", "source_ids": ["metric:accruals_ratio"]},
                    {"text": f"{FB} CFO/净利润 > 1.0，经营现金流转化能力良好", "source_ids": ["metric:cfo_to_net_income"]},
                    {"text": f"{FB} 股权激励占营收比需关注稀释影响", "source_ids": ["metric:sbc_to_revenue"]},
                ],
                "inferences": [
                    {"text": f"{FB} 完整盈利质量判断需 LLM 分析；规则层提示现金转化充分但稀释需监控", "based_on_observation_indices": [0, 1, 2], "confidence": "low"},
                ],
            },
            "business_analyst": {
                "observations": [
                    {"text": f"{FB} 毛利率水平体现定价权与竞争位置", "source_ids": ["metric:gross_margin"]},
                    {"text": f"{FB} ROIC 水平反映资本配置效率", "source_ids": ["metric:roic"]},
                ],
                "inferences": [
                    {"text": f"{FB} 业务质量评估需 LLM 分析护城河可持续性与再投资跑道", "based_on_observation_indices": [0, 1], "confidence": "low"},
                ],
            },
            "valuation_analyst": {
                "observations": [
                    {"text": f"{FB} 当前估值倍数反映市场对未来盈利的定价", "source_ids": ["metric:pe_ratio"]},
                    {"text": f"{FB} 企业价值/盈利反映利润轨迹预期", "source_ids": ["metric:ev_to_ebitda"]},
                ],
                "inferences": [
                    {"text": f"{FB} DCF 敏感性显示永续增速与 WACC 是主导变量；细评需 LLM", "based_on_observation_indices": [0, 1], "confidence": "low"},
                ],
                "counterarguments": [
                    {"text": f"{FB} 估值倍数可能反映结构性变化而非周期性高估", "strength": "moderate", "evidence_ids": []},
                ],
            },
            "variant_analyst": {
                "observations": [
                    {"text": f"{FB} 反向 DCF 隐含增速需对照基准估算", "source_ids": ["reverse_dcf:implied_growth"]},
                    {"text": f"{FB} 一致预期修正动量与广度提供市场预期参照", "source_ids": ["consensus:revision_momentum"]},
                ],
                "inferences": [
                    {"text": f"{FB} 变体论点识别需 LLM 评估与共识的分歧位置", "based_on_observation_indices": [0, 1], "confidence": "low"},
                ],
                "counterarguments": [
                    {"text": f"{FB} 共识可能已纳入模型未体现信息；反向论点需 LLM 校验", "strength": "moderate", "evidence_ids": []},
                ],
            },
            "risk_analyst": {
                "observations": [
                    {"text": f"{FB} 资产负债结构与净负债影响财务风险", "source_ids": ["metric:net_debt"]},
                    {"text": f"{FB} 资本开支强度反映再投资需求", "source_ids": ["metric:capex_to_revenue"]},
                ],
                "inferences": [
                    {"text": f"{FB} 关键风险识别需 LLM 分析；规则层提示资产负债与 capex 为重点监控项", "based_on_observation_indices": [0, 1], "confidence": "low"},
                ],
            },
            "management_analyst": {
                "observations": [
                    {"text": f"{FB} 历史 ROIC 轨迹反映管理层资本配置质量", "source_ids": ["metric:roic"]},
                    {"text": f"{FB} 资本配置决策与投资周期需对照战略目标评估", "source_ids": ["metric:capex_to_revenue"]},
                ],
                "inferences": [
                    {"text": f"{FB} 管理层质量评估需 LLM 分析战略执行与前瞻投资逻辑", "based_on_observation_indices": [0, 1], "confidence": "low"},
                ],
            },
            "sector_context_agent": {
                "observations": [
                    {"text": f"{FB} 行业分类决定可比同业池与周期属性", "source_ids": ["sector_pack:classification"]},
                    {"text": f"{FB} 宏观周期位置影响行业级顺逆风", "source_ids": ["sector_pack:cycle"]},
                ],
                "inferences": [
                    {"text": f"{FB} 行业上下文分析需 LLM 评估对该实体的竞争与宏观敏感性", "based_on_observation_indices": [0, 1], "confidence": "low"},
                ],
            },
        }

        role_key = role.replace("_agent", "").replace("_context", "")
        if role_key in role_details:
            for key, value in role_details[role_key].items():
                base[key] = value
        return base
