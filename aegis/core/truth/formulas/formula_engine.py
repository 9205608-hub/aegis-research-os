"""Formula Engine — deterministic metric computation.

All financial metrics are computed here, never by LLM agents.
Every computation is traceable to input fact_ids and a definition_id.
"""

from dataclasses import dataclass

from aegis.core.truth.validations.validation_engine import ValidationEngine, ValidationResult


@dataclass(frozen=True)
class MetricResult:
    """Result of a deterministic metric computation."""

    definition_id: str
    formula_version: int
    value: float
    unit: str
    entity_id: str
    period: str
    input_fact_ids: list[str]
    validation: ValidationResult


class FormulaEngine:
    """Deterministic formula evaluation engine.

    Agents NEVER compute financial values directly.
    All computations go through this engine.
    """

    def __init__(self) -> None:
        self._validator = ValidationEngine()
        self._formulas: dict[str, "FormulaSpec"] = {}
        self._register_core_formulas()

    def _register_core_formulas(self) -> None:
        """Register the core set of financial formulas."""
        # Profitability
        self.register_formula("gross_margin_v1", ["gross_profit", "revenue"], _div)
        self.register_formula("operating_margin_v1", ["operating_income", "revenue"], _div)
        self.register_formula("net_margin_v1", ["net_income", "revenue"], _div)
        self.register_formula("ebitda_margin_v1", ["ebitda", "revenue"], _div)

        # Returns
        self.register_formula(
            "roe_v1", ["net_income", "avg_shareholders_equity"], _div
        )
        self.register_formula(
            "roa_v1", ["net_income", "avg_total_assets"], _div
        )
        self.register_formula(
            "roic_v1",
            ["nopat", "avg_invested_capital"],
            _div,
        )

        # Cash flow
        self.register_formula(
            "fcf_company_official_v1",
            ["cfo", "capex_ppe", "finance_lease_principal"],
            lambda inputs: inputs["cfo"] - inputs["capex_ppe"] - inputs["finance_lease_principal"],
        )
        self.register_formula(
            "fcf_simple_v1",
            ["cfo", "capex_ppe"],
            lambda inputs: inputs["cfo"] - inputs["capex_ppe"],
        )

        # Leverage
        self.register_formula(
            "net_debt_v1",
            ["total_debt", "cash_and_equivalents"],
            lambda inputs: inputs["total_debt"] - inputs["cash_and_equivalents"],
        )
        self.register_formula(
            "net_debt_to_ebitda_v1",
            ["net_debt", "ebitda"],
            _div,
        )

        # Valuation building blocks
        self.register_formula(
            "enterprise_value_v1",
            ["market_cap", "total_debt", "cash_and_equivalents", "minority_interest"],
            lambda inputs: (
                inputs["market_cap"]
                + inputs["total_debt"]
                - inputs["cash_and_equivalents"]
                + inputs.get("minority_interest", 0)
            ),
        )
        self.register_formula(
            "ev_to_ebitda_v1", ["enterprise_value", "ebitda"], _div
        )
        self.register_formula(
            "ev_to_revenue_v1", ["enterprise_value", "revenue"], _div
        )
        self.register_formula("pe_ratio_v1", ["price", "eps"], _div)

        # Dilution
        self.register_formula(
            "sbc_to_revenue_v1", ["sbc", "revenue"], _div
        )
        self.register_formula(
            "dilution_rate_v1",
            ["diluted_shares_end", "diluted_shares_start"],
            lambda inputs: (
                inputs["diluted_shares_end"] / inputs["diluted_shares_start"] - 1
            ),
        )

        # Working capital
        self.register_formula(
            "nwc_v1",
            ["current_assets", "cash_and_equivalents", "current_liabilities", "short_term_debt"],
            lambda inputs: (
                (inputs["current_assets"] - inputs["cash_and_equivalents"])
                - (inputs["current_liabilities"] - inputs["short_term_debt"])
            ),
        )

    def register_formula(
        self,
        definition_id: str,
        required_inputs: list[str],
        compute_fn: "callable",
    ) -> None:
        """Register a formula with its required inputs and compute function."""
        self._formulas[definition_id] = FormulaSpec(
            definition_id=definition_id,
            required_inputs=required_inputs,
            compute_fn=compute_fn,
        )

    def compute(
        self,
        *,
        definition_id: str,
        formula_version: int,
        entity_id: str,
        period: str,
        period_type: str,
        currency: str,
        inputs: dict[str, float],
        input_fact_ids: dict[str, str],
    ) -> MetricResult:
        """Compute a metric deterministically.

        Args:
            definition_id: The registered formula to use.
            formula_version: Version of the formula.
            entity_id: The entity this computation is for.
            period: Fiscal period (e.g. "FY2025").
            period_type: "annual" or "quarterly".
            currency: Currency of all input values.
            inputs: Named input values.
            input_fact_ids: Mapping of input name -> fact_id for traceability.

        Returns:
            MetricResult with computed value and validation status.

        Raises:
            KeyError: If definition_id is not registered.
        """
        if definition_id not in self._formulas:
            raise KeyError(f"Formula '{definition_id}' not registered in engine.")

        spec = self._formulas[definition_id]

        # Validate inputs
        validation = self._validator.validate_metric_inputs(
            entity_ids=[entity_id],
            currencies=[currency],
            periods=[period],
            period_types=[period_type],
            definition_id=definition_id,
            required_inputs=spec.required_inputs,
            available_inputs=inputs,
        )

        if not validation.passed:
            return MetricResult(
                definition_id=definition_id,
                formula_version=formula_version,
                value=float("nan"),
                unit=currency,
                entity_id=entity_id,
                period=period,
                input_fact_ids=list(input_fact_ids.values()),
                validation=validation,
            )

        value = spec.compute_fn(inputs)

        return MetricResult(
            definition_id=definition_id,
            formula_version=formula_version,
            value=value,
            unit=currency,
            entity_id=entity_id,
            period=period,
            input_fact_ids=list(input_fact_ids.values()),
            validation=validation,
        )

    def list_formulas(self) -> list[str]:
        """List all registered formula definition IDs."""
        return list(self._formulas.keys())


@dataclass(frozen=True)
class FormulaSpec:
    """Internal specification for a registered formula."""

    definition_id: str
    required_inputs: list[str]
    compute_fn: "callable"


# ---------------------------------------------------------------------------
# Helper functions for common formula patterns
# ---------------------------------------------------------------------------

def _div(inputs: dict[str, float]) -> float:
    """Divide the first input by the second. Returns float('inf') on zero denominator."""
    keys = list(inputs.keys())
    numerator = inputs[keys[0]]
    denominator = inputs[keys[1]]
    if denominator == 0:
        return float("inf")
    return numerator / denominator
