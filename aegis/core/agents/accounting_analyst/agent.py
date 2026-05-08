"""Accounting Analyst Agent — Section 19.2.

Responsibilities:
- Earnings quality assessment
- Owner earnings bridge
- Dilution mechanics
- Tax normalization
- Accounting red flags
- Working capital analysis
- Accrual quality
- Off-balance-sheet exposure
- Cross-standard adjustment recommendation

Prohibitions:
- No SBC + dilution double penalty
- No OCF/NI = earnings quality shortcut
- No non-GAAP replacing GAAP without explicit bridge
- No ignoring cross-standard differences
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


class AccountingAnalyst(AgentBase):
    """Specialist agent for accounting quality and earnings analysis."""

    AGENT_NAME = "accounting_analyst"
    AGENT_VERSION = "0.1.0"

    # Metrics this agent specifically inspects
    FOCUS_METRICS = frozenset({
        "gross_margin", "operating_margin", "net_margin", "ebitda_margin",
        "sbc_to_revenue", "dilution_rate", "accruals_ratio",
        "cfo_to_net_income", "nwc", "fcf_simple", "capex_to_revenue",
    })

    # Red-flag thresholds
    ACCRUAL_RATIO_WARN = 0.10
    SBC_TO_REVENUE_WARN = 0.15
    CFO_NI_FLOOR = 0.50  # CFO/NI below this is a red flag

    def _extract_observations(self, inp: AgentInput) -> list[Observation]:
        observations: list[Observation] = []
        metrics = inp.metric_results

        # Earnings quality observations
        if "accruals_ratio" in metrics:
            val = metrics["accruals_ratio"]
            quality = "poor" if abs(val) > self.ACCRUAL_RATIO_WARN else "acceptable"
            observations.append(Observation(
                text=f"Accruals ratio is {val:.4f}, indicating {quality} earnings quality",
                source_ids=[f"metric:accruals_ratio:{inp.entity_id}"],
            ))

        if "cfo_to_net_income" in metrics:
            val = metrics["cfo_to_net_income"]
            flag = "below cash conversion floor" if val < self.CFO_NI_FLOOR else "healthy"
            observations.append(Observation(
                text=f"CFO/Net Income ratio is {val:.2f} — {flag}",
                source_ids=[f"metric:cfo_to_net_income:{inp.entity_id}"],
            ))

        # Dilution observations
        if "sbc_to_revenue" in metrics:
            val = metrics["sbc_to_revenue"]
            level = "elevated" if val > self.SBC_TO_REVENUE_WARN else "moderate"
            observations.append(Observation(
                text=f"SBC/Revenue is {val:.2%}, dilution level is {level}",
                source_ids=[f"metric:sbc_to_revenue:{inp.entity_id}"],
            ))

        if "dilution_rate" in metrics:
            val = metrics["dilution_rate"]
            observations.append(Observation(
                text=f"Annual dilution rate is {val:.2%}",
                source_ids=[f"metric:dilution_rate:{inp.entity_id}"],
            ))

        # Margin observations
        for margin_key in ("gross_margin", "operating_margin", "net_margin"):
            if margin_key in metrics:
                observations.append(Observation(
                    text=f"{margin_key} is {metrics[margin_key]:.2%}",
                    source_ids=[f"metric:{margin_key}:{inp.entity_id}"],
                ))

        # Working capital
        if "nwc" in metrics:
            observations.append(Observation(
                text=f"Net working capital is {metrics['nwc']:,.0f}",
                source_ids=[f"metric:nwc:{inp.entity_id}"],
            ))

        # Evidence-based observations
        for ep in inp.evidence_packets:
            if ep.get("assertion_type") in ("accounting_quality", "earnings_quality",
                                             "related_party", "off_balance_sheet"):
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

        # Earnings quality inference
        accrual_obs_idx = None
        cfo_obs_idx = None
        for i, obs in enumerate(observations):
            if "accruals_ratio" in obs.text.lower():
                accrual_obs_idx = i
            if "cfo/net income" in obs.text.lower():
                cfo_obs_idx = i

        quality_indices = [i for i in (accrual_obs_idx, cfo_obs_idx) if i is not None]
        if quality_indices:
            high_accrual = abs(metrics.get("accruals_ratio", 0)) > self.ACCRUAL_RATIO_WARN
            low_cfo = metrics.get("cfo_to_net_income", 1.0) < self.CFO_NI_FLOOR
            if high_accrual or low_cfo:
                inferences.append(Inference(
                    text="Earnings quality is below par — reported earnings may overstate economic reality",
                    based_on_observation_indices=quality_indices,
                    confidence="medium" if (high_accrual != low_cfo) else "high",
                ))
            else:
                inferences.append(Inference(
                    text="Earnings quality appears sound — accruals and cash conversion are within normal ranges",
                    based_on_observation_indices=quality_indices,
                    confidence="medium",
                ))

        # Dilution inference — MUST NOT double-count with SBC
        sbc_obs_idx = None
        dilution_obs_idx = None
        for i, obs in enumerate(observations):
            if "sbc/revenue" in obs.text.lower():
                sbc_obs_idx = i
            if "dilution rate" in obs.text.lower():
                dilution_obs_idx = i

        if sbc_obs_idx is not None and dilution_obs_idx is not None:
            inferences.append(Inference(
                text=(
                    "SBC and dilution are measured separately — "
                    "valuation must use EITHER SBC expense deduction OR diluted share count, "
                    "never both simultaneously (double-counting prohibition)"
                ),
                based_on_observation_indices=[sbc_obs_idx, dilution_obs_idx],
                confidence="high",
            ))

        return inferences

    def _generate_counterarguments(
        self, inferences: list[Inference], inp: AgentInput
    ) -> list[Counterargument]:
        counterargs: list[Counterargument] = []

        for inf in inferences:
            if "below par" in inf.text.lower():
                counterargs.append(Counterargument(
                    text=(
                        "High accruals may reflect legitimate growth investment "
                        "(e.g., capitalized R&D under IFRS) rather than manipulation"
                    ),
                    strength="moderate",
                    evidence_ids=[],
                ))
            elif "sound" in inf.text.lower() and "earnings quality" in inf.text.lower():
                counterargs.append(Counterargument(
                    text=(
                        "Aggregate metrics can mask segment-level issues — "
                        "a single profitable segment may subsidize losses elsewhere"
                    ),
                    strength="weak",
                    evidence_ids=[],
                ))

        # Always include at least one cross-standard counterargument
        counterargs.append(Counterargument(
            text=(
                "Cross-standard differences in revenue recognition, R&D treatment, "
                "and government subsidy classification may affect metric comparability"
            ),
            strength="moderate",
            evidence_ids=[],
        ))

        return counterargs

    def _identify_disconfirming_triggers(
        self, inferences: list[Inference], inp: AgentInput
    ) -> list[DisconfirmingTrigger]:
        triggers: list[DisconfirmingTrigger] = []

        triggers.append(DisconfirmingTrigger(
            text="Restatement of prior-period financials",
            monitorable=True,
            check_frequency="quarterly",
        ))
        triggers.append(DisconfirmingTrigger(
            text="Auditor change or qualified audit opinion",
            monitorable=True,
            check_frequency="annually",
        ))
        triggers.append(DisconfirmingTrigger(
            text="Material related-party transaction disclosure not previously reported",
            monitorable=True,
            check_frequency="quarterly",
        ))

        return triggers

    def _cognitive_bias_self_check(self, inp: AgentInput) -> CognitiveBiasSelfCheck:
        return CognitiveBiasSelfCheck(
            anchoring_risk="medium",
            confirmation_bias_risk="low",
            recency_bias_risk="medium",
            narrative_fallacy_risk="low",
            mitigation_steps_taken=[
                "Used multiple earnings quality metrics (accruals + CFO/NI) rather than single indicator",
                "Explicitly flagged SBC/dilution double-counting risk",
                "Included cross-standard comparability caveat",
            ],
        )

    def _report_uncertainties(self, inp: AgentInput) -> list[str]:
        uncertainties = [
            "Accruals quality may vary by reporting standard and industry norms",
        ]
        if inp.sector_pack and inp.sector_pack.get("sector_name") in ("China Internet",):
            uncertainties.append(
                "CAS government subsidies classification may obscure operating earnings quality"
            )
        return uncertainties
