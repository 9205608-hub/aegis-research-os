"""Eval Engine — Section 26.2.

Five-layer evaluation framework:
  Layer A: Deterministic Correctness
  Layer B: Research Integrity
  Layer C: Investment Usefulness
  Layer D: Calibration
  Layer E: Backtesting
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class EvalCheck:
    """A single evaluation check result."""

    layer: str  # "A" through "E"
    check_name: str
    passed: bool
    score: float  # 0.0 to 1.0
    details: str = ""


@dataclass
class EvalResult:
    """Aggregate evaluation result across all layers."""

    run_id: str
    checks: list[EvalCheck] = field(default_factory=list)

    @property
    def layer_scores(self) -> dict[str, float]:
        scores: dict[str, list[float]] = {}
        for c in self.checks:
            scores.setdefault(c.layer, []).append(c.score)
        return {layer: sum(s) / len(s) for layer, s in scores.items()}

    @property
    def overall_score(self) -> float:
        if not self.checks:
            return 0.0
        return sum(c.score for c in self.checks) / len(self.checks)

    @property
    def all_passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def failed_checks(self) -> list[EvalCheck]:
        return [c for c in self.checks if not c.passed]


class EvalEngine:
    """Layered evaluation engine for research quality assessment.

    Section 26.4: no new agent goes live without eval.
    """

    def evaluate_layer_a(
        self, facts: list[dict], metrics: dict, context: dict
    ) -> list[EvalCheck]:
        """Layer A: Deterministic Correctness."""
        checks: list[EvalCheck] = []

        # A1: All facts have source hashes
        facts_with_hash = sum(1 for f in facts if f.get("source_hash", "").startswith("sha256:"))
        total_facts = len(facts) if facts else 1
        checks.append(EvalCheck(
            layer="A", check_name="fact_source_traceability",
            passed=facts_with_hash == len(facts),
            score=facts_with_hash / total_facts,
            details=f"{facts_with_hash}/{len(facts)} facts have source hashes",
        ))

        # A2: All metrics computed by engine (not agent-generated)
        engine_computed = sum(1 for v in metrics.values() if isinstance(v, (int, float)))
        total_metrics = len(metrics) if metrics else 1
        checks.append(EvalCheck(
            layer="A", check_name="formula_correctness",
            passed=engine_computed == len(metrics),
            score=engine_computed / total_metrics,
            details=f"{engine_computed}/{len(metrics)} metrics are numeric (engine-computed)",
        ))

        # A3: PIT integrity — no future data
        pit_violations = context.get("pit_violations", 0)
        checks.append(EvalCheck(
            layer="A", check_name="pit_integrity",
            passed=pit_violations == 0,
            score=1.0 if pit_violations == 0 else 0.0,
            details=f"{pit_violations} PIT violations found",
        ))

        # A4: Currency consistency
        currency_errors = context.get("currency_errors", 0)
        checks.append(EvalCheck(
            layer="A", check_name="currency_consistency",
            passed=currency_errors == 0,
            score=1.0 if currency_errors == 0 else 0.0,
            details=f"{currency_errors} currency conversion errors",
        ))

        return checks

    def evaluate_layer_b(
        self, judgments: list, critic_results: list, context: dict
    ) -> list[EvalCheck]:
        """Layer B: Research Integrity."""
        checks: list[EvalCheck] = []

        # B1: All judgments have evidence backing
        with_evidence = sum(
            1 for j in judgments
            if getattr(j, 'used_evidence_ids', []) or
            any(obs.source_ids for obs in getattr(j, 'observations', []))
        )
        total = len(judgments) if judgments else 1
        checks.append(EvalCheck(
            layer="B", check_name="claim_evidence_binding",
            passed=with_evidence == len(judgments),
            score=with_evidence / total,
            details=f"{with_evidence}/{len(judgments)} judgments have evidence",
        ))

        # B2: Critic catch rate
        total_issues = sum(len(cr.issues) for cr in critic_results)
        block_issues = sum(
            sum(1 for i in cr.issues if i.severity == "block")
            for cr in critic_results
        )
        checks.append(EvalCheck(
            layer="B", check_name="critic_catch_rate",
            passed=block_issues == 0,
            score=1.0 if block_issues == 0 else max(0, 1.0 - block_issues * 0.2),
            details=f"{total_issues} total issues, {block_issues} blocks",
        ))

        # B3: Bias detection
        bias_results = [cr for cr in critic_results if cr.critic_type == "cognitive_bias_critic"]
        bias_ran = len(bias_results) > 0
        checks.append(EvalCheck(
            layer="B", check_name="bias_detection_coverage",
            passed=bias_ran,
            score=1.0 if bias_ran else 0.0,
            details="Bias critic ran" if bias_ran else "Bias critic missing",
        ))

        # B4: Counterargument quality
        total_counter = sum(len(getattr(j, 'counterarguments', [])) for j in judgments)
        checks.append(EvalCheck(
            layer="B", check_name="counterargument_quality",
            passed=total_counter >= len(judgments),
            score=min(1.0, total_counter / max(len(judgments), 1)),
            details=f"{total_counter} counterarguments across {len(judgments)} judgments",
        ))

        return checks

    def evaluate_layer_c(self, thesis_decision: Any, context: dict) -> list[EvalCheck]:
        """Layer C: Investment Usefulness."""
        checks: list[EvalCheck] = []

        # C1: Priced-in interpretation exists
        has_priced_in = context.get("has_priced_in", False)
        checks.append(EvalCheck(
            layer="C", check_name="priced_in_interpretation",
            passed=has_priced_in,
            score=1.0 if has_priced_in else 0.0,
        ))

        # C2: Scenario discrimination — bear/base/bull spread
        td = thesis_decision
        bear = getattr(td, 'bear_case_value', None)
        bull = getattr(td, 'bull_case_value', None)
        if bear and bull and bear > 0:
            spread = (bull - bear) / bear
            sufficient = spread >= 0.20
            checks.append(EvalCheck(
                layer="C", check_name="scenario_discrimination",
                passed=sufficient,
                score=min(1.0, spread / 0.50),
                details=f"Bull/bear spread: {spread:.0%}",
            ))

        # C3: Edge assessment exists
        has_edge = getattr(td, 'edge_assessment', None) is not None
        checks.append(EvalCheck(
            layer="C", check_name="edge_assessment_quality",
            passed=has_edge,
            score=1.0 if has_edge else 0.0,
        ))

        # C4: Kill criteria defined
        kill_criteria = getattr(td, 'kill_criteria', [])
        checks.append(EvalCheck(
            layer="C", check_name="kill_criteria_defined",
            passed=len(kill_criteria) > 0,
            score=min(1.0, len(kill_criteria) / 3),
            details=f"{len(kill_criteria)} kill criteria",
        ))

        # C5: Monitorables defined
        monitorables = getattr(td, 'monitorables', [])
        checks.append(EvalCheck(
            layer="C", check_name="monitorables_defined",
            passed=len(monitorables) > 0,
            score=min(1.0, len(monitorables) / 5),
            details=f"{len(monitorables)} monitorables",
        ))

        return checks

    def evaluate_layer_d(self, calibration_data: dict) -> list[EvalCheck]:
        """Layer D: Calibration — requires historical data."""
        checks: list[EvalCheck] = []

        # D1: Confidence bucket precision
        bucket_data = calibration_data.get("bucket_outcomes", {})
        for bucket, outcomes in bucket_data.items():
            if not outcomes:
                continue
            survived = sum(1 for o in outcomes if o.get("survived", False))
            total = len(outcomes)
            precision = survived / total if total > 0 else 0.0

            # Expected precision ranges
            expected = {"very_low": 0.20, "low": 0.35, "medium": 0.50,
                        "high": 0.65, "very_high": 0.80}
            target = expected.get(bucket, 0.50)
            deviation = abs(precision - target)

            checks.append(EvalCheck(
                layer="D", check_name=f"bucket_precision_{bucket}",
                passed=deviation < 0.15,
                score=max(0, 1.0 - deviation),
                details=f"{bucket}: {precision:.0%} survival rate (target: {target:.0%})",
            ))

        if not checks:
            checks.append(EvalCheck(
                layer="D", check_name="calibration_data_available",
                passed=False, score=0.0,
                details="No calibration data available yet",
            ))

        return checks

    def evaluate_layer_e(self, backtest_results: list[dict]) -> list[EvalCheck]:
        """Layer E: Backtesting — requires completed post-mortems."""
        checks: list[EvalCheck] = []

        if not backtest_results:
            checks.append(EvalCheck(
                layer="E", check_name="backtest_data_available",
                passed=False, score=0.0,
                details="No backtest data available yet",
            ))
            return checks

        # E1: Thesis survival rate
        survived = sum(1 for r in backtest_results if r.get("thesis_survived"))
        total = len(backtest_results)
        survival_rate = survived / total
        checks.append(EvalCheck(
            layer="E", check_name="thesis_survival_rate",
            passed=survival_rate > 0.40,
            score=survival_rate,
            details=f"{survived}/{total} theses survived ({survival_rate:.0%})",
        ))

        # E2: Variant realization rate
        variant_realized = sum(1 for r in backtest_results if r.get("variant_realized"))
        variant_rate = variant_realized / total
        checks.append(EvalCheck(
            layer="E", check_name="variant_realization_rate",
            passed=variant_rate > 0.30,
            score=variant_rate,
            details=f"{variant_realized}/{total} variants realized ({variant_rate:.0%})",
        ))

        # E3: Edge hit rate
        edge_realized = sum(1 for r in backtest_results if r.get("edge_realized"))
        edge_rate = edge_realized / total
        checks.append(EvalCheck(
            layer="E", check_name="edge_hit_rate",
            passed=edge_rate > 0.30,
            score=edge_rate,
            details=f"{edge_realized}/{total} edges realized ({edge_rate:.0%})",
        ))

        return checks

    def run_full_eval(
        self,
        run_id: str,
        facts: list[dict],
        metrics: dict,
        judgments: list,
        critic_results: list,
        thesis_decision: Any,
        context: dict,
        calibration_data: dict | None = None,
        backtest_results: list[dict] | None = None,
    ) -> EvalResult:
        """Run all evaluation layers."""
        result = EvalResult(run_id=run_id)
        result.checks.extend(self.evaluate_layer_a(facts, metrics, context))
        result.checks.extend(self.evaluate_layer_b(judgments, critic_results, context))
        result.checks.extend(self.evaluate_layer_c(thesis_decision, context))
        if calibration_data:
            result.checks.extend(self.evaluate_layer_d(calibration_data))
        if backtest_results:
            result.checks.extend(self.evaluate_layer_e(backtest_results))
        return result
