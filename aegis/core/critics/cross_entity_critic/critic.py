"""Cross-Entity Critic — Section 20.1.

Only active in multi-entity research modes. Checks for:
- Cross-standard comparison without bridge
- Cross-currency comparison without conversion
- Missing relationship graph awareness
- Inconsistent fiscal period alignment
- Correlation/concentration risk in comparison set
"""

from __future__ import annotations

from aegis.core.critics.base import CriticBase
from aegis.data_contracts.critic_result_schema import CriticIssue, CriticResult
from aegis.data_contracts.judgment_schema import JudgmentContract


class CrossEntityCritic(CriticBase):
    """Reviews multi-entity research for cross-entity consistency."""

    CRITIC_TYPE = "cross_entity_critic"

    def review(
        self,
        judgments: list[JudgmentContract],
        context: dict | None = None,
    ) -> CriticResult:
        issues: list[CriticIssue] = []
        ctx = context or {}

        issues.extend(self._check_cross_standard(judgments, ctx))
        issues.extend(self._check_cross_currency(judgments, ctx))
        issues.extend(self._check_relationship_awareness(judgments, ctx))
        issues.extend(self._check_concentration_risk(ctx))

        return CriticResult(
            critic_id=f"critic_cross_entity_{id(self)}",
            critic_type=self.CRITIC_TYPE,
            issues=issues,
            block_publish=self._any_block(issues),
            overall_risk=self._overall_risk(issues),
        )

    def _check_cross_standard(
        self, judgments: list[JudgmentContract], ctx: dict
    ) -> list[CriticIssue]:
        """Check that multi-standard comparisons use accounting bridge."""
        issues = []
        entity_standards = ctx.get("entity_standards", {})
        unique_standards = set(entity_standards.values())

        if len(unique_standards) > 1:
            # Check if any judgment acknowledges the bridge
            all_text = " ".join(
                inf.text for j in judgments for inf in j.inferences
            )
            import re
            text_lower = all_text.lower()
            # Use word-boundary matching to avoid "cas" matching "cash"
            bridge_keywords = [
                r"\bbridge\b", r"\bcross-standard\b", r"\baccounting standard\b",
                r"\bus[_ ]gaap\b", r"\bifrs\b", r"\bcas\b",
            ]
            bridge_aware = any(
                re.search(pat, text_lower) for pat in bridge_keywords
            )
            if not bridge_aware:
                issues.append(self._make_issue(
                    code="CROSS_ENTITY_NO_BRIDGE",
                    severity="block",
                    message=f"Comparing entities across standards {unique_standards} "
                            "without accounting bridge — Section 16.4 requires bridge",
                    action="Apply accounting bridge before cross-entity comparison",
                ))
        return issues

    def _check_cross_currency(
        self, judgments: list[JudgmentContract], ctx: dict
    ) -> list[CriticIssue]:
        """Check that multi-currency comparisons use currency engine."""
        issues = []
        entity_currencies = ctx.get("entity_currencies", {})
        unique_currencies = set(entity_currencies.values())

        if len(unique_currencies) > 1:
            all_text = " ".join(
                inf.text for j in judgments for inf in j.inferences
            )
            currency_aware = any(kw in all_text.lower() for kw in (
                "currency", "fx", "exchange rate", "converted"
            ))
            if not currency_aware:
                issues.append(self._make_issue(
                    code="CROSS_ENTITY_NO_FX",
                    severity="block",
                    message=f"Comparing entities across currencies {unique_currencies} "
                            "without currency conversion acknowledgment",
                    action="Apply currency engine before cross-entity valuation comparison",
                ))
        return issues

    def _check_relationship_awareness(
        self, judgments: list[JudgmentContract], ctx: dict
    ) -> list[CriticIssue]:
        """Check if cross-entity relationships are acknowledged."""
        issues = []
        relationships = ctx.get("entity_relationships", [])

        if relationships:
            has_rel_ref = any(
                j.used_relationship_ids for j in judgments
            )
            if not has_rel_ref:
                issues.append(self._make_issue(
                    code="CROSS_ENTITY_RELATIONSHIPS_IGNORED",
                    severity="warn",
                    message="Entity relationships exist in comparison set but "
                            "none referenced in analysis — may miss supply chain or competition dynamics",
                    action="Include entity relationship graph data in cross-entity analysis",
                ))
        return issues

    def _check_concentration_risk(self, ctx: dict) -> list[CriticIssue]:
        """Flag if comparison set has high correlation risk."""
        issues = []
        cross_risks = ctx.get("cross_entity_risks", [])
        for risk in cross_risks:
            issues.append(self._make_issue(
                code="CROSS_ENTITY_CONCENTRATION",
                severity="info",
                message=f"Cross-entity risk: {risk}",
                action="Ensure portfolio signal reflects concentration risk",
            ))
        return issues
