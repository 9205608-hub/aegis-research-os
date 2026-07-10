"""Risk Analyst Agent — Section 19.7.

Responsibilities:
- Downside tree
- Regulation / competition / execution risk map
- Balance sheet constraints
- Kill criteria definition
- Thesis failure paths
- Tail risk assessment
- Scenario stress testing
- Concentration risk
- Supply chain risk (via Entity Relationship Graph)
- Geopolitical risk
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


class RiskAnalyst(AgentBase):
    """Specialist agent for risk assessment and kill criteria."""

    AGENT_NAME = "risk_analyst"
    AGENT_VERSION = "0.1.0"

    # Leverage thresholds
    NET_DEBT_EBITDA_WARN = 3.0
    CURRENT_RATIO_WARN = 1.0

    def _extract_observations(self, inp: AgentInput) -> list[Observation]:
        observations: list[Observation] = []
        metrics = inp.metric_results
        zh = is_zh_input(inp)

        # Balance sheet health
        if "net_debt_to_ebitda" in metrics:
            val = metrics["net_debt_to_ebitda"]
            if zh:
                level = "偏高" if val > self.NET_DEBT_EBITDA_WARN else "可控"
                text = f"净负债/EBITDA: {val:.1f}x——杠杆水平{level}"
            else:
                level = "elevated" if val > self.NET_DEBT_EBITDA_WARN else "manageable"
                text = f"Net Debt/EBITDA: {val:.1f}x — leverage is {level}"
            observations.append(Observation(
                text=text,
                source_ids=[f"metric:net_debt_to_ebitda:{inp.entity_id}"],
            ))

        if "current_ratio" in metrics:
            val = metrics["current_ratio"]
            observations.append(Observation(
                text=(f"流动比率: {val:.2f}x" if zh else f"Current ratio: {val:.2f}x"),
                source_ids=[f"metric:current_ratio:{inp.entity_id}"],
            ))

        if "net_debt" in metrics:
            observations.append(Observation(
                text=(f"净负债: {metrics['net_debt']:,.0f}" if zh
                      else f"Net debt: {metrics['net_debt']:,.0f}"),
                source_ids=[f"metric:net_debt:{inp.entity_id}"],
            ))

        # Supply chain risk from entity relationships
        supplier_count = 0
        customer_count = 0
        for rel in inp.entity_relationships:
            rel_type = rel.get("relationship_type", "")
            other = rel.get("entity_b") if rel.get("entity_a") == inp.entity_id else rel.get("entity_a", "")

            if "supplier" in rel_type:
                supplier_count += 1
                rev_sig = rel.get("revenue_significance", {})
                cost_pct = rev_sig.get("b_cost_from_a_pct", 0)
                if cost_pct > 0.15:
                    observations.append(Observation(
                        text=(f"供应商集中依赖: {other}（占成本 {cost_pct:.0%}）" if zh
                              else f"Concentrated supplier dependency: {other} ({cost_pct:.0%} of costs)"),
                        source_ids=[rel.get("relationship_id", "")],
                    ))
            elif "customer" in rel_type:
                customer_count += 1
                rev_sig = rel.get("revenue_significance", {})
                rev_pct = rev_sig.get("a_revenue_from_b_pct", 0)
                if rev_pct > 0.10:
                    observations.append(Observation(
                        text=(f"客户集中依赖: {other}（占收入 {rev_pct:.0%}）" if zh
                              else f"Concentrated customer dependency: {other} ({rev_pct:.0%} of revenue)"),
                        source_ids=[rel.get("relationship_id", "")],
                    ))

        # Geopolitical risk from sector pack
        sp = inp.sector_pack or {}
        special_risks = sp.get("special_risk_factors", {})
        for risk_key, risk_data in special_risks.items():
            if isinstance(risk_data, dict):
                observations.append(Observation(
                    text=(f"结构性风险——{risk_key}: {risk_data.get('description', '')}" if zh
                          else f"Structural risk — {risk_key}: {risk_data.get('description', '')}"),
                    source_ids=[f"sector_pack:{sp.get('sector_pack_id', '')}:risk:{risk_key}"],
                ))

        # Competitive dynamics disruption risks
        disruptions = sp.get("competitive_dynamics", {}).get("disruption_risks", [])
        for risk in disruptions[:3]:
            observations.append(Observation(
                text=(f"颠覆风险: {risk}" if zh else f"Disruption risk: {risk}"),
                source_ids=[f"sector_pack:{sp.get('sector_pack_id', '')}:disruption"],
            ))

        # Evidence
        for ep in inp.evidence_packets:
            if ep.get("assertion_type") in ("risk_factor", "regulatory_risk",
                                             "competitive_risk", "execution_risk",
                                             "geopolitical_risk"):
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

        # Leverage risk inference
        leverage_obs = [i for i, o in enumerate(observations)
                        if "net debt" in o.text.lower() or "leverage" in o.text.lower()
                        or "净负债" in o.text or "杠杆" in o.text]
        if leverage_obs:
            nd_ebitda = metrics.get("net_debt_to_ebitda", 0)
            if nd_ebitda > self.NET_DEBT_EBITDA_WARN:
                inferences.append(Inference(
                    text=(f"资产负债表风险偏高——净负债/EBITDA {nd_ebitda:.1f}x "
                          "制约战略灵活性，并放大压力情景下的下行风险"
                          if zh else
                          f"Balance sheet risk is elevated — Net Debt/EBITDA {nd_ebitda:.1f}x "
                          "limits strategic flexibility and increases downside in stress scenarios"),
                    based_on_observation_indices=leverage_obs,
                    confidence="high",
                ))
            elif metrics.get("net_debt", 0) < 0:
                inferences.append(Inference(
                    text=("净现金头寸提供资产负债表期权价值——降低压力情景下的下行风险"
                          if zh else
                          "Net cash position provides balance sheet optionality — "
                          "reduces downside risk in stress scenarios"),
                    based_on_observation_indices=leverage_obs,
                    confidence="high",
                ))

        # Concentration risk
        conc_obs = [i for i, o in enumerate(observations)
                    if "concentrated" in o.text.lower() or "集中依赖" in o.text]
        if conc_obs:
            inferences.append(Inference(
                text=("识别出集中度风险——单一关键关系的丧失可能对财务造成不成比例的冲击"
                      if zh else
                      "Concentration risk identified — loss of a single key relationship "
                      "could have outsized impact on financials"),
                based_on_observation_indices=conc_obs,
                confidence="high",
            ))

        # Structural/geopolitical risk
        struct_obs = [i for i, o in enumerate(observations)
                      if "structural risk" in o.text.lower() or "结构性风险" in o.text]
        if struct_obs:
            inferences.append(Inference(
                text=("结构性风险不可分散——应体现为永久性估值折价"
                      if zh else
                      "Structural risks are not diversifiable — "
                      "must be reflected as permanent valuation discount"),
                based_on_observation_indices=struct_obs,
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
            if "balance sheet risk is elevated" in t or "资产负债表风险偏高" in inf.text:
                counterargs.append(Counterargument(
                    text=("高杠杆可能是阶段性或战略性的（如并购后）——应结合管理层去杠杆计划评估"
                          if zh else
                          "High leverage may be temporary or strategic (e.g., post-acquisition) — "
                          "management deleveraging plan should be assessed"),
                    strength="moderate",
                    evidence_ids=[],
                ))
            elif "concentration risk" in t or "集中度风险" in inf.text:
                counterargs.append(Counterargument(
                    text=("集中度也可能反映合作关系的深度而非风险——长期合同与转换成本或可缓释"
                          if zh else
                          "Concentration may reflect strength of relationship rather than risk — "
                          "long-term contracts and switching costs may mitigate"),
                    strength="moderate",
                    evidence_ids=[],
                ))

        counterargs.append(Counterargument(
            text=("风险评估可能偏保守——尾部风险的发生概率本来就低"
                  if zh else
                  "Risk assessment may be overly conservative — "
                  "tail risks by definition have low probability"),
            strength="weak",
            evidence_ids=[],
        ))

        return counterargs

    def _identify_disconfirming_triggers(
        self, inferences: list[Inference], inp: AgentInput
    ) -> list[DisconfirmingTrigger]:
        zh = is_zh_input(inp)
        triggers = [
            DisconfirmingTrigger(
                text=("信用评级下调或展望转负" if zh
                      else "Credit rating downgrade or negative outlook"),
                monitorable=True,
                check_frequency="monthly",
            ),
            DisconfirmingTrigger(
                text=("债务契约违约或申请豁免" if zh
                      else "Debt covenant breach or waiver request"),
                monitorable=True,
                check_frequency="quarterly",
            ),
            DisconfirmingTrigger(
                text=("宣布重要客户/供应商流失" if zh
                      else "Key customer/supplier loss announced"),
                monitorable=True,
                check_frequency="monthly",
            ),
            DisconfirmingTrigger(
                text=("不利的监管行动或立案调查" if zh
                      else "Adverse regulatory action or investigation"),
                monitorable=True,
                check_frequency="monthly",
            ),
        ]
        return triggers

    def _cognitive_bias_self_check(self, inp: AgentInput) -> CognitiveBiasSelfCheck:
        zh = is_zh_input(inp)
        return CognitiveBiasSelfCheck(
            anchoring_risk="low",
            confirmation_bias_risk="low",
            recency_bias_risk="medium",
            narrative_fallacy_risk="low",
            mitigation_steps_taken=(
                [
                    "系统性覆盖资产负债表、集中度、结构性及行业风险",
                    "杠杆评估采用量化阈值",
                    "引用实体关系图谱评估供应链风险",
                ] if zh else [
                    "Systematically covered balance sheet, concentration, structural, and sector risks",
                    "Used quantitative thresholds for leverage assessment",
                    "Referenced entity relationship graph for supply chain risk",
                ]
            ),
        )

    def _report_uncertainties(self, inp: AgentInput) -> list[str]:
        zh = is_zh_input(inp)
        uncertainties = (
            [
                "尾部风险概率本质上难以估计",
                "地缘政治风险情景高度二元化，难以赋予概率",
            ] if zh else [
                "Tail risk probability is inherently difficult to estimate",
                "Geopolitical risk scenarios are binary and hard to assign probabilities",
            ]
        )
        if inp.entity_relationships:
            uncertainties.append(
                "供应链图谱可能不完整——存在未列示的依赖关系" if zh
                else "Supply chain mapping may be incomplete — unlisted dependencies possible"
            )
        return uncertainties
