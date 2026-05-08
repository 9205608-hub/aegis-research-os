"""Validation Engine — deterministic validation gates.

Section 11: First determine if a computation CAN be performed,
then determine how to interpret the result. Block-level errors
prevent publishable metric generation.
"""

from dataclasses import dataclass, field
from enum import Enum

from aegis.data_contracts import Severity


class ValidationCode(str, Enum):
    """All validation gate codes from Section 11.1."""

    SAME_ENTITY = "same_entity"
    SAME_SECURITY = "same_security"
    SAME_CURRENCY = "same_currency"
    CURRENCY_CONVERSION_VALID = "currency_conversion_valid"
    SAME_PERIOD = "same_period"
    SAME_PERIOD_TYPE = "same_period_type"
    PIT_CONSISTENT = "PIT_consistent"
    DEFINITION_RESOLVED = "definition_resolved"
    INPUT_COMPLETENESS = "input_completeness"
    UNIT_CONSISTENCY = "unit_consistency"
    NO_DUPLICATE_PENALTY = "no_duplicate_penalty"
    MARKET_TIMESTAMP_VALID = "market_timestamp_valid"
    DATA_SOURCE_TIER_SUFFICIENT = "data_source_tier_sufficient"
    ACCOUNTING_STANDARD_COMPATIBLE = "accounting_standard_compatible"
    CROSS_STANDARD_ADJUSTMENT_APPLIED = "cross_standard_adjustment_applied"
    MARKET_ADAPTER_APPLIED = "market_adapter_applied"


@dataclass(frozen=True)
class ValidationIssue:
    """A single validation issue."""

    code: ValidationCode
    severity: Severity
    message: str
    context: dict = field(default_factory=dict)


@dataclass
class ValidationResult:
    """Aggregate result of all validation checks."""

    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """True if no block-level issues."""
        return not any(i.severity == Severity.BLOCK for i in self.issues)

    @property
    def has_warnings(self) -> bool:
        return any(i.severity == Severity.WARN for i in self.issues)

    @property
    def block_issues(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == Severity.BLOCK]

    def add(self, issue: ValidationIssue) -> None:
        self.issues.append(issue)


class ValidationEngine:
    """Deterministic validation engine.

    This module is NOT model-driven. Validation pass/fail is purely deterministic.
    """

    @staticmethod
    def check_same_entity(entity_id_a: str, entity_id_b: str) -> ValidationIssue | None:
        if entity_id_a != entity_id_b:
            return ValidationIssue(
                code=ValidationCode.SAME_ENTITY,
                severity=Severity.BLOCK,
                message=f"Entity mismatch: '{entity_id_a}' vs '{entity_id_b}'",
            )
        return None

    @staticmethod
    def check_same_currency(currency_a: str, currency_b: str) -> ValidationIssue | None:
        if currency_a != currency_b:
            return ValidationIssue(
                code=ValidationCode.SAME_CURRENCY,
                severity=Severity.BLOCK,
                message=f"Currency mismatch: '{currency_a}' vs '{currency_b}'. "
                f"Cross-currency computation requires explicit conversion.",
            )
        return None

    @staticmethod
    def check_same_period(period_a: str, period_b: str) -> ValidationIssue | None:
        if period_a != period_b:
            return ValidationIssue(
                code=ValidationCode.SAME_PERIOD,
                severity=Severity.BLOCK,
                message=f"Period mismatch: '{period_a}' vs '{period_b}'",
            )
        return None

    @staticmethod
    def check_same_period_type(type_a: str, type_b: str) -> ValidationIssue | None:
        if type_a != type_b:
            return ValidationIssue(
                code=ValidationCode.SAME_PERIOD_TYPE,
                severity=Severity.BLOCK,
                message=f"Period type mismatch: '{type_a}' vs '{type_b}'. "
                f"Cannot mix quarterly numerator with annual denominator.",
            )
        return None

    @staticmethod
    def check_input_completeness(
        required_inputs: list[str], available_inputs: dict[str, object]
    ) -> ValidationIssue | None:
        missing = [r for r in required_inputs if r not in available_inputs]
        if missing:
            return ValidationIssue(
                code=ValidationCode.INPUT_COMPLETENESS,
                severity=Severity.BLOCK,
                message=f"Missing required inputs: {missing}",
                context={"missing": missing},
            )
        return None

    @staticmethod
    def check_definition_resolved(definition_id: str | None) -> ValidationIssue | None:
        if not definition_id:
            return ValidationIssue(
                code=ValidationCode.DEFINITION_RESOLVED,
                severity=Severity.BLOCK,
                message="No definition_id bound. All metrics require a registered definition.",
            )
        return None

    def validate_metric_inputs(
        self,
        *,
        entity_ids: list[str],
        currencies: list[str],
        periods: list[str],
        period_types: list[str],
        definition_id: str | None,
        required_inputs: list[str],
        available_inputs: dict[str, object],
    ) -> ValidationResult:
        """Run all applicable validation gates on metric computation inputs."""
        result = ValidationResult()

        # Entity consistency
        if len(set(entity_ids)) > 1:
            for i in range(1, len(entity_ids)):
                issue = self.check_same_entity(entity_ids[0], entity_ids[i])
                if issue:
                    result.add(issue)

        # Currency consistency
        if len(set(currencies)) > 1:
            for i in range(1, len(currencies)):
                issue = self.check_same_currency(currencies[0], currencies[i])
                if issue:
                    result.add(issue)

        # Period consistency
        if len(set(periods)) > 1:
            for i in range(1, len(periods)):
                issue = self.check_same_period(periods[0], periods[i])
                if issue:
                    result.add(issue)

        # Period type consistency
        if len(set(period_types)) > 1:
            for i in range(1, len(period_types)):
                issue = self.check_same_period_type(period_types[0], period_types[i])
                if issue:
                    result.add(issue)

        # Definition resolved
        issue = self.check_definition_resolved(definition_id)
        if issue:
            result.add(issue)

        # Input completeness
        issue = self.check_input_completeness(required_inputs, available_inputs)
        if issue:
            result.add(issue)

        return result
