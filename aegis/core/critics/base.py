"""Critic Base Framework — Section 20.

Critics independently review agent judgments for logical consistency,
accounting correctness, evidence sufficiency, sector alignment, and
cognitive bias.

Every critic outputs CriticResult with block_publish decision.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from aegis.data_contracts.critic_result_schema import CriticIssue, CriticResult, Remediation
from aegis.data_contracts.judgment_schema import JudgmentContract


class CriticBase(ABC):
    """Abstract base class for all critics."""

    CRITIC_TYPE: str = "base_critic"

    @abstractmethod
    def review(
        self,
        judgments: list[JudgmentContract],
        context: dict | None = None,
    ) -> CriticResult:
        """Review a set of judgments and return issues found."""

    def _make_issue(
        self,
        code: str,
        severity: str,
        message: str,
        judgment_ids: list[str] | None = None,
        claim_ids: list[str] | None = None,
        action: str = "",
        remediation: Remediation | None = None,
    ) -> CriticIssue:
        return CriticIssue(
            issue_code=code,
            severity=severity,
            offending_judgment_ids=judgment_ids or [],
            offending_claim_ids=claim_ids or [],
            message=message,
            recommended_action=action,
            remediation=remediation,
        )

    def _any_block(self, issues: list[CriticIssue]) -> bool:
        return any(i.severity == "block" for i in issues)

    def _overall_risk(self, issues: list[CriticIssue]) -> str:
        if any(i.severity == "block" for i in issues):
            return "high"
        if any(i.severity == "warn" for i in issues):
            return "medium"
        return "low"
