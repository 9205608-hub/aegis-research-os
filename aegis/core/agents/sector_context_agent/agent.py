"""Sector Context Agent — Section 19.4.

Responsibilities:
- Inject sector-specific analysis framework from Sector Packs
- Sector KPIs and benchmarks
- Industry cycle positioning
- Industry competitive landscape
- Sector-specific risks
- Sector-specific accounting considerations
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


class SectorContextAgent(AgentBase):
    """Specialist agent for sector-specific context injection."""

    AGENT_NAME = "sector_context_agent"
    AGENT_VERSION = "0.1.0"

    def _extract_observations(self, inp: AgentInput) -> list[Observation]:
        observations: list[Observation] = []
        sp = inp.sector_pack
        zh = is_zh_input(inp)

        if not sp:
            observations.append(Observation(
                text=("缺少行业分析包——分析将不含行业基准对照" if zh
                      else "No sector pack available — analysis proceeds without sector benchmarks"),
                source_ids=["system:no_sector_pack"],
            ))
            return observations

        sector_id = sp.get("sector_pack_id", "unknown")
        sector_name = sp.get("sector_name", "Unknown Sector")

        # Sector identification
        observations.append(Observation(
            text=(f"公司所属行业: {sector_name}（分析包: {sector_id}）" if zh
                  else f"Entity classified under sector: {sector_name} (pack: {sector_id})"),
            source_ids=[f"sector_pack:{sector_id}"],
        ))

        # Key KPIs from sector pack
        for kpi in sp.get("key_kpis", []):
            if isinstance(kpi, str):
                # Plain string KPI (e.g., from YAML: "DAU/MAU")
                metric_name = kpi.lower().replace("/", "_").replace(" ", "_")
                importance = "medium"
                display = kpi
            else:
                metric_name = kpi.get("metric", "")
                importance = kpi.get("importance", "medium")
                display = kpi.get("display", metric_name)

            if metric_name in inp.metric_results:
                val = inp.metric_results[metric_name]
                healthy = kpi.get("healthy_range") if isinstance(kpi, dict) else None
                note = ""
                if healthy and isinstance(val, (int, float)):
                    if val < healthy[0]:
                        note = (f"（低于行业健康区间 {healthy}）" if zh
                                else f" (below sector healthy range {healthy})")
                    elif val > healthy[1]:
                        note = (f"（高于行业健康区间 {healthy}）" if zh
                                else f" (above sector healthy range {healthy})")
                    else:
                        note = (f"（处于行业健康区间 {healthy}）" if zh
                                else f" (within sector healthy range {healthy})")
                observations.append(Observation(
                    text=(f"行业 KPI「{display}」= {val}{note}［重要性: {importance}］" if zh
                          else f"Sector KPI '{display}' = {val}{note} [importance: {importance}]"),
                    source_ids=[f"sector_pack:{sector_id}:kpi:{metric_name}"],
                ))

        # Cycle characteristics
        cycle = sp.get("cycle_characteristics", {})
        if cycle:
            cyclicality = cycle.get("cyclicality", "unknown")
            driver = cycle.get("primary_driver", "unknown")
            observations.append(Observation(
                text=(f"行业周期属性: {cyclicality}，主要驱动: {driver}" if zh
                      else f"Sector cyclicality: {cyclicality}, primary driver: {driver}"),
                source_ids=[f"sector_pack:{sector_id}:cycle"],
            ))

        # Sector-specific accounting considerations
        acct_notes = sp.get("accounting_considerations", [])
        if acct_notes:
            observations.append(Observation(
                text=(f"行业会计要点: {'; '.join(acct_notes[:3])}" if zh
                      else f"Sector accounting considerations: {'; '.join(acct_notes[:3])}"),
                source_ids=[f"sector_pack:{sector_id}:accounting"],
            ))

        # Special risk factors (e.g., VIE for China Internet)
        special_risks = sp.get("special_risk_factors", {})
        for risk_key, risk_data in special_risks.items():
            if isinstance(risk_data, dict):
                desc = risk_data.get("description", "")
                observations.append(Observation(
                    text=(f"行业特有风险——{risk_key}: {desc}" if zh
                          else f"Sector special risk — {risk_key}: {desc}"),
                    source_ids=[f"sector_pack:{sector_id}:risk:{risk_key}"],
                ))

        return observations

    def _derive_inferences(
        self, observations: list[Observation], inp: AgentInput
    ) -> list[Inference]:
        inferences: list[Inference] = []
        sp = inp.sector_pack
        zh = is_zh_input(inp)

        if not sp:
            return inferences

        # KPI health inference
        kpi_obs_indices = []
        below_range_count = 0
        above_range_count = 0
        for i, obs in enumerate(observations):
            t = obs.text.lower()
            if "sector kpi" in t or "行业 kpi" in t:
                kpi_obs_indices.append(i)
                if "below sector healthy range" in t or "低于行业健康区间" in obs.text:
                    below_range_count += 1
                elif "above sector healthy range" in t or "高于行业健康区间" in obs.text:
                    above_range_count += 1

        if kpi_obs_indices:
            total = len(kpi_obs_indices)
            if below_range_count > total / 2:
                inferences.append(Inference(
                    text=("多数行业 KPI 低于健康区间——公司表现落后于行业基准" if zh
                          else "Majority of sector KPIs are below healthy ranges — entity underperforms sector benchmarks"),
                    based_on_observation_indices=kpi_obs_indices,
                    confidence="medium",
                ))
            elif above_range_count > total / 2:
                inferences.append(Inference(
                    text=("多数行业 KPI 高于健康区间——公司表现领先于行业基准" if zh
                          else "Majority of sector KPIs exceed healthy ranges — entity outperforms sector benchmarks"),
                    based_on_observation_indices=kpi_obs_indices,
                    confidence="medium",
                ))

        # Cycle positioning inference
        cycle_obs_idx = None
        for i, obs in enumerate(observations):
            if "cyclicality" in obs.text.lower() or "周期属性" in obs.text:
                cycle_obs_idx = i

        if cycle_obs_idx is not None:
            cyclicality = sp.get("cycle_characteristics", {}).get("cyclicality", "")
            if cyclicality in ("high", "very_high"):
                inferences.append(Inference(
                    text=("行业周期性强——当期指标可能无法代表中周期盈利能力" if zh
                          else "Sector is highly cyclical — current-period metrics may not represent mid-cycle earnings power"),
                    based_on_observation_indices=[cycle_obs_idx],
                    confidence="high",
                ))

        # Special risk inference
        risk_obs_indices = [i for i, obs in enumerate(observations)
                            if "special risk" in obs.text.lower() or "特有风险" in obs.text]
        if risk_obs_indices:
            inferences.append(Inference(
                text=("识别出行业特有的结构性风险——须在估值折价中予以体现" if zh
                      else "Sector-specific structural risks identified — these must be factored into valuation discount"),
                based_on_observation_indices=risk_obs_indices,
                confidence="high",
            ))

        return inferences

    def _generate_counterarguments(
        self, inferences: list[Inference], inp: AgentInput
    ) -> list[Counterargument]:
        counterargs: list[Counterargument] = []
        sp = inp.sector_pack or {}
        zh = is_zh_input(inp)

        for inf in inferences:
            t = inf.text.lower()
            if "underperforms sector benchmarks" in t or "落后于行业基准" in inf.text:
                counterargs.append(Counterargument(
                    text=("KPI 低于基准可能源于业务结构差异而非执行力不足——需要直接可比公司对照"
                          if zh else
                          "Below-benchmark KPIs may reflect different business model mix "
                          "rather than inferior execution — direct peer comparison needed"),
                    strength="moderate",
                    evidence_ids=[],
                ))
            elif "highly cyclical" in t or "行业周期性强" in inf.text:
                counterargs.append(Counterargument(
                    text=("结构性变化（如半导体行业的 AI 需求）可能使公司脱离传统周期规律"
                          if zh else
                          "Structural shifts (e.g., AI demand in semiconductors) may decouple "
                          "entity from traditional cycle patterns"),
                    strength="moderate",
                    evidence_ids=[],
                ))

        # Sector pack applicability counterargument
        counterargs.append(Counterargument(
            text=("行业分析包的基准是通用性的——公司特有因素可能足以支撑对行业常态的偏离"
                  if zh else
                  "Sector pack benchmarks are general — company-specific factors "
                  "may justify deviation from sector norms"),
            strength="moderate",
            evidence_ids=[],
        ))

        return counterargs

    def _identify_disconfirming_triggers(
        self, inferences: list[Inference], inp: AgentInput
    ) -> list[DisconfirmingTrigger]:
        triggers: list[DisconfirmingTrigger] = []
        sp = inp.sector_pack or {}
        zh = is_zh_input(inp)

        # Use leading indicators from sector pack
        leading = sp.get("cycle_characteristics", {}).get("leading_indicators", [])
        for indicator in leading[:3]:
            triggers.append(DisconfirmingTrigger(
                text=(f"跟踪领先指标: {indicator}" if zh
                      else f"Monitor leading indicator: {indicator}"),
                monitorable=True,
                check_frequency="monthly",
            ))

        # Disruption risks
        disruptions = sp.get("competitive_dynamics", {}).get("disruption_risks", [])
        for risk in disruptions[:2]:
            triggers.append(DisconfirmingTrigger(
                text=(f"颠覆风险兑现: {risk}" if zh
                      else f"Disruption risk materializes: {risk}"),
                monitorable=True,
                check_frequency="quarterly",
            ))

        if not triggers:
            triggers.append(DisconfirmingTrigger(
                text=("行业基本面相对当前判断发生实质性变化" if zh
                      else "Sector fundamentals shift materially from current assessment"),
                monitorable=True,
                check_frequency="quarterly",
            ))

        return triggers

    def _cognitive_bias_self_check(self, inp: AgentInput) -> CognitiveBiasSelfCheck:
        zh = is_zh_input(inp)
        return CognitiveBiasSelfCheck(
            anchoring_risk="low",
            confirmation_bias_risk="low",
            recency_bias_risk="medium",
            narrative_fallacy_risk="medium",
            mitigation_steps_taken=(
                [
                    "采用结构化行业分析包数据而非主观行业判断",
                    "以量化区间校验 KPI 相对行业基准的位置",
                    "已声明行业分析包可能无法覆盖公司特有偏离",
                ] if zh else [
                    "Used structured sector pack data rather than subjective sector assessment",
                    "Applied quantitative KPI range checks against sector benchmarks",
                    "Acknowledged sector pack may not capture company-specific deviations",
                ]
            ),
        )

    def _report_uncertainties(self, inp: AgentInput) -> list[str]:
        zh = is_zh_input(inp)
        uncertainties = [
            "行业基准属滞后指标——当前周期位置为估计值" if zh
            else "Sector benchmarks are lagging indicators — current cycle position is estimated"
        ]
        sp = inp.sector_pack or {}
        if sp.get("special_risk_factors"):
            uncertainties.append(
                "结构性风险（如 VIE、地缘政治）难以精确量化" if zh
                else "Structural risks (e.g., VIE, geopolitical) are difficult to quantify precisely"
            )
        return uncertainties
