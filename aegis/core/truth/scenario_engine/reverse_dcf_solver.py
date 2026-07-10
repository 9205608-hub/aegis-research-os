"""Reverse DCF Solver — what does the market price imply?

Given current price, solve for the implied growth rate, margin,
or terminal growth that justifies it.
"""

from dataclasses import dataclass

from .dcf_engine import DCFEngine, DCFInput


@dataclass(frozen=True)
class ReverseDCFResult:
    """Result of a reverse DCF solve."""

    target_variable: str  # Which variable was solved for
    implied_value: float
    current_price: float
    tolerance: float
    iterations: int
    converged: bool
    # BUG-Y13 (2026-05-06): when bisection terminates at a boundary, the
    # numeric `implied_value` is a stale midpoint that looks like a real
    # answer (e.g. exactly 0.50 for `growth_high=0.50`). For loss-making /
    # high-capex companies (Cambricon FY2025 FCF=¥-10.7亿), the price-vs-
    # growth curve is non-monotonic — high growth makes capex more negative,
    # which lowers per-share value — and the bisection silently fails. Set
    # this flag to `"low" | "high" | None` and let callers report `n/a`
    # rather than a fake clean number like "implied growth = 50.00%".
    boundary_hit: str | None = None


class ReverseDCFSolver:
    """Solves for implied assumptions given current market price.

    Uses bisection method for robustness — no gradient needed.
    """

    VERSION = "reverse_dcf_v1"

    def __init__(self) -> None:
        self._dcf = DCFEngine()

    def solve_implied_growth(
        self,
        *,
        current_price: float,
        base_revenue: float,
        operating_margin_path: list[float],
        capex_to_revenue_path: list[float],
        effective_tax_rate: float,
        nwc_to_revenue_delta: float,
        terminal_growth_rate: float,
        wacc: float,
        sbc_to_revenue: float,
        dilution_rate_annual: float,
        shares_outstanding: float,
        net_debt: float,
        horizon_years: int,
        growth_low: float = -0.10,
        growth_high: float = 0.50,
        tolerance: float = 0.01,
        max_iterations: int = 100,
        # AUDIT-A2 (2026-07): the solver used to omit these three fields, so
        # its DCFInput fell back to defaults (base_depreciation=0.0) while
        # the forward DCF ran with real D&A — two different FCFF definitions
        # for the same company. Round-tripping the forward model's own price
        # then reported an implied growth ~2× the true rate. Defaults keep
        # the old call signature working; callers should pass through from
        # the same DCFInput used for the forward model.
        base_depreciation: float = 0.0,
        capex_useful_life_years: float = 5.0,
        buyback_yield_annual: float = 0.0,
    ) -> ReverseDCFResult:
        """Solve for the uniform revenue growth rate implied by current price."""

        def price_at_growth(g: float) -> float:
            dcf_input = DCFInput(
                base_revenue=base_revenue,
                revenue_growth_path=[g] * horizon_years,
                operating_margin_path=operating_margin_path,
                capex_to_revenue_path=capex_to_revenue_path,
                effective_tax_rate=effective_tax_rate,
                nwc_to_revenue_delta=nwc_to_revenue_delta,
                terminal_growth_rate=terminal_growth_rate,
                wacc=wacc,
                sbc_to_revenue=sbc_to_revenue,
                dilution_rate_annual=dilution_rate_annual,
                shares_outstanding=shares_outstanding,
                net_debt=net_debt,
                horizon_years=horizon_years,
                # AUDIT-A2: keep reverse model identical to the forward model
                base_depreciation=base_depreciation,
                capex_useful_life_years=capex_useful_life_years,
                buyback_yield_annual=buyback_yield_annual,
            )
            output = self._dcf.compute_dcf(dcf_input)
            return output.per_share_value

        return self._bisect(
            target_variable="implied_revenue_growth",
            target_price=current_price,
            fn=price_at_growth,
            low=growth_low,
            high=growth_high,
            tolerance=tolerance,
            max_iterations=max_iterations,
        )

    def solve_implied_terminal_growth(
        self,
        *,
        current_price: float,
        base_revenue: float,
        revenue_growth_path: list[float],
        operating_margin_path: list[float],
        capex_to_revenue_path: list[float],
        effective_tax_rate: float,
        nwc_to_revenue_delta: float,
        wacc: float,
        sbc_to_revenue: float,
        dilution_rate_annual: float,
        shares_outstanding: float,
        net_debt: float,
        horizon_years: int,
        tg_low: float = 0.0,
        tg_high: float | None = None,
        tolerance: float = 0.01,
        max_iterations: int = 100,
        # AUDIT-A2: same forward/reverse model-parity pass-through as
        # solve_implied_growth above.
        base_depreciation: float = 0.0,
        capex_useful_life_years: float = 5.0,
        buyback_yield_annual: float = 0.0,
    ) -> ReverseDCFResult:
        """Solve for implied terminal growth rate."""
        if tg_high is None:
            tg_high = wacc - 0.005  # Must be below WACC

        def price_at_tg(tg: float) -> float:
            dcf_input = DCFInput(
                base_revenue=base_revenue,
                revenue_growth_path=revenue_growth_path,
                operating_margin_path=operating_margin_path,
                capex_to_revenue_path=capex_to_revenue_path,
                effective_tax_rate=effective_tax_rate,
                nwc_to_revenue_delta=nwc_to_revenue_delta,
                terminal_growth_rate=tg,
                wacc=wacc,
                sbc_to_revenue=sbc_to_revenue,
                dilution_rate_annual=dilution_rate_annual,
                shares_outstanding=shares_outstanding,
                net_debt=net_debt,
                horizon_years=horizon_years,
                # AUDIT-A2: keep reverse model identical to the forward model
                base_depreciation=base_depreciation,
                capex_useful_life_years=capex_useful_life_years,
                buyback_yield_annual=buyback_yield_annual,
            )
            output = self._dcf.compute_dcf(dcf_input)
            return output.per_share_value

        return self._bisect(
            target_variable="implied_terminal_growth",
            target_price=current_price,
            fn=price_at_tg,
            low=tg_low,
            high=tg_high,
            tolerance=tolerance,
            max_iterations=max_iterations,
        )

    @staticmethod
    def _bisect(
        *,
        target_variable: str,
        target_price: float,
        fn: "callable",
        low: float,
        high: float,
        tolerance: float,
        max_iterations: int,
    ) -> ReverseDCFResult:
        """Bisection search for the variable value that produces target_price."""
        # BUG-Y13: capture the original interval so we can later detect
        # boundary-stuck termination (e.g. solver tried to push past
        # growth_high=0.50 because the price-vs-growth curve is non-monotonic).
        orig_low, orig_high = low, high

        for i in range(max_iterations):
            mid = (low + high) / 2
            computed_price = fn(mid)

            if abs(computed_price - target_price) <= tolerance:
                return ReverseDCFResult(
                    target_variable=target_variable,
                    implied_value=mid,
                    current_price=target_price,
                    tolerance=tolerance,
                    iterations=i + 1,
                    converged=True,
                )

            if computed_price < target_price:
                low = mid
            else:
                high = mid

        # Did not converge within max_iterations
        mid = (low + high) / 2

        # BUG-Y13: detect boundary-stuck termination. If the final midpoint
        # is within 1% of either original boundary (and didn't converge),
        # the bisection most likely hit a non-monotonic region or the true
        # implied value lies outside [orig_low, orig_high]. Flag it.
        boundary_hit: str | None = None
        span = (orig_high - orig_low) or 1.0
        if abs(mid - orig_high) / span < 0.01:
            boundary_hit = "high"
        elif abs(mid - orig_low) / span < 0.01:
            boundary_hit = "low"

        return ReverseDCFResult(
            target_variable=target_variable,
            implied_value=mid,
            current_price=target_price,
            tolerance=tolerance,
            iterations=max_iterations,
            converged=False,
            boundary_hit=boundary_hit,
        )
