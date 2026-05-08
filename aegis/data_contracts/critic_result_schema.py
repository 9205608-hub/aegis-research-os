"""Section 20 — Critic Result & Bias Detection Contracts."""

from pydantic import Field

from .common import JudgmentId, Severity, StrictModel


class CriticIssue(StrictModel):
    """A single issue found by a critic."""

    issue_code: str = Field(min_length=1)
    severity: Severity
    offending_judgment_ids: list[JudgmentId] = Field(default_factory=list)
    offending_claim_ids: list[str] = Field(default_factory=list)
    message: str = Field(min_length=1)
    recommended_action: str = ""
    # Structured remediation for block-level issues
    remediation: "Remediation | None" = None


class Remediation(StrictModel):
    """Structured remediation steps for a block-level critic issue.

    Provides specific, actionable steps to resolve the issue,
    including which component/field to change and how.
    """

    steps: list[str] = Field(
        default_factory=list,
        description="Ordered steps to resolve the issue",
    )
    target_component: str = Field(
        default="",
        description="Which component/module to modify (e.g., 'dcf_engine', 'variant_analyst')",
    )
    target_field: str = Field(
        default="",
        description="Specific field or parameter to change (e.g., 'sbc_treatment', 'revenue_growth_path')",
    )
    auto_fixable: bool = Field(
        default=False,
        description="True if the system can auto-apply this fix",
    )
    rerun_required: bool = Field(
        default=True,
        description="True if the pipeline must be re-run after applying the fix",
    )


class CriticResult(StrictModel):
    """Output from any critic (logic, accounting, bias, etc.)."""

    critic_id: str = Field(min_length=1)
    critic_type: str = Field(min_length=1)
    issues: list[CriticIssue] = Field(default_factory=list)
    block_publish: bool = False
    overall_risk: str = Field(
        default="low", pattern=r"^(low|medium|high)$"
    )


class BiasDetectionResult(CriticResult):
    """Specialized critic result for cognitive bias detection."""

    # Inherits all fields from CriticResult
    # bias_types_checked is informational
    bias_types_checked: list[str] = Field(
        default_factory=lambda: [
            "anchoring",
            "confirmation_bias",
            "recency_bias",
            "narrative_fallacy",
            "survivorship_bias",
            "overconfidence",
        ]
    )
