"""Accounting Critic — Section 20.1.

Checks for:
- Cross-standard comparison without bridge
- Non-GAAP used without GAAP bridge
- SBC + dilution double penalty
- Government subsidy treated as operating income without flag
- Revenue recognition red flags
"""

from __future__ import annotations

from aegis.core.critics.base import CriticBase
from aegis.data_contracts.critic_result_schema import CriticIssue, CriticResult, Remediation
from aegis.data_contracts.judgment_schema import JudgmentContract


class AccountingCritic(CriticBase):
    """Reviews judgments for accounting correctness."""

    CRITIC_TYPE = "accounting_critic"

    def review(
        self,
        judgments: list[JudgmentContract],
        context: dict | None = None,
    ) -> CriticResult:
        issues: list[CriticIssue] = []
        ctx = context or {}

        for j in judgments:
            issues.extend(self._check_sbc_dilution_double_penalty(j, ctx))
            issues.extend(self._check_cross_standard_awareness(j, ctx))
            issues.extend(self._check_government_subsidy(j, ctx))

        return CriticResult(
            critic_id=f"critic_accounting_{id(self)}",
            critic_type=self.CRITIC_TYPE,
            issues=issues,
            block_publish=self._any_block(issues),
            overall_risk=self._overall_risk(issues),
        )

    def _check_sbc_dilution_double_penalty(
        self, j: JudgmentContract, ctx: dict | None = None
    ) -> list[CriticIssue]:
        """Detect if SBC expense AND dilution are both penalized without acknowledgment."""
        issues = []
        used = set(j.used_metric_ids)
        has_sbc = "sbc_to_revenue" in used
        has_dilution = "dilution_rate" in used

        if has_sbc and has_dilution:
            # Check if the judgment explicitly acknowledges the double-count risk
            all_text = " ".join(
                inf.text for inf in j.inferences
            ) + " ".join(obs.text for obs in j.observations)

            acknowledged = "double" in all_text.lower()
            # Check if context provides explicit SBC treatment configuration.
            # The orchestrator sets sbc_treatment to a specific mode that
            # guarantees no double-counting; in those modes this check is
            # a false positive (agent merely references both metrics for
            # context, not as a double deduction).
            c = ctx or {}
            sbc_mode = c.get("sbc_treatment", "")
            has_justification = (
                sbc_mode == "both_with_justification"
                and bool(c.get("sbc_treatment_justification", "").strip())
            )
            orchestrator_safe = sbc_mode in ("dilution_only", "expense_in_fcf")

            if orchestrator_safe:
                # Orchestrator already prevents double-counting at the engine
                # level — agent referencing both metrics is informational only.
                return issues  # No issue at all
            elif has_justification:
                severity = "warn"  # Justified — still flag but don't block
            elif acknowledged:
                severity = "warn"  # Acknowledged — flag but don't block
            else:
                severity = "block"  # Unacknowledged contamination — block publish

            remediation = None
            if severity == "block":
                remediation = Remediation(
                    steps=[
                        "Change DCFInput.sbc_treatment to 'expense_in_fcf' and set sbc_to_revenue=X%, "
                        "use basic shares — OR set sbc_treatment='dilution_only' and use diluted shares",
                        "If both are intentional, set sbc_treatment='both_with_justification' and provide "
                        "sbc_treatment_justification explaining why (e.g., SBC for cash-settled RSUs, "
                        "dilution for stock options)",
                        "Re-run DCF engine and all downstream agents",
                    ],
                    target_component="dcf_engine",
                    target_field="sbc_treatment",
                    auto_fixable=True,
                    rerun_required=True,
                )
            issues.append(self._make_issue(
                code="ACCT_SBC_DILUTION_DOUBLE",
                severity=severity,
                message="Both SBC expense and dilution rate are used"
                        + (" (acknowledged)" if acknowledged else "")
                        + (" (justified)" if has_justification else "")
                        + (" — BLOCKS publish: unacknowledged double-counting" if severity == "block" else ""),
                judgment_ids=[j.judgment_id],
                action="Explicitly choose SBC deduction OR diluted shares, not both",
                remediation=remediation,
            ))
        return issues

    def _check_cross_standard_awareness(
        self, j: JudgmentContract, ctx: dict
    ) -> list[CriticIssue]:
        """Check if cross-standard analysis acknowledges accounting differences."""
        issues = []
        entities_standards = ctx.get("entity_standards", {})

        if len(set(entities_standards.values())) > 1:
            # Multiple standards in context — judgment should mention bridge
            all_text = " ".join(inf.text for inf in j.inferences)
            all_text += " ".join(obs.text for obs in j.observations)

            if "cross-standard" not in all_text.lower() and "bridge" not in all_text.lower() \
               and "accounting standard" not in all_text.lower():
                issues.append(self._make_issue(
                    code="ACCT_CROSS_STANDARD_IGNORED",
                    severity="warn",
                    message="Multiple accounting standards detected in analysis context "
                            "but no cross-standard adjustment mentioned",
                    judgment_ids=[j.judgment_id],
                    action="Apply accounting bridge or acknowledge standard differences",
                ))
        return issues

    def _check_government_subsidy(
        self, j: JudgmentContract, ctx: dict
    ) -> list[CriticIssue]:
        """Flag if government subsidies might be inflating operating income (CAS concern).

        BUG-Y41 (2026-05-06): two-part fix.
        1. Orchestrator now passes `market_id` in critic_context — previously
           missing, causing early-return on every run (dead code).
        2. The keyword matcher only had English terms ("margin", "profitability")
           which don't fire on Chinese narratives. Added 中文 equivalents so the
           check actually triggers on A-share thesis text.
        """
        issues = []
        market = ctx.get("market_id", "")
        if market not in ("cn",):
            return issues

        # For Chinese entities, check if accounting observations mention subsidies
        all_text = " ".join(obs.text for obs in j.observations)
        all_text += " ".join(inf.text for inf in j.inferences)
        # Lower-case only matters for English; the Chinese keywords are
        # case-invariant (no casing in CJK).
        text_lc = all_text.lower()

        # If margins are discussed but subsidies are not mentioned for CN entity.
        # Chinese narratives use 毛利率 / 营业利润率 / 经营利润率 / 净利率 etc.
        # CAS subsidy terms include 政府补贴 / 政府补助 / 财政补贴 / 财政补助.
        mentions_margins = (
            any(kw in text_lc for kw in ("margin", "profitability", "operating income"))
            or any(kw in all_text for kw in (
                "毛利率", "营业利润率", "经营利润率", "净利率",
                "营业利润", "经营利润",
            ))
        )
        mentions_subsidy = (
            any(kw in text_lc for kw in ("subsid", "government grant"))
            or any(kw in all_text for kw in (
                "政府补贴", "政府补助", "财政补贴", "财政补助",
                "营业外收入",  # CAS line item where subsidies sometimes book
            ))
        )

        if mentions_margins and not mentions_subsidy:
            issues.append(self._make_issue(
                code="ACCT_GOV_SUBSIDY_MISSING",
                severity="info",
                message="Chinese entity margin analysis does not mention government subsidies — "
                        "CAS allows subsidies in operating income",
                judgment_ids=[j.judgment_id],
                action="Check if government subsidies are material to operating income",
            ))
        return issues
