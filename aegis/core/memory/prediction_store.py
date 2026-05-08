"""Prediction Store — persistent storage for thesis predictions and post-mortems.

Provides JSON file-based persistence for:
  1. Thesis snapshots at publish time (predictions)
  2. Post-mortem evaluations after horizon expiry
  3. Calibration data (bucket precision, edge hit rates)

In production, this would be backed by PostgreSQL.
For now, uses a single JSON file per entity + a global index.

Section 26.4: Every published thesis must post-mortem after horizon expires.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class PredictionRecord:
    """A snapshot of a thesis prediction at publish time."""

    thesis_id: str
    entity_id: str
    run_id: str
    publish_date: str  # ISO format
    confidence_bucket: str
    bear_value: float
    base_value: float
    bull_value: float
    current_price: float
    implied_growth: float
    edge_type: str
    direction: str  # "long", "short", "no_signal"
    conviction: str
    review_date: str | None = None  # When to review
    status: str = "active"  # "active", "reviewed", "killed"

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}

    @classmethod
    def from_dict(cls, d: dict) -> "PredictionRecord":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class PostMortemRecord:
    """A post-mortem evaluation record."""

    postmortem_id: str
    thesis_id: str
    entity_id: str
    review_date: str
    price_at_thesis: float
    price_at_review: float
    total_return: float
    thesis_survived: bool
    variant_realized: bool
    edge_realized: bool
    original_confidence_bucket: str
    error_labels: list[str] = field(default_factory=list)
    lessons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}

    @classmethod
    def from_dict(cls, d: dict) -> "PostMortemRecord":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class PredictionStore:
    """File-based persistent storage for predictions and post-mortems.

    Storage layout:
      {store_dir}/
        predictions.json   — all prediction records
        postmortems.json   — all post-mortem records
        calibration.json   — latest calibration snapshot

    Usage:
        store = PredictionStore(Path("data/predictions"))
        store.record_prediction(prediction_record)
        store.record_postmortem(postmortem_record)
        report = store.get_calibration_snapshot()
    """

    def __init__(self, store_dir: Path | str | None = None) -> None:
        self._dir = Path(store_dir) if store_dir else Path("data/predictions")
        self._dir.mkdir(parents=True, exist_ok=True)
        self._predictions_file = self._dir / "predictions.json"
        self._postmortems_file = self._dir / "postmortems.json"
        self._calibration_file = self._dir / "calibration.json"

    # ── Predictions ──────────────────────────────────────────────────

    def record_prediction(self, record: PredictionRecord) -> None:
        """Save a prediction record."""
        records = self._load_predictions()
        # Update existing or append
        existing_idx = next(
            (i for i, r in enumerate(records) if r["thesis_id"] == record.thesis_id),
            None,
        )
        if existing_idx is not None:
            records[existing_idx] = record.to_dict()
        else:
            records.append(record.to_dict())
        self._save_json(self._predictions_file, records)

    def get_prediction(self, thesis_id: str) -> PredictionRecord | None:
        """Get a specific prediction by thesis_id."""
        records = self._load_predictions()
        for r in records:
            if r["thesis_id"] == thesis_id:
                return PredictionRecord.from_dict(r)
        return None

    def get_predictions_for_entity(self, entity_id: str) -> list[PredictionRecord]:
        """Get all predictions for an entity."""
        records = self._load_predictions()
        return [PredictionRecord.from_dict(r) for r in records if r["entity_id"] == entity_id]

    def get_active_predictions(self) -> list[PredictionRecord]:
        """Get all predictions with status='active'."""
        records = self._load_predictions()
        return [PredictionRecord.from_dict(r) for r in records if r.get("status") == "active"]

    def get_due_for_review(self, as_of: date | None = None) -> list[PredictionRecord]:
        """Get predictions past their review date."""
        check_date = (as_of or date.today()).isoformat()
        records = self._load_predictions()
        due = []
        for r in records:
            if r.get("status") == "active" and r.get("review_date") and r["review_date"] <= check_date:
                due.append(PredictionRecord.from_dict(r))
        return due

    def mark_reviewed(self, thesis_id: str) -> None:
        """Mark a prediction as reviewed."""
        records = self._load_predictions()
        for r in records:
            if r["thesis_id"] == thesis_id:
                r["status"] = "reviewed"
        self._save_json(self._predictions_file, records)

    # ── Post-Mortems ─────────────────────────────────────────────────

    def record_postmortem(self, record: PostMortemRecord) -> None:
        """Save a post-mortem record."""
        records = self._load_postmortems()
        records.append(record.to_dict())
        self._save_json(self._postmortems_file, records)
        # Update prediction status
        self.mark_reviewed(record.thesis_id)
        # Update calibration
        self._update_calibration(record)

    def get_postmortems(self, entity_id: str | None = None) -> list[PostMortemRecord]:
        """Get post-mortems, optionally filtered by entity."""
        records = self._load_postmortems()
        if entity_id:
            records = [r for r in records if r["entity_id"] == entity_id]
        return [PostMortemRecord.from_dict(r) for r in records]

    # ── Calibration ──────────────────────────────────────────────────

    def get_calibration_snapshot(self) -> dict:
        """Get the latest calibration data."""
        return self._load_json(self._calibration_file) or {
            "bucket_stats": {},
            "total_postmortems": 0,
            "last_updated": None,
        }

    def _update_calibration(self, pm: PostMortemRecord) -> None:
        """Update calibration data with a new post-mortem."""
        cal = self.get_calibration_snapshot()

        bucket = pm.original_confidence_bucket
        if bucket not in cal["bucket_stats"]:
            cal["bucket_stats"][bucket] = {
                "total": 0, "survived": 0,
                "variant_realized": 0, "edge_realized": 0,
            }

        stats = cal["bucket_stats"][bucket]
        stats["total"] += 1
        if pm.thesis_survived:
            stats["survived"] += 1
        if pm.variant_realized:
            stats["variant_realized"] += 1
        if pm.edge_realized:
            stats["edge_realized"] += 1

        cal["total_postmortems"] = cal.get("total_postmortems", 0) + 1
        cal["last_updated"] = datetime.now(timezone.utc).isoformat()

        self._save_json(self._calibration_file, cal)

    def get_bucket_precision(self, bucket: str) -> float | None:
        """Get survival rate for a confidence bucket."""
        cal = self.get_calibration_snapshot()
        stats = cal.get("bucket_stats", {}).get(bucket)
        if stats and stats["total"] > 0:
            return stats["survived"] / stats["total"]
        return None

    # ── Storage helpers ──────────────────────────────────────────────

    def _load_predictions(self) -> list[dict]:
        return self._load_json(self._predictions_file) or []

    def _load_postmortems(self) -> list[dict]:
        return self._load_json(self._postmortems_file) or []

    def _load_json(self, path: Path) -> Any:
        if path.exists():
            with open(path) as f:
                return json.load(f)
        return None

    def _save_json(self, path: Path, data: Any) -> None:
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)
