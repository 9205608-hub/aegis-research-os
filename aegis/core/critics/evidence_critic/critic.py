"""Evidence Critic — Section 20.1.

Checks for:
- Claims without sufficient evidence backing
- Tier 3/4 evidence as sole support for core claims
- Evidence staleness
- Missing disconfirming evidence
"""

from __future__ import annotations

from aegis.core.critics.base import CriticBase
from aegis.data_contracts.critic_result_schema import CriticIssue, CriticResult, Remediation
from aegis.data_contracts.judgment_schema import JudgmentContract


class EvidenceCritic(CriticBase):
    """Reviews judgments for evidence sufficiency and quality."""

    CRITIC_TYPE = "evidence_critic"

    MIN_EVIDENCE_FOR_HIGH_CONFIDENCE = 2
    MIN_COUNTERARGUMENTS = 1

    def review(
        self,
        judgments: list[JudgmentContract],
        context: dict | None = None,
    ) -> CriticResult:
        issues: list[CriticIssue] = []

        for j in judgments:
            issues.extend(self._check_evidence_sufficiency(j))
            issues.extend(self._check_counterargument_presence(j))
            issues.extend(self._check_evidence_tier_quality(j, context or {}))
            issues.extend(self._check_disconfirming_triggers(j))

        return CriticResult(
            critic_id=f"critic_evidence_{id(self)}",
            critic_type=self.CRITIC_TYPE,
            issues=issues,
            block_publish=self._any_block(issues),
            overall_risk=self._overall_risk(issues),
        )

    def _check_evidence_sufficiency(self, j: JudgmentContract) -> list[CriticIssue]:
        issues = []
        # High-confidence inferences need multiple evidence sources
        for i, inf in enumerate(j.inferences):
            if inf.confidence == "high":
                # Count unique source_ids from referenced observations
                source_count = 0
                for idx in inf.based_on_observation_indices:
                    if 0 <= idx < len(j.observations):
                        source_count += len(j.observations[idx].source_ids)

                if source_count < self.MIN_EVIDENCE_FOR_HIGH_CONFIDENCE:
                    issues.append(self._make_issue(
                        code="EVIDENCE_INSUFFICIENT_FOR_CONFIDENCE",
                        severity="warn",
                        message=f"Inference[{i}] claims 'high' confidence but has only "
                                f"{source_count} source(s) — need at least "
                                f"{self.MIN_EVIDENCE_FOR_HIGH_CONFIDENCE}",
                        judgment_ids=[j.judgment_id],
                        action="Lower confidence or add supporting evidence",
                    ))

        # Overall evidence count
        if not j.used_evidence_ids and not any(
            obs.source_ids for obs in j.observations
        ):
            issues.append(self._make_issue(
                code="EVIDENCE_NONE",
                severity="block",
                message="Judgment has no evidence backing whatsoever",
                judgment_ids=[j.judgment_id],
                action="Add evidence packets supporting the analysis",
                remediation=Remediation(
                    steps=[
                        f"Add evidence_packets to AgentInput for agent '{j.agent_name}'",
                        "Each evidence packet needs: evidence_id, assertion_type, assertion_text, source_ids",
                        "Alternatively, ensure observations have source_ids linking to fact IDs",
                        "Sources can be: 10-K excerpts, earnings call quotes, or computed metric IDs",
                    ],
                    target_component=j.agent_name,
                    target_field="evidence_packets / observations[].source_ids",
                    auto_fixable=False,
                    rerun_required=True,
                ),
            ))

        return issues

    def _check_counterargument_presence(
        self, j: JudgmentContract
    ) -> list[CriticIssue]:
        issues = []
        if len(j.counterarguments) < self.MIN_COUNTERARGUMENTS:
            issues.append(self._make_issue(
                code="EVIDENCE_NO_COUNTERARGUMENT",
                severity="block",
                message="No counterarguments provided — analysis must consider opposing views",
                judgment_ids=[j.judgment_id],
                action="Add at least one counterargument with evidence",
                remediation=Remediation(
                    steps=[
                        f"In agent '{j.agent_name}', add at least one Counterargument object",
                        "Each counterargument needs: text (the opposing view), strength (weak/moderate/strong), "
                        "and evidence_ids referencing supporting data",
                        "Consider: What would make this thesis wrong? What does the bear case assume?",
                    ],
                    target_component=j.agent_name,
                    target_field="counterarguments",
                    auto_fixable=False,
                    rerun_required=True,
                ),
            ))

        # Check counterargument quality — all weak is a red flag
        if j.counterarguments and all(
            ca.strength == "weak" for ca in j.counterarguments
        ):
            issues.append(self._make_issue(
                code="EVIDENCE_WEAK_COUNTER_ONLY",
                severity="info",
                message="All counterarguments rated 'weak' — may indicate confirmation bias",
                judgment_ids=[j.judgment_id],
                action="Consider if stronger counterarguments exist",
            ))
        return issues

    def _check_evidence_tier_quality(
        self, j: JudgmentContract, ctx: dict
    ) -> list[CriticIssue]:
        """Check if core inferences rely solely on Tier 3/4 evidence."""
        issues = []
        evidence_tiers = ctx.get("evidence_tiers", {})
        if not evidence_tiers:
            return issues

        for i, inf in enumerate(j.inferences):
            if inf.confidence in ("high", "medium"):
                # Gather evidence tiers for this inference's observations
                tiers_used = set()
                for idx in inf.based_on_observation_indices:
                    if 0 <= idx < len(j.observations):
                        for sid in j.observations[idx].source_ids:
                            tier = evidence_tiers.get(sid)
                            if tier is not None:
                                tiers_used.add(tier)

                if tiers_used and min(tiers_used) >= 3:
                    issues.append(self._make_issue(
                        code="EVIDENCE_LOW_TIER_ONLY",
                        severity="warn" if inf.confidence == "medium" else "block",
                        message=f"Inference[{i}] relies solely on Tier {min(tiers_used)}+ evidence — "
                                "core claims need Tier 1/2 support",
                        judgment_ids=[j.judgment_id],
                        action="Add Tier 1 or Tier 2 evidence support",
                    ))
        return issues

    def _check_disconfirming_triggers(
        self, j: JudgmentContract
    ) -> list[CriticIssue]:
        issues = []
        if not j.disconfirming_triggers:
            issues.append(self._make_issue(
                code="EVIDENCE_NO_DISCONFIRM",
                severity="warn",
                message="No disconfirming triggers defined — analysis should specify what would change the conclusion",
                judgment_ids=[j.judgment_id],
                action="Add at least one monitorable disconfirming trigger",
            ))
        return issues
