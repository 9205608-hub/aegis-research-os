"""Market Critic — Section 20.1.

Checks for:
- Thesis without priced-in object
- Variant claim without market expectations data
- Missing edge assessment
- Edge assessment without decay trigger
"""

from __future__ import annotations

from aegis.core.critics.base import CriticBase
from aegis.data_contracts.critic_result_schema import CriticIssue, CriticResult, Remediation
from aegis.data_contracts.judgment_schema import JudgmentContract


class MarketCritic(CriticBase):
    """Reviews thesis for market expectations and edge consistency."""

    CRITIC_TYPE = "market_critic"

    def review(
        self,
        judgments: list[JudgmentContract],
        context: dict | None = None,
    ) -> CriticResult:
        issues: list[CriticIssue] = []
        ctx = context or {}

        issues.extend(self._check_priced_in_object(ctx))
        issues.extend(self._check_edge_assessment(ctx))
        issues.extend(self._check_scenario_matrix(ctx))
        issues.extend(self._check_variant_grounding(judgments, ctx))

        return CriticResult(
            critic_id=f"critic_market_{id(self)}",
            critic_type=self.CRITIC_TYPE,
            issues=issues,
            block_publish=self._any_block(issues),
            overall_risk=self._overall_risk(issues),
        )

    def _check_priced_in_object(self, ctx: dict) -> list[CriticIssue]:
        """Section 14.3: no thesis can claim variant without priced_in_object."""
        issues = []
        priced_in = ctx.get("priced_in")
        if not priced_in:
            issues.append(self._make_issue(
                code="MARKET_NO_PRICED_IN",
                severity="block",
                message="No priced-in object provided — cannot assess what market already prices",
                action="Run reverse DCF / implied assumption solver before publishing",
                remediation=Remediation(
                    steps=[
                        "Run ReverseDCFSolver.solve_implied_growth() with current market price",
                        "Pass result to MarketExpectationsLayer.build_priced_in_object()",
                        "Include priced_in dict in critic context with implied_revenue_growth",
                    ],
                    target_component="market_expectations",
                    target_field="priced_in",
                    auto_fixable=True,
                    rerun_required=True,
                ),
            ))
        return issues

    def _check_edge_assessment(self, ctx: dict) -> list[CriticIssue]:
        """Section 15.3: no thesis without edge assessment."""
        issues = []
        edge = ctx.get("edge_assessment")
        if not edge:
            issues.append(self._make_issue(
                code="MARKET_NO_EDGE",
                severity="block",
                message="No edge assessment — thesis must explain why market pricing is wrong",
                action="Complete edge assessment with type, source, durability, and decay trigger",
                remediation=Remediation(
                    steps=[
                        "Create EdgeAssessment with: primary_edge_type (analytical/informational/behavioral), "
                        "edge_source, edge_durability, edge_decay_trigger",
                        "Add why_market_is_wrong and what_would_change_my_mind",
                        "Pass edge_assessment dict in critic context",
                    ],
                    target_component="decision_engine",
                    target_field="edge_assessment",
                    auto_fixable=False,
                    rerun_required=True,
                ),
            ))
        elif isinstance(edge, dict):
            if not edge.get("edge_decay_trigger"):
                issues.append(self._make_issue(
                    code="MARKET_EDGE_NO_DECAY",
                    severity="warn",
                    message="Edge assessment has no decay trigger — "
                            "must define what would cause the edge to disappear",
                    action="Add edge decay trigger for monitoring",
                ))
            if not edge.get("why_market_is_wrong"):
                issues.append(self._make_issue(
                    code="MARKET_EDGE_NO_WHY",
                    severity="block",
                    message="Edge assessment does not explain why market is wrong",
                    action="Add 'why_market_is_wrong' to edge assessment",
                    remediation=Remediation(
                        steps=[
                            "Add why_market_is_wrong to EdgeAssessment explaining the specific "
                            "market misperception (e.g., 'consensus treats AI capex as pure cost')",
                            "This must be a testable, falsifiable claim about market pricing",
                        ],
                        target_component="decision_engine",
                        target_field="edge_assessment.why_market_is_wrong",
                        auto_fixable=False,
                        rerun_required=False,
                    ),
                ))
        return issues

    def _check_scenario_matrix(self, ctx: dict) -> list[CriticIssue]:
        """Thesis must have scenario matrix with bear/base/bull."""
        issues = []
        scenarios = ctx.get("scenarios", {})
        if not scenarios:
            issues.append(self._make_issue(
                code="MARKET_NO_SCENARIOS",
                severity="block",
                message="No scenario matrix — thesis must have bear/base/bull cases",
                action="Run scenario modeling engine with at least 3 scenarios",
                remediation=Remediation(
                    steps=[
                        "Create 3 DCFInput variants: bear (lower growth/margins), base, bull (higher growth/margins)",
                        "Run DCFEngine.compute_dcf() for each to get per_share_value",
                        "Pass scenarios dict with bear_value, base_value, bull_value in context",
                    ],
                    target_component="dcf_engine",
                    target_field="scenarios",
                    auto_fixable=True,
                    rerun_required=True,
                ),
            ))
        else:
            for case in ("bear_value", "base_value", "bull_value"):
                if scenarios.get(case) is None:
                    issues.append(self._make_issue(
                        code="MARKET_MISSING_SCENARIO",
                        severity="block",
                        message=f"Missing {case.replace('_', ' ')} in scenario matrix",
                        action=f"Add {case} to scenario matrix",
                    ))
        return issues

    def _check_variant_grounding(
        self, judgments: list[JudgmentContract], ctx: dict
    ) -> list[CriticIssue]:
        """Variant analyst must ground claims in market data, not guesses.

        BUG-Y42 (2026-05-06): keyword matcher was English-only
        ("market-implied" / "consensus" / "priced"). Chinese narratives
        for A-share entities use 市场隐含 / 一致预期 / 市场定价 / 卖方
        预期, which never matched → critic spuriously warned that variant
        analyst was ungrounded even when the LLM had ample 中文 references
        to consensus/market-implied. Now both languages match.
        """
        issues = []
        # English-text matches use lower-case substring; Chinese tokens are
        # case-invariant (no casing in CJK), so we check the original text.
        EN_HOOKS = ("market-implied", "consensus", "priced", "implied")
        ZH_HOOKS = (
            "市场隐含", "市场预期", "市场定价",
            "一致预期", "一致预测", "卖方预期", "卖方一致",
            "共识预期", "共识预测",
        )
        for j in judgments:
            if j.agent_name != "variant_analyst":
                continue

            has_market_ref = False
            for obs in j.observations:
                lc = obs.text.lower()
                if any(kw in lc for kw in EN_HOOKS):
                    has_market_ref = True
                    break
                if any(kw in obs.text for kw in ZH_HOOKS):
                    has_market_ref = True
                    break
            if not has_market_ref:
                issues.append(self._make_issue(
                    code="MARKET_VARIANT_UNGROUNDED",
                    severity="warn",
                    message="Variant analyst does not reference market-implied assumptions — "
                            "variant must be grounded in what market prices",
                    judgment_ids=[j.judgment_id],
                    action="Reference priced-in assumptions from reverse DCF / consensus",
                ))
        return issues
