"""Cognitive Bias Critic — Section 20.2.

Core innovation: independent bias detection that cannot be bypassed.

Detects:
- Anchoring bias
- Confirmation bias
- Recency bias
- Narrative fallacy
- Survivorship bias
- Overconfidence

Severity rules (Section 20.2):
- info: mild bias tendency, does not block
- warn: clear bias risk, must annotate in thesis
- block: severe bias — publish blocked until corrected
  - supporting/disconfirming ratio > 10:1 with Tier 1 counter-evidence ignored
  - scenario range < 20% (overconfidence)
  - core inference without any evidence (narrative fallacy)
"""

from __future__ import annotations

from aegis.core.critics.base import CriticBase
from aegis.data_contracts.critic_result_schema import (
    BiasDetectionResult,
    CriticIssue,
)
from aegis.data_contracts.judgment_schema import JudgmentContract


class CognitiveBiasCritic(CriticBase):
    """Independent reviewer for cognitive biases in agent judgments."""

    CRITIC_TYPE = "cognitive_bias_critic"

    # Thresholds from Section 20.2
    CONFIRMATION_WARN_RATIO = 5.0    # supporting:disconfirming > 5:1
    CONFIRMATION_BLOCK_RATIO = 10.0  # > 10:1 with Tier 1 counter ignored
    OVERCONFIDENCE_SCENARIO_MIN_SPREAD = 0.20  # bull/bear < 20% is too narrow
    OVERCONFIDENCE_SCENARIO_BLOCK_SPREAD = 0.20

    def review(
        self,
        judgments: list[JudgmentContract],
        context: dict | None = None,
    ) -> BiasDetectionResult:
        issues: list[CriticIssue] = []

        for j in judgments:
            issues.extend(self._check_anchoring(j, context or {}))
            issues.extend(self._check_confirmation_bias(j, context or {}))
            issues.extend(self._check_recency_bias(j, context or {}))
            issues.extend(self._check_narrative_fallacy(j))
            issues.extend(self._check_overconfidence(j, context or {}))

        return BiasDetectionResult(
            critic_id=f"critic_bias_{id(self)}",
            critic_type=self.CRITIC_TYPE,
            issues=issues,
            block_publish=self._any_block(issues),
            overall_risk=self._overall_risk(issues),
        )

    def _check_anchoring(
        self, j: JudgmentContract, ctx: dict
    ) -> list[CriticIssue]:
        """Check for anchoring bias — over-reliance on initial values.

        We previously warned on ANY self-reported HIGH anchoring risk. That
        created a perverse incentive: agents that honestly self-reflected and
        documented 3-4 mitigation steps were treated identically to agents
        that did neither. Now we only warn when HIGH bias is reported AND no
        mitigation steps are documented.
        """
        issues = []
        bc = j.cognitive_bias_self_check
        if bc.anchoring_risk == "high" and not bc.mitigation_steps_taken:
            issues.append(self._make_issue(
                code="COGNITIVE_ANCHORING",
                severity="warn",
                message=f"Agent '{j.agent_name}' self-reports HIGH anchoring risk "
                        f"with no mitigation steps documented",
                judgment_ids=[j.judgment_id],
                action="Review whether key assumptions are independently derived vs. anchored to guidance/price",
            ))
        return issues

    def _check_confirmation_bias(
        self, j: JudgmentContract, ctx: dict
    ) -> list[CriticIssue]:
        """Check supporting vs. disconfirming evidence ratio.

        BUG-Y35 (2026-05-06): previously counted RAW OBSERVATIONS as
        "supporting" — which conflated factual data points (revenue, margins,
        etc.) with directional support for the thesis. 茅台 v6 had 12
        observations + 1 counterargument → 12:1 ratio → BLOCK, even though
        the observations were just neutral facts about a high-quality
        compounder. Now we count INFERENCES (which ARE directional
        conclusions) as supporting — observations are not evidence FOR a
        thesis, they're inputs to inferences.
        """
        issues = []

        # BUG-Y35: inferences are the actual directional claims; observations
        # are neutral facts. Use inferences as the supporting count.
        supporting_count = len(j.inferences)
        disconfirming_count = len(j.counterarguments)

        if disconfirming_count == 0 and supporting_count > 0:
            issues.append(self._make_issue(
                code="COGNITIVE_CONFIRMATION",
                severity="block",
                message=f"Agent '{j.agent_name}' has {supporting_count} inferences "
                        "but ZERO counterarguments — extreme confirmation bias",
                judgment_ids=[j.judgment_id],
                action="Add counterarguments with disconfirming evidence",
            ))
        elif disconfirming_count > 0:
            ratio = supporting_count / disconfirming_count
            if ratio > self.CONFIRMATION_BLOCK_RATIO:
                issues.append(self._make_issue(
                    code="COGNITIVE_CONFIRMATION",
                    severity="block",
                    message=f"Supporting/disconfirming ratio is {ratio:.1f}:1 "
                            f"(threshold for block: {self.CONFIRMATION_BLOCK_RATIO}:1)",
                    judgment_ids=[j.judgment_id],
                    action="Add more disconfirming evidence or reduce supporting items to essential ones",
                ))
            elif ratio > self.CONFIRMATION_WARN_RATIO:
                issues.append(self._make_issue(
                    code="COGNITIVE_CONFIRMATION",
                    severity="warn",
                    message=f"Supporting/disconfirming ratio is {ratio:.1f}:1 "
                            f"(threshold for warn: {self.CONFIRMATION_WARN_RATIO}:1)",
                    judgment_ids=[j.judgment_id],
                    action="Consider adding stronger counterarguments",
                ))

        # Check counterargument strength distribution
        if j.counterarguments and all(ca.strength == "weak" for ca in j.counterarguments):
            issues.append(self._make_issue(
                code="COGNITIVE_CONFIRMATION",
                severity="warn",
                message="All counterarguments rated 'weak' — possible minimization of opposing views",
                judgment_ids=[j.judgment_id],
                action="Review if moderate/strong counterarguments are being dismissed",
            ))

        return issues

    def _check_recency_bias(
        self, j: JudgmentContract, ctx: dict
    ) -> list[CriticIssue]:
        """Check if analysis relies too heavily on recent data."""
        issues = []

        # Self-reported risk — same logic as anchoring: only warn when no
        # mitigation steps were documented (don't punish honest reflection).
        bc = j.cognitive_bias_self_check
        if bc.recency_bias_risk == "high" and not bc.mitigation_steps_taken:
            issues.append(self._make_issue(
                code="COGNITIVE_RECENCY",
                severity="warn",
                message=f"Agent '{j.agent_name}' self-reports HIGH recency bias risk "
                        f"with no mitigation steps documented",
                judgment_ids=[j.judgment_id],
                action="Ensure analysis covers at least 3 years of data, not just recent quarters",
            ))

        return issues

    def _check_narrative_fallacy(self, j: JudgmentContract) -> list[CriticIssue]:
        """Check for inferences without evidence support (narrative fallacy)."""
        issues = []
        obs_count = len(j.observations)

        for i, inf in enumerate(j.inferences):
            # Check if inference has valid observation support
            valid_refs = [
                idx for idx in inf.based_on_observation_indices
                if 0 <= idx < obs_count
            ]
            if not valid_refs:
                issues.append(self._make_issue(
                    code="COGNITIVE_NARRATIVE",
                    severity="block",
                    message=f"Inference[{i}] has no valid observation support — "
                            "narrative fallacy: conclusion without evidence",
                    judgment_ids=[j.judgment_id],
                    action="Ground this inference in specific observations or remove it",
                ))

        return issues

    def _check_overconfidence(
        self, j: JudgmentContract, ctx: dict
    ) -> list[CriticIssue]:
        """Check for overconfidence signals."""
        issues = []

        # Check if uncertainties are reported
        if not j.self_reported_uncertainties:
            issues.append(self._make_issue(
                code="COGNITIVE_OVERCONFIDENCE",
                severity="warn",
                message="No self-reported uncertainties — may indicate overconfidence",
                judgment_ids=[j.judgment_id],
                action="Identify and report key uncertainties in the analysis",
            ))

        # Check scenario spread if provided in context
        scenario_spread = ctx.get("scenario_spread")
        if scenario_spread is not None and scenario_spread < self.OVERCONFIDENCE_SCENARIO_BLOCK_SPREAD:
            issues.append(self._make_issue(
                code="COGNITIVE_OVERCONFIDENCE",
                severity="block",
                message=f"Scenario spread is {scenario_spread:.0%} — "
                        f"below minimum {self.OVERCONFIDENCE_SCENARIO_BLOCK_SPREAD:.0%}. "
                        "Bull/bear range is too narrow, indicating overconfidence.",
                judgment_ids=[j.judgment_id],
                action="Widen scenario range to at least 30% bull/bear spread",
            ))

        # Check all inferences are "high" confidence — potential overconfidence
        if j.inferences and all(inf.confidence == "high" for inf in j.inferences):
            issues.append(self._make_issue(
                code="COGNITIVE_OVERCONFIDENCE",
                severity="info",
                message="All inferences rated 'high' confidence — review whether this is warranted",
                judgment_ids=[j.judgment_id],
                action="Consider if any inferences should be medium or low confidence",
            ))

        return issues
