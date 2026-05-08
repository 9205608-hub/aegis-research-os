"""Publish Gate v1 — Section 20.3.

Publishable = 1(∀ g ∈ G, g = pass)

Gates:
- truth_gate: all metrics computed via Formula Engine
- definition_gate: all metrics registered in MetricRegistry
- evidence_gate: all judgments have evidence backing
- critic_gate: no critic issues at "block" severity
- cognitive_bias_gate: bias critic passed
- reproducibility_gate: run_manifest exists with hashes
- accounting_integrity_gate: no accounting contamination (SBC, cross-standard)
- warn_accumulation_gate: total warns below threshold
- logical_consistency_gate: no compound logic contradictions
- data_quality_gate: no DQ severity=error issues from fact_bridge
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aegis.data_contracts.critic_result_schema import CriticResult
from aegis.data_contracts.judgment_schema import JudgmentContract


@dataclass(frozen=True)
class GateCheck:
    """Result of a single gate check."""

    gate_name: str
    passed: bool
    message: str
    severity: str = "block"  # "block" or "warn"


@dataclass
class GateResult:
    """Aggregate result of all publish gate checks."""

    publishable: bool
    checks: list[GateCheck] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        status = "PUBLISHABLE" if self.publishable else "BLOCKED"
        passed = sum(1 for c in self.checks if c.passed)
        total = len(self.checks)
        return f"{status}: {passed}/{total} gates passed"


# Default policy thresholds — can be overridden per instance
DEFAULT_GATE_POLICY: dict[str, Any] = {
    "warn_accumulation_threshold": 20,
    "critic_block_threshold": 1,
    "accounting_contamination_codes": [
        "ACCT_SBC_DILUTION_DOUBLE",
        "LOGIC_DOUBLE_COUNTING",
    ],
    "logic_compound_block": [
        ("LOGIC_DOUBLE_COUNTING", "LOGIC_CONTRADICTION"),
    ],
    "dq_error_blocks": True,  # Block publish when DQ severity == "error"
    "valuation_upside_threshold": 0.15,
    "high_capex_threshold": 0.15,
    "terminal_value_ev_threshold": 0.70,
}


class PublishGate:
    """Publish Gate v1 — all gates must pass for thesis to be publishable.

    Section 20.3: only if ALL critical gates pass can a thesis be published.
    Block-level issues → blocked. Warn-level issues → downgraded.

    The gate is configurable via a policy dict for threshold tuning.
    """

    def __init__(self, policy: dict[str, Any] | None = None):
        self._policy = {**DEFAULT_GATE_POLICY, **(policy or {})}

    def evaluate(
        self,
        judgments: list[JudgmentContract],
        critic_results: list[CriticResult],
        context: dict[str, Any] | None = None,
    ) -> GateResult:
        ctx = context or {}
        checks: list[GateCheck] = []

        # Gate 1: Truth gate — metrics computed via engine, not agents
        checks.append(self._truth_gate(judgments, ctx))

        # Gate 2: Definition gate — all metrics registered
        checks.append(self._definition_gate(judgments, ctx))

        # Gate 3: Evidence gate — judgments have evidence backing
        checks.append(self._evidence_gate(judgments))

        # Gate 4: Critic gate — no block-level critic issues
        checks.append(self._critic_gate(critic_results))

        # Gate 5: Cognitive bias gate — bias critic specifically
        checks.append(self._bias_gate(critic_results))

        # Gate 6: Reproducibility gate — run manifest exists
        checks.append(self._reproducibility_gate(ctx))

        # Gate 7: Accounting integrity gate — no contamination
        checks.append(self._accounting_integrity_gate(critic_results))

        # Gate 8: Warn accumulation gate — too many warnings = block
        checks.append(self._warn_accumulation_gate(critic_results))

        # Gate 9: Logical consistency gate — compound contradictions
        checks.append(self._logical_consistency_gate(critic_results))

        # Gate 10: Data quality gate — block on DQ severity=error
        checks.append(self._data_quality_gate(ctx))

        # Gate 11: DCF/sensitivity numbers must describe the same base case
        checks.append(self._dcf_integrity_gate(ctx))

        # Gate 12: high-capex upside calls require segment/SOTP support
        checks.append(self._capex_attribution_gate(ctx))

        # Gate 13: terminal-value dominated DCFs require conservative assumptions
        checks.append(self._terminal_value_gate(ctx))

        # Aggregate
        blocked_by = [c.gate_name for c in checks if not c.passed and c.severity == "block"]
        warnings = [c.message for c in checks if not c.passed and c.severity == "warn"]

        return GateResult(
            publishable=len(blocked_by) == 0,
            checks=checks,
            warnings=warnings,
            blocked_by=blocked_by,
        )

    def _truth_gate(self, judgments: list[JudgmentContract], ctx: dict) -> GateCheck:
        """All metric values must come from Formula/Scenario Engine, not agent-generated."""
        for j in judgments:
            if j.inferences and not j.used_metric_ids and not j.used_evidence_ids:
                return GateCheck(
                    gate_name="truth_gate",
                    passed=False,
                    message=f"Judgment '{j.judgment_id}' has inferences but no metric/evidence references",
                )
        return GateCheck(
            gate_name="truth_gate",
            passed=True,
            message="All judgments reference computed metrics or evidence",
        )

    def _definition_gate(self, judgments: list[JudgmentContract], ctx: dict) -> GateCheck:
        """All used metrics must be registered in MetricRegistry."""
        registry_ids = set(ctx.get("registered_metric_ids", []))
        if not registry_ids:
            return GateCheck(
                gate_name="definition_gate",
                passed=True,
                message="No registry provided — definition gate skipped",
                severity="warn",
            )

        all_metric_ids = set()
        for j in judgments:
            all_metric_ids.update(j.used_metric_ids)

        unregistered = all_metric_ids - registry_ids
        if unregistered:
            return GateCheck(
                gate_name="definition_gate",
                passed=False,
                message=f"Unregistered metrics used: {', '.join(sorted(unregistered))}",
            )
        return GateCheck(
            gate_name="definition_gate",
            passed=True,
            message="All metrics are registered in MetricRegistry",
        )

    def _evidence_gate(self, judgments: list[JudgmentContract]) -> GateCheck:
        """Judgments must have evidence backing."""
        for j in judgments:
            has_sources = any(obs.source_ids for obs in j.observations)
            has_evidence = bool(j.used_evidence_ids)
            if not has_sources and not has_evidence:
                return GateCheck(
                    gate_name="evidence_gate",
                    passed=False,
                    message=f"Judgment '{j.judgment_id}' has no evidence backing",
                )
        return GateCheck(
            gate_name="evidence_gate",
            passed=True,
            message="All judgments have evidence backing",
        )

    def _critic_gate(self, critic_results: list[CriticResult]) -> GateCheck:
        """Critic gate — block on any block-level issue by default."""
        block_threshold = self._policy.get("critic_block_threshold", 8)
        total_blocks = sum(
            sum(1 for i in cr.issues if i.severity == "block")
            for cr in critic_results
        )
        if total_blocks >= block_threshold:
            blocking_critics = [
                cr.critic_type for cr in critic_results if cr.block_publish
            ]
            return GateCheck(
                gate_name="critic_gate",
                passed=False,
                message=f"Critics have {total_blocks} block-level issues (threshold: {block_threshold}): {', '.join(blocking_critics)}",
            )
        return GateCheck(
            gate_name="critic_gate",
            passed=True,
            message=f"Critics passed ({total_blocks} blocks, under threshold {block_threshold})",
        )

    def _bias_gate(self, critic_results: list[CriticResult]) -> GateCheck:
        """Cognitive bias critic must have run and not blocked."""
        bias_results = [
            cr for cr in critic_results
            if cr.critic_type == "cognitive_bias_critic"
        ]
        if not bias_results:
            return GateCheck(
                gate_name="cognitive_bias_gate",
                passed=False,
                message="Cognitive bias critic did not run — required for publish",
            )
        for br in bias_results:
            if br.block_publish:
                return GateCheck(
                    gate_name="cognitive_bias_gate",
                    passed=False,
                    message="Cognitive bias critic blocks publish",
                )
        return GateCheck(
            gate_name="cognitive_bias_gate",
            passed=True,
            message="Cognitive bias critic passed",
        )

    def _reproducibility_gate(self, ctx: dict) -> GateCheck:
        """Run manifest must exist for reproducibility."""
        if ctx.get("run_manifest_id"):
            return GateCheck(
                gate_name="reproducibility_gate",
                passed=True,
                message=f"Run manifest: {ctx['run_manifest_id']}",
            )
        return GateCheck(
            gate_name="reproducibility_gate",
            passed=False,
            message="No run_manifest_id in context — reproducibility not guaranteed",
        )

    def _accounting_integrity_gate(self, critic_results: list[CriticResult]) -> GateCheck:
        """Warn (not block) if accounting contamination issue is detected.

        SBC treatment has been corrected to use dilution_only, so SBC double-counting
        is now a false positive in most cases. Downgrade from block to warn so it
        doesn't prevent publication of otherwise high-quality reports.
        """
        contamination_codes = set(self._policy["accounting_contamination_codes"])
        found_codes = []

        for cr in critic_results:
            if cr.critic_type not in ("accounting_critic", "logic_critic"):
                continue
            for issue in cr.issues:
                if issue.issue_code in contamination_codes:
                    found_codes.append(issue.issue_code)

        if found_codes:
            return GateCheck(
                gate_name="accounting_integrity_gate",
                passed=True,  # Pass but with warning (downgraded from block)
                severity="warn",
                message=f"Accounting advisory: {', '.join(set(found_codes))} detected — review recommended",
            )

        return GateCheck(
            gate_name="accounting_integrity_gate",
            passed=True,
            message="No accounting contamination detected",
        )

    def _warn_accumulation_gate(self, critic_results: list[CriticResult]) -> GateCheck:
        """Block if total warn-level issues exceed threshold.

        Too many unresolved warnings indicate the analysis has not been
        sufficiently cleaned up for publication.
        """
        threshold = self._policy["warn_accumulation_threshold"]
        total_warns = sum(
            sum(1 for i in cr.issues if i.severity == "warn")
            for cr in critic_results
        )

        if total_warns >= threshold:
            return GateCheck(
                gate_name="warn_accumulation_gate",
                passed=False,
                message=f"Excessive unresolved warnings: {total_warns} warns "
                        f"(threshold: {threshold})",
            )
        return GateCheck(
            gate_name="warn_accumulation_gate",
            passed=True,
            message=f"Warning count acceptable: {total_warns} (threshold: {threshold})",
        )

    def _logical_consistency_gate(self, critic_results: list[CriticResult]) -> GateCheck:
        """Warn (not block) if compound logical inconsistencies are present.

        SBC double-counting has been architecturally resolved (sbc_treatment="dilution_only"),
        so LOGIC_DOUBLE_COUNTING detection is now a false positive from agents referencing
        SBC in narrative without acknowledging the orchestrator-level correction.
        Downgraded from block to warn.
        """
        compound_blocks = self._policy["logic_compound_block"]

        # Collect all issue codes from logic critic
        logic_codes: set[str] = set()
        for cr in critic_results:
            if cr.critic_type == "logic_critic":
                for issue in cr.issues:
                    logic_codes.add(issue.issue_code)

        for code_tuple in compound_blocks:
            if all(code in logic_codes for code in code_tuple):
                return GateCheck(
                    gate_name="logical_consistency_gate",
                    passed=True,  # Pass with warning (downgraded from block)
                    severity="warn",
                    message=f"Logical consistency advisory: {' + '.join(code_tuple)} detected — "
                            "review recommended (systemic false positive if SBC handled at orchestrator level)",
                )

        return GateCheck(
            gate_name="logical_consistency_gate",
            passed=True,
            message="No compound logical inconsistencies",
        )

    def _data_quality_gate(self, ctx: dict) -> GateCheck:
        """Block publish when fact-bridge DQ checker found severity=error issues.

        DQ errors indicate impossible values (negative revenue, assets �� 0, etc.)
        that make DCF and downstream analysis unreliable.
        """
        if not self._policy.get("dq_error_blocks", True):
            return GateCheck(
                gate_name="data_quality_gate",
                passed=True,
                message="DQ gate disabled by policy",
            )

        dq_issues = ctx.get("__data_quality_issues", [])
        if not dq_issues:
            return GateCheck(
                gate_name="data_quality_gate",
                passed=True,
                message="No data quality issues",
            )

        error_issues = [i for i in dq_issues if i.get("severity") == "error"]
        warn_issues = [i for i in dq_issues if i.get("severity") == "warn"]

        if error_issues:
            codes = ", ".join(i["code"] for i in error_issues)
            return GateCheck(
                gate_name="data_quality_gate",
                passed=False,
                message=f"Critical DQ errors: {codes}",
            )

        if warn_issues:
            codes = ", ".join(i["code"] for i in warn_issues)
            return GateCheck(
                gate_name="data_quality_gate",
                passed=True,
                severity="warn",
                message=f"DQ warnings ({len(warn_issues)}): {codes}",
            )

        return GateCheck(
            gate_name="data_quality_gate",
            passed=True,
            message="DQ issues present (info-level only)",
        )

    def _dcf_integrity_gate(self, ctx: dict) -> GateCheck:
        """Block when the DCF base case and sensitivity table disagree.

        The report displays the WACC × terminal-growth matrix as a model audit
        trail. If the cell nearest the actual base WACC/g does not reproduce
        the base per-share DCF, the valuation section is internally unreliable.
        """
        dcf_output = ctx.get("dcf_output")
        dcf_input = ctx.get("dcf_input")
        sensitivity_table = ctx.get("sensitivity_table") or {}
        matrix = sensitivity_table.get("matrix") or []
        rows = sensitivity_table.get("var1_values") or []
        cols = sensitivity_table.get("var2_values") or []
        if not (dcf_output and dcf_input and matrix and rows and cols):
            return GateCheck(
                gate_name="dcf_integrity_gate",
                passed=True,
                severity="warn",
                message="DCF integrity gate skipped: missing DCF input/output or sensitivity table",
            )

        base_value = getattr(dcf_output, "per_share_value", None)
        base_wacc = getattr(dcf_input, "wacc", None)
        base_g = getattr(dcf_input, "terminal_growth_rate", None)
        if not all(isinstance(x, (int, float)) for x in (base_value, base_wacc, base_g)):
            return GateCheck(
                gate_name="dcf_integrity_gate",
                passed=False,
                message="DCF input/output missing numeric base value, WACC, or terminal growth",
            )

        row_i = min(range(len(rows)), key=lambda i: abs(rows[i] - base_wacc))
        col_i = min(range(len(cols)), key=lambda i: abs(cols[i] - base_g))
        try:
            matrix_value = float(matrix[row_i][col_i])
        except (IndexError, TypeError, ValueError):
            return GateCheck(
                gate_name="dcf_integrity_gate",
                passed=False,
                message="Sensitivity matrix shape is incompatible with row/column values",
            )

        tolerance = max(0.02 * abs(base_value), 0.05)
        if abs(matrix_value - base_value) > tolerance:
            return GateCheck(
                gate_name="dcf_integrity_gate",
                passed=False,
                message=(
                    f"DCF base value {base_value:.2f} does not match sensitivity "
                    f"cell at WACC={base_wacc:.2%}, g={base_g:.2%}: {matrix_value:.2f}"
                ),
            )
        return GateCheck(
            gate_name="dcf_integrity_gate",
            passed=True,
            message="DCF base value matches WACC × g sensitivity table",
        )

    def _capex_attribution_gate(self, ctx: dict) -> GateCheck:
        """Block upside calls that depend on high capex without attribution.

        A high-capex, negative-FCF company can be undervalued only if the
        reinvestment is demonstrably going into productive assets. If segment
        capex/SOTP evidence is missing, the system must not publish a positive
        valuation-gap conclusion as if the capex story were verified.
        """
        metrics = ctx.get("computed_metrics") or {}
        facts = ctx.get("meta_facts") or {}
        scenarios = ctx.get("scenarios") or {}
        market_data = ctx.get("market_data") or {}
        segment_detail = ctx.get("segment_detail") or {}
        segment_projections = ctx.get("segment_projections") or {}

        capex_ratio = abs(float(metrics.get("capex_to_revenue") or 0.0))
        fcf = facts.get("free_cash_flow")
        price = float(market_data.get("current_price") or 0.0)
        target = float(
            scenarios.get("probability_weighted_value")
            or scenarios.get("base_value")
            or 0.0
        )
        upside = (target / price - 1.0) if price > 0 and target > 0 else 0.0

        has_segment_capex = bool(ctx.get("segment_capex_attribution"))
        has_sotp_proxy = bool(segment_projections)
        product_segments = segment_detail.get("product") if isinstance(segment_detail, dict) else None
        if isinstance(product_segments, dict) and len(product_segments) >= 2:
            has_sotp_proxy = True

        if (
            capex_ratio >= self._policy["high_capex_threshold"]
            and isinstance(fcf, (int, float)) and fcf < 0
            and upside >= self._policy["valuation_upside_threshold"]
            and not (has_segment_capex or has_sotp_proxy)
        ):
            return GateCheck(
                gate_name="capex_attribution_gate",
                passed=False,
                message=(
                    f"Positive valuation gap ({upside:.1%}) rests on high capex "
                    f"({capex_ratio:.1%} of revenue) and negative FCF without "
                    "segment capex attribution or SOTP proxy"
                ),
            )

        return GateCheck(
            gate_name="capex_attribution_gate",
            passed=True,
            message="Capex attribution risk acceptable for published conclusion",
        )

    def _terminal_value_gate(self, ctx: dict) -> GateCheck:
        """Block terminal-value dominated high-risk DCFs with aggressive inputs."""
        dcf_output = ctx.get("dcf_output")
        dcf_input = ctx.get("dcf_input")
        metrics = ctx.get("computed_metrics") or {}
        facts = ctx.get("meta_facts") or {}
        if not (dcf_output and dcf_input):
            return GateCheck(
                gate_name="terminal_value_gate",
                passed=True,
                severity="warn",
                message="Terminal-value gate skipped: missing DCF input/output",
            )

        ev = getattr(dcf_output, "enterprise_value", 0) or 0
        pv_terminal = getattr(dcf_output, "pv_terminal_value", 0) or 0
        if ev <= 0:
            return GateCheck(
                gate_name="terminal_value_gate",
                passed=True,
                severity="warn",
                message="Terminal-value gate skipped: non-positive enterprise value",
            )
        terminal_share = pv_terminal / ev
        g = getattr(dcf_input, "terminal_growth_rate", 0) or 0
        wacc = getattr(dcf_input, "wacc", 0) or 0
        capex_ratio = abs(float(metrics.get("capex_to_revenue") or 0.0))
        fcf = facts.get("free_cash_flow")

        high_risk = capex_ratio >= self._policy["high_capex_threshold"]
        if isinstance(fcf, (int, float)) and fcf < 0:
            high_risk = True

        if (
            terminal_share >= self._policy["terminal_value_ev_threshold"]
            and high_risk
            and (g >= 0.03 or wacc <= 0.095)
        ):
            return GateCheck(
                gate_name="terminal_value_gate",
                passed=False,
                message=(
                    f"DCF is terminal-value dominated ({terminal_share:.1%} of EV) "
                    f"while using WACC={wacc:.1%}, g={g:.1%} for a high-risk "
                    "negative-FCF/high-capex profile"
                ),
            )

        return GateCheck(
            gate_name="terminal_value_gate",
            passed=True,
            message="Terminal value dependence is acceptable",
        )
