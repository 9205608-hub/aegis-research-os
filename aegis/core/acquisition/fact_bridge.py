"""Fact Normalization Bridge — adapted filing data → complete meta_facts dict.

Bridges the gap between MarketAdapter.adapt_filing_data() output
(canonical concept IDs) and the meta_facts dict expected by
FormulaEngine, DCFEngine, and Agent pipeline.

Supports both US GAAP (via USMarketAdapter/XBRL) and CAS (via CNMarketAdapter/yfinance).

Responsibilities:
  1. Alias resolution (operating_cash_flow ↔ cfo, capex ↔ capex_ppe, etc.)
  2. Derived fact computation (gross_profit, ebitda, nwc, net_debt, etc.)
  3. Segment data structuring
  4. Completeness validation
  5. Currency tagging
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class BridgeResult:
    """Result of fact normalization."""

    meta_facts: dict[str, Any]
    segment_data: dict[str, dict[str, Any]]
    missing_fields: list[str]
    derived_fields: list[str]
    warnings: list[str]

    @property
    def is_complete(self) -> bool:
        """True if all critical fields are present."""
        return len(self.missing_fields) == 0


# Fields that MUST exist for the pipeline to run
CRITICAL_FIELDS = {
    "revenue", "net_income", "total_assets",
}

# Fields we attempt to derive if missing
DERIVABLE_FIELDS = {
    "gross_profit": ("revenue", "cost_of_revenue"),  # revenue - cost_of_revenue
    "ebitda": ("operating_income", "depreciation_amortization"),  # opinc + da
    "free_cash_flow": ("operating_cash_flow", "capex"),  # cfo - capex
    "net_debt": ("total_debt", "cash_and_equivalents"),  # debt - cash
    "nwc": ("current_assets", "current_liabilities"),  # ca - cl
    "total_equity": ("total_assets", "total_liabilities"),  # assets - liabilities
    "ebit": ("revenue", "cost_of_revenue"),  # revenue - cogs - opex (computed separately)
}

# Alias map: adapter output key → meta_facts key used by demo/engine
# Multiple adapter keys can map to the same meta_facts key.
ALIAS_MAP: dict[str, str] = {
    "cfo": "operating_cash_flow",
    "capex_ppe": "capex",
    "shareholders_equity": "total_equity",
    "long_term_debt": "long_term_debt",
    "short_term_debt": "short_term_debt",
    "total_debt_carrying": "total_debt_carrying",
    "long_term_debt_noncurrent": "long_term_debt_noncurrent",
    "long_term_debt_current": "long_term_debt_current",
}

# Reverse: meta_facts key → adapter key (for lookup)
REVERSE_ALIAS: dict[str, str] = {v: k for k, v in ALIAS_MAP.items()}


class FactNormalizationBridge:
    """Convert MarketAdapter output to a complete meta_facts dictionary.

    Supports both US GAAP and CAS (Chinese Accounting Standards) input.

    Usage:
        bridge = FactNormalizationBridge()
        result = bridge.normalize(
            adapted_data=adapted_dict,         # from MarketAdapter.adapt_filing_data()
            segment_facts=segment_dict,         # from parser segment_facts
            filing_context={"entity_id": "meta_platforms", "fiscal_year": 2024},
        )
        meta_facts = result.meta_facts
    """

    def normalize(
        self,
        adapted_data: dict[str, Any],
        segment_facts: dict[str, dict[str, Any]] | None = None,
        filing_context: dict[str, Any] | None = None,
        market_id: str = "us",
        currency: str = "USD",
    ) -> BridgeResult:
        """Normalize adapted filing data into a complete meta_facts dict.

        Args:
            adapted_data: Output from MarketAdapter.adapt_filing_data()[0].
                Keys are canonical concept IDs (e.g. "revenue", "net_income").
            segment_facts: Per-segment facts, already adapted through the
                market adapter. Keys are segment IDs.
            filing_context: Optional metadata (entity_id, fiscal_year, etc.).
            market_id: "us" or "cn" — affects segment adapter and metadata.
            currency: "USD", "CNY", etc. — tagged on meta_facts.
        """
        meta_facts: dict[str, Any] = {}
        warnings: list[str] = []
        derived: list[str] = []

        # Step 1: Copy all adapted data, resolving aliases
        for key, value in adapted_data.items():
            if key.startswith("us-gaap:"):
                # Unmapped XBRL concept — skip (adapter already warned)
                continue
            if isinstance(value, (int, float)):
                # Resolve alias → canonical meta_facts key
                canonical = ALIAS_MAP.get(key, key)
                # Keep both the canonical name AND the original if different
                meta_facts[canonical] = value
                if canonical != key:
                    meta_facts[key] = value
            else:
                meta_facts[key] = value

        # Step 2: Ensure critical aliases exist in both directions
        # If "cfo" exists but "operating_cash_flow" doesn't, copy it over
        for alias, canonical in ALIAS_MAP.items():
            if alias in meta_facts and canonical not in meta_facts:
                meta_facts[canonical] = meta_facts[alias]
            elif canonical in meta_facts and alias not in meta_facts:
                meta_facts[alias] = meta_facts[canonical]

        # Step 2b: A-share net income scope — prefer 归母 (parent attributable).
        # AUDIT-A7 (2026-07): CN adapter maps akshare NETPROFIT (consolidated,
        # includes minority interest) → net_income and PARENT_NETPROFIT →
        # net_income_to_parent, but downstream (net_margin/ROE/EPS/PE, agent
        # KEY FINANCIALS) all consumed the consolidated figure while akshare's
        # eps_basic is 归母口径 — overstating earnings for companies with
        # large minority interests (归母/合并 can be 60-80%). Sell-side
        # convention is 归母口径: promote it to net_income, keeping the
        # consolidated figure available as net_income_incl_minority.
        if market_id == "cn":
            ni_parent = meta_facts.get("net_income_to_parent")
            if isinstance(ni_parent, (int, float)):
                ni_total = meta_facts.get("net_income")
                if isinstance(ni_total, (int, float)):
                    meta_facts["net_income_incl_minority"] = ni_total
                meta_facts["net_income"] = ni_parent

        # Step 3: Compute total_debt — prefer carrying amount, fallback to components
        if "total_debt" not in meta_facts:
            # Try total_debt_carrying first (DebtInstrumentCarryingAmount — most accurate)
            carrying = meta_facts.get("total_debt_carrying")
            if carrying and carrying > 0:
                meta_facts["total_debt"] = carrying
                derived.append("total_debt")
            else:
                # Fallback: sum debt components.
                # AUDIT-A6 (2026-07): previously only ltd + std + cp, dropping
                # bonds_payable (CN 应付债券) and the LongTermDebtCurrent/
                # Noncurrent split concepts (filings like NVDA tag only the
                # split, not us-gaap:LongTermDebt) → total_debt missing/low →
                # net_debt deeply negative → DCF equity value inflated and a
                # false "net cash" narrative for bond-financed issuers.
                ltd = meta_facts.get("long_term_debt") or 0
                ltd_cur = meta_facts.get("long_term_debt_current") or 0
                ltd_noncur = meta_facts.get("long_term_debt_noncurrent") or 0
                if ltd > 0:
                    # Whole-value concept present — use it and ignore the split
                    # concepts (US: us-gaap:LongTermDebt already includes the
                    # current portion). CN exception: 长期借款 EXCLUDES
                    # 一年内到期的非流动负债 (separate line item mapped to
                    # long_term_debt_current), so add it back.
                    ltd_total = ltd + (ltd_cur if market_id == "cn" else 0)
                else:
                    ltd_total = ltd_noncur + ltd_cur
                std = meta_facts.get("short_term_debt") or 0
                cp = meta_facts.get("commercial_paper") or 0
                bonds = meta_facts.get("bonds_payable") or 0  # CN 应付债券
                total = ltd_total + std + cp + bonds
                if total > 0:
                    meta_facts["total_debt"] = total
                    derived.append("total_debt")

        # Step 3b: Compute total_cash (cash + marketable securities) for net debt
        cash = meta_facts.get("cash_and_equivalents", 0)
        mkt_current = meta_facts.get("marketable_securities_current", 0)
        mkt_noncurrent = meta_facts.get("marketable_securities_noncurrent", 0)
        if mkt_current or mkt_noncurrent:
            meta_facts["total_cash_and_investments"] = cash + mkt_current + mkt_noncurrent
            derived.append("total_cash_and_investments")

        # Step 4: Derive computable fields
        # BUG-Y38 (2026-05-06): the `derived.append(target)` previously sat
        # at the if-block level (after the elif chain) and fired even when
        # NO elif matched the target — `ebit` is in DERIVABLE_FIELDS but
        # its formula is mis-mapped (would compute gross_profit, not ebit),
        # so the elif chain has no branch for it. The result was `derived`
        # list claiming "ebit" was derived when in fact `meta_facts["ebit"]`
        # was never set (Step 5c later aliases it from operating_income).
        # Move the append inside each branch so it only fires when a value
        # is actually written.
        for target, (a, b) in DERIVABLE_FIELDS.items():
            if target not in meta_facts:
                va = meta_facts.get(a)
                vb = meta_facts.get(b)
                if va is not None and vb is not None:
                    if target == "free_cash_flow":
                        # BUG-28: capex sign convention varies by data source:
                        # US/EDGAR stores capex as POSITIVE magnitude,
                        # A-share/akshare flips to NEGATIVE (cash outflow).
                        # FCF = OCF - |CapEx| regardless of convention.
                        meta_facts[target] = va - abs(vb)
                        derived.append(target)
                    elif target in ("net_debt",):
                        meta_facts[target] = va - vb
                        derived.append(target)
                    elif target in ("gross_profit",):
                        meta_facts[target] = va - vb
                        derived.append(target)
                    elif target in ("ebitda",):
                        meta_facts[target] = va + vb
                        derived.append(target)
                    elif target in ("nwc",):
                        meta_facts[target] = va - vb
                        derived.append(target)
                    elif target in ("total_equity",):
                        meta_facts[target] = va - vb
                        derived.append(target)
                    # Targets without a branch (e.g. "ebit" — handled by
                    # Step 5c alias from operating_income) are intentionally
                    # left unset here. Step 5c will fill them.

        # Step 5: Compute dilution_rate if both share counts available
        diluted = meta_facts.get("diluted_shares")
        basic = meta_facts.get("shares_outstanding") or meta_facts.get("basic_shares")
        if diluted and basic and "dilution_rate" not in meta_facts:
            meta_facts["dilution_rate"] = (diluted - basic) / basic if basic else 0
            derived.append("dilution_rate")
        # Ensure basic_shares alias
        if basic and "basic_shares" not in meta_facts:
            meta_facts["basic_shares"] = basic

        # Step 5b: Compute shareholder return metrics
        buybacks = meta_facts.get("share_buybacks", 0)
        dividends = meta_facts.get("dividends_paid", 0)
        if buybacks or dividends:
            meta_facts["total_shareholder_return_cash"] = buybacks + dividends
            derived.append("total_shareholder_return_cash")

        # Step 5c: Ensure operating_income alias (some use EBIT)
        if "operating_income" in meta_facts and "ebit" not in meta_facts:
            meta_facts["ebit"] = meta_facts["operating_income"]

        # Step 5d: Derive depreciation_amortization from components.
        # Filings vary: some use the combined `DepreciationAndAmortization`
        # concept, others (like Alphabet) report `Depreciation` and
        # `AmortizationOfIntangibleAssets` separately. Without this fallback,
        # GOOG/GOOGL DCF runs with D&A=0 and silently understates FCFF
        # (caught by the new fact_bridge DQ checker on 2026-04-15).
        if not meta_facts.get("depreciation_amortization"):
            dep = meta_facts.get("depreciation") or 0
            amort = meta_facts.get("amortization") or 0
            if dep or amort:
                meta_facts["depreciation_amortization"] = dep + amort
                derived.append("depreciation_amortization")

        # Step 5e: Derive effective_tax_rate from the income statement when
        # the filing doesn't report a rate concept directly.
        # AUDIT-A8 (2026-07): us-gaap:EffectiveIncomeTaxRateContinuingOperations
        # only exists on the US/XBRL path — A-share filings report 所得税费用/
        # 利润总额 but no rate, so every CN DCF silently fell back to the US
        # 21% default (config.effective_tax_rate), a ±5-7% FCFF bias. Derive
        # the rate here; consumed by auto_research's tax-rate resolution via
        # facts["effective_tax_rate"] (same consumption point as US path).
        if "effective_tax_rate" not in meta_facts:
            tax = meta_facts.get("income_tax_expense")
            pbt = meta_facts.get("profit_before_tax")
            if (isinstance(tax, (int, float)) and isinstance(pbt, (int, float))
                    and pbt > 0):
                rate = tax / pbt
                # Clamp to a sane corporate band — one-off credits/distortions
                # shouldn't push the DCF to extremes.
                meta_facts["effective_tax_rate"] = min(max(rate, 0.05), 0.50)
                derived.append("effective_tax_rate")

        # Step 6: Process segments
        segment_data: dict[str, dict[str, Any]] = {}
        if segment_facts:
            # Select adapter based on market
            if market_id == "cn":
                from aegis.core.market_adapter.cn_adapter import CNMarketAdapter
                seg_adapter = CNMarketAdapter()
            else:
                from aegis.core.market_adapter.us_adapter import USMarketAdapter
                seg_adapter = USMarketAdapter()

            for seg_id, seg_raw in segment_facts.items():
                seg_adapted, _ = seg_adapter.adapt_filing_data(seg_raw)
                seg_clean: dict[str, Any] = {}
                for k, v in seg_adapted.items():
                    if isinstance(v, (int, float)):
                        canonical = ALIAS_MAP.get(k, k)
                        seg_clean[canonical] = v
                    elif not k.startswith("us-gaap:"):
                        seg_clean[k] = v
                segment_data[seg_id] = seg_clean

        # Step 7: Check for missing critical fields
        missing = [f for f in CRITICAL_FIELDS if f not in meta_facts]
        if missing:
            warnings.append(f"Missing critical fields: {missing}")

        # Step 7b: Data-quality sanity checks (2026-04-15)
        # Catches sign errors, unit errors, internal inconsistencies, and
        # known data-source bugs (e.g. akshare D&A=0 → fake negative FCFF).
        # Writes structured issues to `meta_facts["__data_quality_issues"]`
        # so downstream consumers (LLM context, report) can surface them.
        dq_issues = _run_data_quality_checks(meta_facts)
        if dq_issues:
            meta_facts["__data_quality_issues"] = dq_issues
            for iss in dq_issues:
                warnings.append(f"DQ[{iss['severity']}] {iss['code']}: {iss['message']}")

        # Step 8: Add filing context and market metadata to meta_facts
        meta_facts["__currency"] = currency
        meta_facts["__market_id"] = market_id

        # Refactor 2 (2026-05-04): centralize per-currency display context
        # so renderers (CLI / HTML / KPI panel / sidebar) consume one source
        # of truth instead of each branching on `__currency` themselves.
        # `symbol` is the prefix sign, `scale` is the divisor for "big
        # number" formatting, `unit` is the human-readable suffix matched
        # to that scale. Add new currencies by extending the table here.
        _DISPLAY_TABLE = {
            "CNY": {"symbol": "¥", "scale": 1e8, "unit": "亿",
                    "big_scale": 1e12, "big_unit": "万亿"},
            "USD": {"symbol": "$", "scale": 1e9, "unit": "B",
                    "big_scale": 1e12, "big_unit": "T"},
            "EUR": {"symbol": "€", "scale": 1e9, "unit": "B",
                    "big_scale": 1e12, "big_unit": "T"},
            "GBP": {"symbol": "£", "scale": 1e9, "unit": "B",
                    "big_scale": 1e12, "big_unit": "T"},
            "JPY": {"symbol": "¥", "scale": 1e8, "unit": "億",
                    "big_scale": 1e12, "big_unit": "兆"},
        }
        meta_facts["__display"] = dict(_DISPLAY_TABLE.get(
            currency, _DISPLAY_TABLE["USD"],
        ))
        meta_facts["__display"]["currency"] = currency

        if filing_context:
            for ctx_key in ("entity_id", "fiscal_year", "fiscal_period"):
                if ctx_key in filing_context:
                    meta_facts[f"__{ctx_key}"] = filing_context[ctx_key]

        return BridgeResult(
            meta_facts=meta_facts,
            segment_data=segment_data,
            missing_fields=missing,
            derived_fields=derived,
            warnings=warnings,
        )


def _run_data_quality_checks(facts: dict[str, Any]) -> list[dict[str, Any]]:
    """Run sanity checks on adapted financial facts.

    Returns a list of issue dicts with `code`, `severity`, `message`,
    and (where applicable) `field` keys. Severities:
      - "error":  impossible value (sign/unit error) — DCF will produce garbage
      - "warn":   internal inconsistency or known data-source bug pattern
      - "info":   suspect-but-possibly-valid (e.g. extreme but real value)

    Each rule is independent and conservative: tolerances are wide enough
    to avoid false positives on legitimate edge cases (financial entities,
    luxury margins, software co's with no D&A, etc.).
    """
    issues: list[dict[str, Any]] = []

    def _add(code: str, severity: str, message: str, field: str | None = None) -> None:
        d = {"code": code, "severity": severity, "message": message}
        if field:
            d["field"] = field
        issues.append(d)

    rev = facts.get("revenue")
    ni = facts.get("net_income")
    gp = facts.get("gross_profit")
    op_inc = facts.get("operating_income") or facts.get("ebit")
    ebitda = facts.get("ebitda")
    da = facts.get("depreciation_amortization") or 0
    capex = facts.get("capex") or 0
    cash = facts.get("cash_and_equivalents") or 0
    total_assets = facts.get("total_assets")
    total_debt = facts.get("total_debt") or 0

    # ── Impossible value checks (severity=error) ──
    if rev is not None and rev < 0:
        _add("DQ_NEGATIVE_REVENUE", "error",
             f"Revenue is negative ({rev:,.0f}) — sign error or non-recurring loss",
             field="revenue")

    if total_assets is not None and total_assets <= 0:
        _add("DQ_NONPOSITIVE_ASSETS", "error",
             f"Total assets is non-positive ({total_assets:,.0f}) — data error",
             field="total_assets")

    if gp is not None and rev is not None and rev > 0 and gp > rev * 1.02:
        # 2% slack for rounding in alias derivation
        _add("DQ_GROSS_PROFIT_EXCEEDS_REVENUE", "error",
             f"gross_profit ({gp:,.0f}) > revenue ({rev:,.0f}) — sign error in cost_of_revenue",
             field="gross_profit")

    # Only check cash > assets when total_assets is itself plausible —
    # otherwise we'd double-report a cascade from non-positive assets.
    if cash and total_assets and total_assets > 0 and cash > total_assets * 1.05:
        _add("DQ_CASH_EXCEEDS_ASSETS", "error",
             f"cash ({cash:,.0f}) > total_assets ({total_assets:,.0f}) — unit error or sign error",
             field="cash_and_equivalents")

    # ── Internal consistency (severity=warn) ──
    # EBITDA - EBIT should ≈ D&A. This catches the akshare bug where the
    # adapter forgot to map FA_IR_DEPR / IA_AMORTIZE → depreciation_amortization,
    # causing D&A=0 and DCF to compute negative FCFF for capital-intensive
    # companies (real incident: 301358 base case ¥-53/share before the fix).
    if ebitda is not None and op_inc is not None and rev and rev > 0:
        implied_da = ebitda - op_inc
        if da > 0:
            # Both reported — they should agree within 15%
            denom = max(implied_da, da, 1.0)
            if denom > 0 and abs(implied_da - da) / denom > 0.15:
                _add("DQ_DA_RECONCILIATION", "warn",
                     f"EBITDA - EBIT = {implied_da:,.0f} but reported D&A = {da:,.0f} "
                     f"({abs(implied_da-da)/denom:.0%} mismatch) — adapter mapping issue",
                     field="depreciation_amortization")
        elif implied_da > rev * 0.02:
            # D&A=0 reported but EBITDA-EBIT implies it should be > 2% of revenue
            _add("DQ_DA_MISSING", "warn",
                 f"D&A reported as 0 but EBITDA-EBIT implies {implied_da:,.0f} "
                 f"({implied_da/rev:.1%} of revenue) — adapter likely missing field",
                 field="depreciation_amortization")

    # Capital-intensive company with D&A=0 is implausible. Catches
    # akshare-style mapping omissions even when EBITDA isn't available.
    if da == 0 and capex and rev and rev > 0:
        capex_intensity = abs(capex) / rev
        if capex_intensity > 0.05:
            _add("DQ_DA_ZERO_HIGH_CAPEX", "warn",
                 f"D&A is 0 but capex/revenue = {capex_intensity:.1%} — "
                 f"capital-intensive companies should have non-zero D&A",
                 field="depreciation_amortization")

    # ── Plausibility (severity=info) ──
    if rev is not None and op_inc is not None and rev > 0:
        op_margin = op_inc / rev
        if op_margin > 0.80:
            _add("DQ_HIGH_OP_MARGIN", "info",
                 f"Operating margin {op_margin:.0%} is unusually high — "
                 f"verify (typical of holding companies, IP-licensing, or data error)",
                 field="operating_income")
        elif op_margin < -1.0:
            _add("DQ_DEEP_NEG_MARGIN", "info",
                 f"Operating margin {op_margin:.0%} is deeply negative — "
                 f"check for one-time write-downs or unit/sign error",
                 field="operating_income")

    if ni is not None and rev is not None and rev > 0:
        ni_margin = ni / rev
        if ni_margin > 0.60:
            _add("DQ_HIGH_NI_MARGIN", "info",
                 f"Net margin {ni_margin:.0%} >60% — verify (rare outside finance/IP)",
                 field="net_income")

    # AUDIT-A6: total_debt must be >= any single debt component — a smaller
    # total means the aggregation (carrying amount / fallback sum) missed a
    # component (e.g. 应付债券 unmapped) and net_debt is understated.
    if total_debt:
        for comp_field in ("long_term_debt", "short_term_debt",
                           "commercial_paper", "bonds_payable",
                           "long_term_debt_noncurrent", "long_term_debt_current"):
            comp = facts.get(comp_field) or 0
            if comp > total_debt * 1.001:  # slack for rounding
                _add("DQ_TOTAL_DEBT_LT_COMPONENT", "warn",
                     f"total_debt ({total_debt:,.0f}) < {comp_field} "
                     f"({comp:,.0f}) — debt aggregation is missing components, "
                     f"net_debt will be understated",
                     field="total_debt")

    # Total debt vs assets: very leveraged structures get flagged for context
    if total_debt and total_assets and total_assets > 0:
        leverage = total_debt / total_assets
        if leverage > 0.90:
            _add("DQ_EXTREME_LEVERAGE", "info",
                 f"Debt/Assets = {leverage:.0%} is extreme — verify "
                 f"(typical of REITs, financials, distressed borrowers)",
                 field="total_debt")

    return issues
