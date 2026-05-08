"""Eval, Calibration & Backtesting — Section 26-27."""

from aegis.core.evals.eval_engine import EvalEngine, EvalResult, EvalCheck
from aegis.core.evals.regression import (
    RegressionHarness,
    RegressionCase,
    RegressionResult,
    ERROR_TAXONOMY,
    classify_error,
)
from aegis.core.evals.backtesting import BacktestingFramework, CalibrationReport
from aegis.core.evals.golden_cases import GoldenCaseRegistry, GoldenCase

__all__ = [
    "EvalEngine", "EvalResult", "EvalCheck",
    "RegressionHarness", "RegressionCase", "RegressionResult",
    "ERROR_TAXONOMY", "classify_error",
    "BacktestingFramework", "CalibrationReport",
    "GoldenCaseRegistry", "GoldenCase",
]
