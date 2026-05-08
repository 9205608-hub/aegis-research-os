"""Tests for the prediction backtesting / calibration system.

Round 21 — Prediction backtesting system.
Tests cover:
- ForecastAccuracyReport computation
- review_due_predictions() auto post-mortem
- get_calibration_context() for pipeline injection
- Direction accuracy, scenario hit rate, bias detection
- CalibrationLoop integration with PredictionStore
"""

import pytest
from datetime import date
from pathlib import Path

from aegis.core.memory.prediction_store import (
    PredictionStore,
    PredictionRecord,
    PostMortemRecord,
)
from aegis.core.memory.calibration_loop import (
    CalibrationLoop,
    ForecastAccuracyReport,
    CalibrationReportOutput,
)


@pytest.fixture
def tmp_store(tmp_path):
    return PredictionStore(tmp_path / "test_predictions")


@pytest.fixture
def loop(tmp_store):
    return CalibrationLoop(store=tmp_store)


def _make_pred(
    thesis_id="th_test_001",
    entity_id="meta_platforms",
    confidence="medium",
    price=500.0,
    direction="long",
    bear=400.0,
    base=600.0,
    bull=800.0,
    review_date="2020-01-01",
) -> PredictionRecord:
    return PredictionRecord(
        thesis_id=thesis_id,
        entity_id=entity_id,
        run_id="run_test",
        publish_date="2025-01-15",
        confidence_bucket=confidence,
        bear_value=bear,
        base_value=base,
        bull_value=bull,
        current_price=price,
        implied_growth=0.12,
        edge_type="analytical",
        direction=direction,
        conviction="medium",
        review_date=review_date,
    )


# ============================================================
# ForecastAccuracyReport Tests
# ============================================================

class TestForecastAccuracy:

    def test_empty_returns_zero(self, loop):
        report = loop.compute_forecast_accuracy()
        assert isinstance(report, ForecastAccuracyReport)
        assert report.total_evaluated == 0
        assert report.direction_accuracy == 0.0
        assert report.bias == "neutral"

    def test_direction_accuracy_long_correct(self, loop, tmp_store):
        """Long call with price increase → direction correct."""
        pred = _make_pred(direction="long", price=500.0)
        tmp_store.record_prediction(pred)
        loop.create_postmortem("th_test_001", current_price=600.0)

        report = loop.compute_forecast_accuracy()
        assert report.total_evaluated == 1
        assert report.direction_accuracy == 1.0

    def test_direction_accuracy_long_wrong(self, loop, tmp_store):
        """Long call with price decrease → direction wrong."""
        pred = _make_pred(direction="long", price=500.0)
        tmp_store.record_prediction(pred)
        loop.create_postmortem("th_test_001", current_price=400.0)

        report = loop.compute_forecast_accuracy()
        assert report.direction_accuracy == 0.0

    def test_direction_accuracy_short_correct(self, loop, tmp_store):
        """Short call with price decrease → direction correct."""
        pred = _make_pred(direction="short", price=500.0)
        tmp_store.record_prediction(pred)
        loop.create_postmortem("th_test_001", current_price=400.0)

        report = loop.compute_forecast_accuracy()
        assert report.direction_accuracy == 1.0

    def test_mixed_direction_accuracy(self, loop, tmp_store):
        """3 out of 4 correct → 75% accuracy."""
        for i in range(4):
            pred = _make_pred(
                thesis_id=f"th_{i}", direction="long", price=100.0,
            )
            tmp_store.record_prediction(pred)
            # 3 correct (price up), 1 wrong (price down)
            final_price = 120.0 if i < 3 else 80.0
            loop.create_postmortem(f"th_{i}", current_price=final_price)

        report = loop.compute_forecast_accuracy()
        assert report.direction_accuracy == pytest.approx(0.75)

    def test_scenario_hit_rate_base(self, loop, tmp_store):
        """Price lands near base case → base scenario hit."""
        pred = _make_pred(bear=400.0, base=600.0, bull=800.0, price=500.0)
        tmp_store.record_prediction(pred)
        # Actual price = 580, which is between mid(bear,base)=500 and mid(base,bull)=700
        loop.create_postmortem("th_test_001", current_price=580.0)

        report = loop.compute_forecast_accuracy()
        assert report.scenario_hit_rate.get("base", 0) > 0

    def test_scenario_hit_rate_bear(self, loop, tmp_store):
        """Price drops below mid(bear,base) → bear scenario hit."""
        pred = _make_pred(bear=400.0, base=600.0, bull=800.0, price=500.0)
        tmp_store.record_prediction(pred)
        # Actual price = 450, below mid(400,600)=500
        loop.create_postmortem("th_test_001", current_price=450.0)

        report = loop.compute_forecast_accuracy()
        assert report.scenario_hit_rate.get("bear", 0) > 0

    def test_bias_detection_optimistic(self, loop, tmp_store):
        """Consistently overestimating → optimistic bias."""
        for i in range(5):
            pred = _make_pred(
                thesis_id=f"th_{i}", direction="long", price=100.0,
                base=150.0,  # Predicted 50% upside
            )
            tmp_store.record_prediction(pred)
            # Actual: only 5% up → we overestimated
            loop.create_postmortem(f"th_{i}", current_price=105.0)

        report = loop.compute_forecast_accuracy()
        assert report.bias == "optimistic"
        assert report.bias_magnitude > 0.05

    def test_bias_detection_neutral(self, loop, tmp_store):
        """Predictions roughly match reality → neutral bias."""
        for i in range(4):
            pred = _make_pred(
                thesis_id=f"th_{i}", direction="long", price=100.0,
                base=120.0,  # Predicted 20% upside
            )
            tmp_store.record_prediction(pred)
            # Actual: 18-22% up → close to prediction
            loop.create_postmortem(f"th_{i}", current_price=119.0 + i)

        report = loop.compute_forecast_accuracy()
        assert report.bias == "neutral"

    def test_by_direction_stats(self, loop, tmp_store):
        """Verify stats are broken down by direction."""
        for i in range(3):
            pred = _make_pred(thesis_id=f"th_long_{i}", direction="long", price=100.0)
            tmp_store.record_prediction(pred)
            loop.create_postmortem(f"th_long_{i}", current_price=120.0)

        pred = _make_pred(thesis_id="th_short_0", direction="short", price=100.0)
        tmp_store.record_prediction(pred)
        loop.create_postmortem("th_short_0", current_price=80.0)

        report = loop.compute_forecast_accuracy()
        assert "long" in report.by_direction
        assert report.by_direction["long"]["count"] == 3
        assert report.by_direction["long"]["avg_return"] == pytest.approx(0.20)
        assert "short" in report.by_direction

    def test_by_confidence_stats(self, loop, tmp_store):
        """Verify stats are broken down by confidence bucket."""
        pred_high = _make_pred(thesis_id="th_h", confidence="high", price=100.0)
        tmp_store.record_prediction(pred_high)
        loop.create_postmortem("th_h", current_price=130.0)

        pred_low = _make_pred(thesis_id="th_l", confidence="low", price=100.0)
        tmp_store.record_prediction(pred_low)
        loop.create_postmortem("th_l", current_price=90.0)

        report = loop.compute_forecast_accuracy()
        assert "high" in report.by_confidence
        assert "low" in report.by_confidence
        assert report.by_confidence["high"]["avg_return"] == pytest.approx(0.30)


# ============================================================
# review_due_predictions() Tests
# ============================================================

class TestReviewDuePredictions:

    def test_reviews_due_predictions(self, loop, tmp_store):
        """Auto-reviews past-due predictions when price_fetcher is provided."""
        pred = _make_pred(review_date="2020-01-01")  # Past due
        tmp_store.record_prediction(pred)

        def mock_fetcher(entity_id):
            return 650.0

        results = loop.review_due_predictions(price_fetcher=mock_fetcher)
        assert len(results) == 1
        assert results[0].price_at_review == 650.0
        assert results[0].thesis_survived is True  # long + price up

    def test_skips_future_predictions(self, loop, tmp_store):
        """Does not review predictions with future review dates."""
        pred = _make_pred(review_date="2099-01-01")
        tmp_store.record_prediction(pred)

        results = loop.review_due_predictions(price_fetcher=lambda _: 650.0)
        assert len(results) == 0

    def test_skips_when_no_price_fetcher(self, loop, tmp_store):
        """Returns empty when no price_fetcher provided."""
        pred = _make_pred(review_date="2020-01-01")
        tmp_store.record_prediction(pred)

        results = loop.review_due_predictions()
        assert len(results) == 0

    def test_skips_when_price_fetch_fails(self, loop, tmp_store):
        """Skips predictions where price fetch raises."""
        pred = _make_pred(review_date="2020-01-01")
        tmp_store.record_prediction(pred)

        def failing_fetcher(entity_id):
            raise ConnectionError("API down")

        results = loop.review_due_predictions(price_fetcher=failing_fetcher)
        assert len(results) == 0

    def test_multiple_due_predictions(self, loop, tmp_store):
        """Reviews all past-due predictions."""
        for i in range(3):
            pred = _make_pred(
                thesis_id=f"th_{i}",
                entity_id=f"entity_{i}",
                review_date="2020-01-01",
            )
            tmp_store.record_prediction(pred)

        prices = {"entity_0": 600.0, "entity_1": 400.0, "entity_2": 550.0}
        results = loop.review_due_predictions(
            price_fetcher=lambda eid: prices.get(eid),
        )
        assert len(results) == 3


# ============================================================
# get_calibration_context() Tests
# ============================================================

class TestCalibrationContext:

    def test_empty_context(self, loop):
        ctx = loop.get_calibration_context()
        assert ctx["total_postmortems"] == 0
        assert ctx["forecast_accuracy"] is None

    def test_context_with_data(self, loop, tmp_store):
        """Context should contain calibration + accuracy data."""
        for i in range(3):
            pred = _make_pred(thesis_id=f"th_{i}", confidence="medium", price=100.0)
            tmp_store.record_prediction(pred)
            loop.create_postmortem(f"th_{i}", current_price=120.0)

        ctx = loop.get_calibration_context()
        assert ctx["total_postmortems"] == 3
        assert ctx["overall_calibration_score"] > 0
        assert ctx["forecast_accuracy"] is not None
        assert ctx["forecast_accuracy"]["direction_accuracy"] == 1.0  # All long + up
        assert "medium" in ctx["bucket_precision"]

    def test_context_format_for_agent_macro(self, loop, tmp_store):
        """Context should be dict-serializable for agent_macro injection."""
        pred = _make_pred(confidence="high")
        tmp_store.record_prediction(pred)
        loop.create_postmortem("th_test_001", current_price=600.0)

        ctx = loop.get_calibration_context()
        # Should be plain dict/list/str/float — no dataclasses or custom objects
        import json
        json_str = json.dumps(ctx, default=str)
        assert len(json_str) > 10


# ============================================================
# Integration: Full Pipeline Simulation
# ============================================================

class TestFullPipelineSimulation:
    """Simulate a multi-research-run backtesting workflow."""

    def test_multi_run_calibration_loop(self, loop, tmp_store):
        """Simulate 10 research runs → post-mortems → calibration report."""
        # Phase 1: Record predictions from 10 "research runs"
        configs = [
            ("th_meta_001", "meta", "high", "long", 500.0, 400, 600, 800),
            ("th_aapl_001", "apple", "medium", "long", 200.0, 160, 240, 300),
            ("th_goog_001", "google", "high", "long", 150.0, 120, 180, 220),
            ("th_nvda_001", "nvidia", "very_high", "long", 800.0, 600, 1000, 1300),
            ("th_tsla_001", "tesla", "low", "short", 250.0, 150, 200, 280),
            ("th_amzn_001", "amazon", "medium", "long", 180.0, 140, 210, 260),
            ("th_msft_001", "microsoft", "high", "long", 400.0, 350, 450, 520),
            ("th_jpm_001", "jpmorgan", "medium", "long", 190.0, 160, 220, 260),
            ("th_bac_001", "bofa", "low", "long", 40.0, 32, 48, 58),
            ("th_dis_001", "disney", "medium", "short", 110.0, 70, 85, 100),
        ]

        for tid, eid, conf, direction, price, bear, base, bull in configs:
            pred = _make_pred(
                thesis_id=tid, entity_id=eid, confidence=conf,
                direction=direction, price=price,
                bear=bear, base=base, bull=bull,
                review_date="2020-01-01",
            )
            tmp_store.record_prediction(pred)

        assert len(tmp_store.get_active_predictions()) == 10

        # Phase 2: Create post-mortems (simulate actual outcomes)
        outcomes = {
            "th_meta_001": 650.0,   # Long, price up → correct
            "th_aapl_001": 220.0,   # Long, price up → correct
            "th_goog_001": 140.0,   # Long, price down → wrong
            "th_nvda_001": 1100.0,  # Long, price up → correct
            "th_tsla_001": 200.0,   # Short, price down → correct
            "th_amzn_001": 195.0,   # Long, price up → correct
            "th_msft_001": 380.0,   # Long, price down → wrong
            "th_jpm_001": 210.0,    # Long, price up → correct
            "th_bac_001": 38.0,     # Long, price down → wrong
            "th_dis_001": 120.0,    # Short, price up → wrong
        }

        for tid, actual_price in outcomes.items():
            loop.create_postmortem(tid, current_price=actual_price)

        # Phase 3: Verify calibration
        report = loop.generate_calibration_report()
        assert report.total_postmortems == 10
        assert report.active_predictions == 0

        # Phase 4: Verify forecast accuracy
        accuracy = loop.compute_forecast_accuracy()
        assert accuracy.total_evaluated == 10
        # 6 correct out of 10: META(T), AAPL(T), GOOG(F), NVDA(T), TSLA(T), AMZN(T), MSFT(F), JPM(T), BAC(F), DIS(F)
        assert accuracy.direction_accuracy == pytest.approx(0.60)
        assert accuracy.by_direction["long"]["count"] == 8
        assert accuracy.by_direction["short"]["count"] == 2

        # Phase 5: Verify calibration context is injectable
        ctx = loop.get_calibration_context()
        assert ctx["total_postmortems"] == 10
        assert ctx["forecast_accuracy"]["direction_accuracy"] == pytest.approx(0.60)
        assert "high" in ctx["bucket_precision"]

    def test_auto_review_workflow(self, loop, tmp_store):
        """Simulate auto-review with price_fetcher."""
        for i in range(5):
            pred = _make_pred(
                thesis_id=f"th_auto_{i}",
                entity_id=f"entity_{i}",
                review_date="2020-01-01",
                price=100.0,
            )
            tmp_store.record_prediction(pred)

        # Auto-review with mock price fetcher
        mock_prices = {f"entity_{i}": 100.0 + (i * 10) for i in range(5)}
        results = loop.review_due_predictions(
            price_fetcher=lambda eid: mock_prices.get(eid),
        )
        assert len(results) == 5

        # All should now be reviewed
        assert len(tmp_store.get_active_predictions()) == 0

        # Accuracy should reflect outcomes
        accuracy = loop.compute_forecast_accuracy()
        assert accuracy.total_evaluated == 5
