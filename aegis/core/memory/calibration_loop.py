"""Calibration Loop — connects predictions → post-mortems → feedback.

Implements Section 26.4: the closed-loop calibration system.

Flow:
  1. At publish time: record prediction with thesis snapshot
  2. At review time: create post-mortem comparing prediction vs. actual
  3. Quarterly: generate calibration report
  4. Feedback: adjust confidence policy based on empirical precision

Usage:
    loop = CalibrationLoop(store=PredictionStore(Path("data/predictions")))

    # At publish:
    loop.record_thesis(decision, signal, market_data)

    # At review:
    loop.create_postmortem("th_meta_001", current_price=650.0, actuals={...})

    # Quarterly:
    report = loop.generate_calibration_report()

    # Feedback:
    adjustments = loop.get_confidence_adjustments()
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any
from uuid import uuid4

from .prediction_store import PredictionRecord, PostMortemRecord, PredictionStore


# Expected empirical precision by confidence bucket (Section 26.4)
EXPECTED_PRECISION = {
    "very_low": 0.20,
    "low": 0.35,
    "medium": 0.50,
    "high": 0.65,
    "very_high": 0.80,
}

# Minimum post-mortems per bucket before considered calibrated
MIN_CALIBRATION_SAMPLE = 20


@dataclass
class CalibrationAdjustment:
    """A recommended adjustment to the confidence policy."""

    bucket: str
    expected_precision: float
    actual_precision: float
    deviation: float
    sample_size: int
    recommendation: str
    is_calibrated: bool


@dataclass
class ForecastAccuracyReport:
    """Forecast accuracy metrics from post-mortem data."""

    total_evaluated: int
    direction_accuracy: float  # % of long/short calls that were directionally correct
    mean_absolute_error_pct: float  # Average |predicted return - actual return|
    scenario_hit_rate: dict[str, float]  # {"bear": 0.2, "base": 0.5, "bull": 0.25, "outside": 0.05}
    bias: str  # "optimistic", "pessimistic", "neutral"
    bias_magnitude: float  # Absolute average error (signed)
    by_direction: dict[str, dict]  # {"long": {"count": N, "avg_return": 0.12, "win_rate": 0.65}}
    by_confidence: dict[str, dict]  # {"high": {"count": N, "avg_return": 0.15, "win_rate": 0.70}}


@dataclass
class CalibrationReportOutput:
    """Full calibration report."""

    report_date: str
    total_predictions: int
    total_postmortems: int
    active_predictions: int
    due_for_review: int
    bucket_adjustments: list[CalibrationAdjustment]
    overall_calibration_score: float  # 0-1, average across buckets
    is_system_calibrated: bool


class CalibrationLoop:
    """Closed-loop calibration system.

    Connects thesis publishing → outcome tracking → confidence adjustment.
    """

    def __init__(self, store: PredictionStore | None = None) -> None:
        self._store = store or PredictionStore()

    # ── Record at publish time ───────────────────────────────────────

    def record_thesis(
        self,
        decision: Any,
        signal: Any,
        market_data: dict[str, float],
    ) -> PredictionRecord:
        """Record a thesis prediction at publish time.

        Called after DecisionEngine + PortfolioIntegration.
        """
        scenarios = getattr(decision, "scenarios", {})
        if isinstance(scenarios, dict):
            bear = scenarios.get("bear_value", 0)
            base = scenarios.get("base_value", 0)
            bull = scenarios.get("bull_value", 0)
        else:
            bear = base = bull = 0

        review_date = None
        if hasattr(signal, "review_date") and signal.review_date:
            review_date = signal.review_date.isoformat() if isinstance(signal.review_date, date) else str(signal.review_date)

        record = PredictionRecord(
            thesis_id=f"th_{getattr(decision, 'entity_id', 'unknown')}_{getattr(decision, 'run_id', '')}",
            entity_id=getattr(decision, "entity_id", "unknown"),
            run_id=getattr(decision, "run_id", ""),
            publish_date=datetime.now(timezone.utc).date().isoformat(),
            confidence_bucket=getattr(decision, "confidence_bucket", "medium"),
            bear_value=bear,
            base_value=base,
            bull_value=bull,
            current_price=market_data.get("current_price", 0),
            implied_growth=0.0,
            edge_type=getattr(getattr(decision, "edge_assessment", None), "primary_edge_type", "analytical")
                if hasattr(decision, "edge_assessment") else "analytical",
            direction=getattr(signal, "direction", "no_signal"),
            conviction=getattr(signal, "conviction", "very_low"),
            review_date=review_date,
        )

        self._store.record_prediction(record)
        return record

    # ── Post-mortem at review time ───────────────────────────────────

    def create_postmortem(
        self,
        thesis_id: str,
        current_price: float,
        thesis_survived: bool | None = None,
        variant_realized: bool = False,
        edge_realized: bool = False,
        error_labels: list[str] | None = None,
        lessons: list[str] | None = None,
    ) -> PostMortemRecord | None:
        """Create a post-mortem for a thesis.

        If thesis_survived is None, it's auto-determined based on direction + price movement.
        """
        pred = self._store.get_prediction(thesis_id)
        if pred is None:
            return None

        # Auto-determine thesis survival
        if thesis_survived is None:
            if pred.direction == "long":
                thesis_survived = current_price > pred.current_price
            elif pred.direction == "short":
                thesis_survived = current_price < pred.current_price
            else:
                thesis_survived = False

        total_return = (current_price - pred.current_price) / pred.current_price if pred.current_price else 0

        record = PostMortemRecord(
            postmortem_id=f"pm_{uuid4().hex[:8]}",
            thesis_id=thesis_id,
            entity_id=pred.entity_id,
            review_date=date.today().isoformat(),
            price_at_thesis=pred.current_price,
            price_at_review=current_price,
            total_return=total_return,
            thesis_survived=thesis_survived,
            variant_realized=variant_realized,
            edge_realized=edge_realized,
            original_confidence_bucket=pred.confidence_bucket,
            error_labels=error_labels or [],
            lessons=lessons or [],
        )

        self._store.record_postmortem(record)
        return record

    # ── Calibration report ───────────────────────────────────────────

    def generate_calibration_report(self) -> CalibrationReportOutput:
        """Generate a calibration report from stored data."""
        cal = self._store.get_calibration_snapshot()
        bucket_stats = cal.get("bucket_stats", {})

        adjustments = []
        scores = []

        for bucket, expected in EXPECTED_PRECISION.items():
            stats = bucket_stats.get(bucket, {})
            total = stats.get("total", 0)

            if total == 0:
                adjustments.append(CalibrationAdjustment(
                    bucket=bucket,
                    expected_precision=expected,
                    actual_precision=0.0,
                    deviation=0.0,
                    sample_size=0,
                    recommendation=f"No data yet for '{bucket}' bucket — need {MIN_CALIBRATION_SAMPLE} post-mortems",
                    is_calibrated=False,
                ))
                continue

            actual = stats["survived"] / total
            deviation = abs(actual - expected)
            is_calibrated = total >= MIN_CALIBRATION_SAMPLE and deviation < 0.15
            score = max(0, 1.0 - deviation)
            scores.append(score)

            if deviation < 0.10:
                rec = f"Well calibrated (deviation {deviation:.0%})"
            elif actual > expected:
                rec = f"Over-confident: actual precision {actual:.0%} > expected {expected:.0%}. Consider upgrading some '{bucket}' theses to higher confidence."
            else:
                rec = f"Under-confident: actual precision {actual:.0%} < expected {expected:.0%}. Consider downgrading some '{bucket}' theses to lower confidence."

            adjustments.append(CalibrationAdjustment(
                bucket=bucket,
                expected_precision=expected,
                actual_precision=actual,
                deviation=deviation,
                sample_size=total,
                recommendation=rec,
                is_calibrated=is_calibrated,
            ))

        all_preds = self._store._load_predictions()
        active = [r for r in all_preds if r.get("status") == "active"]
        due = self._store.get_due_for_review()

        return CalibrationReportOutput(
            report_date=date.today().isoformat(),
            total_predictions=len(all_preds),
            total_postmortems=cal.get("total_postmortems", 0),
            active_predictions=len(active),
            due_for_review=len(due),
            bucket_adjustments=adjustments,
            overall_calibration_score=sum(scores) / len(scores) if scores else 0.0,
            is_system_calibrated=all(a.is_calibrated for a in adjustments if a.sample_size > 0),
        )

    def get_confidence_adjustments(self) -> list[CalibrationAdjustment]:
        """Get actionable adjustments for the confidence policy."""
        report = self.generate_calibration_report()
        return [a for a in report.bucket_adjustments if a.sample_size > 0 and a.deviation >= 0.10]

    # ── Automated review ────────────────────────────────────────────

    def review_due_predictions(
        self,
        price_fetcher: Any = None,
        as_of: date | None = None,
    ) -> list[PostMortemRecord]:
        """Auto-review all predictions past their review date.

        If price_fetcher is provided, it should be a callable:
            price_fetcher(ticker: str) -> float | None

        Otherwise, no auto-review is performed (returns empty list).
        """
        due = self._store.get_due_for_review(as_of)
        results: list[PostMortemRecord] = []

        for pred in due:
            # Fetch current price
            current_price = None
            if price_fetcher:
                try:
                    current_price = price_fetcher(pred.entity_id)
                except Exception:
                    pass

            if current_price is None:
                continue

            pm = self.create_postmortem(
                pred.thesis_id,
                current_price=current_price,
            )
            if pm:
                results.append(pm)

        return results

    # ── Forecast accuracy metrics ───────────────────────────────────

    def compute_forecast_accuracy(self) -> ForecastAccuracyReport:
        """Compute forecast accuracy metrics from post-mortem data.

        Measures how well our bear/base/bull scenarios predicted actual outcomes.
        """
        predictions = self._store._load_predictions()
        postmortems = self._store._load_postmortems()

        if not postmortems:
            return ForecastAccuracyReport(
                total_evaluated=0,
                direction_accuracy=0.0,
                mean_absolute_error_pct=0.0,
                scenario_hit_rate={},
                bias="neutral",
                bias_magnitude=0.0,
                by_direction={},
                by_confidence={},
            )

        # Build lookup: thesis_id → prediction
        pred_map = {p["thesis_id"]: p for p in predictions}

        total = 0
        direction_correct = 0
        errors = []
        scenario_hits = {"bear": 0, "base": 0, "bull": 0, "outside": 0}
        by_direction: dict[str, list[float]] = {}
        by_confidence: dict[str, list[float]] = {}

        for pm in postmortems:
            pred = pred_map.get(pm["thesis_id"])
            if not pred:
                continue

            total += 1
            price_at_thesis = pm.get("price_at_thesis", 0)
            price_at_review = pm.get("price_at_review", 0)
            if not price_at_thesis:
                continue

            actual_return = (price_at_review - price_at_thesis) / price_at_thesis
            base_return = (pred["base_value"] - price_at_thesis) / price_at_thesis if price_at_thesis else 0

            # Direction accuracy
            direction = pred.get("direction", "no_signal")
            if direction == "long" and actual_return > 0:
                direction_correct += 1
            elif direction == "short" and actual_return < 0:
                direction_correct += 1

            # Scenario hit rate: did actual price land in bear/base/bull range?
            bear_v = pred.get("bear_value", 0)
            base_v = pred.get("base_value", 0)
            bull_v = pred.get("bull_value", 0)
            if bear_v and base_v and bull_v:
                mid_bear_base = (bear_v + base_v) / 2
                mid_base_bull = (base_v + bull_v) / 2
                if price_at_review < mid_bear_base:
                    scenario_hits["bear"] += 1
                elif price_at_review <= mid_base_bull:
                    scenario_hits["base"] += 1
                elif price_at_review <= bull_v * 1.1:
                    scenario_hits["bull"] += 1
                else:
                    scenario_hits["outside"] += 1

            # Error: base case prediction vs actual
            error = base_return - actual_return
            errors.append(error)

            # By direction
            by_direction.setdefault(direction, []).append(actual_return)

            # By confidence
            bucket = pred.get("confidence_bucket", "medium")
            by_confidence.setdefault(bucket, []).append(actual_return)

        # Compute aggregates
        mae = sum(abs(e) for e in errors) / len(errors) if errors else 0.0
        mean_error = sum(errors) / len(errors) if errors else 0.0
        if mean_error > 0.05:
            bias = "optimistic"
        elif mean_error < -0.05:
            bias = "pessimistic"
        else:
            bias = "neutral"

        # Scenario hit rate as percentages
        scenario_rate = {k: v / total if total else 0 for k, v in scenario_hits.items()}

        # Summarize by_direction and by_confidence
        dir_summary = {}
        for d, returns in by_direction.items():
            avg_ret = sum(returns) / len(returns)
            dir_summary[d] = {
                "count": len(returns),
                "avg_return": round(avg_ret, 4),
                "win_rate": sum(1 for r in returns if r > 0) / len(returns),
            }

        conf_summary = {}
        for bucket, returns in by_confidence.items():
            avg_ret = sum(returns) / len(returns)
            conf_summary[bucket] = {
                "count": len(returns),
                "avg_return": round(avg_ret, 4),
                "win_rate": sum(1 for r in returns if r > 0) / len(returns),
            }

        return ForecastAccuracyReport(
            total_evaluated=total,
            direction_accuracy=direction_correct / total if total else 0.0,
            mean_absolute_error_pct=mae,
            scenario_hit_rate=scenario_rate,
            bias=bias,
            bias_magnitude=abs(mean_error),
            by_direction=dir_summary,
            by_confidence=conf_summary,
        )

    def get_calibration_context(self) -> dict[str, Any]:
        """Get calibration data formatted for injection into agent_macro.

        Used to inform agents and DecisionEngine about historical accuracy.
        """
        report = self.generate_calibration_report()
        accuracy = self.compute_forecast_accuracy()

        bucket_precision = {}
        for adj in report.bucket_adjustments:
            if adj.sample_size > 0:
                bucket_precision[adj.bucket] = {
                    "precision": round(adj.actual_precision, 3),
                    "sample_size": adj.sample_size,
                    "is_calibrated": adj.is_calibrated,
                }

        return {
            "total_predictions": report.total_predictions,
            "total_postmortems": report.total_postmortems,
            "active_predictions": report.active_predictions,
            "due_for_review": report.due_for_review,
            "overall_calibration_score": round(report.overall_calibration_score, 3),
            "is_system_calibrated": report.is_system_calibrated,
            "bucket_precision": bucket_precision,
            "forecast_accuracy": {
                "direction_accuracy": round(accuracy.direction_accuracy, 3),
                "mean_absolute_error_pct": round(accuracy.mean_absolute_error_pct, 4),
                "bias": accuracy.bias,
                "scenario_hit_rate": accuracy.scenario_hit_rate,
            } if accuracy.total_evaluated > 0 else None,
        }
