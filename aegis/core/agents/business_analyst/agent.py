"""Business Analyst Agent — Section 19.3.

Responsibilities:
- Business engine quality assessment
- Segment economics
- Moat durability analysis
- Monetization path evaluation
- Reinvestment efficiency
- Competitive positioning map
- TAM/SAM/SOM assessment (evidence-based)
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


class BusinessAnalyst(AgentBase):
    """Specialist agent for business quality and competitive analysis."""

    AGENT_NAME = "business_analyst"
    AGENT_VERSION = "0.1.0"

    FOCUS_METRICS = frozenset({
        "gross_margin", "operating_margin", "roe", "roic",
        "fcf_simple", "capex_to_revenue", "ev_to_revenue",
    })

    def _extract_observations(self, inp: AgentInput) -> list[Observation]:
        observations: list[Observation] = []
        metrics = inp.metric_results

        # Profitability observations
        if "gross_margin" in metrics:
            gm = metrics["gross_margin"]
            observations.append(Observation(
                text=f"Gross margin is {gm:.2%}",
                source_ids=[f"metric:gross_margin:{inp.entity_id}"],
            ))

        if "roic" in metrics:
            roic = metrics["roic"]
            quality = "value-creating" if roic > 0.10 else "marginal"
            observations.append(Observation(
                text=f"ROIC is {roic:.2%}, indicating {quality} capital allocation",
                source_ids=[f"metric:roic:{inp.entity_id}"],
            ))

        if "roe" in metrics:
            observations.append(Observation(
                text=f"ROE is {metrics['roe']:.2%}",
                source_ids=[f"metric:roe:{inp.entity_id}"],
            ))

        # Reinvestment efficiency
        if "capex_to_revenue" in metrics:
            observations.append(Observation(
                text=f"Capex/Revenue is {metrics['capex_to_revenue']:.2%}",
                source_ids=[f"metric:capex_to_revenue:{inp.entity_id}"],
            ))

        # Sector-specific KPIs from sector pack
        if inp.sector_pack:
            kpis = inp.sector_pack.get("key_kpis", [])
            for kpi in kpis:
                # Handle both dict format {"metric": "...", "display": "..."}
                # and plain string format "DAU/MAU"
                if isinstance(kpi, dict):
                    metric_name = kpi.get("metric", "")
                    display = kpi.get("display", metric_name)
                else:
                    metric_name = str(kpi).lower().replace("/", "_").replace(" ", "_")
                    display = str(kpi)
                if metric_name in metrics:
                    observations.append(Observation(
                        text=f"Sector KPI '{display}' = {metrics[metric_name]}",
                        source_ids=[f"metric:{metric_name}:{inp.entity_id}"],
                    ))

        # Competitive positioning from relationships
        for rel in inp.entity_relationships:
            if "competition" in rel.get("relationship_type", ""):
                other = rel.get("entity_b") if rel.get("entity_a") == inp.entity_id else rel.get("entity_a", "")
                observations.append(Observation(
                    text=f"Competitive relationship with {other} ({rel.get('relationship_type', '')})",
                    source_ids=[rel.get("relationship_id", "")],
                ))

        # Evidence-based observations
        for ep in inp.evidence_packets:
            if ep.get("assertion_type") in ("competitive_position", "moat", "tam",
                                             "business_model", "segment_economics"):
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

        # Business quality inference from margin + ROIC
        margin_idx = None
        roic_idx = None
        for i, obs in enumerate(observations):
            if "gross margin" in obs.text.lower():
                margin_idx = i
            if "roic" in obs.text.lower():
                roic_idx = i

        if margin_idx is not None and roic_idx is not None:
            gm = metrics.get("gross_margin", 0)
            roic = metrics.get("roic", 0)
            if gm > 0.50 and roic > 0.15:
                inferences.append(Inference(
                    text="Business exhibits strong pricing power and capital efficiency — consistent with a durable moat",
                    based_on_observation_indices=[margin_idx, roic_idx],
                    confidence="medium",
                ))
            elif gm > 0.30 and roic > 0.08:
                inferences.append(Inference(
                    text="Business has moderate competitive positioning — moat may be narrowing or sector-typical",
                    based_on_observation_indices=[margin_idx, roic_idx],
                    confidence="medium",
                ))
            else:
                inferences.append(Inference(
                    text="Business shows weak economic moat indicators — commodity-like returns",
                    based_on_observation_indices=[margin_idx, roic_idx],
                    confidence="medium",
                ))

        # Reinvestment inference
        capex_idx = None
        for i, obs in enumerate(observations):
            if "capex/revenue" in obs.text.lower():
                capex_idx = i

        if capex_idx is not None:
            capex_ratio = metrics.get("capex_to_revenue", 0)
            if capex_ratio > 0.20:
                inferences.append(Inference(
                    text="High reinvestment rate — business is capital-intensive; FCF conversion may lag earnings",
                    based_on_observation_indices=[capex_idx],
                    confidence="medium",
                ))

        # Driver tree decomposition from sector pack
        if inp.sector_pack:
            decomp = inp.sector_pack.get("revenue_drivers", {}).get("decomposition", {})
            if decomp:
                formula = decomp.get("formula", "")
                tree_nodes = decomp.get("tree", [])
                if tree_nodes:
                    # Get actual driver values from macro context if available
                    driver_values = (inp.macro_context or {}).get("driver_values", {})

                    driver_lines = [f"Revenue driver decomposition: {formula}"]
                    for node in tree_nodes:
                        name = node.get("name", "")
                        unit = node.get("unit", "")
                        note = node.get("note", "")
                        growth_driver = node.get("growth_driver", "")
                        line = f"  - {name}"
                        if unit:
                            line += f" ({unit})"
                        # Fill in actual value if available
                        actual_value = driver_values.get(growth_driver)
                        if actual_value:
                            line += f" = {actual_value}"
                        if note:
                            line += f" [{note}]"
                        driver_lines.append(line)
                    # Add capex ROI decomposition if present
                    capex_roi = decomp.get("capex_roi", {})
                    if capex_roi:
                        driver_lines.append(f"  AI CapEx ROI: {capex_roi.get('formula', '')}")
                        driver_lines.append(f"  Key question: {capex_roi.get('key_question', '')}")

                    # Use first observation as anchor (always exists)
                    anchor_idx = 0 if observations else 0
                    inferences.append(Inference(
                        text="\n".join(driver_lines),
                        based_on_observation_indices=[anchor_idx],
                        confidence="medium",
                    ))

        return inferences

    def _generate_counterarguments(
        self, inferences: list[Inference], inp: AgentInput
    ) -> list[Counterargument]:
        counterargs: list[Counterargument] = []

        for inf in inferences:
            if "durable moat" in inf.text.lower():
                counterargs.append(Counterargument(
                    text="High margins may reflect temporary market position rather than structural moat — "
                         "new entrants or regulation could compress margins",
                    strength="moderate",
                    evidence_ids=[],
                ))
            elif "weak economic moat" in inf.text.lower():
                counterargs.append(Counterargument(
                    text="Low margins may reflect deliberate growth investment rather than poor business quality — "
                         "unit economics at scale could differ materially",
                    strength="moderate",
                    evidence_ids=[],
                ))

        # Default counterargument
        if not counterargs:
            counterargs.append(Counterargument(
                text="TAM estimates are inherently uncertain — actual addressable market may differ from projections",
                strength="moderate",
                evidence_ids=[],
            ))

        return counterargs

    def _identify_disconfirming_triggers(
        self, inferences: list[Inference], inp: AgentInput
    ) -> list[DisconfirmingTrigger]:
        triggers = [
            DisconfirmingTrigger(
                text="Gross margin decline of >300bps for two consecutive quarters",
                monitorable=True,
                check_frequency="quarterly",
            ),
            DisconfirmingTrigger(
                text="Major customer loss or contract non-renewal",
                monitorable=True,
                check_frequency="quarterly",
            ),
            DisconfirmingTrigger(
                text="New well-funded competitor entering core market",
                monitorable=True,
                check_frequency="quarterly",
            ),
        ]
        return triggers

    def _cognitive_bias_self_check(self, inp: AgentInput) -> CognitiveBiasSelfCheck:
        return CognitiveBiasSelfCheck(
            anchoring_risk="medium",
            confirmation_bias_risk="medium",
            recency_bias_risk="low",
            narrative_fallacy_risk="high",
            mitigation_steps_taken=[
                "Grounded moat assessment in quantitative metrics (GM, ROIC) not narrative",
                "Included counterarguments for both strong and weak moat conclusions",
                "Used competitive relationship data rather than subjective assessment",
            ],
        )

    def _report_uncertainties(self, inp: AgentInput) -> list[str]:
        return [
            "Moat durability assessment is inherently forward-looking and uncertain",
            "Segment-level economics may not be disclosed with sufficient granularity",
        ]
