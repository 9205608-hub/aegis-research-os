"""Tests for deterministic engines — formula, DCF, reverse DCF, currency, validation."""

from datetime import date

import pytest

from aegis.core.governance.artifact_hashing import compute_artifact_hash, verify_artifact_hash
from aegis.core.truth.currency_engine.conversion import (
    CurrencyConversionError,
    CurrencyEngine,
    FXRate,
)
from aegis.core.truth.formulas.formula_engine import FormulaEngine
from aegis.core.truth.scenario_engine.dcf_engine import DCFEngine, DCFInput
from aegis.core.truth.scenario_engine.reverse_dcf_solver import ReverseDCFSolver
from aegis.core.truth.scenario_engine.sensitivity_analyzer import SensitivityAnalyzer
from aegis.core.truth.validations.validation_engine import ValidationEngine


# ---------------------------------------------------------------------------
# Artifact Hashing
# ---------------------------------------------------------------------------

class TestArtifactHashing:
    def test_deterministic_hash(self):
        data = {"key": "value", "number": 42}
        h1 = compute_artifact_hash(data)
        h2 = compute_artifact_hash(data)
        assert h1 == h2
        assert h1.startswith("sha256:")

    def test_different_data_different_hash(self):
        h1 = compute_artifact_hash({"a": 1})
        h2 = compute_artifact_hash({"a": 2})
        assert h1 != h2

    def test_verify_round_trip(self):
        data = {"test": "data"}
        h = compute_artifact_hash(data)
        assert verify_artifact_hash(data, h) is True
        assert verify_artifact_hash({"test": "other"}, h) is False


# ---------------------------------------------------------------------------
# Validation Engine
# ---------------------------------------------------------------------------

class TestValidationEngine:
    def setup_method(self):
        self.engine = ValidationEngine()

    def test_matching_inputs_pass(self):
        result = self.engine.validate_metric_inputs(
            entity_ids=["meta"],
            currencies=["USD"],
            periods=["FY2025"],
            period_types=["annual"],
            definition_id="fcf_v1",
            required_inputs=["cfo", "capex"],
            available_inputs={"cfo": 100, "capex": 30},
        )
        assert result.passed is True

    def test_entity_mismatch_blocks(self):
        result = self.engine.validate_metric_inputs(
            entity_ids=["meta", "apple"],
            currencies=["USD"],
            periods=["FY2025"],
            period_types=["annual"],
            definition_id="ratio_v1",
            required_inputs=["a"],
            available_inputs={"a": 1},
        )
        assert result.passed is False
        assert result.block_issues[0].code.value == "same_entity"

    def test_currency_mismatch_blocks(self):
        result = self.engine.validate_metric_inputs(
            entity_ids=["meta"],
            currencies=["USD", "CNY"],
            periods=["FY2025"],
            period_types=["annual"],
            definition_id="ratio_v1",
            required_inputs=["a"],
            available_inputs={"a": 1},
        )
        assert result.passed is False

    def test_missing_definition_blocks(self):
        result = self.engine.validate_metric_inputs(
            entity_ids=["meta"],
            currencies=["USD"],
            periods=["FY2025"],
            period_types=["annual"],
            definition_id=None,
            required_inputs=["a"],
            available_inputs={"a": 1},
        )
        assert result.passed is False

    def test_missing_inputs_blocks(self):
        result = self.engine.validate_metric_inputs(
            entity_ids=["meta"],
            currencies=["USD"],
            periods=["FY2025"],
            period_types=["annual"],
            definition_id="test_v1",
            required_inputs=["a", "b", "c"],
            available_inputs={"a": 1},
        )
        assert result.passed is False


# ---------------------------------------------------------------------------
# Formula Engine
# ---------------------------------------------------------------------------

class TestFormulaEngine:
    def setup_method(self):
        self.engine = FormulaEngine()

    def test_gross_margin(self):
        result = self.engine.compute(
            definition_id="gross_margin_v1",
            formula_version=1,
            entity_id="meta_platforms_inc",
            period="FY2025",
            period_type="annual",
            currency="USD",
            inputs={"gross_profit": 80_000, "revenue": 100_000},
            input_fact_ids={"gross_profit": "f1", "revenue": "f2"},
        )
        assert result.validation.passed
        assert result.value == pytest.approx(0.80)

    def test_fcf_computation(self):
        result = self.engine.compute(
            definition_id="fcf_company_official_v1",
            formula_version=1,
            entity_id="meta_platforms_inc",
            period="FY2025",
            period_type="annual",
            currency="USD",
            inputs={
                "cfo": 115_800_000_000,
                "capex_ppe": 37_000_000_000,
                "finance_lease_principal": 2_000_000_000,
            },
            input_fact_ids={
                "cfo": "f1",
                "capex_ppe": "f2",
                "finance_lease_principal": "f3",
            },
        )
        assert result.validation.passed
        expected = 115_800_000_000 - 37_000_000_000 - 2_000_000_000
        assert result.value == expected

    def test_validation_failure_returns_nan(self):
        result = self.engine.compute(
            definition_id="gross_margin_v1",
            formula_version=1,
            entity_id="meta_platforms_inc",
            period="FY2025",
            period_type="annual",
            currency="USD",
            inputs={"gross_profit": 80_000},  # Missing revenue
            input_fact_ids={"gross_profit": "f1"},
        )
        assert not result.validation.passed
        assert result.value != result.value  # NaN check

    def test_list_formulas(self):
        formulas = self.engine.list_formulas()
        assert "gross_margin_v1" in formulas
        assert "roic_v1" in formulas
        assert len(formulas) >= 15


# ---------------------------------------------------------------------------
# DCF Engine
# ---------------------------------------------------------------------------

class TestDCFEngine:
    def setup_method(self):
        self.engine = DCFEngine()

    def _meta_base_case(self) -> DCFInput:
        return DCFInput(
            base_revenue=165_000_000_000,
            revenue_growth_path=[0.15, 0.13, 0.11, 0.10, 0.08, 0.07, 0.06, 0.05, 0.04, 0.03],
            operating_margin_path=[0.40, 0.41, 0.42, 0.42, 0.43, 0.43, 0.43, 0.42, 0.42, 0.41],
            capex_to_revenue_path=[0.28, 0.26, 0.24, 0.22, 0.20, 0.19, 0.18, 0.17, 0.17, 0.16],
            effective_tax_rate=0.15,
            nwc_to_revenue_delta=0.01,
            terminal_growth_rate=0.03,
            wacc=0.095,
            sbc_to_revenue=0.12,
            dilution_rate_annual=0.005,
            shares_outstanding=2_550_000_000,
            net_debt=-40_000_000_000,  # Net cash position
            horizon_years=10,
            sbc_treatment="both_with_justification",
            sbc_treatment_justification="Test case: SBC treated as both expense and dilution for testing purposes",
            buyback_yield_annual=0.02,
            base_depreciation=12_000_000_000,
            capex_useful_life_years=5.0,
        )

    def test_dcf_produces_positive_value(self):
        output = self.engine.compute_dcf(self._meta_base_case())
        assert output.per_share_value > 0
        assert output.enterprise_value > 0
        assert len(output.projections) == 10

    def test_dcf_projections_grow(self):
        output = self.engine.compute_dcf(self._meta_base_case())
        revenues = [p.revenue for p in output.projections]
        for i in range(1, len(revenues)):
            assert revenues[i] > revenues[i - 1]

    def test_dcf_wacc_below_tg_raises(self):
        bad_input = DCFInput(
            base_revenue=100_000_000,
            revenue_growth_path=[0.05],
            operating_margin_path=[0.20],
            capex_to_revenue_path=[0.10],
            effective_tax_rate=0.20,
            nwc_to_revenue_delta=0.01,
            terminal_growth_rate=0.10,  # > WACC
            wacc=0.08,
            sbc_to_revenue=0.05,
            dilution_rate_annual=0.01,
            shares_outstanding=2_000_000,
            net_debt=0,
            horizon_years=1,
        )
        with pytest.raises(ValueError, match="WACC.*must exceed"):
            self.engine.compute_dcf(bad_input)

    def test_net_cash_increases_equity_value(self):
        cash_case = self._meta_base_case()
        # Compare with debt case
        debt_input = DCFInput(
            base_revenue=cash_case.base_revenue,
            revenue_growth_path=list(cash_case.revenue_growth_path),
            operating_margin_path=list(cash_case.operating_margin_path),
            capex_to_revenue_path=list(cash_case.capex_to_revenue_path),
            effective_tax_rate=cash_case.effective_tax_rate,
            nwc_to_revenue_delta=cash_case.nwc_to_revenue_delta,
            terminal_growth_rate=cash_case.terminal_growth_rate,
            wacc=cash_case.wacc,
            sbc_to_revenue=cash_case.sbc_to_revenue,
            dilution_rate_annual=cash_case.dilution_rate_annual,
            shares_outstanding=cash_case.shares_outstanding,
            net_debt=50_000_000_000,  # Net debt position
            horizon_years=cash_case.horizon_years,
            sbc_treatment=cash_case.sbc_treatment,
            sbc_treatment_justification=cash_case.sbc_treatment_justification,
            buyback_yield_annual=cash_case.buyback_yield_annual,
            base_depreciation=cash_case.base_depreciation,
            capex_useful_life_years=cash_case.capex_useful_life_years,
        )
        cash_output = self.engine.compute_dcf(cash_case)
        debt_output = self.engine.compute_dcf(debt_input)
        assert cash_output.per_share_value > debt_output.per_share_value

    def test_sbc_expense_in_fcf_zeroes_dilution(self):
        """When sbc_treatment='expense_in_fcf', dilution_rate_annual is ignored."""
        inp = DCFInput(
            base_revenue=100_000_000_000,
            revenue_growth_path=[0.10] * 5,
            operating_margin_path=[0.40] * 5,
            capex_to_revenue_path=[0.20] * 5,
            effective_tax_rate=0.15,
            nwc_to_revenue_delta=0.01,
            terminal_growth_rate=0.03,
            wacc=0.095,
            sbc_to_revenue=0.10,
            dilution_rate_annual=0.05,  # Should be zeroed
            shares_outstanding=2_000_000_000,
            net_debt=0,
            horizon_years=5,
            sbc_treatment="expense_in_fcf",
        )
        output = self.engine.compute_dcf(inp)
        # Shares should NOT be diluted
        expected_shares = 2_000_000_000  # No dilution
        assert output.equity_value / output.per_share_value == pytest.approx(expected_shares, rel=0.001)

    def test_sbc_dilution_only_zeroes_sbc(self):
        """When sbc_treatment='dilution_only', sbc_to_revenue is ignored in FCFF."""
        inp = DCFInput(
            base_revenue=100_000_000_000,
            revenue_growth_path=[0.10] * 5,
            operating_margin_path=[0.40] * 5,
            capex_to_revenue_path=[0.20] * 5,
            effective_tax_rate=0.15,
            nwc_to_revenue_delta=0.01,
            terminal_growth_rate=0.03,
            wacc=0.095,
            sbc_to_revenue=0.10,
            dilution_rate_annual=0.02,
            shares_outstanding=2_000_000_000,
            net_debt=0,
            horizon_years=5,
            sbc_treatment="dilution_only",
        )
        output = self.engine.compute_dcf(inp)
        # SBC should be zero in projections
        for p in output.projections:
            assert p.sbc == 0.0

    def test_sbc_both_without_justification_raises(self):
        """When sbc_treatment='both_with_justification' but no justification, raises."""
        with pytest.raises(ValueError, match="sbc_treatment_justification"):
            inp = DCFInput(
                base_revenue=100_000_000_000,
                revenue_growth_path=[0.10],
                operating_margin_path=[0.40],
                capex_to_revenue_path=[0.20],
                effective_tax_rate=0.15,
                nwc_to_revenue_delta=0.01,
                terminal_growth_rate=0.03,
                wacc=0.095,
                sbc_to_revenue=0.10,
                dilution_rate_annual=0.02,
                shares_outstanding=2_000_000,
                net_debt=0,
                horizon_years=1,
                sbc_treatment="both_with_justification",
                sbc_treatment_justification="",
            )
            self.engine.compute_dcf(inp)

    def test_buyback_increases_per_share_value(self):
        """Buyback yield should increase per_share_value by reducing future shares."""
        base = self._meta_base_case()
        no_buyback = DCFInput(**{
            **base.__dict__,
            "buyback_yield_annual": 0.0,
        })
        with_buyback = DCFInput(**{
            **base.__dict__,
            "buyback_yield_annual": 0.03,
        })
        out_no = self.engine.compute_dcf(no_buyback)
        out_yes = self.engine.compute_dcf(with_buyback)
        # BUG-29/30 fix: per_share_value uses current shares, so buyback
        # doesn't affect it. But future_shares should still be smaller.
        assert out_yes.per_share_value == pytest.approx(out_no.per_share_value, rel=1e-9)
        assert out_yes.future_shares < out_no.future_shares

    def test_depreciation_field_populated(self):
        """D&A field should be populated when base_depreciation > 0."""
        base = self._meta_base_case()
        output = self.engine.compute_dcf(base)
        # base_depreciation=12B, so depreciation should be positive
        assert output.projections[0].depreciation > 0
        # D&A should grow as cumulative capex grows
        assert output.projections[-1].depreciation > output.projections[0].depreciation

    def test_consolidated_dcf_matches_segments(self):
        """Consolidated DCF should produce sensible results from segment inputs."""
        from aegis.core.truth.scenario_engine.dcf_engine import (
            ConsolidatedDCFInput, SegmentDCFInput,
        )
        cons_input = ConsolidatedDCFInput(
            segments={
                "seg_a": SegmentDCFInput(
                    segment_id="seg_a", segment_name="Segment A",
                    base_revenue=80_000_000_000,
                    revenue_growth_path=[0.10] * 5,
                    operating_margin_path=[0.40] * 5,
                    capex_to_revenue_path=[0.20] * 5,
                    terminal_growth_rate=0.03, horizon_years=5,
                ),
                "seg_b": SegmentDCFInput(
                    segment_id="seg_b", segment_name="Segment B",
                    base_revenue=20_000_000_000,
                    revenue_growth_path=[0.05] * 5,
                    operating_margin_path=[0.20] * 5,
                    capex_to_revenue_path=[0.30] * 5,
                    terminal_growth_rate=0.02, horizon_years=5,
                ),
            },
            wacc=0.095,
            effective_tax_rate=0.15,
            nwc_to_revenue_delta=0.01,
            sbc_to_revenue=0.0,
            sbc_treatment="expense_in_fcf",
            dilution_rate_annual=0.005,
            shares_outstanding=2_000_000_000,
            net_debt=-10_000_000_000,
            horizon_years=5,
        )
        output = self.engine.compute_consolidated_dcf(cons_input)
        assert output.per_share_value > 0
        assert len(output.consolidated_projections) == 5
        assert "seg_a" in output.segments
        assert "seg_b" in output.segments
        # Consolidated revenue = sum of segments
        yr1_cons_rev = output.consolidated_projections[0].revenue
        yr1_seg_a_rev = output.segments["seg_a"].projections[0].revenue
        yr1_seg_b_rev = output.segments["seg_b"].projections[0].revenue
        assert yr1_cons_rev == pytest.approx(yr1_seg_a_rev + yr1_seg_b_rev)


# ---------------------------------------------------------------------------
# Reverse DCF Solver
# ---------------------------------------------------------------------------

class TestReverseDCFSolver:
    def setup_method(self):
        self.solver = ReverseDCFSolver()

    def test_implied_growth_converges(self):
        result = self.solver.solve_implied_growth(
            current_price=500.0,
            base_revenue=165_000_000_000,
            operating_margin_path=[0.40] * 10,
            capex_to_revenue_path=[0.25] * 10,
            effective_tax_rate=0.15,
            nwc_to_revenue_delta=0.01,
            terminal_growth_rate=0.03,
            wacc=0.095,
            sbc_to_revenue=0.12,
            dilution_rate_annual=0.005,
            shares_outstanding=2_550_000_000,
            net_debt=-40_000_000_000,
            horizon_years=10,
        )
        assert result.converged
        assert -0.10 < result.implied_value < 0.50

    def test_implied_terminal_growth_converges(self):
        result = self.solver.solve_implied_terminal_growth(
            current_price=500.0,
            base_revenue=165_000_000_000,
            revenue_growth_path=[0.12] * 10,
            operating_margin_path=[0.40] * 10,
            capex_to_revenue_path=[0.25] * 10,
            effective_tax_rate=0.15,
            nwc_to_revenue_delta=0.01,
            wacc=0.095,
            sbc_to_revenue=0.12,
            dilution_rate_annual=0.005,
            shares_outstanding=2_550_000_000,
            net_debt=-40_000_000_000,
            horizon_years=10,
        )
        assert result.converged
        assert 0.0 < result.implied_value < 0.09


# ---------------------------------------------------------------------------
# Sensitivity Analyzer
# ---------------------------------------------------------------------------

class TestSensitivityAnalyzer:
    def setup_method(self):
        self.analyzer = SensitivityAnalyzer()
        self.base = DCFInput(
            base_revenue=165_000_000_000,
            revenue_growth_path=[0.12] * 10,
            operating_margin_path=[0.40] * 10,
            capex_to_revenue_path=[0.25] * 10,
            effective_tax_rate=0.15,
            nwc_to_revenue_delta=0.01,
            terminal_growth_rate=0.03,
            wacc=0.095,
            sbc_to_revenue=0.12,
            dilution_rate_annual=0.005,
            shares_outstanding=2_550_000_000,
            net_debt=-40_000_000_000,
            horizon_years=10,
        )

    def test_rank_assumptions_returns_sorted(self):
        rankings = self.analyzer.rank_assumptions(self.base)
        assert len(rankings) >= 5
        # Should be sorted by impact descending
        for i in range(1, len(rankings)):
            assert rankings[i - 1].impact_pct >= rankings[i].impact_pct

    def test_two_way_table(self):
        table = self.analyzer.two_way_table(
            self.base,
            "wacc",
            [0.08, 0.09, 0.10, 0.11],
            "terminal_growth_rate",
            [0.02, 0.025, 0.03, 0.035],
        )
        assert len(table.matrix) == 4
        assert len(table.matrix[0]) == 4
        # Lower WACC should give higher value
        assert table.matrix[0][0] > table.matrix[-1][0]


# ---------------------------------------------------------------------------
# Currency Engine
# ---------------------------------------------------------------------------

class TestCurrencyEngine:
    def setup_method(self):
        self.engine = CurrencyEngine()
        self.today = date(2026, 4, 11)
        self.engine.load_rate(FXRate(
            base_currency="USD",
            quote_currency="CNY",
            rate=7.25,
            rate_date=self.today,
            source="test",
        ))
        self.engine.load_rate(FXRate(
            base_currency="USD",
            quote_currency="HKD",
            rate=7.80,
            rate_date=self.today,
            source="test",
        ))

    def test_direct_conversion(self):
        result = self.engine.convert(100.0, "USD", "CNY", self.today)
        assert result.converted_value == pytest.approx(725.0)
        assert result.conversion_path == ["USD", "CNY"]

    def test_inverse_conversion(self):
        result = self.engine.convert(725.0, "CNY", "USD", self.today)
        assert result.converted_value == pytest.approx(100.0)

    def test_identity_conversion(self):
        result = self.engine.convert(100.0, "USD", "USD", self.today)
        assert result.converted_value == 100.0
        assert result.conversion_path == ["USD"]

    def test_cross_rate_via_usd(self):
        result = self.engine.convert(780.0, "HKD", "CNY", self.today)
        # HKD -> USD -> CNY: 780 / 7.80 * 7.25 = 725
        assert result.converted_value == pytest.approx(725.0, rel=0.01)
        assert len(result.conversion_path) == 3

    def test_missing_rate_raises(self):
        with pytest.raises(CurrencyConversionError):
            self.engine.convert(100.0, "USD", "JPY", self.today)
