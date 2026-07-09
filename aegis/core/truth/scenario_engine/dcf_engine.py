"""DCF Engine — deterministic forward valuation.

LLM agents provide assumptions. This engine executes calculations.
Agent never computes directly. Engine never assumes.

Supports:
- Company-level DCF (DCFInput → DCFOutput)
- Segment-level DCF (ConsolidatedDCFInput → ConsolidatedDCFOutput)
- Driver-based revenue modeling (RevenueDriverTree → revenue_growth_path)
- Buyback modeling (net share change = SBC dilution - buyback yield)
- Ratio-based D&A bridge (AUDIT-A1): D&A_t = da_ratio_t × revenue_t, with
  da_ratio_t converging linearly to the capex/revenue ratio so the terminal
  year satisfies the steady-state conservation D&A ≈ CapEx
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DCFInput:
    """Inputs for a company-level DCF valuation.

    SBC Treatment:
        sbc_treatment controls how stock-based compensation enters the model.
        - "expense_in_fcf": SBC deducted from FCF; dilution_rate_annual ignored.
        - "dilution_only": sbc_to_revenue ignored; diluted shares used instead.
        - "both_with_justification": both applied; requires non-empty justification.
        Default is "expense_in_fcf" to avoid double-counting by default.

    Buyback Modeling:
        buyback_yield_annual represents annual share reduction from buybacks.
        Net share change = (1 + dilution) × (1 - buyback_yield) - 1
    """

    base_revenue: float
    revenue_growth_path: list[float]  # Year-by-year growth rates
    operating_margin_path: list[float]
    capex_to_revenue_path: list[float]
    effective_tax_rate: float
    nwc_to_revenue_delta: float  # Change in NWC as % of revenue delta
    terminal_growth_rate: float
    wacc: float
    sbc_to_revenue: float
    dilution_rate_annual: float
    shares_outstanding: float
    net_debt: float  # Positive = debt > cash
    horizon_years: int
    sbc_treatment: str = "expense_in_fcf"
    sbc_treatment_justification: str = ""
    buyback_yield_annual: float = 0.0  # Annual share reduction from buybacks
    # AUDIT-A1: anchors the ratio-based D&A bridge (da_ratio = base D&A /
    # base revenue). Enters FCFF via the add-back — not display-only.
    base_depreciation: float = 0.0  # Year-0 D&A
    # AUDIT-A1: retained for input-model compatibility (callers/solver pass
    # it), but no longer drives the D&A bridge since the ratio model.
    capex_useful_life_years: float = 5.0  # Avg useful life for new CapEx
    currency: str = "USD"  # "USD", "CNY", etc.


# ---------------------------------------------------------------------------
# Driver-based revenue modeling
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RevenueDriver:
    """A single component in a revenue decomposition.

    Example: DAU, Sessions/DAU, Ads/Session, CPM
    Each driver has its own base value and 10-year growth path.
    """

    name: str
    base_value: float  # Current period absolute value
    growth_path: list[float]  # Year-by-year growth rates for this driver
    unit: str = ""  # "millions", "$/CPM", "sessions/day"
    note: str = ""  # Why this growth rate


@dataclass(frozen=True)
class RevenueDriverTree:
    """Structured revenue decomposition: Revenue = ∏ drivers × base_scale.

    The tree uses multiplicative composition:
      Revenue_Y(n) = base_revenue × ∏_d (driver_d_base * ∏_{i=1..n} (1 + growth_d_i))
                     ÷ ∏_d (driver_d_base)

    Simplified: for each year, project each driver forward, multiply them together,
    and scale so that Year 0 matches base_revenue.
    """

    entity_id: str
    sector_pack_id: str
    decomposition_formula: str  # e.g. "Revenue = DAU × Sessions/DAU × CPM/1000"
    drivers: list[RevenueDriver]
    horizon_years: int = 10


def resolve_driver_revenue(
    base_revenue: float,
    driver_tree: RevenueDriverTree,
) -> tuple[list[float], list[dict[str, float]]]:
    """Convert a driver tree into a revenue growth path.

    Returns:
        (revenue_growth_path, driver_projections)

        revenue_growth_path: list[float] of length horizon_years
        driver_projections: list of dicts, one per year, mapping driver name
            to its projected absolute value for that year
    """
    n = driver_tree.horizon_years
    drivers = driver_tree.drivers
    if not drivers:
        return [0.03] * n, []

    # Validate all drivers have growth paths of length n
    for d in drivers:
        if len(d.growth_path) < n:
            raise ValueError(
                f"Driver '{d.name}' has growth_path of length {len(d.growth_path)}, "
                f"need {n}"
            )

    # Year 0: product of all driver base values
    base_product = 1.0
    for d in drivers:
        base_product *= d.base_value

    # Scale factor so that base_product × scale = base_revenue
    scale = base_revenue / base_product if base_product != 0 else 1.0

    # Project forward year by year
    current_values = {d.name: d.base_value for d in drivers}
    prev_revenue = base_revenue
    growth_path: list[float] = []
    projections: list[dict[str, float]] = []

    for yr in range(n):
        # Advance each driver
        for d in drivers:
            current_values[d.name] *= (1 + d.growth_path[yr])

        # Revenue = scale × product of current driver values
        product = 1.0
        for d in drivers:
            product *= current_values[d.name]
        year_revenue = scale * product

        # Derive implied revenue growth rate
        g = (year_revenue - prev_revenue) / prev_revenue if prev_revenue else 0.0
        growth_path.append(round(g, 6))

        projections.append(dict(current_values))
        prev_revenue = year_revenue

    return growth_path, projections


def apply_driver_deltas(
    driver_tree: RevenueDriverTree,
    driver_deltas: dict[str, list[float]],
) -> RevenueDriverTree:
    """Create a new driver tree with scenario deltas applied to specific drivers.

    driver_deltas maps driver name → list of growth rate deltas (additive).
    Example: {"CPM": [-0.02, -0.01, 0.0, ...]} reduces CPM growth by 2% in Y1.
    """
    new_drivers = []
    for d in driver_tree.drivers:
        if d.name in driver_deltas:
            deltas = driver_deltas[d.name]
            new_growth = [
                d.growth_path[i] + deltas[i]
                for i in range(min(len(d.growth_path), len(deltas)))
            ]
            # Pad if deltas is shorter
            new_growth.extend(d.growth_path[len(new_growth):])
            new_drivers.append(RevenueDriver(
                name=d.name,
                base_value=d.base_value,
                growth_path=new_growth,
                unit=d.unit,
                note=d.note,
            ))
        else:
            new_drivers.append(d)

    return RevenueDriverTree(
        entity_id=driver_tree.entity_id,
        sector_pack_id=driver_tree.sector_pack_id,
        decomposition_formula=driver_tree.decomposition_formula,
        drivers=new_drivers,
        horizon_years=driver_tree.horizon_years,
    )


@dataclass(frozen=True)
class DCFProjection:
    """Year-by-year projected financials."""

    year: int
    revenue: float
    operating_income: float
    taxes: float
    nopat: float
    depreciation: float  # Projected D&A add-back (ratio model — AUDIT-A1)
    capex: float
    change_in_nwc: float
    sbc: float
    fcff: float  # Free cash flow to firm
    fcfe: float  # Free cash flow to equity (FCFF - SBC)
    discount_factor: float
    pv_fcff: float


@dataclass(frozen=True)
class DCFOutput:
    """Complete DCF valuation output."""

    projections: list[DCFProjection]
    terminal_value: float
    pv_terminal_value: float
    pv_fcff_sum: float
    enterprise_value: float
    equity_value: float
    per_share_value: float
    implied_exit_multiple: float | None  # Terminal EV / Terminal EBITDA; None when terminal EBITDA ≤ 0
    future_shares: float = 0.0  # Share count at horizon (after dilution/buyback)
    # Refactor 4 (2026-05-04): the engine self-reports whether its output is
    # interpretable as a price target. False when:
    #   - per_share_value ≤ 0 (DCF says equity is worthless or negative)
    #   - enterprise_value ≤ 0 (cumulative discounted FCFs net negative)
    #   - the loss-making margin path produces a structurally negative FCFF
    #     trajectory that mean-reversion can't rescue within the horizon
    # Renderers and downstream consumers MUST check this flag before
    # rendering a price target. The previous design re-derived an
    # `_dcf_meaningful` proxy at every renderer site, missing edge cases.
    is_meaningful: bool = True
    # Optional human-readable reason — surfaces in UI when false.
    not_meaningful_reason: str = ""


# ---------------------------------------------------------------------------
# Segment-level DCF dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SegmentDCFInput:
    """Per-segment DCF assumptions.

    Each segment has its own revenue, margin, and capex trajectory.
    Company-level items (tax, WACC, SBC, shares, debt) are in ConsolidatedDCFInput.
    """

    segment_id: str
    segment_name: str
    base_revenue: float
    revenue_growth_path: list[float]
    operating_margin_path: list[float]
    capex_to_revenue_path: list[float]
    terminal_growth_rate: float
    horizon_years: int


@dataclass(frozen=True)
class SegmentProjection:
    """Year-by-year projected financials for a single segment."""

    year: int
    revenue: float
    operating_income: float
    capex: float


@dataclass(frozen=True)
class SegmentDCFOutput:
    """Per-segment valuation result."""

    segment_id: str
    segment_name: str
    projections: list[SegmentProjection]
    terminal_revenue: float
    terminal_operating_income: float


@dataclass(frozen=True)
class ConsolidatedDCFInput:
    """Multi-segment consolidated DCF input.

    Segments define revenue/margin/capex paths individually.
    Company-level items are shared across all segments.
    """

    segments: dict[str, SegmentDCFInput]
    wacc: float
    effective_tax_rate: float
    nwc_to_revenue_delta: float
    sbc_to_revenue: float
    sbc_treatment: str = "expense_in_fcf"
    sbc_treatment_justification: str = ""
    dilution_rate_annual: float = 0.0
    buyback_yield_annual: float = 0.0
    shares_outstanding: float = 0
    net_debt: float = 0.0
    horizon_years: int = 10
    base_depreciation: float = 0.0
    capex_useful_life_years: float = 5.0


@dataclass(frozen=True)
class ConsolidatedDCFOutput:
    """Multi-segment consolidated output.

    The `projections` property aliases `consolidated_projections` so that
    downstream code can consume both DCFOutput and ConsolidatedDCFOutput
    uniformly via `output.projections` instead of brittle hasattr() checks.
    """

    segments: dict[str, SegmentDCFOutput]
    consolidated_projections: list[DCFProjection]
    terminal_value: float
    pv_terminal_value: float
    pv_fcff_sum: float
    enterprise_value: float
    equity_value: float
    per_share_value: float
    implied_exit_multiple: float | None  # None when terminal EBITDA ≤ 0
    future_shares: float = 0.0

    @property
    def projections(self) -> list[DCFProjection]:
        """Alias for consolidated_projections — unifies DCFOutput interface."""
        return self.consolidated_projections


class DCFEngine:
    """Deterministic DCF valuation engine.

    This is a pure computation module with zero LLM inference.
    Supports both company-level and segment-level DCF.
    """

    VERSION = "dcf_engine_v2"

    def compute_dcf(self, inputs: DCFInput) -> DCFOutput:
        """Execute a full company-level DCF valuation."""
        n = inputs.horizon_years
        assert len(inputs.revenue_growth_path) == n
        assert len(inputs.operating_margin_path) == n
        assert len(inputs.capex_to_revenue_path) == n

        # ── Input validation (P0-REVIEW 2026-04-16) ──
        if inputs.shares_outstanding < 1e6:
            raise ValueError(
                f"shares_outstanding={inputs.shares_outstanding:.0f} is implausibly "
                f"small (<1M). Check data pipeline."
            )
        if inputs.base_revenue <= 0:
            raise ValueError(f"base_revenue={inputs.base_revenue} must be positive.")
        # BUG-26 (2026-05-04): CAS cashflow reports capex as a negative
        # outflow, and a few callers (scenario architect / direct test
        # fixtures) still hand us those raw values. Sanitize to a local
        # positive-ratio list so FCFF math, D&A retirement, and sensitivity
        # analyzer all see a consistent positive ratio. DCFInput is frozen,
        # so we bind to a local name instead of mutating the input.
        capex_ratio_path = [abs(r) for r in inputs.capex_to_revenue_path]
        if any(r < 0 for r in inputs.capex_to_revenue_path):
            negs = [r for r in inputs.capex_to_revenue_path if r < 0]
            import logging as _logging
            _logging.getLogger(__name__).debug(
                "capex_to_revenue_path had %d negative values %s — "
                "auto-corrected with abs() (CAS outflow convention).",
                len(negs), negs[:3],
            )
        if inputs.wacc <= 0 or inputs.wacc > 0.30:
            import warnings
            warnings.warn(f"WACC={inputs.wacc:.2%} outside typical 3-25% range.")

        # Enforce SBC treatment to prevent double-counting
        if inputs.sbc_treatment == "expense_in_fcf":
            effective_sbc = inputs.sbc_to_revenue
            effective_dilution = 0.0
        elif inputs.sbc_treatment == "dilution_only":
            effective_sbc = 0.0
            effective_dilution = inputs.dilution_rate_annual
        elif inputs.sbc_treatment == "both_with_justification":
            if not inputs.sbc_treatment_justification.strip():
                raise ValueError(
                    "sbc_treatment='both_with_justification' requires non-empty "
                    "sbc_treatment_justification explaining why double application "
                    "is appropriate."
                )
            effective_sbc = inputs.sbc_to_revenue
            effective_dilution = inputs.dilution_rate_annual
        else:
            raise ValueError(f"Unknown sbc_treatment: {inputs.sbc_treatment}")

        projections: list[DCFProjection] = []
        prev_revenue = inputs.base_revenue

        # AUDIT-A1 (2026-07): ratio-based D&A bridge.
        # The old bridge (`base_depreciation + active_capex/life`) held the
        # base D&A constant for all 10 years (base asset pool never retired)
        # while the GAAP EBIT margin already embeds D&A scaling with revenue
        # — double-counting that inflated FCFF, the Gordon terminal value,
        # and per-share value by ~27% for capital-intensive names. New
        # model: D&A_t = da_ratio_t × revenue_t, where da_ratio_t converges
        # linearly from base D&A / base revenue to that year's capex/revenue
        # ratio, so the terminal year satisfies the steady-state
        # conservation D&A == CapEx. When base_depreciation is missing
        # upstream (0.0 default), da_ratio ramps from 0 to the capex ratio —
        # a bounded, mild undervaluation instead of the old D&A cliff.
        # abs() mirrors the BUG-26 capex sanitize (outflow sign convention).
        da_ratio_base = abs(inputs.base_depreciation) / inputs.base_revenue

        for i in range(n):
            year = i + 1
            revenue = prev_revenue * (1 + inputs.revenue_growth_path[i])
            operating_income = revenue * inputs.operating_margin_path[i]
            taxes = operating_income * inputs.effective_tax_rate
            nopat = operating_income - taxes
            capex = revenue * capex_ratio_path[i]
            revenue_delta = revenue - prev_revenue
            change_in_nwc = revenue_delta * inputs.nwc_to_revenue_delta
            sbc = revenue * effective_sbc

            # AUDIT-A1: blend weight reaches 1.0 in the terminal year, so
            # terminal D&A == terminal capex (steady-state conservation).
            w = year / n
            da_ratio_t = da_ratio_base * (1.0 - w) + capex_ratio_path[i] * w
            depreciation = revenue * da_ratio_t

            # FCFF = NOPAT + D&A - CapEx - ΔNWC. EBIT already subtracted D&A,
            # so NOPAT needs D&A added back as a non-cash item. abs(capex) makes
            # us robust to either sign convention (yfinance: negative = outflow;
            # eastmoney: positive = magnitude).
            fcff = nopat + depreciation - abs(capex) - change_in_nwc
            fcfe = fcff - sbc

            # ── FCFF sanity checks (P0-REVIEW 2026-04-16) ──
            # BUG-Y19 (2026-05-06): warning text used `${X/1e9:.1f}B` which
            # was misleading for A-share inputs (¥-10.7亿 FCFF rendered as
            # `-1.1B`). The engine is currency-agnostic, so just show the
            # ratio — that's the actual signal these warnings are flagging.
            if year == 1:
                if fcff > revenue:
                    import warnings
                    warnings.warn(
                        f"Year 1 FCFF/Revenue = {fcff/revenue:.2f} > 1.0. "
                        f"Check margin/capex assumptions."
                    )
                if fcff < -revenue * 0.5:
                    import warnings
                    warnings.warn(
                        f"Year 1 FCFF/Revenue = {fcff/revenue:.2f} (< -0.50). "
                        f"Model may be unstable for loss-making cos."
                    )

            discount_factor = 1 / (1 + inputs.wacc) ** year
            pv_fcff = fcff * discount_factor

            projections.append(DCFProjection(
                year=year,
                revenue=revenue,
                operating_income=operating_income,
                taxes=taxes,
                nopat=nopat,
                depreciation=depreciation,
                capex=capex,
                change_in_nwc=change_in_nwc,
                sbc=sbc,
                fcff=fcff,
                fcfe=fcfe,
                discount_factor=discount_factor,
                pv_fcff=pv_fcff,
            ))
            prev_revenue = revenue

        # Terminal value (Gordon Growth Model on terminal year FCFF)
        terminal_fcff = projections[-1].fcff
        if inputs.wacc <= inputs.terminal_growth_rate:
            raise ValueError(
                f"WACC ({inputs.wacc}) must exceed terminal growth rate "
                f"({inputs.terminal_growth_rate}) for convergent terminal value."
            )
        terminal_value = (
            terminal_fcff * (1 + inputs.terminal_growth_rate)
            / (inputs.wacc - inputs.terminal_growth_rate)
        )
        terminal_discount_factor = 1 / (1 + inputs.wacc) ** n
        pv_terminal_value = terminal_value * terminal_discount_factor

        pv_fcff_sum = sum(p.pv_fcff for p in projections)
        enterprise_value = pv_fcff_sum + pv_terminal_value
        equity_value = enterprise_value - inputs.net_debt

        # BUG-29/30 fix: In a FCFF-based DCF, buybacks are a financing
        # decision — they don't reduce FCFF.  Using reduced future_shares
        # without subtracting buyback cash from equity_value double-counts
        # the buyback benefit.  Standard practice: divide equity_value by
        # CURRENT shares outstanding.
        #
        # SBC dilution is different: when sbc_treatment == "expense_in_fcf",
        # the SBC cost is already deducted from cash flows, so additional
        # share dilution is NOT double-counting — it reflects the real
        # ownership dilution.  We still project future_shares for SBC-only
        # dilution (ignoring buyback) to give a conservative per-share.
        net_share_change = (
            (1 + effective_dilution) * (1 - inputs.buyback_yield_annual) - 1
        )
        future_shares = inputs.shares_outstanding * ((1 + net_share_change) ** n)
        # Use current shares — cleanest FCFF-to-equity conversion
        per_share_value = equity_value / inputs.shares_outstanding if inputs.shares_outstanding > 0 else 0

        # Implied exit multiple
        terminal_ebitda = projections[-1].operating_income  # Approximation
        # TODO-Y7 (2026-05-06): use None as the sentinel rather than
        # float("inf"). inf serializes to invalid JSON (`Infinity`) and
        # breaks any downstream React/JSON consumer; None is portable and
        # the html_report layer already handles it as "n/m".
        implied_exit_multiple = (
            terminal_value / terminal_ebitda if terminal_ebitda > 0 else None
        )

        # Refactor 4 (2026-05-04): self-report whether the output is a
        # valid price-target signal. A negative or zero per-share value
        # signals the company's cash flows can't support an equity claim
        # under the supplied assumptions — common for distressed/loss-
        # making issuers where margin recovery never reaches breakeven.
        # Downstream consumers should display "n/m" + alternate anchor
        # (book value, peer multiples) when this flag is False.
        _meaningful = per_share_value > 0 and enterprise_value > 0
        _reason = ""
        if not _meaningful:
            _why = []
            if per_share_value <= 0:
                _why.append(f"per_share_value={per_share_value:.2f}")
            if enterprise_value <= 0:
                _why.append(f"enterprise_value={enterprise_value:.0f}")
            _reason = "DCF base non-positive: " + ", ".join(_why)
        return DCFOutput(
            projections=projections,
            terminal_value=terminal_value,
            pv_terminal_value=pv_terminal_value,
            pv_fcff_sum=pv_fcff_sum,
            enterprise_value=enterprise_value,
            equity_value=equity_value,
            per_share_value=per_share_value,
            implied_exit_multiple=implied_exit_multiple,
            future_shares=future_shares,
            is_meaningful=_meaningful,
            not_meaningful_reason=_reason,
        )

    def compute_consolidated_dcf(
        self, inputs: ConsolidatedDCFInput
    ) -> ConsolidatedDCFOutput:
        """Execute a segment-level DCF and consolidate to company level.

        Each segment has its own revenue/margin/capex paths.
        Tax, NWC, SBC, WACC, terminal value are applied at consolidated level.
        """
        n = inputs.horizon_years

        # Enforce SBC treatment
        if inputs.sbc_treatment == "expense_in_fcf":
            effective_sbc = inputs.sbc_to_revenue
            effective_dilution = 0.0
        elif inputs.sbc_treatment == "dilution_only":
            effective_sbc = 0.0
            effective_dilution = inputs.dilution_rate_annual
        elif inputs.sbc_treatment == "both_with_justification":
            if not inputs.sbc_treatment_justification.strip():
                raise ValueError(
                    "sbc_treatment='both_with_justification' requires non-empty "
                    "sbc_treatment_justification."
                )
            effective_sbc = inputs.sbc_to_revenue
            effective_dilution = inputs.dilution_rate_annual
        else:
            raise ValueError(f"Unknown sbc_treatment: {inputs.sbc_treatment}")

        # Step 1: Compute per-segment projections
        segment_outputs: dict[str, SegmentDCFOutput] = {}
        for seg_id, seg in inputs.segments.items():
            assert len(seg.revenue_growth_path) == n
            assert len(seg.operating_margin_path) == n
            assert len(seg.capex_to_revenue_path) == n

            # BUG-26: same CAS-outflow sanitize as flat path above. SegmentDCFInput
            # is also frozen, so use a local positive-ratio list.
            seg_capex_ratio_path = [abs(r) for r in seg.capex_to_revenue_path]

            seg_projections: list[SegmentProjection] = []
            prev_rev = seg.base_revenue
            for i in range(n):
                rev = prev_rev * (1 + seg.revenue_growth_path[i])
                oi = rev * seg.operating_margin_path[i]
                cx = rev * seg_capex_ratio_path[i]
                seg_projections.append(SegmentProjection(
                    year=i + 1, revenue=rev, operating_income=oi, capex=cx,
                ))
                prev_rev = rev

            segment_outputs[seg_id] = SegmentDCFOutput(
                segment_id=seg_id,
                segment_name=seg.segment_name,
                projections=seg_projections,
                terminal_revenue=seg_projections[-1].revenue,
                terminal_operating_income=seg_projections[-1].operating_income,
            )

        # Step 2: Consolidate year-by-year
        consolidated_projections: list[DCFProjection] = []
        prev_total_revenue = sum(s.base_revenue for s in inputs.segments.values())

        # AUDIT-A1: ratio-based D&A bridge — same model as the flat DCF
        # (see compute_dcf for the full rationale).
        da_ratio_base = (
            abs(inputs.base_depreciation) / prev_total_revenue
            if prev_total_revenue > 0 else 0.0
        )

        for i in range(n):
            year = i + 1
            total_revenue = sum(
                seg.projections[i].revenue for seg in segment_outputs.values()
            )
            total_oi = sum(
                seg.projections[i].operating_income for seg in segment_outputs.values()
            )
            total_capex = sum(
                seg.projections[i].capex for seg in segment_outputs.values()
            )

            taxes = total_oi * inputs.effective_tax_rate
            nopat = total_oi - taxes
            revenue_delta = total_revenue - prev_total_revenue
            change_in_nwc = revenue_delta * inputs.nwc_to_revenue_delta
            sbc = total_revenue * effective_sbc

            # AUDIT-A1: D&A ratio converges to the consolidated capex/revenue
            # ratio by the terminal year (steady-state D&A == CapEx).
            capex_ratio_t = (
                abs(total_capex) / total_revenue if total_revenue > 0 else 0.0
            )
            w = year / n
            da_ratio_t = da_ratio_base * (1.0 - w) + capex_ratio_t * w
            depreciation = total_revenue * da_ratio_t

            # FCFF = NOPAT + D&A - CapEx - ΔNWC (see flat-DCF block for rationale)
            fcff = nopat + depreciation - abs(total_capex) - change_in_nwc
            fcfe = fcff - sbc
            discount_factor = 1 / (1 + inputs.wacc) ** year
            pv_fcff = fcff * discount_factor

            consolidated_projections.append(DCFProjection(
                year=year,
                revenue=total_revenue,
                operating_income=total_oi,
                taxes=taxes,
                nopat=nopat,
                depreciation=depreciation,
                capex=total_capex,
                change_in_nwc=change_in_nwc,
                sbc=sbc,
                fcff=fcff,
                fcfe=fcfe,
                discount_factor=discount_factor,
                pv_fcff=pv_fcff,
            ))
            prev_total_revenue = total_revenue

        # Step 3: Terminal value on consolidated FCFF
        terminal_fcff = consolidated_projections[-1].fcff
        # Use weighted-average terminal growth from segments
        total_terminal_rev = sum(
            seg.terminal_revenue for seg in segment_outputs.values()
        )
        if total_terminal_rev > 0:
            weighted_tg = sum(
                inputs.segments[sid].terminal_growth_rate
                * segment_outputs[sid].terminal_revenue
                / total_terminal_rev
                for sid in segment_outputs
            )
        else:
            weighted_tg = 0.03

        if inputs.wacc <= weighted_tg:
            raise ValueError(
                f"WACC ({inputs.wacc}) must exceed weighted terminal growth "
                f"({weighted_tg:.4f})."
            )
        terminal_value = (
            terminal_fcff * (1 + weighted_tg)
            / (inputs.wacc - weighted_tg)
        )
        pv_terminal_value = terminal_value / (1 + inputs.wacc) ** n

        pv_fcff_sum = sum(p.pv_fcff for p in consolidated_projections)
        enterprise_value = pv_fcff_sum + pv_terminal_value
        equity_value = enterprise_value - inputs.net_debt

        # Net share change (kept for reference / display)
        net_share_change = (
            (1 + effective_dilution) * (1 - inputs.buyback_yield_annual) - 1
        )
        future_shares = inputs.shares_outstanding * ((1 + net_share_change) ** n)
        # BUG-29/30: use current shares (see main compute_dcf for rationale)
        per_share_value = equity_value / inputs.shares_outstanding if inputs.shares_outstanding > 0 else 0

        terminal_ebitda = consolidated_projections[-1].operating_income
        # TODO-Y7: None instead of float("inf") for JSON serialisability.
        implied_exit_multiple = (
            terminal_value / terminal_ebitda if terminal_ebitda > 0 else None
        )

        return ConsolidatedDCFOutput(
            segments=segment_outputs,
            consolidated_projections=consolidated_projections,
            terminal_value=terminal_value,
            pv_terminal_value=pv_terminal_value,
            pv_fcff_sum=pv_fcff_sum,
            enterprise_value=enterprise_value,
            equity_value=equity_value,
            per_share_value=per_share_value,
            implied_exit_multiple=implied_exit_multiple,
            future_shares=future_shares,
        )
