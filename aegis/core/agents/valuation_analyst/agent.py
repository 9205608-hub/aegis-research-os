"""Valuation Analyst Agent — Section 19.6.

Responsibilities:
- Market-implied assumptions (via Reverse DCF)
- Forward scenario modeling (provide assumptions to Scenario Engine)
- Scenario sensitivities
- Assumption bottlenecks
- Peer relative valuation
- Historical range context
- Macro-adjusted framework

Prohibitions:
- No "cheap/expensive" without definition
- No variant claim without market expectations data
- No ignoring earnings quality when choosing profit metric
- No ignoring macro context
"""

from __future__ import annotations

from aegis.core.agents.base import AgentBase, AgentInput, is_zh_input
from aegis.data_contracts.judgment_schema import (
    CognitiveBiasSelfCheck,
    Counterargument,
    DisconfirmingTrigger,
    Inference,
    Observation,
)


class ValuationAnalyst(AgentBase):
    """Specialist agent for valuation and scenario analysis."""

    AGENT_NAME = "valuation_analyst"
    AGENT_VERSION = "0.1.0"

    FOCUS_METRICS = frozenset({
        "ev_to_ebitda", "ev_to_revenue", "pe_ratio", "pe_ratio_ttm",
        "enterprise_value", "fcf_simple", "gross_margin",
        "operating_margin", "roic",
    })

    def _extract_observations(self, inp: AgentInput) -> list[Observation]:
        observations: list[Observation] = []
        metrics = inp.metric_results
        zh = is_zh_input(inp)

        # Current valuation multiples — prefer TTM P/E when available
        # (matches peer comparison source), but surface both if they
        # disagree materially so the LLM understands the FY-vs-TTM gap.
        for key in ("pe_ratio_ttm", "pe_ratio", "ev_to_ebitda", "ev_to_revenue"):
            if key in metrics and metrics[key]:
                label = ({
                    "pe_ratio_ttm": "市盈率 P/E (TTM)",
                    "pe_ratio": "市盈率 P/E (静态)",
                    "ev_to_ebitda": "EV/EBITDA",
                    "ev_to_revenue": "EV/营收",
                } if zh else {
                    "pe_ratio_ttm": "P/E (TTM)",
                    "pe_ratio": "P/E (FY static)",
                    "ev_to_ebitda": "EV/EBITDA",
                    "ev_to_revenue": "EV/Revenue",
                })[key]
                observations.append(Observation(
                    text=f"{label}: {metrics[key]:.1f}x",
                    source_ids=[f"metric:{key}:{inp.entity_id}"],
                ))

        # Enterprise value
        if "enterprise_value" in metrics:
            observations.append(Observation(
                text=(f"企业价值 (EV): {metrics['enterprise_value']:,.0f}" if zh
                      else f"Enterprise value: {metrics['enterprise_value']:,.0f}"),
                source_ids=[f"metric:enterprise_value:{inp.entity_id}"],
            ))

        # Implied assumptions from market expectations (in prior judgments or context)
        if inp.macro_context:
            priced_in = inp.macro_context.get("priced_in", {})
            # Aegis 2.0 Phase 0（设计红线 2）：优先使用条件化预期前沿——
            # 「若利润率 X%，现价需要 Y% 增速支撑」句式（lines 已由
            # orchestrator 按市场语言渲染）。前沿可用时不再输出单点
            # 「市场隐含增速 Z%」（一个价格反解不出两个未知数）。
            _frontier = priced_in.get("expectations_frontier") or {}
            if _frontier.get("lines"):
                for _line in _frontier["lines"][:4]:
                    observations.append(Observation(
                        text=(f"市场隐含预期（条件化反解）: {_line}" if zh
                              else f"Market-implied expectation (conditional): {_line}"),
                        source_ids=["expectations_frontier"],
                    ))
            # AUDIT-A9 (BUG-Y20 third path): boundary-hit reverse-DCF values
            # are fake-clean artifacts (e.g. exactly 0.50). The orchestrator
            # nulls the value and sets `implied_growth_unreliable`; guard
            # here too so the artifact can never become an Observation.
            # (Legacy single-point fallback — only when the frontier is
            # unavailable; 设计红线 2 prohibits it otherwise.)
            elif (priced_in.get("implied_revenue_growth") is not None
                    and not priced_in.get("implied_growth_unreliable")):
                observations.append(Observation(
                    text=(f"市场隐含营收增速: {priced_in['implied_revenue_growth']:.2%}" if zh
                          else f"Market-implied revenue growth: {priced_in['implied_revenue_growth']:.2%}"),
                    source_ids=["reverse_dcf:implied_growth"],
                ))
            if priced_in.get("implied_terminal_growth") is not None:
                observations.append(Observation(
                    text=(f"市场隐含永续增长率: {priced_in['implied_terminal_growth']:.2%}" if zh
                          else f"Market-implied terminal growth: {priced_in['implied_terminal_growth']:.2%}"),
                    source_ids=["reverse_dcf:implied_terminal"],
                ))

        # Scenario outputs from context
        scenarios = inp.macro_context.get("scenarios", {}) if inp.macro_context else {}
        _scenario_zh = {"bear": "熊市", "base": "基准", "bull": "牛市"}
        for name in ("bear", "base", "bull"):
            val = scenarios.get(f"{name}_value")
            if val is not None:
                observations.append(Observation(
                    text=(f"「{_scenario_zh[name]}」情景每股价值: {val:.2f}" if zh
                          else f"Scenario '{name}' per-share value: {val:.2f}"),
                    source_ids=[f"scenario_engine:{name}"],
                ))

        # Macro context for discount rate
        if inp.macro_context:
            phase = inp.macro_context.get("cycle_phase", "")
            if phase:
                observations.append(Observation(
                    text=(f"宏观周期阶段: {phase}" if zh
                          else f"Macro cycle phase: {phase}"),
                    source_ids=["macro_context:cycle_phase"],
                ))

        # Evidence
        for ep in inp.evidence_packets:
            if ep.get("assertion_type") in ("valuation", "guidance", "consensus",
                                             "earnings_outlook", "revenue_guidance"):
                observations.append(Observation(
                    text=ep.get("assertion_text", ""),
                    source_ids=[ep.get("evidence_id", "")],
                ))

        return observations

    def _derive_inferences(
        self, observations: list[Observation], inp: AgentInput
    ) -> list[Inference]:
        inferences: list[Inference] = []
        metrics = inp.metric_results
        zh = is_zh_input(inp)

        # Valuation vs implied assumptions
        implied_obs = [i for i, o in enumerate(observations)
                       if "market-implied" in o.text.lower() or "市场隐含" in o.text]
        scenario_obs = [i for i, o in enumerate(observations)
                        if "scenario" in o.text.lower() or "情景" in o.text]
        multiple_obs = [i for i, o in enumerate(observations) if any(
            k in o.text.lower() for k in ("pe ", "ev ", "enterprise")
        ) or "市盈率" in o.text or "企业价值" in o.text]

        # Aegis 2.0 Phase 0：前沿观察在场时，推理必须保持条件化框架
        # （设计红线 2），并把预期与可验证事实的对照作为核心任务。
        frontier_obs = [i for i, o in enumerate(observations)
                        if "expectations_frontier" in (o.source_ids or [])]
        _frontier_ctx = (inp.macro_context or {}).get("priced_in", {}).get(
            "expectations_frontier") or {}
        if frontier_obs and _frontier_ctx.get("lines"):
            inferences.append(Inference(
                text=("市场定价须按条件化预期解读：同一现价在不同终年利润率情景下"
                      "对应不同的隐含增速，结论应表述为「若利润率 X%，需 Y% 增速支撑」，"
                      "并对照业绩预告、公告与财务证据检验该预期的可信度"
                      if zh else
                      "Price must be read through conditional expectations: the same "
                      "price maps to different implied growth under each terminal-margin "
                      "scenario. State conclusions as 'at margin X%, ~Y% growth is "
                      "required' and test that expectation against disclosed facts"),
                based_on_observation_indices=frontier_obs[:1],
                confidence="high",
            ))

        # Implied growth vs consensus comparison (legacy single-point path —
        # only when the conditional frontier is unavailable, 设计红线 2)
        if implied_obs and inp.macro_context and not _frontier_ctx.get("lines"):
            priced_in = inp.macro_context.get("priced_in", {})
            implied_g = priced_in.get("implied_revenue_growth")
            # AUDIT-A9: skip the aggressive/modest judgment when the value
            # is a boundary-hit artifact (>25% branch would fire on the
            # fake 0.50 edge value).
            if priced_in.get("implied_growth_unreliable"):
                implied_g = None
            if implied_g is not None:
                if implied_g > 0.25:
                    inferences.append(Inference(
                        text=(f"市场定价隐含 {implied_g:.0%} 的营收增速——预期激进；"
                              "若增长不及预期，风险偏向下行"
                              if zh else
                              f"Market implies {implied_g:.0%} revenue growth — aggressive; "
                              "risk skewed to downside if growth disappoints"),
                        based_on_observation_indices=implied_obs[:1],
                        confidence="medium",
                    ))
                elif implied_g < 0.05:
                    inferences.append(Inference(
                        text=(f"市场定价仅隐含 {implied_g:.0%} 的营收增速——预期保守；"
                              "若增长跨过这一低门槛，则存在上行空间"
                              if zh else
                              f"Market implies only {implied_g:.0%} revenue growth — modest; "
                              "potential upside if growth exceeds low bar"),
                        based_on_observation_indices=implied_obs[:1],
                        confidence="medium",
                    ))

        # Scenario spread inference
        if scenario_obs:
            scenarios = inp.macro_context.get("scenarios", {}) if inp.macro_context else {}
            bear = scenarios.get("bear_value")
            bull = scenarios.get("bull_value")
            if bear and bull and bear > 0:
                spread = (bull - bear) / bear
                inferences.append(Inference(
                    text=(f"情景区间（牛市/熊市）: {spread:.0%}——"
                          f"结果分布{'较宽' if spread > 0.5 else '较窄'}"
                          if zh else
                          f"Scenario spread (bull/bear): {spread:.0%} — "
                          f"{'wide' if spread > 0.5 else 'narrow'} range of outcomes"),
                    based_on_observation_indices=scenario_obs,
                    confidence="medium",
                ))

        # Macro context should influence discount rate
        macro_obs = [i for i, o in enumerate(observations)
                     if "macro cycle" in o.text.lower() or "宏观周期" in o.text]
        if macro_obs:
            inferences.append(Inference(
                text=("宏观周期位置应反映在 WACC 假设与情景权重之中" if zh
                      else "Macro cycle position should be reflected in WACC assumption and scenario weights"),
                based_on_observation_indices=macro_obs,
                confidence="high",
            ))

        return inferences

    def _generate_counterarguments(
        self, inferences: list[Inference], inp: AgentInput
    ) -> list[Counterargument]:
        counterargs: list[Counterargument] = []
        zh = is_zh_input(inp)

        for inf in inferences:
            t = inf.text.lower()
            if "risk skewed to downside" in t or "风险偏向下行" in inf.text:
                counterargs.append(Counterargument(
                    text=("若公司具备长期成长顺风（如 AI 变现）足以维持超越市场的增速，"
                          "高隐含增速未必不合理"
                          if zh else
                          "High implied growth may be warranted if company has secular growth tailwinds "
                          "(e.g., AI monetization) that sustain above-market rates"),
                    strength="moderate",
                    evidence_ids=[],
                ))
            elif "potential upside" in t or "存在上行空间" in inf.text:
                counterargs.append(Counterargument(
                    text=("低隐含增速可能恰当地反映了结构性逆风——价值陷阱往往呈现这种特征"
                          if zh else
                          "Low implied growth may correctly reflect structural headwinds — "
                          "value traps exhibit this pattern"),
                    strength="moderate",
                    evidence_ids=[],
                ))

        counterargs.append(Counterargument(
            text=("DCF 估值对永续增长率与 WACC 假设高度敏感——微小的参数变化即可导致估值大幅波动"
                  if zh else
                  "DCF valuation is highly sensitive to terminal growth and WACC assumptions — "
                  "small changes drive large value swings"),
            strength="strong",
            evidence_ids=[],
        ))

        return counterargs

    def _identify_disconfirming_triggers(
        self, inferences: list[Inference], inp: AgentInput
    ) -> list[DisconfirmingTrigger]:
        zh = is_zh_input(inp)
        return [
            DisconfirmingTrigger(
                text=("一致预期营收连续 3 个月下修" if zh
                      else "Consensus revenue revision turns negative for 3 consecutive months"),
                monitorable=True,
                check_frequency="monthly",
            ),
            DisconfirmingTrigger(
                text=("预期市盈率升破 5 年区间 95 分位" if zh
                      else "Forward P/E moves above 95th percentile of 5-year range"),
                monitorable=True,
                check_frequency="weekly",
            ),
            DisconfirmingTrigger(
                text=("管理层业绩指引下调至低于熊市情景假设" if zh
                      else "Management lowers guidance below bear-case assumption"),
                monitorable=True,
                check_frequency="quarterly",
            ),
        ]

    def _cognitive_bias_self_check(self, inp: AgentInput) -> CognitiveBiasSelfCheck:
        zh = is_zh_input(inp)
        return CognitiveBiasSelfCheck(
            anchoring_risk="high",
            confirmation_bias_risk="medium",
            recency_bias_risk="medium",
            narrative_fallacy_risk="medium",
            mitigation_steps_taken=(
                [
                    "以反向 DCF 锚定市场隐含假设，而非自身先验",
                    "通过情景区间分析避免过度自信的点估计",
                    "在贴现率讨论中引用宏观周期阶段",
                ] if zh else [
                    "Used reverse DCF to anchor on market-implied assumptions, not own prior",
                    "Included scenario spread analysis to avoid overconfident point estimates",
                    "Referenced macro cycle phase in discount rate discussion",
                ]
            ),
        )

    def _report_uncertainties(self, inp: AgentInput) -> list[str]:
        if is_zh_input(inp):
            return [
                "DCF 结果对终值假设高度敏感",
                "反向 DCF 的隐含增速依赖 WACC 估计的准确性",
                "相对估值隐含「可比公司定价正确」的假设",
            ]
        return [
            "DCF output is highly sensitive to terminal value assumptions",
            "Reverse DCF implied growth depends on WACC estimate accuracy",
            "Relative valuation assumes peers are correctly priced",
        ]
