"""End-to-end DCF engine tests — three production-representative scenarios.

Covers the bugs surfaced in the 2026-04-16 P0-REVIEW audit:
  - D&A=0 handling (GOOG-like)
  - CapEx sign convention (A-share with positive capex input)
  - Segment vs flat DCF output consistency
  - Buyback + dilution share counting (BUG-29/30)
  - FCFF = NOPAT + D&A - CapEx - ΔNWC formula integrity
  - Input validation guards

Each scenario uses simplified but realistic numbers so expected values
can be hand-verified.
"""

from __future__ import annotations

import math
import warnings

import pytest

from aegis.core.truth.scenario_engine.dcf_engine import (
    ConsolidatedDCFInput,
    ConsolidatedDCFOutput,
    DCFEngine,
    DCFInput,
    DCFOutput,
    SegmentDCFInput,
)

engine = DCFEngine()


# ═══════════════════════════════════════════════════════════════════════
# Helper: manual FCFF calculation for cross-check
# ═══════════════════════════════════════════════════════════════════════

def _manual_fcff(
    revenue: float,
    prev_revenue: float,
    margin: float,
    tax_rate: float,
    capex_ratio: float,
    nwc_delta_ratio: float,
    sbc_ratio: float,
    depreciation: float,
) -> float:
    """Replicate the engine's FCFF formula by hand."""
    oi = revenue * margin
    nopat = oi * (1 - tax_rate)
    capex = revenue * capex_ratio
    nwc = (revenue - prev_revenue) * nwc_delta_ratio
    fcff = nopat + depreciation - abs(capex) - nwc
    return fcff


# ═══════════════════════════════════════════════════════════════════════
# Scenario 1 — AAPL-like segment DCF
#   - 3 segments (iPhone/Services/Mac), 5-year horizon for tractability
#   - Buyback yield 3%, SBC expense_in_fcf
#   - Tests: segment revenue roll-up, consolidated FCFF, per-share value
# ═══════════════════════════════════════════════════════════════════════

class TestScenario_AAPL_Segment:
    """AAPL-like: segment DCF with buyback, base D&A, no dilution double-count."""

    @pytest.fixture()
    def inputs(self) -> ConsolidatedDCFInput:
        n = 5
        return ConsolidatedDCFInput(
            segments={
                "iphone": SegmentDCFInput(
                    segment_id="iphone",
                    segment_name="iPhone",
                    base_revenue=200e9,
                    revenue_growth_path=[0.05, 0.04, 0.03, 0.03, 0.02],
                    operating_margin_path=[0.35] * n,
                    capex_to_revenue_path=[0.04] * n,
                    terminal_growth_rate=0.025,
                    horizon_years=n,
                ),
                "services": SegmentDCFInput(
                    segment_id="services",
                    segment_name="Services",
                    base_revenue=100e9,
                    revenue_growth_path=[0.12, 0.11, 0.10, 0.09, 0.08],
                    operating_margin_path=[0.70] * n,
                    capex_to_revenue_path=[0.02] * n,
                    terminal_growth_rate=0.03,
                    horizon_years=n,
                ),
                "mac": SegmentDCFInput(
                    segment_id="mac",
                    segment_name="Mac & Other",
                    base_revenue=90e9,
                    revenue_growth_path=[0.03, 0.02, 0.02, 0.01, 0.01],
                    operating_margin_path=[0.25] * n,
                    capex_to_revenue_path=[0.05] * n,
                    terminal_growth_rate=0.02,
                    horizon_years=n,
                ),
            },
            wacc=0.09,
            effective_tax_rate=0.15,
            nwc_to_revenue_delta=0.01,
            sbc_to_revenue=0.02,
            sbc_treatment="expense_in_fcf",
            dilution_rate_annual=0.005,   # ignored b/c expense_in_fcf
            buyback_yield_annual=0.03,
            shares_outstanding=15_000_000_000,
            net_debt=-60e9,  # net cash
            horizon_years=5,
            base_depreciation=11e9,
            capex_useful_life_years=5.0,
        )

    def test_output_type(self, inputs):
        out = engine.compute_consolidated_dcf(inputs)
        assert isinstance(out, ConsolidatedDCFOutput)

    def test_segment_revenue_rollup(self, inputs):
        """Consolidated Y1 revenue == sum of segment Y1 revenues."""
        out = engine.compute_consolidated_dcf(inputs)
        y1 = out.consolidated_projections[0]
        seg_rev = sum(
            out.segments[sid].projections[0].revenue
            for sid in out.segments
        )
        assert y1.revenue == pytest.approx(seg_rev, rel=1e-9)

    def test_segment_revenue_rollup_all_years(self, inputs):
        out = engine.compute_consolidated_dcf(inputs)
        for i in range(inputs.horizon_years):
            cons_rev = out.consolidated_projections[i].revenue
            seg_rev = sum(
                out.segments[sid].projections[i].revenue
                for sid in out.segments
            )
            assert cons_rev == pytest.approx(seg_rev, rel=1e-9), f"Year {i+1}"

    def test_fcff_formula_integrity(self, inputs):
        """FCFF = NOPAT + D&A - |CapEx| - ΔNWC for every year."""
        out = engine.compute_consolidated_dcf(inputs)
        prev_rev = sum(s.base_revenue for s in inputs.segments.values())
        for p in out.consolidated_projections:
            expected_fcff = (
                p.nopat + p.depreciation - abs(p.capex) - p.change_in_nwc
            )
            assert p.fcff == pytest.approx(expected_fcff, rel=1e-9), (
                f"Year {p.year}: FCFF mismatch"
            )
            prev_rev = p.revenue

    def test_per_share_uses_current_shares(self, inputs):
        """BUG-29/30: per_share_value = equity_value / current shares, not future."""
        out = engine.compute_consolidated_dcf(inputs)
        expected = out.equity_value / inputs.shares_outstanding
        assert out.per_share_value == pytest.approx(expected, rel=1e-9)

    def test_future_shares_differ_from_current(self, inputs):
        """future_shares should reflect buyback shrinkage (no dilution b/c expense_in_fcf)."""
        out = engine.compute_consolidated_dcf(inputs)
        # With 3% buyback and 0% effective dilution, shares should shrink
        assert out.future_shares < inputs.shares_outstanding

    def test_positive_valuation(self, inputs):
        out = engine.compute_consolidated_dcf(inputs)
        assert out.per_share_value > 0
        assert out.enterprise_value > 0
        assert out.equity_value > 0

    def test_net_cash_boosts_equity(self, inputs):
        """Net cash position means equity_value > enterprise_value."""
        out = engine.compute_consolidated_dcf(inputs)
        assert out.equity_value > out.enterprise_value

    def test_depreciation_grows_with_capex(self, inputs):
        out = engine.compute_consolidated_dcf(inputs)
        deps = [p.depreciation for p in out.consolidated_projections]
        # With 5-year horizon and 5-year useful life, D&A should grow each year
        # (no capex retires within the horizon)
        for i in range(1, len(deps)):
            assert deps[i] >= deps[i - 1], f"D&A should grow: Y{i} vs Y{i+1}"


# ═══════════════════════════════════════════════════════════════════════
# Scenario 2 — GOOG-like flat DCF
#   - base_depreciation = 21B (the bug that was missing → 130% undervalue)
#   - Tests: D&A > 0 adds to FCFF, D&A=0 degrades valuation, magnitude check
# ═══════════════════════════════════════════════════════════════════════

class TestScenario_GOOG_Flat:
    """GOOG-like: flat DCF, large D&A, verifying D&A=0 undervalues significantly."""

    @pytest.fixture()
    def base_inputs(self) -> DCFInput:
        n = 10
        return DCFInput(
            base_revenue=350e9,
            revenue_growth_path=[0.12, 0.10, 0.09, 0.08, 0.07,
                                 0.06, 0.05, 0.04, 0.03, 0.03],
            operating_margin_path=[0.30] * n,
            capex_to_revenue_path=[0.12] * n,
            effective_tax_rate=0.18,
            nwc_to_revenue_delta=0.01,
            terminal_growth_rate=0.03,
            wacc=0.09,
            sbc_to_revenue=0.06,
            dilution_rate_annual=0.01,
            shares_outstanding=6_000_000_000,
            net_debt=-100e9,  # Alphabet's big net cash
            horizon_years=n,
            sbc_treatment="expense_in_fcf",
            buyback_yield_annual=0.01,
            base_depreciation=21e9,  # The critical D&A
            capex_useful_life_years=5.0,
        )

    def test_output_type(self, base_inputs):
        out = engine.compute_dcf(base_inputs)
        assert isinstance(out, DCFOutput)

    def test_da_adds_to_fcff(self, base_inputs):
        """D&A is added back in FCFF — each year's FCFF > NOPAT - CapEx."""
        out = engine.compute_dcf(base_inputs)
        for p in out.projections:
            nopat_minus_capex = p.nopat - abs(p.capex)
            assert p.fcff > nopat_minus_capex, (
                f"Year {p.year}: D&A should make FCFF > NOPAT-CapEx"
            )

    def test_da_zero_undervalues(self, base_inputs):
        """Without D&A (the old GOOG bug), valuation drops massively."""
        with_da = engine.compute_dcf(base_inputs)

        no_da_input = DCFInput(**{**base_inputs.__dict__, "base_depreciation": 0.0})
        no_da = engine.compute_dcf(no_da_input)

        # D&A=21B should make a meaningful difference — at least 10% higher
        # (In production GOOG the impact was ~130% because capex ≈ D&A,
        #  so removing D&A nearly halved FCFF. Here the ratio is less extreme.)
        ratio = with_da.per_share_value / no_da.per_share_value
        assert ratio > 1.10, (
            f"D&A=21B should boost valuation >10%, got {ratio:.1%}"
        )

    def test_fcff_formula_integrity(self, base_inputs):
        """FCFF = NOPAT + D&A - |CapEx| - ΔNWC for every year."""
        out = engine.compute_dcf(base_inputs)
        for p in out.projections:
            expected = p.nopat + p.depreciation - abs(p.capex) - p.change_in_nwc
            assert p.fcff == pytest.approx(expected, rel=1e-9), f"Year {p.year}"

    def test_per_share_uses_current_shares(self, base_inputs):
        """BUG-29/30: per_share = equity / current_shares."""
        out = engine.compute_dcf(base_inputs)
        expected = out.equity_value / base_inputs.shares_outstanding
        assert out.per_share_value == pytest.approx(expected, rel=1e-9)

    def test_terminal_value_gordon_growth(self, base_inputs):
        """TV = terminal_FCFF × (1+g) / (WACC-g)."""
        out = engine.compute_dcf(base_inputs)
        terminal_fcff = out.projections[-1].fcff
        expected_tv = (
            terminal_fcff * (1 + base_inputs.terminal_growth_rate)
            / (base_inputs.wacc - base_inputs.terminal_growth_rate)
        )
        assert out.terminal_value == pytest.approx(expected_tv, rel=1e-9)

    def test_ev_composition(self, base_inputs):
        """EV = PV(FCFF) + PV(TV)."""
        out = engine.compute_dcf(base_inputs)
        assert out.enterprise_value == pytest.approx(
            out.pv_fcff_sum + out.pv_terminal_value, rel=1e-9
        )

    def test_discount_factors(self, base_inputs):
        """Discount factor = 1/(1+WACC)^year."""
        out = engine.compute_dcf(base_inputs)
        for p in out.projections:
            expected_df = 1 / (1 + base_inputs.wacc) ** p.year
            assert p.discount_factor == pytest.approx(expected_df, rel=1e-9)

    def test_pv_fcff_equals_fcff_times_df(self, base_inputs):
        out = engine.compute_dcf(base_inputs)
        for p in out.projections:
            assert p.pv_fcff == pytest.approx(
                p.fcff * p.discount_factor, rel=1e-9
            ), f"Year {p.year}"

    def test_revenue_path_correct(self, base_inputs):
        """Revenue compounds correctly through growth path."""
        out = engine.compute_dcf(base_inputs)
        expected_rev = base_inputs.base_revenue
        for i, p in enumerate(out.projections):
            expected_rev *= (1 + base_inputs.revenue_growth_path[i])
            assert p.revenue == pytest.approx(expected_rev, rel=1e-9), f"Year {p.year}"


# ═══════════════════════════════════════════════════════════════════════
# Scenario 3 — 301358 A-share
#   - CNY, no buyback, negative FCF is realistic
#   - CapEx passed as positive (post-BUG-28 fix)
#   - Tests: capex sign handling, CNY currency, negative FCF acceptance
# ═══════════════════════════════════════════════════════════════════════

class TestScenario_AShare_301358:
    """301358-like: A-share, negative FCF, CNY, no buyback."""

    @pytest.fixture()
    def inputs(self) -> DCFInput:
        n = 10
        return DCFInput(
            base_revenue=30e8,   # ¥30亿
            revenue_growth_path=[0.20, 0.18, 0.15, 0.12, 0.10,
                                 0.08, 0.07, 0.06, 0.05, 0.04],
            operating_margin_path=[0.08, 0.09, 0.10, 0.11, 0.12,
                                   0.13, 0.14, 0.14, 0.15, 0.15],
            capex_to_revenue_path=[0.35, 0.30, 0.25, 0.22, 0.20,
                                   0.18, 0.16, 0.15, 0.14, 0.13],
            effective_tax_rate=0.25,
            nwc_to_revenue_delta=0.05,
            terminal_growth_rate=0.03,
            wacc=0.10,
            sbc_to_revenue=0.005,  # A-share: minimal SBC
            dilution_rate_annual=0.0,
            shares_outstanding=400_000_000,  # 4亿股
            net_debt=5e8,  # ¥5亿 net debt
            horizon_years=n,
            sbc_treatment="expense_in_fcf",
            buyback_yield_annual=0.0,  # No buyback (A-share norm)
            base_depreciation=2e8,  # ¥2亿
            capex_useful_life_years=7.0,  # Longer asset life
            currency="CNY",
        )

    def test_output_type(self, inputs):
        out = engine.compute_dcf(inputs)
        assert isinstance(out, DCFOutput)

    def test_early_years_negative_fcf(self, inputs):
        """Heavy capex early → FCFF should be negative in year 1."""
        out = engine.compute_dcf(inputs)
        # Y1: rev=36亿, margin=8%, tax=25% → NOPAT~2.16亿
        # D&A~2亿, CapEx=35%*36亿=12.6亿, NWC=5%*6亿=0.3亿
        # FCFF ~ 2.16 + 2 - 12.6 - 0.3 = -8.74亿
        assert out.projections[0].fcff < 0, "Early high-capex year should have negative FCFF"

    def test_later_years_improving_fcf(self, inputs):
        """As margins improve and capex declines, FCFF should improve."""
        out = engine.compute_dcf(inputs)
        # Last year should be much better than first
        assert out.projections[-1].fcff > out.projections[0].fcff

    def test_capex_positive_convention(self, inputs):
        """CapEx in projections should be positive (post-BUG-28)."""
        out = engine.compute_dcf(inputs)
        for p in out.projections:
            assert p.capex >= 0, f"Year {p.year}: capex should be non-negative"

    def test_fcff_formula_integrity(self, inputs):
        """FCFF = NOPAT + D&A - |CapEx| - ΔNWC."""
        out = engine.compute_dcf(inputs)
        for p in out.projections:
            expected = p.nopat + p.depreciation - abs(p.capex) - p.change_in_nwc
            assert p.fcff == pytest.approx(expected, rel=1e-9), f"Year {p.year}"

    def test_per_share_uses_current_shares(self, inputs):
        out = engine.compute_dcf(inputs)
        expected = out.equity_value / inputs.shares_outstanding
        assert out.per_share_value == pytest.approx(expected, rel=1e-9)

    def test_no_buyback_no_dilution(self, inputs):
        """With 0% buyback and 0% dilution, future_shares == current shares."""
        out = engine.compute_dcf(inputs)
        assert out.future_shares == pytest.approx(
            inputs.shares_outstanding, rel=1e-9
        )

    def test_nwc_drain_in_growth_phase(self, inputs):
        """Positive nwc_to_revenue_delta + growing revenue → negative ΔNWC cash impact."""
        out = engine.compute_dcf(inputs)
        for p in out.projections:
            assert p.change_in_nwc > 0, (
                f"Year {p.year}: growing company with positive NWC ratio → positive ΔNWC"
            )


# ═══════════════════════════════════════════════════════════════════════
# Input validation tests (P0-REVIEW guards)
# ═══════════════════════════════════════════════════════════════════════

class TestDCFInputValidation:
    """Tests for the input guards added in the P0-REVIEW."""

    def _minimal(self, **overrides) -> DCFInput:
        defaults = dict(
            base_revenue=100e9,
            revenue_growth_path=[0.05] * 5,
            operating_margin_path=[0.20] * 5,
            capex_to_revenue_path=[0.10] * 5,
            effective_tax_rate=0.20,
            nwc_to_revenue_delta=0.01,
            terminal_growth_rate=0.03,
            wacc=0.09,
            sbc_to_revenue=0.05,
            dilution_rate_annual=0.01,
            shares_outstanding=2_000_000_000,
            net_debt=0,
            horizon_years=5,
        )
        defaults.update(overrides)
        return DCFInput(**defaults)

    def test_shares_too_small_raises(self):
        inp = self._minimal(shares_outstanding=100)
        with pytest.raises(ValueError, match="shares_outstanding.*implausibly small"):
            engine.compute_dcf(inp)

    def test_negative_revenue_raises(self):
        inp = self._minimal(base_revenue=-1e9)
        with pytest.raises(ValueError, match="base_revenue.*must be positive"):
            engine.compute_dcf(inp)

    def test_negative_capex_ratio_auto_corrects(self):
        """BUG-26 (2026-05-04, REVISED Y33): CAS cashflow reports capex as
        negative outflow. The engine now silently `abs()` the path and logs
        to debug rather than emitting a `warnings.warn`. The test was
        previously asserting a warning that no longer fires; rewrite to
        check the actual contract: same DCF result regardless of sign.
        """
        inp_neg = self._minimal(capex_to_revenue_path=[-0.10] * 5)
        inp_pos = self._minimal(capex_to_revenue_path=[0.10] * 5)
        out_neg = engine.compute_dcf(inp_neg)
        out_pos = engine.compute_dcf(inp_pos)
        # Sign convention should not affect per-share output (within float epsilon)
        assert abs(out_neg.per_share_value - out_pos.per_share_value) < 0.01

    def test_extreme_wacc_warns(self):
        inp = self._minimal(wacc=0.50)
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            engine.compute_dcf(inp)
            wacc_warnings = [x for x in w if "WACC" in str(x.message)]
            assert len(wacc_warnings) > 0

    def test_wacc_equals_terminal_growth_raises(self):
        inp = self._minimal(wacc=0.03, terminal_growth_rate=0.03)
        with pytest.raises(ValueError, match="WACC.*must exceed"):
            engine.compute_dcf(inp)


# ═══════════════════════════════════════════════════════════════════════
# D&A retirement / accumulation correctness
# ═══════════════════════════════════════════════════════════════════════

class TestDepreciationAccumulation:
    """Verify D&A accumulation behavior — known issue: capex never retires."""

    def test_da_year1_equals_base_plus_first_capex_share(self):
        """Year 1 D&A = base_depreciation + capex_Y1 / useful_life."""
        inp = DCFInput(
            base_revenue=100e9,
            revenue_growth_path=[0.05],
            operating_margin_path=[0.20],
            capex_to_revenue_path=[0.10],
            effective_tax_rate=0.20,
            nwc_to_revenue_delta=0.01,
            terminal_growth_rate=0.03,
            wacc=0.09,
            sbc_to_revenue=0.0,
            dilution_rate_annual=0.0,
            shares_outstanding=2_000_000_000,
            net_debt=0,
            horizon_years=1,
            base_depreciation=5e9,
            capex_useful_life_years=5.0,
        )
        out = engine.compute_dcf(inp)
        # Y1: cumulative_capex starts at 0, then capex=105B*0.10=10.5B added AFTER depreciation calc
        # So Y1 depreciation = base(5B) + 0/5 = 5B (cumulative is 0 at start of Y1)
        assert out.projections[0].depreciation == pytest.approx(5e9, rel=1e-9)

    def test_da_year2_includes_y1_capex(self):
        """Year 2 D&A includes amortization of Year 1's capex."""
        inp = DCFInput(
            base_revenue=100e9,
            revenue_growth_path=[0.05, 0.05],
            operating_margin_path=[0.20, 0.20],
            capex_to_revenue_path=[0.10, 0.10],
            effective_tax_rate=0.20,
            nwc_to_revenue_delta=0.01,
            terminal_growth_rate=0.03,
            wacc=0.09,
            sbc_to_revenue=0.0,
            dilution_rate_annual=0.0,
            shares_outstanding=2_000_000_000,
            net_debt=0,
            horizon_years=2,
            base_depreciation=5e9,
            capex_useful_life_years=5.0,
        )
        out = engine.compute_dcf(inp)
        y1_capex = out.projections[0].capex  # ~10.5B
        # Y2 depreciation = base(5B) + y1_capex/5
        expected_y2_da = 5e9 + abs(y1_capex) / 5.0
        assert out.projections[1].depreciation == pytest.approx(expected_y2_da, rel=1e-6)

    def test_da_grows_then_stabilizes_with_constant_capex(self):
        """With constant capex ratio and 5% growth, D&A grows for useful_life years then stabilizes."""
        n = 10
        inp = DCFInput(
            base_revenue=100e9,
            revenue_growth_path=[0.05] * n,
            operating_margin_path=[0.20] * n,
            capex_to_revenue_path=[0.10] * n,
            effective_tax_rate=0.20,
            nwc_to_revenue_delta=0.01,
            terminal_growth_rate=0.03,
            wacc=0.09,
            sbc_to_revenue=0.0,
            dilution_rate_annual=0.0,
            shares_outstanding=2_000_000_000,
            net_debt=0,
            horizon_years=n,
            base_depreciation=5e9,
            capex_useful_life_years=5.0,
        )
        out = engine.compute_dcf(inp)
        deps = [p.depreciation for p in out.projections]
        # D&A should grow during first useful_life years as new capex accumulates
        for i in range(1, 5):  # Years 2-5
            assert deps[i] >= deps[i - 1], (
                f"D&A should grow in early years: Y{i}={deps[i-1]:.0f} vs Y{i+1}={deps[i]:.0f}"
            )
        # After useful_life, growth should slow dramatically (only driven by revenue growth)
        late_growth = deps[9] / deps[8] - 1
        early_growth = deps[1] / deps[0] - 1
        assert late_growth < early_growth, "D&A growth should slow after useful_life"

    def test_da_plateaus_after_useful_life(self):
        """After useful_life years, old capex should retire and D&A should plateau.

        With constant capex and 5-year useful life, D&A should stabilize by year 6
        as year 1's capex retires.
        """
        n = 10
        # Constant revenue (0% growth) + constant capex ratio → constant annual capex
        inp = DCFInput(
            base_revenue=100e9,
            revenue_growth_path=[0.0] * n,
            operating_margin_path=[0.20] * n,
            capex_to_revenue_path=[0.10] * n,
            effective_tax_rate=0.20,
            nwc_to_revenue_delta=0.0,
            terminal_growth_rate=0.02,
            wacc=0.09,
            sbc_to_revenue=0.0,
            dilution_rate_annual=0.0,
            shares_outstanding=2_000_000_000,
            net_debt=0,
            horizon_years=n,
            base_depreciation=0.0,  # No base → only new capex drives D&A
            capex_useful_life_years=5.0,
        )
        out = engine.compute_dcf(inp)
        # With constant 10B capex/year and 5-year life:
        # Y1 capex starts depreciating in Y2, so the window fills at Y6:
        # Y6 D&A = sum(Y1..Y5 capex)/5 = 50B/5 = 10B
        # Y7 D&A = sum(Y2..Y6 capex)/5 = 50B/5 = 10B (Y1 retired, Y6 entered)
        y6_da = out.projections[5].depreciation  # Year 6
        y7_da = out.projections[6].depreciation  # Year 7
        # After the window is full, D&A should plateau with constant capex
        assert y7_da == pytest.approx(y6_da, rel=0.01), (
            f"D&A should plateau after useful_life+1: Y6={y6_da/1e9:.1f}B, Y7={y7_da/1e9:.1f}B"
        )
        # Also verify the plateau value is correct: 5 × 10B/5 = 10B
        assert y6_da == pytest.approx(10e9, rel=0.01)


# ═══════════════════════════════════════════════════════════════════════
# Segment vs Flat equivalence
# ═══════════════════════════════════════════════════════════════════════

class TestSegmentFlatEquivalence:
    """When a company has 1 segment = the whole company, segment DCF ≈ flat DCF."""

    def test_single_segment_matches_flat(self):
        """Single-segment consolidated DCF should produce same result as flat DCF."""
        n = 5
        growth = [0.10, 0.08, 0.06, 0.05, 0.04]
        margin = [0.30] * n
        capex = [0.15] * n

        flat_input = DCFInput(
            base_revenue=200e9,
            revenue_growth_path=growth,
            operating_margin_path=margin,
            capex_to_revenue_path=capex,
            effective_tax_rate=0.20,
            nwc_to_revenue_delta=0.02,
            terminal_growth_rate=0.03,
            wacc=0.09,
            sbc_to_revenue=0.04,
            dilution_rate_annual=0.0,
            shares_outstanding=5_000_000_000,
            net_debt=10e9,
            horizon_years=n,
            sbc_treatment="expense_in_fcf",
            buyback_yield_annual=0.0,
            base_depreciation=10e9,
            capex_useful_life_years=5.0,
        )

        seg_input = ConsolidatedDCFInput(
            segments={
                "whole_co": SegmentDCFInput(
                    segment_id="whole_co",
                    segment_name="Whole Company",
                    base_revenue=200e9,
                    revenue_growth_path=growth,
                    operating_margin_path=margin,
                    capex_to_revenue_path=capex,
                    terminal_growth_rate=0.03,
                    horizon_years=n,
                ),
            },
            wacc=0.09,
            effective_tax_rate=0.20,
            nwc_to_revenue_delta=0.02,
            sbc_to_revenue=0.04,
            sbc_treatment="expense_in_fcf",
            dilution_rate_annual=0.0,
            buyback_yield_annual=0.0,
            shares_outstanding=5_000_000_000,
            net_debt=10e9,
            horizon_years=n,
            base_depreciation=10e9,
            capex_useful_life_years=5.0,
        )

        flat_out = engine.compute_dcf(flat_input)
        seg_out = engine.compute_consolidated_dcf(seg_input)

        # Per-share values should be identical
        assert flat_out.per_share_value == pytest.approx(
            seg_out.per_share_value, rel=1e-6
        ), (
            f"Single-segment DCF should match flat: "
            f"flat=${flat_out.per_share_value:.2f} vs seg=${seg_out.per_share_value:.2f}"
        )

        # EV should be identical
        assert flat_out.enterprise_value == pytest.approx(
            seg_out.enterprise_value, rel=1e-6
        )

        # Year-by-year FCFF should match
        for i in range(n):
            assert flat_out.projections[i].fcff == pytest.approx(
                seg_out.consolidated_projections[i].fcff, rel=1e-6
            ), f"Year {i+1} FCFF mismatch"

    def test_both_output_types_have_projections_property(self):
        """Both DCFOutput and ConsolidatedDCFOutput expose .projections uniformly."""
        n = 3
        flat_inp = DCFInput(
            base_revenue=100e9,
            revenue_growth_path=[0.05] * n,
            operating_margin_path=[0.20] * n,
            capex_to_revenue_path=[0.10] * n,
            effective_tax_rate=0.20,
            nwc_to_revenue_delta=0.01,
            terminal_growth_rate=0.03,
            wacc=0.09,
            sbc_to_revenue=0.0,
            dilution_rate_annual=0.0,
            shares_outstanding=2_000_000_000,
            net_debt=0,
            horizon_years=n,
        )
        seg_inp = ConsolidatedDCFInput(
            segments={
                "s": SegmentDCFInput(
                    segment_id="s", segment_name="S",
                    base_revenue=100e9,
                    revenue_growth_path=[0.05] * n,
                    operating_margin_path=[0.20] * n,
                    capex_to_revenue_path=[0.10] * n,
                    terminal_growth_rate=0.03, horizon_years=n,
                ),
            },
            wacc=0.09, effective_tax_rate=0.20, nwc_to_revenue_delta=0.01,
            sbc_to_revenue=0.0, shares_outstanding=2_000_000_000,
            net_debt=0, horizon_years=n,
        )

        flat_out = engine.compute_dcf(flat_inp)
        seg_out = engine.compute_consolidated_dcf(seg_inp)

        # Both types expose .projections
        assert hasattr(flat_out, "projections")
        assert len(flat_out.projections) == n

        assert hasattr(seg_out, "projections")
        assert len(seg_out.projections) == n
        # .projections should alias .consolidated_projections
        assert seg_out.projections is seg_out.consolidated_projections
