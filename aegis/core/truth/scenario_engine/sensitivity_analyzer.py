"""Sensitivity Analyzer — required step for every scenario valuation.

Outputs assumption_sensitivity_ranking showing which assumptions
have the largest impact on the final valuation.
"""

from dataclasses import dataclass

from .dcf_engine import DCFEngine, DCFInput


@dataclass(frozen=True)
class SensitivityResult:
    """Sensitivity of valuation to a single assumption."""

    assumption: str
    base_value: float
    shocked_value: float
    shock_pct: float
    base_per_share: float
    shocked_per_share: float
    impact_pct: float  # Absolute % change in per_share_value (used for ranking)
    signed_impact_pct: float = 0.0  # Signed % change — positive if shock raised value


@dataclass(frozen=True)
class SensitivityTable:
    """2D sensitivity table for two variables."""

    variable_1: str
    variable_2: str
    var1_values: list[float]
    var2_values: list[float]
    matrix: list[list[float]]  # matrix[i][j] = per_share_value at var1[i], var2[j]


class SensitivityAnalyzer:
    """Deterministic sensitivity analysis on DCF assumptions."""

    VERSION = "sensitivity_v1"

    def __init__(self) -> None:
        self._dcf = DCFEngine()

    def one_way_sensitivity(
        self,
        base_inputs: DCFInput,
        assumption_name: str,
        shock_pct: float = 0.10,
    ) -> SensitivityResult:
        """Compute impact of shocking one assumption by ±shock_pct.

        Returns the absolute % impact on per_share_value.
        """
        base_output = self._dcf.compute_dcf(base_inputs)
        base_price = base_output.per_share_value

        shocked_inputs = self._apply_shock(base_inputs, assumption_name, shock_pct)
        shocked_output = self._dcf.compute_dcf(shocked_inputs)
        shocked_price = shocked_output.per_share_value

        # BUG-55: Equity value has a floor at zero — shareholders' liability is
        # limited to zero. DCFs with high capex intensity can legitimately
        # produce negative per-share values under large shocks (e.g. META capex
        # shock took FV to -$215/share), but reporting a negative dollar figure
        # is financially nonsensical and erodes report credibility. Floor both
        # base and shocked at 0 for display; impact_pct is still computed from
        # the raw shock so the ranking by magnitude remains correct, but
        # displayed values stay in a meaningful range.
        base_price_display = max(base_price, 0.0)
        shocked_price_display = max(shocked_price, 0.0)

        # impact_pct is computed from raw (unflored) values so ranking magnitude
        # is preserved even when one endpoint clips to zero.
        # BUG-FIX (2026-04-15): previously only stored |Δ|/base, stripping the
        # sign — so the sensitivity table showed "WACC 15.5%" when the actual
        # effect was -15.5% (shocking WACC up makes value go down). We now
        # compute both: impact_pct (abs, for ranking) and signed_impact_pct
        # (signed, for display).
        if base_price != 0:
            signed = (shocked_price - base_price) / abs(base_price)
            impact = abs(signed)
        else:
            signed = float("inf")
            impact = float("inf")

        base_val = self._get_assumption_value(base_inputs, assumption_name)
        shocked_val = self._get_assumption_value(shocked_inputs, assumption_name)

        return SensitivityResult(
            assumption=assumption_name,
            base_value=base_val,
            shocked_value=shocked_val,
            shock_pct=shock_pct,
            base_per_share=base_price_display,     # BUG-55: floored at 0
            shocked_per_share=shocked_price_display,  # BUG-55: floored at 0
            impact_pct=impact,
            signed_impact_pct=signed,
        )

    def rank_assumptions(
        self,
        base_inputs: DCFInput,
        shock_pct: float = 0.10,
    ) -> list[SensitivityResult]:
        """Rank all assumptions by their impact on valuation.

        This is a required step — every thesis must have this ranking.
        """
        assumptions = [
            "revenue_growth",
            "operating_margin",
            "capex_to_revenue",
            "terminal_growth_rate",
            "wacc",
            "effective_tax_rate",
            "sbc_to_revenue",
            "buyback_yield_annual",
        ]

        results = []
        for assumption in assumptions:
            try:
                result = self.one_way_sensitivity(base_inputs, assumption, shock_pct)
                results.append(result)
            except (ValueError, ZeroDivisionError):
                continue

        results.sort(key=lambda r: r.impact_pct, reverse=True)
        return results

    def two_way_table(
        self,
        base_inputs: DCFInput,
        var1_name: str,
        var1_range: list[float],
        var2_name: str,
        var2_range: list[float],
    ) -> SensitivityTable:
        """Generate a 2D sensitivity table."""
        matrix: list[list[float]] = []

        for v1 in var1_range:
            row: list[float] = []
            for v2 in var2_range:
                modified = self._set_assumption(base_inputs, var1_name, v1)
                modified = self._set_assumption(modified, var2_name, v2)
                output = self._dcf.compute_dcf(modified)
                # BUG-55: floor at 0 — negative per-share equity is nonsensical
                row.append(round(max(output.per_share_value, 0.0), 2))
            matrix.append(row)

        return SensitivityTable(
            variable_1=var1_name,
            variable_2=var2_name,
            var1_values=var1_range,
            var2_values=var2_range,
            matrix=matrix,
        )

    def _apply_shock(
        self, inputs: DCFInput, assumption: str, shock_pct: float
    ) -> DCFInput:
        """Create a new DCFInput with one assumption shocked.

        BUG-FIX (2026-04-15): For path-type assumptions (revenue_growth,
        operating_margin, capex_to_revenue), the previous implementation
        scaled `path[0]` and then called `_set_assumption`, which REPLACED
        the entire horizon with that single scalar. For expanding paths
        like `[3.2%, 3.5%, 3.8%, ...]`, a +10% shock collapsed years 2+
        from 3.5/3.8 down to 3.52, producing a NEGATIVE valuation impact
        from a "positive" margin shock — the opposite of what sensitivity
        analysis is supposed to measure. Now we scale every element by
        the same factor so the shape of the path is preserved.
        """
        if assumption in ("revenue_growth", "operating_margin", "capex_to_revenue"):
            return self._scale_path(inputs, assumption, 1 + shock_pct)
        val = self._get_assumption_value(inputs, assumption)
        new_val = val * (1 + shock_pct)
        return self._set_assumption(inputs, assumption, new_val)

    @staticmethod
    def _scale_path(inputs: DCFInput, assumption: str, factor: float) -> DCFInput:
        """Scale every element of a path-type assumption by the same factor.

        Preserves the shape (expansion/contraction profile) of the base
        path so sensitivity reflects a uniform stress, not a flat-line
        replacement.
        """
        kwargs = {
            "base_revenue": inputs.base_revenue,
            "revenue_growth_path": list(inputs.revenue_growth_path),
            "operating_margin_path": list(inputs.operating_margin_path),
            "capex_to_revenue_path": list(inputs.capex_to_revenue_path),
            "effective_tax_rate": inputs.effective_tax_rate,
            "nwc_to_revenue_delta": inputs.nwc_to_revenue_delta,
            "terminal_growth_rate": inputs.terminal_growth_rate,
            "wacc": inputs.wacc,
            "sbc_to_revenue": inputs.sbc_to_revenue,
            "dilution_rate_annual": inputs.dilution_rate_annual,
            "shares_outstanding": inputs.shares_outstanding,
            "net_debt": inputs.net_debt,
            "horizon_years": inputs.horizon_years,
            "sbc_treatment": inputs.sbc_treatment,
            "sbc_treatment_justification": inputs.sbc_treatment_justification,
            "buyback_yield_annual": inputs.buyback_yield_annual,
            "base_depreciation": inputs.base_depreciation,
            "capex_useful_life_years": inputs.capex_useful_life_years,
        }
        if assumption == "revenue_growth":
            kwargs["revenue_growth_path"] = [v * factor for v in inputs.revenue_growth_path]
        elif assumption == "operating_margin":
            kwargs["operating_margin_path"] = [v * factor for v in inputs.operating_margin_path]
        elif assumption == "capex_to_revenue":
            kwargs["capex_to_revenue_path"] = [v * factor for v in inputs.capex_to_revenue_path]
        else:
            raise ValueError(f"_scale_path called with non-path assumption: {assumption}")
        return DCFInput(**kwargs)

    @staticmethod
    def _get_assumption_value(inputs: DCFInput, assumption: str) -> float:
        """Extract a scalar representative value for an assumption."""
        if assumption == "revenue_growth":
            return inputs.revenue_growth_path[0]
        elif assumption == "operating_margin":
            return inputs.operating_margin_path[0]
        elif assumption == "capex_to_revenue":
            return inputs.capex_to_revenue_path[0]
        elif assumption == "terminal_growth_rate":
            return inputs.terminal_growth_rate
        elif assumption == "wacc":
            return inputs.wacc
        elif assumption == "effective_tax_rate":
            return inputs.effective_tax_rate
        elif assumption == "sbc_to_revenue":
            return inputs.sbc_to_revenue
        elif assumption == "buyback_yield_annual":
            return inputs.buyback_yield_annual
        else:
            raise ValueError(f"Unknown assumption: {assumption}")

    @staticmethod
    def _set_assumption(inputs: DCFInput, assumption: str, value: float) -> DCFInput:
        """Create a new DCFInput with one assumption replaced."""
        kwargs = {
            "base_revenue": inputs.base_revenue,
            "revenue_growth_path": list(inputs.revenue_growth_path),
            "operating_margin_path": list(inputs.operating_margin_path),
            "capex_to_revenue_path": list(inputs.capex_to_revenue_path),
            "effective_tax_rate": inputs.effective_tax_rate,
            "nwc_to_revenue_delta": inputs.nwc_to_revenue_delta,
            "terminal_growth_rate": inputs.terminal_growth_rate,
            "wacc": inputs.wacc,
            "sbc_to_revenue": inputs.sbc_to_revenue,
            "dilution_rate_annual": inputs.dilution_rate_annual,
            "shares_outstanding": inputs.shares_outstanding,
            "net_debt": inputs.net_debt,
            "horizon_years": inputs.horizon_years,
            "sbc_treatment": inputs.sbc_treatment,
            "sbc_treatment_justification": inputs.sbc_treatment_justification,
            "buyback_yield_annual": inputs.buyback_yield_annual,
            "base_depreciation": inputs.base_depreciation,
            "capex_useful_life_years": inputs.capex_useful_life_years,
        }

        if assumption == "revenue_growth":
            kwargs["revenue_growth_path"] = [value] * inputs.horizon_years
        elif assumption == "operating_margin":
            kwargs["operating_margin_path"] = [value] * inputs.horizon_years
        elif assumption == "capex_to_revenue":
            kwargs["capex_to_revenue_path"] = [value] * inputs.horizon_years
        elif assumption == "terminal_growth_rate":
            kwargs["terminal_growth_rate"] = value
        elif assumption == "wacc":
            kwargs["wacc"] = value
        elif assumption == "effective_tax_rate":
            kwargs["effective_tax_rate"] = value
        elif assumption == "sbc_to_revenue":
            kwargs["sbc_to_revenue"] = value
        elif assumption == "buyback_yield_annual":
            kwargs["buyback_yield_annual"] = value
        else:
            raise ValueError(f"Unknown assumption: {assumption}")

        return DCFInput(**kwargs)
