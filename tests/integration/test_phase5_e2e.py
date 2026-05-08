"""Phase 5 End-to-End Integration Test.

Validates:
  Eval Engine (Layers A-E) → Regression Harness → Backtesting
  → Confidence Calibration → Golden Cases → Edge Hit Rate

Exit criteria (Section 32):
  - Confidence bucket 有经验精度 (empirical precision from post-mortems)
"""

import pytest
from datetime import date, datetime, timezone

from aegis.data_contracts.common import ConfidenceBucket, EdgeType
from aegis.data_contracts.postmortem_schema import PostMortem

from aegis.core.evals import (
    EvalEngine,
    RegressionHarness,
    RegressionCase,
    BacktestingFramework,
    GoldenCaseRegistry,
    GoldenCase,
    ERROR_TAXONOMY,
    classify_error,
)
from aegis.core.agents.base import AgentInput
from aegis.core.agents.accounting_analyst.agent import AccountingAnalyst
from aegis.core.agents.business_analyst.agent import BusinessAnalyst
from aegis.core.critics import LogicCritic, CognitiveBiasCritic
from aegis.core.decision_engine import DecisionEngine
from aegis.core.publish_gate import PublishGate
from aegis.data_contracts.edge_assessment_schema import EdgeAssessment
from aegis.data_contracts.judgment_schema import (
    JudgmentContract, Observation, Inference, Counterargument,
    DisconfirmingTrigger, CognitiveBiasSelfCheck,
)


# ──────────────────────── Eval Engine Tests ────────────────────────

class TestEvalEngine:
    def test_layer_a_deterministic_correctness(self):
        engine = EvalEngine()
        facts = [
            {"source_hash": "sha256:" + "a" * 64, "value": 100},
            {"source_hash": "sha256:" + "b" * 64, "value": 200},
        ]
        metrics = {"gross_margin": 0.81, "roic": 0.25}
        checks = engine.evaluate_layer_a(facts, metrics, {"pit_violations": 0, "currency_errors": 0})
        assert all(c.passed for c in checks)
        assert all(c.layer == "A" for c in checks)

    def test_layer_a_catches_pit_violation(self):
        engine = EvalEngine()
        checks = engine.evaluate_layer_a([], {}, {"pit_violations": 2, "currency_errors": 0})
        pit_check = next(c for c in checks if c.check_name == "pit_integrity")
        assert not pit_check.passed
        assert pit_check.score == 0.0

    def test_layer_b_research_integrity(self):
        engine = EvalEngine()
        inp = AgentInput(entity_id="meta", run_id="run_eval", question_id="q",
            metric_results={"gross_margin": 0.81, "accruals_ratio": 0.05, "cfo_to_net_income": 1.2,
                            "sbc_to_revenue": 0.12, "dilution_rate": 0.02})
        j = AccountingAnalyst().run(inp).judgment
        critics = [LogicCritic().review([j]), CognitiveBiasCritic().review([j])]
        checks = engine.evaluate_layer_b([j], critics, {})
        assert all(c.layer == "B" for c in checks)
        bias_check = next(c for c in checks if c.check_name == "bias_detection_coverage")
        assert bias_check.passed

    def test_layer_c_investment_usefulness(self):
        from types import SimpleNamespace
        td = SimpleNamespace(
            bear_case_value=420, base_case_value=580, bull_case_value=750,
            edge_assessment=SimpleNamespace(primary_edge_type="analytical"),
            kill_criteria=[{"description": "Revenue miss"}],
            monitorables=[{"description": "Margin decline"}, {"description": "Customer loss"}],
        )
        engine = EvalEngine()
        checks = engine.evaluate_layer_c(td, {"has_priced_in": True})
        assert all(c.layer == "C" for c in checks)
        priced_in = next(c for c in checks if c.check_name == "priced_in_interpretation")
        assert priced_in.passed

    def test_layer_d_calibration_precision(self):
        engine = EvalEngine()
        calibration = {
            "bucket_outcomes": {
                "high": [
                    {"survived": True}, {"survived": True}, {"survived": True},
                    {"survived": False}, {"survived": False},
                    {"survived": True}, {"survived": True}, {"survived": True},
                    {"survived": True}, {"survived": False},
                ],  # 70% survival, target 65%, deviation = 0.05
                "medium": [
                    {"survived": True}, {"survived": True}, {"survived": True},
                    {"survived": False}, {"survived": False},
                    {"survived": False}, {"survived": True}, {"survived": False},
                    {"survived": False}, {"survived": True},
                ],  # 50% survival, target 50%, deviation = 0.00
            }
        }
        checks = engine.evaluate_layer_d(calibration)
        assert len(checks) == 2
        high_check = next(c for c in checks if "high" in c.check_name)
        assert high_check.passed  # 70% vs 65% target, deviation=0.05 < 0.15

    def test_layer_e_backtesting(self):
        engine = EvalEngine()
        backtest = [
            {"thesis_survived": True, "variant_realized": True, "edge_realized": True},
            {"thesis_survived": True, "variant_realized": False, "edge_realized": True},
            {"thesis_survived": False, "variant_realized": False, "edge_realized": False},
            {"thesis_survived": True, "variant_realized": True, "edge_realized": False},
        ]
        checks = engine.evaluate_layer_e(backtest)
        survival = next(c for c in checks if c.check_name == "thesis_survival_rate")
        assert survival.score == 0.75  # 3/4

    def test_full_eval_pipeline(self):
        engine = EvalEngine()
        inp = AgentInput(entity_id="meta", run_id="run_eval", question_id="q",
            metric_results={"gross_margin": 0.81, "accruals_ratio": 0.05,
                            "cfo_to_net_income": 1.2, "sbc_to_revenue": 0.12, "dilution_rate": 0.02})
        j = AccountingAnalyst().run(inp).judgment
        critics = [CognitiveBiasCritic().review([j])]

        from types import SimpleNamespace
        td = SimpleNamespace(
            bear_case_value=420, base_case_value=580, bull_case_value=750,
            edge_assessment=True, kill_criteria=[{"x": 1}],
            monitorables=[{"x": 1}],
        )

        result = engine.run_full_eval(
            run_id="run_eval",
            facts=[{"source_hash": "sha256:" + "a" * 64}],
            metrics={"gross_margin": 0.81},
            judgments=[j],
            critic_results=critics,
            thesis_decision=td,
            context={"pit_violations": 0, "currency_errors": 0, "has_priced_in": True},
        )
        assert result.overall_score > 0.5
        scores = result.layer_scores
        assert "A" in scores
        assert "B" in scores
        assert "C" in scores


# ──────────────────────── Regression Harness Tests ────────────────────────

class TestRegressionHarness:
    def test_register_and_run_cases(self):
        harness = RegressionHarness()
        harness.register_case(RegressionCase(
            case_id="reg_001", description="Gross margin formula",
            category="deterministic", runner=lambda: True,
        ))
        harness.register_case(RegressionCase(
            case_id="reg_002", description="Double counting detection",
            category="critic", runner=lambda: True,
        ))
        result = harness.run_suite("suite_001")
        assert result.all_passed
        assert result.total == 2
        assert result.passed == 2

    def test_failed_case_captured(self):
        harness = RegressionHarness()
        harness.register_case(RegressionCase(
            case_id="reg_fail", description="Intentional failure",
            category="deterministic", runner=lambda: False,
        ))
        result = harness.run_suite()
        assert not result.all_passed
        assert result.failed == 1
        assert result.failures[0]["case_id"] == "reg_fail"

    def test_exception_captured(self):
        def bad_runner():
            raise ValueError("Something broke")

        harness = RegressionHarness()
        harness.register_case(RegressionCase(
            case_id="reg_exc", description="Exception case",
            category="deterministic", runner=bad_runner,
        ))
        result = harness.run_suite()
        assert not result.all_passed
        assert "Something broke" in result.failures[0]["error"]

    def test_compare_runs(self):
        harness = RegressionHarness()
        harness.register_case(RegressionCase("a", "test a", "x", lambda: True))
        harness.register_case(RegressionCase("b", "test b", "x", lambda: False))
        harness.run_suite("run1")

        # Simulate fix: now b passes too
        harness._cases[1] = RegressionCase("b", "test b", "x", lambda: True)
        harness.register_case(RegressionCase("c", "test c", "x", lambda: False))
        harness.run_suite("run2")

        comparison = harness.compare_runs("run1", "run2")
        assert "b" in comparison["fixed"]
        assert "c" in comparison["new_failures"]

    def test_run_by_category(self):
        harness = RegressionHarness()
        harness.register_case(RegressionCase("d1", "det", "deterministic", lambda: True))
        harness.register_case(RegressionCase("c1", "crit", "critic", lambda: True))
        result = harness.run_category("deterministic")
        assert result.total == 1


# ──────────────────────── Error Taxonomy Tests ────────────────────────

class TestErrorTaxonomy:
    def test_taxonomy_has_20_labels(self):
        assert len(ERROR_TAXONOMY) == 20

    def test_classify_double_counting(self):
        labels = classify_error("Double counting of SBC and dilution in valuation")
        assert "double_counting" in labels

    def test_classify_pit_leakage(self):
        labels = classify_error("Used future data from next quarter in model")
        assert "pit_leakage" in labels

    def test_classify_confirmation_bias(self):
        labels = classify_error("Only supporting evidence was cited, ignored contrary signals")
        assert "confirmation_bias_error" in labels

    def test_classify_narrative_fallacy(self):
        labels = classify_error("Inference based on narrative with no evidence backing")
        assert "narrative_fallacy_error" in labels

    def test_classify_unknown(self):
        labels = classify_error("Something completely unrelated happened")
        assert "unclassified" in labels


# ──────────────────────── Backtesting Framework Tests ────────────────────────

class TestBacktestingFramework:
    def _make_postmortem(
        self, thesis_id: str, bucket: str, edge: str,
        survived: bool, variant: bool, edge_realized: bool,
        errors: list[str] | None = None,
    ) -> PostMortem:
        return PostMortem(
            postmortem_id=f"pm_{thesis_id}",
            thesis_id=thesis_id,
            thesis_version=1,
            original_thesis_date=date(2025, 10, 1),
            review_date=date(2026, 4, 1),
            price_at_thesis=100.0,
            price_at_review=120.0 if survived else 80.0,
            total_return=0.20 if survived else -0.20,
            thesis_survived=survived,
            variant_realized=variant,
            edge_type=EdgeType(edge),
            edge_realized=edge_realized,
            bias_warnings_at_publish=[],
            what_was_right=["Growth assumption"],
            what_was_wrong=["Timing assumption"],
            error_taxonomy_labels=errors or [],
            original_confidence_bucket=ConfidenceBucket(bucket),
            original_run_id="run_backtest",
        )

    def test_confidence_bucket_calibration(self):
        """EXIT CRITERIA: confidence bucket has empirical precision."""
        bt = BacktestingFramework()

        # Simulate 25 post-mortems per bucket for calibration
        # High confidence: 80% survival (target ~65%)
        for i in range(25):
            bt.record_postmortem(self._make_postmortem(
                f"th_high_{i}", "high", "analytical",
                survived=i < 20, variant=i < 15, edge_realized=i < 18,
            ))

        # Medium confidence: 52% survival (target ~50%)
        for i in range(25):
            bt.record_postmortem(self._make_postmortem(
                f"th_med_{i}", "medium", "behavioral",
                survived=i < 13, variant=i < 8, edge_realized=i < 10,
            ))

        # Low confidence: 32% survival (target ~35%)
        for i in range(25):
            bt.record_postmortem(self._make_postmortem(
                f"th_low_{i}", "low", "informational",
                survived=i < 8, variant=i < 5, edge_realized=i < 6,
            ))

        # Verify calibration
        report = bt.generate_calibration_report()
        assert report.total_postmortems == 75

        # Bucket precision
        high_precision = bt.get_bucket_precision("high")
        med_precision = bt.get_bucket_precision("medium")
        low_precision = bt.get_bucket_precision("low")

        assert high_precision is not None
        assert med_precision is not None
        assert low_precision is not None

        # Monotonicity: high > medium > low
        assert high_precision > med_precision > low_precision, \
            f"Bucket precision not monotonic: high={high_precision}, med={med_precision}, low={low_precision}"

        # Calibration basis should reflect calibrated status
        assert bt.get_confidence_basis("high").startswith("calibrated")
        assert bt.get_confidence_basis("medium").startswith("calibrated")
        assert bt.get_confidence_basis("low").startswith("calibrated")

        # Uncalibrated bucket
        assert bt.get_confidence_basis("very_high") == "not_calibrated"

    def test_edge_hit_rate_by_type(self):
        """Edge type hit rate tracking."""
        bt = BacktestingFramework()

        # Analytical edges: 70% hit rate
        for i in range(10):
            bt.record_postmortem(self._make_postmortem(
                f"th_ana_{i}", "medium", "analytical",
                survived=True, variant=i < 7, edge_realized=i < 7,
            ))

        # Behavioral edges: 40% hit rate
        for i in range(10):
            bt.record_postmortem(self._make_postmortem(
                f"th_beh_{i}", "medium", "behavioral",
                survived=True, variant=i < 4, edge_realized=i < 4,
            ))

        ana_rate = bt.get_edge_hit_rate("analytical")
        beh_rate = bt.get_edge_hit_rate("behavioral")
        assert ana_rate == 0.7
        assert beh_rate == 0.4

        report = bt.generate_calibration_report()
        assert "analytical" in report.edge_stats
        assert "behavioral" in report.edge_stats

    def test_error_frequency_tracking(self):
        bt = BacktestingFramework()
        bt.record_postmortem(self._make_postmortem(
            "th_err_1", "medium", "analytical", True, True, True,
            errors=["overconfidence_error", "anchoring_error"],
        ))
        bt.record_postmortem(self._make_postmortem(
            "th_err_2", "low", "behavioral", False, False, False,
            errors=["overconfidence_error", "narrative_fallacy_error"],
        ))

        ranking = bt.get_error_ranking()
        assert ranking[0] == ("overconfidence_error", 2)
        report = bt.generate_calibration_report()
        assert report.error_frequency["overconfidence_error"] == 2


# ──────────────────────── Golden Cases Tests ────────────────────────

class TestGoldenCases:
    def test_golden_case_registry(self):
        reg = GoldenCaseRegistry()
        reg.register(GoldenCase(
            case_id="gc_us_saas", description="US SaaS mega-cap",
            category="sector", entity_type="mega_cap",
            accounting_standard="US_GAAP", test_fn=lambda: True,
        ))
        reg.register(GoldenCase(
            case_id="gc_cn_vie", description="China VIE structure",
            category="sector", entity_type="vie",
            accounting_standard="CAS", test_fn=lambda: True,
        ))
        reg.register(GoldenCase(
            case_id="gc_err_double_count", description="Double counting injection",
            category="error_injection", entity_type="mega_cap",
            accounting_standard="US_GAAP", test_fn=lambda: True,
            expected_critic_codes=["LOGIC_DOUBLE_COUNTING"],
        ))

        assert len(reg.list_cases()) == 3
        assert len(reg.list_cases("sector")) == 2
        assert len(reg.list_cases("error_injection")) == 1

    def test_golden_case_error_injection(self):
        """Golden case: error injection for double counting — critic must catch."""
        from aegis.core.critics import LogicCritic

        def double_count_test() -> bool:
            j = JudgmentContract(
                judgment_id="j_gc_double", agent_name="test", agent_version="0.1.0",
                question_id="q", run_id="r",
                observations=[
                    Observation(text="SBC is 15%", source_ids=["f1"]),
                    Observation(text="Dilution 3%", source_ids=["f2"]),
                ],
                inferences=[Inference(text="High cost", based_on_observation_indices=[0, 1], confidence="high")],
                counterarguments=[Counterargument(text="Market practice", strength="weak")],
                disconfirming_triggers=[DisconfirmingTrigger(text="Policy change")],
                used_metric_ids=["sbc_to_revenue", "dilution_rate"],
                cognitive_bias_self_check=CognitiveBiasSelfCheck(
                    anchoring_risk="low", confirmation_bias_risk="low",
                    recency_bias_risk="low", narrative_fallacy_risk="low",
                ),
                judgment_status="complete",
            )
            result = LogicCritic().review([j])
            return "LOGIC_DOUBLE_COUNTING" in [i.issue_code for i in result.issues]

        reg = GoldenCaseRegistry()
        reg.register(GoldenCase(
            case_id="gc_err_double", description="Double counting injection",
            category="error_injection", entity_type="mega_cap",
            accounting_standard="US_GAAP", test_fn=double_count_test,
            expected_critic_codes=["LOGIC_DOUBLE_COUNTING"],
        ))
        results = reg.run_all()
        assert results["gc_err_double"] is True

    def test_golden_case_bias_injection(self):
        """Golden case: confirmation bias injection — bias critic must catch."""
        from aegis.core.critics import CognitiveBiasCritic

        def bias_test() -> bool:
            j = JudgmentContract(
                judgment_id="j_gc_bias", agent_name="test", agent_version="0.1.0",
                question_id="q", run_id="r",
                observations=[Observation(text=f"Support {i}", source_ids=[f"f{i}"]) for i in range(8)],
                inferences=[Inference(text="Great", based_on_observation_indices=[0], confidence="high")],
                counterarguments=[],
                disconfirming_triggers=[],
                used_metric_ids=["revenue"],
                cognitive_bias_self_check=CognitiveBiasSelfCheck(
                    anchoring_risk="low", confirmation_bias_risk="low",
                    recency_bias_risk="low", narrative_fallacy_risk="low",
                ),
                judgment_status="complete",
            )
            result = CognitiveBiasCritic().review([j])
            return result.block_publish  # Should block due to zero counterarguments

        reg = GoldenCaseRegistry()
        reg.register(GoldenCase(
            case_id="gc_err_bias", description="Confirmation bias injection",
            category="bias_injection", entity_type="mega_cap",
            accounting_standard="US_GAAP", test_fn=bias_test,
            expected_critic_codes=["COGNITIVE_CONFIRMATION"],
        ))
        results = reg.run_all()
        assert results["gc_err_bias"] is True

    def test_golden_case_cross_standard(self):
        """Golden case: cross-standard comparison without bridge."""
        from aegis.core.critics import CrossEntityCritic

        def cross_standard_test() -> bool:
            j = JudgmentContract(
                judgment_id="j_gc_cross", agent_name="test", agent_version="0.1.0",
                question_id="q", run_id="r",
                observations=[Observation(text="Margins compared", source_ids=["f1"])],
                inferences=[Inference(text="A beats B", based_on_observation_indices=[0], confidence="medium")],
                counterarguments=[Counterargument(text="Different mix", strength="moderate")],
                disconfirming_triggers=[DisconfirmingTrigger(text="Reverse")],
                cognitive_bias_self_check=CognitiveBiasSelfCheck(
                    anchoring_risk="low", confirmation_bias_risk="low",
                    recency_bias_risk="low", narrative_fallacy_risk="low",
                ),
                judgment_status="complete",
            )
            result = CrossEntityCritic().review([j], context={
                "entity_standards": {"entity_a": "US_GAAP", "entity_b": "IFRS"},
            })
            return "CROSS_ENTITY_NO_BRIDGE" in [i.issue_code for i in result.issues]

        reg = GoldenCaseRegistry()
        reg.register(GoldenCase(
            case_id="gc_err_cross", description="Cross-standard without bridge",
            category="error_injection", entity_type="multi",
            accounting_standard="mixed", test_fn=cross_standard_test,
        ))
        assert reg.run_all()["gc_err_cross"] is True

    def test_coverage_summary(self):
        reg = GoldenCaseRegistry()
        for i, cat in enumerate(["sector", "sector", "error_injection", "bias_injection", "multi_entity"]):
            reg.register(GoldenCase(
                case_id=f"gc_{i}", description=f"Case {i}",
                category=cat, entity_type="test",
                accounting_standard="US_GAAP", test_fn=lambda: True,
            ))
        summary = reg.coverage_summary
        assert summary["sector"] == 2
        assert summary["error_injection"] == 1


# ──────────────────────── Integrated Regression Suite ────────────────────────

class TestIntegratedRegression:
    def test_full_regression_suite_with_golden_cases(self):
        """Run golden cases as regression suite."""
        from aegis.core.critics import LogicCritic, CognitiveBiasCritic

        def make_double_count_case():
            j = JudgmentContract(
                judgment_id="j_reg_dc", agent_name="test", agent_version="0.1.0",
                question_id="q", run_id="r",
                observations=[Observation(text="SBC", source_ids=["f1"]), Observation(text="Dilution", source_ids=["f2"])],
                inferences=[Inference(text="Cost", based_on_observation_indices=[0, 1], confidence="high")],
                counterarguments=[Counterargument(text="Normal", strength="weak")],
                disconfirming_triggers=[DisconfirmingTrigger(text="Change")],
                used_metric_ids=["sbc_to_revenue", "dilution_rate"],
                cognitive_bias_self_check=CognitiveBiasSelfCheck(
                    anchoring_risk="low", confirmation_bias_risk="low",
                    recency_bias_risk="low", narrative_fallacy_risk="low",
                ),
                judgment_status="complete",
            )
            return "LOGIC_DOUBLE_COUNTING" in [i.issue_code for i in LogicCritic().review([j]).issues]

        harness = RegressionHarness()
        harness.register_cases([
            RegressionCase("reg_dc", "Double counting detection", "critic", make_double_count_case),
            RegressionCase("reg_formula", "Gross margin formula", "deterministic", lambda: True),
            RegressionCase("reg_pit", "PIT integrity check", "deterministic", lambda: True),
        ])

        result = harness.run_suite("regression_v1")
        assert result.all_passed
        assert result.success_rate == 1.0
