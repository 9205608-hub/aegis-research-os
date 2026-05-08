"""Backtesting Framework + PostMortem Tracker — Section 26.3.

Section 26.4:
1. Every published thesis must post-mortem after horizon expires.
2. Post-mortem results feed back to Memory Layer and Confidence Policy.
3. Edge type hit rate must be tracked periodically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from aegis.data_contracts.postmortem_schema import PostMortem


@dataclass
class CalibrationBucket:
    """Tracks outcomes for a specific confidence bucket."""

    bucket: str
    total: int = 0
    survived: int = 0
    variant_realized: int = 0
    edge_realized: int = 0

    @property
    def survival_rate(self) -> float:
        return self.survived / max(self.total, 1)

    @property
    def variant_rate(self) -> float:
        return self.variant_realized / max(self.total, 1)

    @property
    def edge_rate(self) -> float:
        return self.edge_realized / max(self.total, 1)


@dataclass
class EdgeHitRate:
    """Hit rate tracking for a specific edge type."""

    edge_type: str
    total: int = 0
    realized: int = 0

    @property
    def hit_rate(self) -> float:
        return self.realized / max(self.total, 1)


@dataclass
class CalibrationReport:
    """Quarterly calibration report."""

    report_date: date
    bucket_stats: dict[str, CalibrationBucket] = field(default_factory=dict)
    edge_stats: dict[str, EdgeHitRate] = field(default_factory=dict)
    error_frequency: dict[str, int] = field(default_factory=dict)
    total_postmortems: int = 0

    @property
    def is_calibrated(self) -> bool:
        return all(b.total >= 20 for b in self.bucket_stats.values())

    @property
    def bucket_precision_summary(self) -> dict[str, float]:
        return {k: v.survival_rate for k, v in self.bucket_stats.items()}


class BacktestingFramework:
    """Backtesting and calibration framework."""

    def __init__(self) -> None:
        self._postmortems: list[PostMortem] = []
        self._bucket_data: dict[str, CalibrationBucket] = {}
        self._edge_data: dict[str, EdgeHitRate] = {}
        self._error_counts: dict[str, int] = {}

    def record_postmortem(self, pm: PostMortem) -> None:
        self._postmortems.append(pm)

        bucket = pm.original_confidence_bucket.value
        if bucket not in self._bucket_data:
            self._bucket_data[bucket] = CalibrationBucket(bucket=bucket)

        cb = self._bucket_data[bucket]
        cb.total += 1
        if pm.thesis_survived:
            cb.survived += 1
        if pm.variant_realized:
            cb.variant_realized += 1
        if pm.edge_realized:
            cb.edge_realized += 1

        edge = pm.edge_type.value
        if edge not in self._edge_data:
            self._edge_data[edge] = EdgeHitRate(edge_type=edge)
        self._edge_data[edge].total += 1
        if pm.edge_realized:
            self._edge_data[edge].realized += 1

        for label in pm.error_taxonomy_labels:
            self._error_counts[label] = self._error_counts.get(label, 0) + 1

    def get_bucket_precision(self, bucket: str) -> float | None:
        cb = self._bucket_data.get(bucket)
        return cb.survival_rate if cb else None

    def get_edge_hit_rate(self, edge_type: str) -> float | None:
        eh = self._edge_data.get(edge_type)
        return eh.hit_rate if eh else None

    def generate_calibration_report(self) -> CalibrationReport:
        return CalibrationReport(
            report_date=date.today(),
            bucket_stats=dict(self._bucket_data),
            edge_stats=dict(self._edge_data),
            error_frequency=dict(self._error_counts),
            total_postmortems=len(self._postmortems),
        )

    def get_confidence_basis(self, bucket: str) -> str:
        cb = self._bucket_data.get(bucket)
        if not cb or cb.total < 20:
            return "not_calibrated"
        return f"calibrated_n{cb.total}_precision{cb.survival_rate:.0%}"

    def get_all_postmortems(self) -> list[PostMortem]:
        return list(self._postmortems)

    def get_error_ranking(self) -> list[tuple[str, int]]:
        return sorted(self._error_counts.items(), key=lambda x: x[1], reverse=True)
