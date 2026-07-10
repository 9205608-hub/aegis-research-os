"""AUDIT-A1 / AUDIT-A2 regression tests — D&A 口径一致性 (2026-07 audit).

A1 (P0): the old D&A bridge (`base_depreciation + active_capex/life`) held
base D&A constant for the whole horizon while the GAAP EBIT margin already
embeds D&A scaling with revenue — double-counting that inflated per-share
value ~27% for capital-intensive names (old benchmark per_share ≈ 338.87).
The ratio-based bridge (D&A_t = da_ratio_t × revenue_t, converging to the
capex ratio by the terminal year) must reproduce the audit reviewer's
consistent baseline ≈ 263.5.

A2 (P1): the reverse DCF solver must run the exact same FCFF model as the
forward DCF — before the fix it dropped base_depreciation (default 0.0) and
round-tripping the forward model's own price reported implied growth ~2×
the true rate.

Bonus (P2): two_way_table must not let a single undefined cell
(terminal growth ≥ WACC) crash the whole sensitivity step — undefined
cells become None.
"""

from __future__ import annotations

import pytest

from aegis.core.truth.scenario_engine.dcf_engine import DCFEngine, DCFInput
from aegis.core.truth.scenario_engine.reverse_dcf_solver import ReverseDCFSolver
from aegis.core.truth.scenario_engine.sensitivity_analyzer import SensitivityAnalyzer

engine = DCFEngine()

# Audit reviewer's benchmark parameter set (AUDIT_2026-07, dcf-engine A1):
# base_revenue 100B, 5% growth × 10y, 20% operating margin, capex = 10% of
# revenue, base D&A = 10B, WACC 9.5%, terminal growth 2.5%, tax 25%.
BENCHMARK = dict(
    base_revenue=100e9,
    revenue_growth_path=[0.05] * 10,
    operating_margin_path=[0.20] * 10,
    capex_to_revenue_path=[0.10] * 10,
    effective_tax_rate=0.25,
    nwc_to_revenue_delta=0.01,
    terminal_growth_rate=0.025,
    wacc=0.095,
    sbc_to_revenue=0.0,
    dilution_rate_annual=0.0,
    shares_outstanding=1_000_000_000,
    net_debt=0.0,
    horizon_years=10,
    base_depreciation=10e9,
    capex_useful_life_years=5.0,
)


class TestA1RatioModelBenchmark:
    """Lock the audit reviewer's numbers for the ratio-based D&A bridge."""

    def test_benchmark_per_share_matches_reviewer_baseline(self):
        """Per-share must be ≈263.5 (±2%) — the calibre-consistent baseline.

        The pre-fix window model produced 338.87 on the same inputs
        (verified before the fix), i.e. +28.6% overstatement.
        """
        out = engine.compute_dcf(DCFInput(**BENCHMARK))
        assert out.per_share_value == pytest.approx(263.5, rel=0.02), (
            f"per_share={out.per_share_value:.2f}, expected ≈263.5 — a "
            f"regression toward the old ~338.87 means base D&A is being "
            f"double-counted again"
        )

    def test_terminal_year_da_equals_capex(self):
        """Steady-state conservation: terminal D&A == terminal capex.

        The old model carried Y10 D&A = 24.1B vs capex 16.3B into the
        Gordon terminal value — a perpetual 7.8B/yr phantom cash flow.
        """
        out = engine.compute_dcf(DCFInput(**BENCHMARK))
        last = out.projections[-1]
        assert last.depreciation == pytest.approx(abs(last.capex), rel=1e-9)

    def test_da_calibre_consistent_each_year(self):
        """With da_base_ratio == capex ratio (both 10%), D&A_t = 10% × rev_t
        for every year — the margin-embedded D&A calibre exactly."""
        out = engine.compute_dcf(DCFInput(**BENCHMARK))
        for p in out.projections:
            assert p.depreciation == pytest.approx(p.revenue * 0.10, rel=1e-9), (
                f"Year {p.year}"
            )

    def test_missing_da_ramps_and_stays_bounded(self):
        """base_depreciation=0 (D&A missing upstream): the ratio ramps from
        0 to the capex ratio, terminal conservation still holds, and the
        undervaluation gap equals the PV of the horizon D&A wedge — no cliff.
        """
        out0 = engine.compute_dcf(DCFInput(**{**BENCHMARK, "base_depreciation": 0.0}))
        base = engine.compute_dcf(DCFInput(**BENCHMARK))

        # Y1 D&A = rev_1 × capex_ratio × 1/n = 105B × 0.10 × 0.1 = 1.05B
        assert out0.projections[0].depreciation == pytest.approx(
            105e9 * 0.10 * 0.1, rel=1e-9
        )
        # Terminal conservation holds even without base D&A
        last = out0.projections[-1]
        assert last.depreciation == pytest.approx(abs(last.capex), rel=1e-9)

        # Undervaluation vs the with-D&A run is exactly the PV of the
        # horizon wedge Σ PV(rev_t × da_base_ratio × (1 − t/n)).
        assert out0.per_share_value < base.per_share_value
        n = BENCHMARK["horizon_years"]
        da_base_ratio = BENCHMARK["base_depreciation"] / BENCHMARK["base_revenue"]
        rev = BENCHMARK["base_revenue"]
        expected_gap_ev = 0.0
        for t in range(1, n + 1):
            rev *= 1.05
            expected_gap_ev += (
                rev * da_base_ratio * (1 - t / n) / (1 + BENCHMARK["wacc"]) ** t
            )
        expected_gap_ps = expected_gap_ev / BENCHMARK["shares_outstanding"]
        actual_gap_ps = base.per_share_value - out0.per_share_value
        assert actual_gap_ps == pytest.approx(expected_gap_ps, rel=1e-9)

    def test_consolidated_path_terminal_conservation(self):
        """The segment (consolidated) path uses the same ratio bridge."""
        from aegis.core.truth.scenario_engine.dcf_engine import (
            ConsolidatedDCFInput,
            SegmentDCFInput,
        )
        n = 10
        cons = ConsolidatedDCFInput(
            segments={
                "a": SegmentDCFInput(
                    segment_id="a", segment_name="A",
                    base_revenue=60e9,
                    revenue_growth_path=[0.08] * n,
                    operating_margin_path=[0.25] * n,
                    capex_to_revenue_path=[0.15] * n,
                    terminal_growth_rate=0.025, horizon_years=n,
                ),
                "b": SegmentDCFInput(
                    segment_id="b", segment_name="B",
                    base_revenue=40e9,
                    revenue_growth_path=[0.03] * n,
                    operating_margin_path=[0.15] * n,
                    capex_to_revenue_path=[0.05] * n,
                    terminal_growth_rate=0.02, horizon_years=n,
                ),
            },
            wacc=0.095,
            effective_tax_rate=0.25,
            nwc_to_revenue_delta=0.01,
            sbc_to_revenue=0.0,
            shares_outstanding=1_000_000_000,
            net_debt=0.0,
            horizon_years=n,
            base_depreciation=8e9,
            capex_useful_life_years=5.0,
        )
        out = engine.compute_consolidated_dcf(cons)
        last = out.consolidated_projections[-1]
        assert last.depreciation == pytest.approx(abs(last.capex), rel=1e-9)


class TestA2ReverseDCFRoundTrip:
    """Reverse DCF must be the exact same model as the forward DCF."""

    def test_round_trip_recovers_true_growth(self):
        """Forward at 5% growth → reverse-solve on that price → ≈5.0% ±0.3pp.

        Pre-fix, the solver dropped base_depreciation and returned ~2× the
        true growth (converged=True, no boundary_hit — a silent calibre split).
        """
        true_g = 0.05
        inp = DCFInput(**BENCHMARK)
        fwd = engine.compute_dcf(inp)

        solver = ReverseDCFSolver()
        # Mirrors the orchestrator call site (auto_research.py, AUDIT-A2):
        # every field passed through from the same forward DCFInput.
        res = solver.solve_implied_growth(
            current_price=fwd.per_share_value,
            base_revenue=inp.base_revenue,
            operating_margin_path=inp.operating_margin_path,
            capex_to_revenue_path=inp.capex_to_revenue_path,
            effective_tax_rate=inp.effective_tax_rate,
            nwc_to_revenue_delta=inp.nwc_to_revenue_delta,
            terminal_growth_rate=inp.terminal_growth_rate,
            wacc=inp.wacc,
            sbc_to_revenue=inp.sbc_to_revenue,
            dilution_rate_annual=inp.dilution_rate_annual,
            shares_outstanding=inp.shares_outstanding,
            net_debt=inp.net_debt,
            horizon_years=inp.horizon_years,
            base_depreciation=inp.base_depreciation,
            capex_useful_life_years=inp.capex_useful_life_years,
            buyback_yield_annual=inp.buyback_yield_annual,
        )
        assert res.converged, "round-trip solve should converge"
        assert res.boundary_hit is None
        assert res.implied_value == pytest.approx(true_g, abs=0.003), (
            f"implied growth {res.implied_value:.4f} should recover the true "
            f"5.0% within ±0.3pp — a big gap means forward/reverse calibre split"
        )

    def test_round_trip_recovers_true_terminal_growth(self):
        """Same round-trip discipline for solve_implied_terminal_growth."""
        inp = DCFInput(**BENCHMARK)
        fwd = engine.compute_dcf(inp)

        solver = ReverseDCFSolver()
        res = solver.solve_implied_terminal_growth(
            current_price=fwd.per_share_value,
            base_revenue=inp.base_revenue,
            revenue_growth_path=inp.revenue_growth_path,
            operating_margin_path=inp.operating_margin_path,
            capex_to_revenue_path=inp.capex_to_revenue_path,
            effective_tax_rate=inp.effective_tax_rate,
            nwc_to_revenue_delta=inp.nwc_to_revenue_delta,
            wacc=inp.wacc,
            sbc_to_revenue=inp.sbc_to_revenue,
            dilution_rate_annual=inp.dilution_rate_annual,
            shares_outstanding=inp.shares_outstanding,
            net_debt=inp.net_debt,
            horizon_years=inp.horizon_years,
            base_depreciation=inp.base_depreciation,
            capex_useful_life_years=inp.capex_useful_life_years,
            buyback_yield_annual=inp.buyback_yield_annual,
        )
        assert res.converged
        assert res.implied_value == pytest.approx(
            inp.terminal_growth_rate, abs=0.003
        )

    def test_legacy_call_without_da_still_works(self):
        """Backward compatibility: the pre-A2 signature (no D&A kwargs)
        must keep working with base_depreciation defaulting to 0."""
        inp = DCFInput(**BENCHMARK)
        solver = ReverseDCFSolver()
        res = solver.solve_implied_growth(
            current_price=100.0,
            base_revenue=inp.base_revenue,
            operating_margin_path=inp.operating_margin_path,
            capex_to_revenue_path=inp.capex_to_revenue_path,
            effective_tax_rate=inp.effective_tax_rate,
            nwc_to_revenue_delta=inp.nwc_to_revenue_delta,
            terminal_growth_rate=inp.terminal_growth_rate,
            wacc=inp.wacc,
            sbc_to_revenue=inp.sbc_to_revenue,
            dilution_rate_annual=inp.dilution_rate_annual,
            shares_outstanding=inp.shares_outstanding,
            net_debt=inp.net_debt,
            horizon_years=inp.horizon_years,
        )
        assert res.converged


class TestTwoWayTableUndefinedCells:
    """AUDIT sensitivity-guard (P2): tg ≥ WACC cells must not crash the run."""

    def test_invalid_cells_become_none_valid_cells_survive(self):
        """A wacc/tg grid straddling tg ≥ wacc yields None for undefined
        cells and real values elsewhere — no ValueError escapes."""
        inp = DCFInput(**{**BENCHMARK, "wacc": 0.05})
        sa = SensitivityAnalyzer()
        table = sa.two_way_table(
            inp,
            "wacc", [0.035, 0.05, 0.065],
            "terminal_growth_rate", [0.02, 0.04],
        )
        # (wacc=0.035, tg=0.04): tg > wacc → Gordon diverges → None
        assert table.matrix[0][1] is None
        # (wacc=0.035, tg=0.02) is well-defined
        assert table.matrix[0][0] is not None and table.matrix[0][0] > 0
        # All higher-wacc rows fully defined
        for i in (1, 2):
            for j in (0, 1):
                assert table.matrix[i][j] is not None, f"cell [{i}][{j}]"
