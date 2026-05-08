"""Seed Metric Registry — first batch of core metric definitions.

Section 10: Metric Registry is a governance system, not a dictionary.
All metrics must be registered before use. This module seeds
the minimum required definitions for Phase 1.

Coverage areas:
- Profitability
- Returns
- Cash flow / Capital allocation
- Leverage / Liquidity
- Valuation
- Dilution / SBC
- Accounting quality
"""

from datetime import date

from aegis.data_contracts import AccountingStandard, MetricDefinition

from .metric_registry import MetricRegistry

ALL_STANDARDS = [AccountingStandard.US_GAAP, AccountingStandard.IFRS, AccountingStandard.CAS]


def seed_core_metrics(registry: MetricRegistry) -> MetricRegistry:
    """Register all Phase 1 core metric definitions."""

    definitions = [
        # =====================================================================
        # PROFITABILITY
        # =====================================================================
        MetricDefinition(
            metric_name="gross_margin",
            display_name="Gross Margin",
            definition_id="gross_margin_v1",
            definition_status="approved",
            formula_version=1,
            expression="gross_profit / revenue",
            allowed_inputs=["gross_profit", "revenue"],
            unit_policy="ratio",
            period_compatibility=["quarterly", "annual"],
            accounting_standard_compatibility=ALL_STANDARDS,
            quality_tier="A",
            publishable=True,
            common_failure_modes=["mixing cost_of_revenue definitions across segments"],
            validation_rules=["same_entity", "same_period", "same_currency"],
            effective_from=date(2020, 1, 1),
        ),
        MetricDefinition(
            metric_name="operating_margin",
            display_name="Operating Margin",
            definition_id="operating_margin_v1",
            definition_status="approved",
            formula_version=1,
            expression="operating_income / revenue",
            allowed_inputs=["operating_income", "revenue"],
            unit_policy="ratio",
            period_compatibility=["quarterly", "annual"],
            accounting_standard_compatibility=ALL_STANDARDS,
            cross_standard_notes="CAS operating income may include government subsidies in '其他收益'",
            quality_tier="A",
            publishable=True,
            common_failure_modes=[
                "including/excluding SBC inconsistently",
                "CAS government subsidy classification",
            ],
            validation_rules=["same_entity", "same_period", "same_currency"],
            effective_from=date(2020, 1, 1),
        ),
        MetricDefinition(
            metric_name="net_margin",
            display_name="Net Margin",
            definition_id="net_margin_v1",
            definition_status="approved",
            formula_version=1,
            expression="net_income / revenue",
            allowed_inputs=["net_income", "revenue"],
            unit_policy="ratio",
            period_compatibility=["quarterly", "annual"],
            accounting_standard_compatibility=ALL_STANDARDS,
            quality_tier="A",
            publishable=True,
            validation_rules=["same_entity", "same_period", "same_currency"],
            effective_from=date(2020, 1, 1),
        ),
        MetricDefinition(
            metric_name="ebitda_margin",
            display_name="EBITDA Margin",
            definition_id="ebitda_margin_v1",
            definition_status="approved",
            formula_version=1,
            expression="ebitda / revenue",
            allowed_inputs=["ebitda", "revenue"],
            unit_policy="ratio",
            period_compatibility=["quarterly", "annual"],
            accounting_standard_compatibility=ALL_STANDARDS,
            cross_standard_notes="IFRS 16 broadens lease capitalization, inflating EBITDA vs ASC 842",
            quality_tier="A",
            publishable=True,
            common_failure_modes=["IFRS 16 vs ASC 842 lease treatment inflates EBITDA"],
            validation_rules=["same_entity", "same_period", "same_currency"],
            effective_from=date(2020, 1, 1),
        ),

        # =====================================================================
        # RETURNS
        # =====================================================================
        MetricDefinition(
            metric_name="roe",
            display_name="Return on Equity",
            definition_id="roe_v1",
            definition_status="approved",
            formula_version=1,
            expression="net_income / avg_shareholders_equity",
            allowed_inputs=["net_income", "avg_shareholders_equity"],
            unit_policy="ratio",
            period_compatibility=["annual"],
            accounting_standard_compatibility=ALL_STANDARDS,
            cross_standard_notes="IFRS asset revaluation inflates equity, depressing ROE",
            quality_tier="A",
            publishable=True,
            validation_rules=["same_entity", "same_period", "same_currency"],
            effective_from=date(2020, 1, 1),
        ),
        MetricDefinition(
            metric_name="roa",
            display_name="Return on Assets",
            definition_id="roa_v1",
            definition_status="approved",
            formula_version=1,
            expression="net_income / avg_total_assets",
            allowed_inputs=["net_income", "avg_total_assets"],
            unit_policy="ratio",
            period_compatibility=["annual"],
            accounting_standard_compatibility=ALL_STANDARDS,
            quality_tier="A",
            publishable=True,
            validation_rules=["same_entity", "same_period", "same_currency"],
            effective_from=date(2020, 1, 1),
        ),
        MetricDefinition(
            metric_name="roic",
            display_name="Return on Invested Capital",
            definition_id="roic_v1",
            definition_status="approved",
            formula_version=1,
            expression="nopat / avg_invested_capital",
            allowed_inputs=["nopat", "avg_invested_capital"],
            unit_policy="ratio",
            period_compatibility=["annual"],
            accounting_standard_compatibility=ALL_STANDARDS,
            quality_tier="A",
            publishable=True,
            common_failure_modes=[
                "inconsistent invested capital definition",
                "mixing NOPAT tax rate with statutory rate",
            ],
            validation_rules=["same_entity", "same_period", "same_currency"],
            effective_from=date(2020, 1, 1),
        ),

        # =====================================================================
        # CASH FLOW / CAPITAL ALLOCATION
        # =====================================================================
        MetricDefinition(
            metric_name="free_cash_flow",
            display_name="Free Cash Flow (Official)",
            definition_id="fcf_company_official_v1",
            definition_status="approved",
            formula_version=1,
            expression="cfo - capex_ppe - finance_lease_principal",
            allowed_inputs=["cfo", "capex_ppe", "finance_lease_principal"],
            disallowed_inputs=["sbc", "share_repurchases", "acquisitions"],
            unit_policy="currency",
            period_compatibility=["quarterly", "annual"],
            accounting_standard_compatibility=ALL_STANDARDS,
            cross_standard_notes="IFRS 16 finance_lease_principal scope is broader",
            quality_tier="A",
            publishable=True,
            common_failure_modes=[
                "excludes finance lease principal",
                "mixes official and generic fcf",
            ],
            validation_rules=["same_entity", "same_period", "same_currency"],
            effective_from=date(2020, 1, 1),
        ),
        MetricDefinition(
            metric_name="free_cash_flow_simple",
            display_name="Free Cash Flow (Simple)",
            definition_id="fcf_simple_v1",
            definition_status="approved",
            formula_version=1,
            expression="cfo - capex_ppe",
            allowed_inputs=["cfo", "capex_ppe"],
            unit_policy="currency",
            period_compatibility=["quarterly", "annual"],
            accounting_standard_compatibility=ALL_STANDARDS,
            quality_tier="B",
            publishable=True,
            common_failure_modes=["ignores finance lease capex"],
            validation_rules=["same_entity", "same_period", "same_currency"],
            effective_from=date(2020, 1, 1),
        ),
        MetricDefinition(
            metric_name="capex_to_revenue",
            display_name="CapEx to Revenue",
            definition_id="capex_to_revenue_v1",
            definition_status="approved",
            formula_version=1,
            expression="capex_ppe / revenue",
            allowed_inputs=["capex_ppe", "revenue"],
            unit_policy="ratio",
            period_compatibility=["quarterly", "annual"],
            accounting_standard_compatibility=ALL_STANDARDS,
            quality_tier="A",
            publishable=True,
            validation_rules=["same_entity", "same_period", "same_currency"],
            effective_from=date(2020, 1, 1),
        ),

        # =====================================================================
        # LEVERAGE / LIQUIDITY
        # =====================================================================
        MetricDefinition(
            metric_name="net_debt",
            display_name="Net Debt",
            definition_id="net_debt_v1",
            definition_status="approved",
            formula_version=1,
            expression="total_debt - cash_and_equivalents",
            allowed_inputs=["total_debt", "cash_and_equivalents"],
            unit_policy="currency",
            period_compatibility=["quarterly", "annual"],
            accounting_standard_compatibility=ALL_STANDARDS,
            quality_tier="A",
            publishable=True,
            common_failure_modes=["excluding lease liabilities from total_debt"],
            validation_rules=["same_entity", "same_period", "same_currency"],
            effective_from=date(2020, 1, 1),
        ),
        MetricDefinition(
            metric_name="net_debt_to_ebitda",
            display_name="Net Debt / EBITDA",
            definition_id="net_debt_to_ebitda_v1",
            definition_status="approved",
            formula_version=1,
            expression="net_debt / ebitda",
            allowed_inputs=["net_debt", "ebitda"],
            unit_policy="ratio",
            period_compatibility=["annual"],
            accounting_standard_compatibility=ALL_STANDARDS,
            quality_tier="A",
            publishable=True,
            validation_rules=["same_entity", "same_period", "same_currency"],
            effective_from=date(2020, 1, 1),
        ),
        MetricDefinition(
            metric_name="current_ratio",
            display_name="Current Ratio",
            definition_id="current_ratio_v1",
            definition_status="approved",
            formula_version=1,
            expression="current_assets / current_liabilities",
            allowed_inputs=["current_assets", "current_liabilities"],
            unit_policy="ratio",
            period_compatibility=["quarterly", "annual"],
            accounting_standard_compatibility=ALL_STANDARDS,
            quality_tier="A",
            publishable=True,
            validation_rules=["same_entity", "same_period", "same_currency"],
            effective_from=date(2020, 1, 1),
        ),

        # =====================================================================
        # VALUATION
        # =====================================================================
        MetricDefinition(
            metric_name="enterprise_value",
            display_name="Enterprise Value",
            definition_id="enterprise_value_v1",
            definition_status="approved",
            formula_version=1,
            expression="market_cap + total_debt - cash_and_equivalents + minority_interest",
            allowed_inputs=["market_cap", "total_debt", "cash_and_equivalents", "minority_interest"],
            unit_policy="currency",
            period_compatibility=["quarterly", "annual"],
            accounting_standard_compatibility=ALL_STANDARDS,
            quality_tier="A",
            publishable=True,
            validation_rules=["same_entity", "same_currency"],
            effective_from=date(2020, 1, 1),
        ),
        MetricDefinition(
            metric_name="ev_to_ebitda",
            display_name="EV / EBITDA",
            definition_id="ev_to_ebitda_v1",
            definition_status="approved",
            formula_version=1,
            expression="enterprise_value / ebitda",
            allowed_inputs=["enterprise_value", "ebitda"],
            unit_policy="ratio",
            period_compatibility=["annual"],
            accounting_standard_compatibility=ALL_STANDARDS,
            quality_tier="A",
            publishable=True,
            validation_rules=["same_entity", "same_period", "same_currency"],
            effective_from=date(2020, 1, 1),
        ),
        MetricDefinition(
            metric_name="ev_to_revenue",
            display_name="EV / Revenue",
            definition_id="ev_to_revenue_v1",
            definition_status="approved",
            formula_version=1,
            expression="enterprise_value / revenue",
            allowed_inputs=["enterprise_value", "revenue"],
            unit_policy="ratio",
            period_compatibility=["annual"],
            accounting_standard_compatibility=ALL_STANDARDS,
            quality_tier="A",
            publishable=True,
            validation_rules=["same_entity", "same_period", "same_currency"],
            effective_from=date(2020, 1, 1),
        ),
        MetricDefinition(
            metric_name="pe_ratio",
            display_name="P/E Ratio",
            definition_id="pe_ratio_v1",
            definition_status="approved",
            formula_version=1,
            expression="price / eps",
            allowed_inputs=["price", "eps"],
            unit_policy="ratio",
            period_compatibility=["annual"],
            accounting_standard_compatibility=ALL_STANDARDS,
            quality_tier="A",
            publishable=True,
            common_failure_modes=["using basic EPS instead of diluted"],
            validation_rules=["same_entity", "same_currency"],
            effective_from=date(2020, 1, 1),
        ),

        # =====================================================================
        # DILUTION / SBC
        # =====================================================================
        MetricDefinition(
            metric_name="sbc_to_revenue",
            display_name="SBC to Revenue",
            definition_id="sbc_to_revenue_v1",
            definition_status="approved",
            formula_version=1,
            expression="sbc / revenue",
            allowed_inputs=["sbc", "revenue"],
            unit_policy="ratio",
            period_compatibility=["quarterly", "annual"],
            accounting_standard_compatibility=ALL_STANDARDS,
            quality_tier="A",
            publishable=True,
            common_failure_modes=["SBC+dilution double counting"],
            validation_rules=["same_entity", "same_period", "same_currency"],
            effective_from=date(2020, 1, 1),
        ),
        MetricDefinition(
            metric_name="dilution_rate",
            display_name="Annual Dilution Rate",
            definition_id="dilution_rate_v1",
            definition_status="approved",
            formula_version=1,
            expression="diluted_shares_end / diluted_shares_start - 1",
            allowed_inputs=["diluted_shares_end", "diluted_shares_start"],
            unit_policy="ratio",
            period_compatibility=["annual"],
            accounting_standard_compatibility=ALL_STANDARDS,
            quality_tier="A",
            publishable=True,
            validation_rules=["same_entity"],
            effective_from=date(2020, 1, 1),
        ),

        # =====================================================================
        # ACCOUNTING QUALITY
        # =====================================================================
        MetricDefinition(
            metric_name="accruals_ratio",
            display_name="Accruals Ratio",
            definition_id="accruals_ratio_v1",
            definition_status="approved",
            formula_version=1,
            expression="(net_income - cfo) / avg_total_assets",
            allowed_inputs=["net_income", "cfo", "avg_total_assets"],
            unit_policy="ratio",
            period_compatibility=["annual"],
            accounting_standard_compatibility=ALL_STANDARDS,
            quality_tier="A",
            publishable=True,
            common_failure_modes=["using OCF/NI as earnings quality proxy without context"],
            validation_rules=["same_entity", "same_period", "same_currency"],
            effective_from=date(2020, 1, 1),
        ),
        MetricDefinition(
            metric_name="cfo_to_net_income",
            display_name="CFO / Net Income",
            definition_id="cfo_to_net_income_v1",
            definition_status="approved",
            formula_version=1,
            expression="cfo / net_income",
            allowed_inputs=["cfo", "net_income"],
            unit_policy="ratio",
            period_compatibility=["annual"],
            accounting_standard_compatibility=ALL_STANDARDS,
            quality_tier="B",
            publishable=True,
            common_failure_modes=["treating ratio as sole earnings quality indicator"],
            validation_rules=["same_entity", "same_period", "same_currency"],
            effective_from=date(2020, 1, 1),
        ),

        # =====================================================================
        # WORKING CAPITAL
        # =====================================================================
        MetricDefinition(
            metric_name="net_working_capital",
            display_name="Net Working Capital",
            definition_id="nwc_v1",
            definition_status="approved",
            formula_version=1,
            expression="(current_assets - cash) - (current_liabilities - short_term_debt)",
            allowed_inputs=[
                "current_assets", "cash_and_equivalents",
                "current_liabilities", "short_term_debt",
            ],
            unit_policy="currency",
            period_compatibility=["quarterly", "annual"],
            accounting_standard_compatibility=ALL_STANDARDS,
            quality_tier="A",
            publishable=True,
            validation_rules=["same_entity", "same_period", "same_currency"],
            effective_from=date(2020, 1, 1),
        ),
    ]

    for defn in definitions:
        registry.register(defn)

    return registry


def create_seeded_registry() -> MetricRegistry:
    """Create a new MetricRegistry pre-seeded with all Phase 1 core metrics."""
    registry = MetricRegistry()
    return seed_core_metrics(registry)
