"""Management Analyst Agent — Section 19.5.

Responsibilities:
- Management track record
- Capital allocation history
- Insider transaction analysis
- Compensation alignment
- Communication quality
- Board composition
- Succession risk
- Related-party transaction risk assessment (especially China market)

Prohibitions:
- No equating "famous CEO" with "excellent management"
- No ignoring dual-class / VIE governance impact
- No replacing quantitative track record with qualitative impressions
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


class ManagementAnalyst(AgentBase):
    """Specialist agent for management quality and governance analysis."""

    AGENT_NAME = "management_analyst"
    AGENT_VERSION = "0.1.0"

    # Capital allocation scoring thresholds
    ROIC_GOOD = 0.12
    ROIC_POOR = 0.06

    def _extract_observations(self, inp: AgentInput) -> list[Observation]:
        observations: list[Observation] = []
        metrics = inp.metric_results

        # Capital allocation track record (quantitative)
        if "roic" in metrics:
            observations.append(Observation(
                text=f"ROIC is {metrics['roic']:.2%} — reflects capital allocation effectiveness",
                source_ids=[f"metric:roic:{inp.entity_id}"],
            ))

        if "roe" in metrics:
            observations.append(Observation(
                text=f"ROE is {metrics['roe']:.2%}",
                source_ids=[f"metric:roe:{inp.entity_id}"],
            ))

        # SBC as compensation alignment signal
        if "sbc_to_revenue" in metrics:
            val = metrics["sbc_to_revenue"]
            observations.append(Observation(
                text=f"SBC/Revenue is {val:.2%} — proxy for management compensation alignment",
                source_ids=[f"metric:sbc_to_revenue:{inp.entity_id}"],
            ))

        # Related-party transactions from relationships
        for rel in inp.entity_relationships:
            rel_type = rel.get("relationship_type", "")
            if "related_party" in rel_type or "ownership" in rel_type:
                other = rel.get("entity_b") if rel.get("entity_a") == inp.entity_id else rel.get("entity_a", "")
                rev_sig = rel.get("revenue_significance", {})
                pct = rev_sig.get("a_revenue_from_b_pct", 0)
                note = f", revenue significance: {pct:.1%}" if pct else ""
                observations.append(Observation(
                    text=f"Related-party relationship with {other} ({rel_type}){note}",
                    source_ids=[rel.get("relationship_id", "")],
                ))

        # Governance-related evidence
        for ep in inp.evidence_packets:
            if ep.get("assertion_type") in ("management_quality", "insider_transaction",
                                             "board_composition", "governance",
                                             "related_party", "compensation"):
                observations.append(Observation(
                    text=ep.get("assertion_text", ""),
                    source_ids=[ep.get("evidence_id", "")],
                ))

        # Insider trading activity (from SEC Form 4 via agent_macro)
        insider = (inp.macro_context or {}).get("insider_trading")
        if insider:
            buy_ct = insider.get("buy_count", 0)
            sell_ct = insider.get("sell_count", 0)
            buy_val = insider.get("total_buy_value", 0)
            sell_val = insider.get("total_sell_value", 0)
            net_val = insider.get("net_value", 0)
            sentiment = insider.get("sentiment", "neutral")

            direction = "net buying" if net_val > 0 else "net selling"
            observations.append(Observation(
                text=(
                    f"Over the past 12 months, insiders made {buy_ct} purchases "
                    f"(${buy_val:,.0f}) and {sell_ct} sales (${sell_val:,.0f}) — "
                    f"{direction} of ${abs(net_val):,.0f} (sentiment: {sentiment})"
                ),
                source_ids=[f"form4:{inp.entity_id}"],
            ))

            if insider.get("cluster_detected"):
                observations.append(Observation(
                    text="Cluster insider activity detected — 3+ insiders transacted "
                         "in the same direction within a 30-day window",
                    source_ids=[f"form4:{inp.entity_id}:cluster"],
                ))

            # Notable transactions (C-suite buys are especially informative)
            for txn in insider.get("notable_transactions", [])[:3]:
                txn_type = "purchased" if txn["type"] == "P" else "sold"
                observations.append(Observation(
                    text=(
                        f"{txn['name']} ({txn['title']}) {txn_type} "
                        f"${txn['value']:,.0f} worth of shares on {txn['date']}"
                    ),
                    source_ids=[f"form4:{inp.entity_id}:notable"],
                ))

        # Governance structure from sector pack
        sp = inp.sector_pack or {}
        special_risks = sp.get("special_risk_factors", {})
        if "vie_structure" in special_risks:
            observations.append(Observation(
                text="Entity operates under VIE structure — "
                     "foreign investors hold contractual rights, not equity ownership",
                source_ids=[f"sector_pack:{sp.get('sector_pack_id', '')}:risk:vie_structure"],
            ))
        if "related_party" in special_risks:
            observations.append(Observation(
                text="Sector has elevated related-party transaction risk — "
                     "founder-controlled conglomerates common",
                source_ids=[f"sector_pack:{sp.get('sector_pack_id', '')}:risk:related_party"],
            ))

        return observations

    def _derive_inferences(
        self, observations: list[Observation], inp: AgentInput
    ) -> list[Inference]:
        inferences: list[Inference] = []
        metrics = inp.metric_results

        # Capital allocation quality inference
        roic_obs_idx = None
        for i, obs in enumerate(observations):
            if "roic" in obs.text.lower() and "capital allocation" in obs.text.lower():
                roic_obs_idx = i

        if roic_obs_idx is not None:
            roic = metrics.get("roic", 0)
            if roic >= self.ROIC_GOOD:
                inferences.append(Inference(
                    text="Management demonstrates strong capital allocation — ROIC exceeds cost of capital",
                    based_on_observation_indices=[roic_obs_idx],
                    confidence="medium",
                ))
            elif roic < self.ROIC_POOR:
                inferences.append(Inference(
                    text="Management's capital allocation track record is poor — ROIC suggests value destruction",
                    based_on_observation_indices=[roic_obs_idx],
                    confidence="medium",
                ))

        # Related-party risk inference
        rpt_obs_indices = [
            i for i, obs in enumerate(observations)
            if "related-party" in obs.text.lower() or "related party" in obs.text.lower()
        ]
        if rpt_obs_indices:
            # Check for material related-party exposure
            has_material_rpt = any(
                "revenue significance" in observations[i].text.lower()
                for i in rpt_obs_indices
            )
            if has_material_rpt:
                inferences.append(Inference(
                    text="Material related-party transactions detected — governance discount warranted",
                    based_on_observation_indices=rpt_obs_indices,
                    confidence="high",
                ))

        # VIE governance inference
        vie_obs_indices = [i for i, obs in enumerate(observations) if "vie" in obs.text.lower()]
        if vie_obs_indices:
            inferences.append(Inference(
                text="VIE structure creates governance risk — "
                     "minority shareholders have limited legal recourse",
                based_on_observation_indices=vie_obs_indices,
                confidence="high",
            ))

        # Insider trading inferences
        insider_obs_indices = [
            i for i, obs in enumerate(observations)
            if "form4:" in ",".join(obs.source_ids)
        ]
        if insider_obs_indices:
            insider = (inp.macro_context or {}).get("insider_trading", {})
            sentiment = insider.get("sentiment", "neutral")
            cluster = insider.get("cluster_detected", False)

            if sentiment == "bullish":
                conf = "high" if cluster else "medium"
                inferences.append(Inference(
                    text="Insider buying signals management confidence — "
                         "insiders are deploying personal capital at current prices",
                    based_on_observation_indices=insider_obs_indices,
                    confidence=conf,
                ))
            elif sentiment == "bearish":
                inferences.append(Inference(
                    text="Significant insider selling may indicate management concern — "
                         "though sales can also reflect diversification or tax planning",
                    based_on_observation_indices=insider_obs_indices,
                    confidence="medium",
                ))
            elif sentiment == "mixed":
                inferences.append(Inference(
                    text="Mixed insider activity — some buying and selling suggest "
                         "divergent views among management team",
                    based_on_observation_indices=insider_obs_indices,
                    confidence="low",
                ))

            if cluster:
                cluster_obs = [
                    i for i, obs in enumerate(observations)
                    if "cluster" in obs.text.lower()
                ]
                if cluster_obs:
                    direction = "buying" if sentiment == "bullish" else "selling"
                    inferences.append(Inference(
                        text=f"Cluster insider {direction} is a stronger signal than "
                             f"individual transactions — multiple insiders independently "
                             f"reached the same conclusion",
                        based_on_observation_indices=cluster_obs,
                        confidence="high",
                    ))

        return inferences

    def _generate_counterarguments(
        self, inferences: list[Inference], inp: AgentInput
    ) -> list[Counterargument]:
        counterargs: list[Counterargument] = []

        for inf in inferences:
            if "strong capital allocation" in inf.text.lower():
                counterargs.append(Counterargument(
                    text="Historical ROIC reflects past decisions — current management's strategy may differ. "
                         "CEO tenure and recent major decisions should be weighted more heavily.",
                    strength="moderate",
                    evidence_ids=[],
                ))
            elif "value destruction" in inf.text.lower():
                counterargs.append(Counterargument(
                    text="Low ROIC may reflect deliberate growth-phase reinvestment "
                         "that will generate returns over a longer horizon",
                    strength="moderate",
                    evidence_ids=[],
                ))
            elif "governance discount" in inf.text.lower():
                counterargs.append(Counterargument(
                    text="Related-party transactions may be at arm's length and properly disclosed — "
                         "auditor and board oversight may mitigate risk",
                    strength="weak",
                    evidence_ids=[],
                ))

        # Always include governance structure caveat
        counterargs.append(Counterargument(
            text="Quantitative track record alone cannot capture management quality — "
                 "communication transparency and strategic consistency also matter",
            strength="moderate",
            evidence_ids=[],
        ))

        return counterargs

    def _identify_disconfirming_triggers(
        self, inferences: list[Inference], inp: AgentInput
    ) -> list[DisconfirmingTrigger]:
        triggers = [
            DisconfirmingTrigger(
                text="CEO or CFO departure / succession event",
                monitorable=True,
                check_frequency="monthly",
            ),
            DisconfirmingTrigger(
                text="Material insider selling exceeding normal vesting schedule",
                monitorable=True,
                check_frequency="monthly",
            ),
            DisconfirmingTrigger(
                text="New related-party transaction disclosed with material revenue impact",
                monitorable=True,
                check_frequency="quarterly",
            ),
            DisconfirmingTrigger(
                text="Regulatory action targeting corporate governance or related parties",
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
                "Used ROIC as quantitative track record rather than reputation-based assessment",
                "Explicitly flagged VIE and related-party governance risks",
                "Did not equate management reputation with management quality",
            ],
        )

    def _report_uncertainties(self, inp: AgentInput) -> list[str]:
        uncertainties = [
            "Insider transaction data may be delayed or incomplete",
            "Board independence assessment requires qualitative judgment beyond available data",
        ]
        if any("vie" in obs for obs in [r.get("relationship_type", "") for r in inp.entity_relationships]):
            uncertainties.append("VIE enforceability under Chinese law remains legally untested at scale")
        return uncertainties
