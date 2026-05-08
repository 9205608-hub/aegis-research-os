"""Macro Consistency Critic — Section 20.1.

Checks for:
- Judgments that ignore macro context
- Growth assumptions inconsistent with macro cycle
- Discount rate assumptions inconsistent with rate environment
- Missing macro dependency declaration
"""

from __future__ import annotations

from aegis.core.critics.base import CriticBase
from aegis.data_contracts.critic_result_schema import CriticIssue, CriticResult
from aegis.data_contracts.judgment_schema import JudgmentContract


class MacroConsistencyCritic(CriticBase):
    """Reviews judgments for macro context consistency."""

    CRITIC_TYPE = "macro_consistency_critic"

    def review(
        self,
        judgments: list[JudgmentContract],
        context: dict | None = None,
    ) -> CriticResult:
        issues: list[CriticIssue] = []
        ctx = context or {}

        for j in judgments:
            issues.extend(self._check_macro_awareness(j, ctx))
            issues.extend(self._check_cycle_consistency(j, ctx))

        return CriticResult(
            critic_id=f"critic_macro_{id(self)}",
            critic_type=self.CRITIC_TYPE,
            issues=issues,
            block_publish=self._any_block(issues),
            overall_risk=self._overall_risk(issues),
        )

    def _check_macro_awareness(
        self, j: JudgmentContract, ctx: dict
    ) -> list[CriticIssue]:
        """Check if valuation/variant agents reference macro context."""
        issues = []
        if j.agent_name not in ("valuation_analyst", "variant_analyst", "risk_analyst"):
            return issues

        # Check if macro context was used
        # BUG-Y44 (2026-05-06): keyword set was EN-only (macro/cycle/fed/
        # rate/gdp/pmi/inflation). Chinese narratives use 宏观/周期/美联储/
        # 利率/PMI/通胀/通货膨胀 etc. and were systemically flagged with
        # MACRO_IGNORED warns even when the agent had clear macro
        # references. Add CN equivalents.
        if not j.used_macro_context_ids:
            all_text = " ".join(inf.text for inf in j.inferences)
            all_text += " ".join(obs.text for obs in j.observations)
            text_lc = all_text.lower()
            macro_en = ("macro", "cycle", "fed", "rate", "gdp", "pmi",
                        "inflation", "monetary", "policy")
            macro_zh = (
                "宏观", "周期", "美联储", "联储", "央行", "人行",
                "利率", "通胀", "通货膨胀", "GDP", "国内生产总值",
                "PMI", "采购经理", "货币政策", "财政政策",
                "汇率", "信用周期",
            )
            has_en = any(kw in text_lc for kw in macro_en)
            has_zh = any(kw in all_text for kw in macro_zh)
            if not (has_en or has_zh):
                issues.append(self._make_issue(
                    code="MACRO_IGNORED",
                    severity="warn",
                    message=f"Agent '{j.agent_name}' does not reference macro context — "
                            "Section 13.3: agents cannot ignore macro context in valuation",
                    judgment_ids=[j.judgment_id],
                    action="Include macro cycle phase and its implications in analysis",
                ))
        return issues

    def _check_cycle_consistency(
        self, j: JudgmentContract, ctx: dict
    ) -> list[CriticIssue]:
        """Check if growth assumptions are consistent with macro cycle."""
        issues = []
        cycle_phase = ctx.get("cycle_phase", "")

        if not cycle_phase or j.agent_name != "valuation_analyst":
            return issues

        all_text = " ".join(inf.text for inf in j.inferences)

        # Late cycle + aggressive growth is inconsistent
        # BUG-Y44 (2026-05-06): same EN-only matcher. Chinese 加速增长 / 高
        # 速增长 / 快速扩张 / 强劲增长 used by A-share valuation analysts
        # never matched, so the cycle-mismatch info never fired on CN runs.
        if cycle_phase in ("late_expansion", "contraction"):
            text_lc = all_text.lower()
            en_aggressive = ("acceleration", "aggressive growth", "rapid expansion",
                             "accelerating", "robust growth")
            zh_aggressive = ("加速增长", "加速扩张", "高速增长", "高速扩张",
                             "快速扩张", "快速增长", "强劲增长", "爆发式增长")
            if (any(kw in text_lc for kw in en_aggressive)
                    or any(kw in all_text for kw in zh_aggressive)):
                issues.append(self._make_issue(
                    code="MACRO_CYCLE_MISMATCH",
                    severity="info",
                    message=f"Macro cycle is '{cycle_phase}' but analysis references aggressive growth — "
                            "ensure this is company-specific rather than ignoring macro headwinds",
                    judgment_ids=[j.judgment_id],
                    action="Justify growth assumption in context of macro cycle",
                ))

        return issues
