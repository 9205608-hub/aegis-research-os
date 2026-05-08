"""Golden Cases — Section 27.

Golden cases are regression-grade test scenarios covering:
- Multiple sectors and accounting standards
- Time-dimension cases (same company at different points)
- Error injection tests (PIT leak, double counting, bias injection, etc.)
- Multi-entity cases (pair trade, thematic)

Section 27: error injection tests must verify critics catch:
- PIT leak, double counting, definition drift, stale consensus
- Wrong sector pack, FX error
- Confirmation bias, anchoring, narrative fallacy
- Cross-standard comparison without bridge
- Missing edge assessment
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(frozen=True)
class GoldenCase:
    """A single golden case for regression testing."""

    case_id: str
    description: str
    category: str  # "sector", "accounting", "error_injection", "bias_injection", "multi_entity", "time_dimension"
    entity_type: str  # "mega_cap", "mid_cap", "soe", "vie", etc.
    accounting_standard: str  # "US_GAAP", "IFRS", "CAS"
    test_fn: Callable[[], bool]
    expected_critic_codes: list[str] = field(default_factory=list)


class GoldenCaseRegistry:
    """Registry for golden cases used in regression testing.

    Section 27: minimum coverage dimensions include software/SaaS,
    ad/platform, semiconductor, consumer, industrial, etc.
    """

    def __init__(self) -> None:
        self._cases: dict[str, GoldenCase] = {}

    def register(self, case: GoldenCase) -> None:
        self._cases[case.case_id] = case

    def get_case(self, case_id: str) -> GoldenCase | None:
        return self._cases.get(case_id)

    def list_cases(self, category: str | None = None) -> list[GoldenCase]:
        cases = list(self._cases.values())
        if category:
            cases = [c for c in cases if c.category == category]
        return cases

    def run_all(self) -> dict[str, bool]:
        """Run all golden cases, return case_id -> passed."""
        results = {}
        for case_id, case in self._cases.items():
            try:
                results[case_id] = case.test_fn()
            except Exception:
                results[case_id] = False
        return results

    def run_category(self, category: str) -> dict[str, bool]:
        """Run all cases in a specific category."""
        results = {}
        for case_id, case in self._cases.items():
            if case.category != category:
                continue
            try:
                results[case_id] = case.test_fn()
            except Exception:
                results[case_id] = False
        return results

    @property
    def coverage_summary(self) -> dict[str, int]:
        """Summary of case counts by category."""
        summary: dict[str, int] = {}
        for case in self._cases.values():
            summary[case.category] = summary.get(case.category, 0) + 1
        return summary
