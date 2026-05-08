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

from aegis.core.agents.base import AgentBase, AgentInput
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

        # Balance sheet health
        if "net_debt_to_ebitda" in metrics:
            val = metrics["net_debt_to_ebitda"]
            level = "elevated" if val > self.NET_DEBT_EBITDA_WARN else "manageable"
            observations.append(Observation(
                text=f"Net Debt/EBITDA: {val:.1f}x — leverage is {level}",
                source_ids=[f"metric:net_debt_to_ebitda:{inp.entity_id}"],
            ))

        if "current_ratio" in metrics:
            val = metrics["current_ratio"]
            observations.append(Observation(
                text=f"Current ratio: {val:.2f}x",
                source_ids=[f"metric:current_ratio:{inp.entity_id}"],
            ))

        if "net_debt" in metrics:
            observations.append(Observation(
                text=f"Net debt: {metrics['net_debt']:,.0f}",
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
                        text=f"Concentrated supplier dependency: {other} ({cost_pct:.0%} of costs)",
                        source_ids=[rel.get("relationship_id", "")],
                    ))
            elif "customer" in rel_type:
                customer_count += 1
                rev_sig = rel.get("revenue_significance", {})
                rev_pct = rev_sig.get("a_revenue_from_b_pct", 0)
                if rev_pct > 0.10:
                    observations.append(Observation(
                        text=f"Concentrated customer dependency: {other} ({rev_pct:.0%} of revenue)",
                        source_ids=[rel.get("relationship_id", "")],
                    ))

        # Geopolitical risk from sector pack
        sp = inp.sector_pack or {}
        special_risks = sp.get("special_risk_factors", {})
        for risk_key, risk_data in special_risks.items():
            if isinstance(risk_data, dict):
                observations.append(Observation(
                    text=f"Structural risk — {risk_key}: {risk_data.get('description', '')}",
                    source_ids=[f"sector_pack:{sp.get('sector_pack_id', '')}:risk:{risk_key}"],
                ))

        # Competitive dynamics disruption risks
        disruptions = sp.get("competitive_dynamics", {}).get("disruption_risks", [])
        for risk in disruptions[:3]:
            observations.append(Observation(
                text=f"Disruption risk: {risk}",
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

        # Leverage risk inference
        leverage_obs = [i for i, o in enumerate(observations) if "net debt" in o.text.lower() or "leverage" in o.text.lower()]
        if leverage_obs:
            nd_ebitda = metrics.get("net_debt_to_ebitda", 0)
            if nd_ebitda > self.NET_DEBT_EBITDA_WARN:
                inferences.append(Inference(
                    text=f"Balance sheet risk is elevated — Net Debt/EBITDA {nd_ebitda:.1f}x "
                         "limits strategic flexibility and increases downside in stress scenarios",
                    based_on_observation_indices=leverage_obs,
                    confidence="high",
                ))
            elif metrics.get("net_debt", 0) < 0:
                inferences.append(Inference(
                    text="Net cash position provides balance sheet optionality — "
                         "reduces downside risk in stress scenarios",
                    based_on_observation_indices=leverage_obs,
                    confidence="high",
                ))

        # Concentration risk
        conc_obs = [i for i, o in enumerate(observations) if "concentrated" in o.text.lower()]
        if conc_obs:
            inferences.append(Inference(
                text="Concentration risk identified — loss of a single key relationship "
                     "could have outsized impact on financials",
                based_on_observation_indices=conc_obs,
                confidence="high",
            ))

        # Structural/geopolitical risk
        struct_obs = [i for i, o in enumerate(observations) if "structural risk" in o.text.lower()]
        if struct_obs:
            inferences.append(Inference(
                text="Structural risks are not diversifiable — "
                     "must be reflected as permanent valuation discount",
                based_on_observation_indices=struct_obs,
                confidence="high",
            ))

        return inferences

    def _generate_counterarguments(
        self, inferences: list[Inference], inp: AgentInput
    ) -> list[Counterargument]:
        counterargs: list[Counterargument] = []

        for inf in inferences:
            if "balance sheet risk is elevated" in inf.text.lower():
                counterargs.append(Counterargument(
                    text="High leverage may be temporary or strategic (e.g., post-acquisition) — "
                         "management deleveraging plan should be assessed",
                    strength="moderate",
                    evidence_ids=[],
                ))
            elif "concentration risk" in inf.text.lower():
                counterargs.append(Counterargument(
                    text="Concentration may reflect strength of relationship rather than risk — "
                         "long-term contracts and switching costs may mitigate",
                    strength="moderate",
                    evidence_ids=[],
                ))

        counterargs.append(Counterargument(
            text="Risk assessment may be overly conservative — "
                 "tail risks by definition have low probability",
            strength="weak",
            evidence_ids=[],
        ))

        return counterargs

    def _identify_disconfirming_triggers(
        self, inferences: list[Inference], inp: AgentInput
    ) -> list[DisconfirmingTrigger]:
        triggers = [
            DisconfirmingTrigger(
                text="Credit rating downgrade or negative outlook",
                monitorable=True,
                check_frequency="monthly",
            ),
            DisconfirmingTrigger(
                text="Debt covenant breach or waiver request",
                monitorable=True,
                check_frequency="quarterly",
            ),
            DisconfirmingTrigger(
                text="Key customer/supplier loss announced",
                monitorable=True,
                check_frequency="monthly",
            ),
            DisconfirmingTrigger(
                text="Adverse regulatory action or investigation",
                monitorable=True,
                check_frequency="monthly",
            ),
        ]
        return triggers

    def _cognitive_bias_self_check(self, inp: AgentInput) -> CognitiveBiasSelfCheck:
        return CognitiveBiasSelfCheck(
            anchoring_risk="low",
            confirmation_bias_risk="low",
            recency_bias_risk="medium",
            narrative_fallacy_risk="low",
            mitigation_steps_taken=[
                "Systematically covered balance sheet, concentration, structural, and sector risks",
                "Used quantitative thresholds for leverage assessment",
                "Referenced entity relationship graph for supply chain risk",
            ],
        )

    def _report_uncertainties(self, inp: AgentInput) -> list[str]:
        uncertainties = [
            "Tail risk probability is inherently difficult to estimate",
            "Geopolitical risk scenarios are binary and hard to assign probabilities",
        ]
        if inp.entity_relationships:
            uncertainties.append("Supply chain mapping may be incomplete — unlisted dependencies possible")
        return uncertainties
