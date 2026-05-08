"""Tests for PredictionStore and CalibrationLoop (P1-5)."""

import pytest
import tempfile
from datetime import date
from pathlib import Path

from aegis.core.memory.prediction_store import (
    PredictionStore,
    PredictionRecord,
    PostMortemRecord,
)
from aegis.core.memory.calibration_loop import (
    CalibrationLoop,
    CalibrationReportOutput,
    EXPECTED_PRECISION,
)


@pytest.fixture
def tmp_store(tmp_path):
    return PredictionStore(tmp_path / "test_predictions")


@pytest.fixture
def loop(tmp_store):
    return CalibrationLoop(store=tmp_store)


def _make_prediction(
    thesis_id: str = "th_meta_001",
    entity_id: str = "meta_platforms",
    confidence: str = "medium",
    price: float = 500.0,
    direction: str = "long",
) -> PredictionRecord:
    return PredictionRecord(
        thesis_id=thesis_id,
        entity_id=entity_id,
        run_id="run_test",
        publish_date="2025-01-15",
        confidence_bucket=confidence,
        bear_value=400.0,
        base_value=600.0,
        bull_value=800.0,
        current_price=price,
        implied_growth=0.12,
        edge_type="analytical",
        direction=direction,
        conviction="medium",
        review_date="2025-07-15",
    )


class TestPredictionStore:

    def test_record_and_retrieve(self, tmp_store):
        pred = _make_prediction()
        tmp_store.record_prediction(pred)

        retrieved = tmp_store.get_prediction("th_meta_001")
        assert retrieved is not None
        assert retrieved.thesis_id == "th_meta_001"
        assert retrieved.entity_id == "meta_platforms"
        assert retrieved.current_price == 500.0

    def test_get_predictions_for_entity(self, tmp_store):
        tmp_store.record_prediction(_make_prediction("th_meta_001", "meta_platforms"))
        tmp_store.record_prediction(_make_prediction("th_meta_002", "meta_platforms"))
        tmp_store.record_prediction(_make_prediction("th_googl_001", "googl"))

        meta_preds = tmp_store.get_predictions_for_entity("meta_platforms")
        assert len(meta_preds) == 2

        googl_preds = tmp_store.get_predictions_for_entity("googl")
        assert len(googl_preds) == 1

    def test_get_active_predictions(self, tmp_store):
        tmp_store.record_prediction(_make_prediction("th_1"))
        tmp_store.record_prediction(_make_prediction("th_2"))
        assert len(tmp_store.get_active_predictions()) == 2

        tmp_store.mark_reviewed("th_1")
        assert len(tmp_store.get_active_predictions()) == 1

    def test_get_due_for_review(self, tmp_store):
        pred = _make_prediction()
        pred.review_date = "2020-01-01"  # Past date
        tmp_store.record_prediction(pred)

        due = tmp_store.get_due_for_review()
        assert len(due) == 1

    def test_get_due_for_review_future(self, tmp_store):
        pred = _make_prediction()
        pred.review_date = "2099-01-01"  # Far future
        tmp_store.record_prediction(pred)

        due = tmp_store.get_due_for_review()
        assert len(due) == 0

    def test_update_existing_prediction(self, tmp_store):
        pred = _make_prediction()
        tmp_store.record_prediction(pred)

        pred.current_price = 550.0
        tmp_store.record_prediction(pred)

        retrieved = tmp_store.get_prediction("th_meta_001")
        assert retrieved.current_price == 550.0
        assert len(tmp_store.get_active_predictions()) == 1  # No duplicate

    def test_record_postmortem(self, tmp_store):
        pred = _make_prediction()
        tmp_store.record_prediction(pred)

        pm = PostMortemRecord(
            postmortem_id="pm_001",
            thesis_id="th_meta_001",
            entity_id="meta_platforms",
            review_date="2025-07-15",
            price_at_thesis=500.0,
            price_at_review=650.0,
            total_return=0.30,
            thesis_survived=True,
            variant_realized=True,
            edge_realized=True,
            original_confidence_bucket="medium",
        )
        tmp_store.record_postmortem(pm)

        # Prediction should be marked as reviewed
        retrieved = tmp_store.get_prediction("th_meta_001")
        assert retrieved.status == "reviewed"

        # Post-mortem should be retrievable
        pms = tmp_store.get_postmortems("meta_platforms")
        assert len(pms) == 1
        assert pms[0].total_return == 0.30

    def test_calibration_updated_after_postmortem(self, tmp_store):
        pred = _make_prediction(confidence="high")
        tmp_store.record_prediction(pred)

        pm = PostMortemRecord(
            postmortem_id="pm_001",
            thesis_id="th_meta_001",
            entity_id="meta_platforms",
            review_date="2025-07-15",
            price_at_thesis=500.0,
            price_at_review=650.0,
            total_return=0.30,
            thesis_survived=True,
            variant_realized=False,
            edge_realized=True,
            original_confidence_bucket="high",
        )
        tmp_store.record_postmortem(pm)

        precision = tmp_store.get_bucket_precision("high")
        assert precision == 1.0  # 1/1 survived

    def test_nonexistent_prediction(self, tmp_store):
        assert tmp_store.get_prediction("nonexistent") is None

    def test_empty_store(self, tmp_store):
        assert tmp_store.get_active_predictions() == []
        assert tmp_store.get_postmortems() == []
        assert tmp_store.get_bucket_precision("high") is None


class TestCalibrationLoop:

    def test_create_postmortem_auto_survival(self, loop, tmp_store):
        """Long thesis with price increase → survived."""
        pred = _make_prediction(direction="long", price=500.0)
        tmp_store.record_prediction(pred)

        pm = loop.create_postmortem("th_meta_001", current_price=650.0)
        assert pm is not None
        assert pm.thesis_survived is True
        assert pm.total_return == pytest.approx(0.30)

    def test_create_postmortem_auto_survival_loss(self, loop, tmp_store):
        """Long thesis with price decrease → didn't survive."""
        pred = _make_prediction(direction="long", price=500.0)
        tmp_store.record_prediction(pred)

        pm = loop.create_postmortem("th_meta_001", current_price=400.0)
        assert pm.thesis_survived is False
        assert pm.total_return == pytest.approx(-0.20)

    def test_create_postmortem_explicit_survival(self, loop, tmp_store):
        pred = _make_prediction()
        tmp_store.record_prediction(pred)

        pm = loop.create_postmortem(
            "th_meta_001", current_price=600.0,
            thesis_survived=False,  # Override auto-detection
            error_labels=["overconfidence_error"],
            lessons=["Should have weighted regulatory risk higher"],
        )
        assert pm.thesis_survived is False
        assert "overconfidence_error" in pm.error_labels

    def test_create_postmortem_nonexistent(self, loop):
        pm = loop.create_postmortem("nonexistent", current_price=100.0)
        assert pm is None

    def test_calibration_report_empty(self, loop):
        report = loop.generate_calibration_report()
        assert isinstance(report, CalibrationReportOutput)
        assert report.total_predictions == 0
        assert report.total_postmortems == 0
        assert report.is_system_calibrated is True  # No data = vacuously true

    def test_calibration_report_with_data(self, loop, tmp_store):
        """Record several predictions and post-mortems, check calibration."""
        for i in range(5):
            pred = _make_prediction(
                thesis_id=f"th_test_{i}",
                confidence="medium",
                price=100.0,
            )
            tmp_store.record_prediction(pred)
            # 3 out of 5 survive (60% vs expected 50%)
            survived = i < 3
            loop.create_postmortem(
                f"th_test_{i}",
                current_price=120.0 if survived else 80.0,
            )

        report = loop.generate_calibration_report()
        assert report.total_postmortems == 5
        assert report.active_predictions == 0  # All reviewed

        medium_adj = next(
            (a for a in report.bucket_adjustments if a.bucket == "medium"), None,
        )
        assert medium_adj is not None
        assert medium_adj.actual_precision == pytest.approx(0.60)
        assert medium_adj.expected_precision == 0.50
        assert medium_adj.sample_size == 5

    def test_get_confidence_adjustments(self, loop, tmp_store):
        """Adjustments should only include buckets with deviation >= 10%."""
        # Create predictions with high deviation
        for i in range(5):
            pred = _make_prediction(
                thesis_id=f"th_high_{i}",
                confidence="high",
                price=100.0,
            )
            tmp_store.record_prediction(pred)
            # 1 out of 5 survive (20% vs expected 65% → deviation 45%)
            survived = i == 0
            loop.create_postmortem(
                f"th_high_{i}",
                current_price=120.0 if survived else 80.0,
            )

        adjustments = loop.get_confidence_adjustments()
        assert len(adjustments) >= 1
        high_adj = next((a for a in adjustments if a.bucket == "high"), None)
        assert high_adj is not None
        assert high_adj.deviation >= 0.10
        assert "downgrading" in high_adj.recommendation.lower() or "under" in high_adj.recommendation.lower()


class TestPredictionRecord:

    def test_to_dict_roundtrip(self):
        pred = _make_prediction()
        d = pred.to_dict()
        restored = PredictionRecord.from_dict(d)
        assert restored.thesis_id == pred.thesis_id
        assert restored.current_price == pred.current_price

    def test_postmortem_to_dict_roundtrip(self):
        pm = PostMortemRecord(
            postmortem_id="pm_1",
            thesis_id="th_1",
            entity_id="entity_1",
            review_date="2025-07-15",
            price_at_thesis=100.0,
            price_at_review=120.0,
            total_return=0.20,
            thesis_survived=True,
            variant_realized=False,
            edge_realized=True,
            original_confidence_bucket="medium",
            error_labels=["test_error"],
        )
        d = pm.to_dict()
        restored = PostMortemRecord.from_dict(d)
        assert restored.postmortem_id == pm.postmortem_id
        assert restored.error_labels == ["test_error"]
