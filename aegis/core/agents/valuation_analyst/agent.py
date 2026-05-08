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

from aegis.core.agents.base import AgentBase, AgentInput
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

        # Current valuation multiples — prefer TTM P/E when available
        # (matches peer comparison source), but surface both if they
        # disagree materially so the LLM understands the FY-vs-TTM gap.
        for key in ("pe_ratio_ttm", "pe_ratio", "ev_to_ebitda", "ev_to_revenue"):
            if key in metrics and metrics[key]:
                label = {
                    "pe_ratio_ttm": "P/E (TTM)",
                    "pe_ratio": "P/E (FY static)",
                    "ev_to_ebitda": "EV/EBITDA",
                    "ev_to_revenue": "EV/Revenue",
                }[key]
                observations.append(Observation(
                    text=f"{label}: {metrics[key]:.1f}x",
                    source_ids=[f"metric:{key}:{inp.entity_id}"],
                ))

        # Enterprise value
        if "enterprise_value" in metrics:
            observations.append(Observation(
                text=f"Enterprise value: {metrics['enterprise_value']:,.0f}",
                source_ids=[f"metric:enterprise_value:{inp.entity_id}"],
            ))

        # Implied assumptions from market expectations (in prior judgments or context)
        if inp.macro_context:
            priced_in = inp.macro_context.get("priced_in", {})
            if priced_in.get("implied_revenue_growth") is not None:
                observations.append(Observation(
                    text=f"Market-implied revenue growth: {priced_in['implied_revenue_growth']:.2%}",
                    source_ids=["reverse_dcf:implied_growth"],
                ))
            if priced_in.get("implied_terminal_growth") is not None:
                observations.append(Observation(
                    text=f"Market-implied terminal growth: {priced_in['implied_terminal_growth']:.2%}",
                    source_ids=["reverse_dcf:implied_terminal"],
                ))

        # Scenario outputs from context
        scenarios = inp.macro_context.get("scenarios", {}) if inp.macro_context else {}
        for name in ("bear", "base", "bull"):
            val = scenarios.get(f"{name}_value")
            if val is not None:
                observations.append(Observation(
                    text=f"Scenario '{name}' per-share value: {val:.2f}",
                    source_ids=[f"scenario_engine:{name}"],
                ))

        # Macro context for discount rate
        if inp.macro_context:
            phase = inp.macro_context.get("cycle_phase", "")
            if phase:
                observations.append(Observation(
                    text=f"Macro cycle phase: {phase}",
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

        # Valuation vs implied assumptions
        implied_obs = [i for i, o in enumerate(observations) if "market-implied" in o.text.lower()]
        scenario_obs = [i for i, o in enumerate(observations) if "scenario" in o.text.lower()]
        multiple_obs = [i for i, o in enumerate(observations) if any(
            k in o.text.lower() for k in ("pe ", "ev ", "enterprise")
        )]

        # Implied growth vs consensus comparison
        if implied_obs and inp.macro_context:
            priced_in = inp.macro_context.get("priced_in", {})
            implied_g = priced_in.get("implied_revenue_growth")
            if implied_g is not None:
                if implied_g > 0.25:
                    inferences.append(Inference(
                        text=f"Market implies {implied_g:.0%} revenue growth — aggressive; "
                             "risk skewed to downside if growth disappoints",
                        based_on_observation_indices=implied_obs[:1],
                        confidence="medium",
                    ))
                elif implied_g < 0.05:
                    inferences.append(Inference(
                        text=f"Market implies only {implied_g:.0%} revenue growth — modest; "
                             "potential upside if growth exceeds low bar",
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
                    text=f"Scenario spread (bull/bear): {spread:.0%} — "
                         f"{'wide' if spread > 0.5 else 'narrow'} range of outcomes",
                    based_on_observation_indices=scenario_obs,
                    confidence="medium",
                ))

        # Macro context should influence discount rate
        macro_obs = [i for i, o in enumerate(observations) if "macro cycle" in o.text.lower()]
        if macro_obs:
            inferences.append(Inference(
                text="Macro cycle position should be reflected in WACC assumption and scenario weights",
                based_on_observation_indices=macro_obs,
                confidence="high",
            ))

        return inferences

    def _generate_counterarguments(
        self, inferences: list[Inference], inp: AgentInput
    ) -> list[Counterargument]:
        counterargs: list[Counterargument] = []

        for inf in inferences:
            if "risk skewed to downside" in inf.text.lower():
                counterargs.append(Counterargument(
                    text="High implied growth may be warranted if company has secular growth tailwinds "
                         "(e.g., AI monetization) that sustain above-market rates",
                    strength="moderate",
                    evidence_ids=[],
                ))
            elif "potential upside" in inf.text.lower():
                counterargs.append(Counterargument(
                    text="Low implied growth may correctly reflect structural headwinds — "
                         "value traps exhibit this pattern",
                    strength="moderate",
                    evidence_ids=[],
                ))

        counterargs.append(Counterargument(
            text="DCF valuation is highly sensitive to terminal growth and WACC assumptions — "
                 "small changes drive large value swings",
            strength="strong",
            evidence_ids=[],
        ))

        return counterargs

    def _identify_disconfirming_triggers(
        self, inferences: list[Inference], inp: AgentInput
    ) -> list[DisconfirmingTrigger]:
        return [
            DisconfirmingTrigger(
                text="Consensus revenue revision turns negative for 3 consecutive months",
                monitorable=True,
                check_frequency="monthly",
            ),
            DisconfirmingTrigger(
                text="Forward P/E moves above 95th percentile of 5-year range",
                monitorable=True,
                check_frequency="weekly",
            ),
            DisconfirmingTrigger(
                text="Management lowers guidance below bear-case assumption",
                monitorable=True,
                check_frequency="quarterly",
            ),
        ]

    def _cognitive_bias_self_check(self, inp: AgentInput) -> CognitiveBiasSelfCheck:
        return CognitiveBiasSelfCheck(
            anchoring_risk="high",
            confirmation_bias_risk="medium",
            recency_bias_risk="medium",
            narrative_fallacy_risk="medium",
            mitigation_steps_taken=[
                "Used reverse DCF to anchor on market-implied assumptions, not own prior",
                "Included scenario spread analysis to avoid overconfident point estimates",
                "Referenced macro cycle phase in discount rate discussion",
            ],
        )

    def _report_uncertainties(self, inp: AgentInput) -> list[str]:
        return [
            "DCF output is highly sensitive to terminal value assumptions",
            "Reverse DCF implied growth depends on WACC estimate accuracy",
            "Relative valuation assumes peers are correctly priced",
        ]
