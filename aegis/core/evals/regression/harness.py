"""Regression Harness + Error Taxonomy — Section 26.

Section 26.4: any definition/formula/critic/prompt change must run regression.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable


# Error taxonomy from Section 26.1
ERROR_TAXONOMY = [
    "factual_extraction_error",
    "definition_drift",
    "pit_leakage",
    "evidence_overreach",
    "consensus_misread",
    "valuation_framing_error",
    "double_counting",
    "regime_transfer_error",
    "sector_analogy_misuse",
    "overconfidence_error",
    "macro_context_ignored",
    "management_risk_underestimated",
    "cycle_position_misjudged",
    "catalyst_timing_error",
    "anchoring_error",
    "confirmation_bias_error",
    "narrative_fallacy_error",
    "edge_misclassification",
    "supply_chain_cascade_miss",
    "cross_market_comparison_error",
]


@dataclass(frozen=True)
class RegressionCase:
    """A single regression test case."""

    case_id: str
    description: str
    category: str  # "deterministic", "integrity", "critic", "golden_case"
    runner: Callable[[], bool]  # Returns True if passed
    expected_errors: list[str] = field(default_factory=list)  # For error injection cases


@dataclass
class RegressionResult:
    """Result of running the regression suite."""

    suite_id: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    total: int = 0
    passed: int = 0
    failed: int = 0
    failures: list[dict] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        return self.passed / max(self.total, 1)

    @property
    def all_passed(self) -> bool:
        return self.failed == 0


class RegressionHarness:
    """Regression test harness for systematic quality assurance.

    Section 26.4: runs after any definition/formula/critic policy change.
    """

    def __init__(self) -> None:
        self._cases: list[RegressionCase] = []
        self._history: list[RegressionResult] = []

    def register_case(self, case: RegressionCase) -> None:
        self._cases.append(case)

    def register_cases(self, cases: list[RegressionCase]) -> None:
        self._cases.extend(cases)

    def run_suite(self, suite_id: str = "") -> RegressionResult:
        """Run all registered regression cases."""
        sid = suite_id or f"reg_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
        result = RegressionResult(suite_id=sid, total=len(self._cases))

        for case in self._cases:
            try:
                passed = case.runner()
                if passed:
                    result.passed += 1
                else:
                    result.failed += 1
                    result.failures.append({
                        "case_id": case.case_id,
                        "description": case.description,
                        "error": "Case returned False",
                    })
            except Exception as e:
                result.failed += 1
                result.failures.append({
                    "case_id": case.case_id,
                    "description": case.description,
                    "error": str(e),
                })

        self._history.append(result)
        return result

    def run_category(self, category: str) -> RegressionResult:
        """Run only cases in a specific category."""
        cases = [c for c in self._cases if c.category == category]
        sid = f"reg_{category}_{datetime.now(timezone.utc).strftime('%H%M%S')}"
        result = RegressionResult(suite_id=sid, total=len(cases))

        for case in cases:
            try:
                if case.runner():
                    result.passed += 1
                else:
                    result.failed += 1
                    result.failures.append({"case_id": case.case_id, "error": "Failed"})
            except Exception as e:
                result.failed += 1
                result.failures.append({"case_id": case.case_id, "error": str(e)})

        self._history.append(result)
        return result

    def get_history(self) -> list[RegressionResult]:
        return list(self._history)

    def compare_runs(self, run_a: str, run_b: str) -> dict:
        """Compare two regression runs to detect regressions."""
        a = next((r for r in self._history if r.suite_id == run_a), None)
        b = next((r for r in self._history if r.suite_id == run_b), None)
        if not a or not b:
            return {"error": "Run not found"}

        a_failed = {f["case_id"] for f in a.failures}
        b_failed = {f["case_id"] for f in b.failures}

        return {
            "new_failures": list(b_failed - a_failed),
            "fixed": list(a_failed - b_failed),
            "persistent_failures": list(a_failed & b_failed),
            "regression_detected": bool(b_failed - a_failed),
        }


def classify_error(error_description: str) -> list[str]:
    """Map an error description to error taxonomy labels."""
    desc_lower = error_description.lower()
    labels = []

    keyword_map = {
        "factual_extraction_error": ["extract", "parsing", "wrong value"],
        "definition_drift": ["definition", "formula changed", "metric renamed"],
        "pit_leakage": ["future data", "pit", "lookahead"],
        "double_counting": ["double count", "sbc.*dilution", "counted twice"],
        "overconfidence_error": ["overconfiden", "too narrow", "scenario range"],
        "anchoring_error": ["anchor", "guidance as starting"],
        "confirmation_bias_error": ["confirmation", "only supporting", "ignored contrary"],
        "narrative_fallacy_error": ["narrative", "no evidence", "ungrounded"],
        "edge_misclassification": ["edge type", "misclassif"],
        "macro_context_ignored": ["macro ignored", "without macro"],
        "cross_market_comparison_error": ["cross-standard", "without bridge", "cross-market"],
    }

    for label, keywords in keyword_map.items():
        if any(kw in desc_lower for kw in keywords):
            labels.append(label)

    return labels if labels else ["unclassified"]
