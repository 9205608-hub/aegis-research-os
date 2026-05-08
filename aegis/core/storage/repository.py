"""Research Repository — high-level data access for the research pipeline.

Wraps SQLAlchemy session operations with domain-specific methods.
All writes and reads go through this repository.

Usage:
    from aegis.core.storage import ResearchRepository
    repo = ResearchRepository("sqlite:///aegis_research.db")

    # Save a research run
    repo.save_research_run(result)

    # Query
    runs = repo.get_runs_for_entity("meta_platforms")
    latest = repo.get_latest_run("META")
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from .models import (
    Base,
    ConsensusSnapshotRow,
    PostMortemRow,
    PortfolioSignalRow,
    PredictionRow,
    ResearchRunRow,
    init_db,
)


class ResearchRepository:
    """High-level repository for research pipeline data."""

    def __init__(self, db_url: str = "sqlite:///aegis_research.db") -> None:
        self._session_factory = init_db(db_url)

    def _session(self) -> Session:
        return self._session_factory()

    # ── Research Runs ────────────────────────────────────────────────

    def save_research_run(self, result: Any) -> ResearchRunRow:
        """Save an AutoResearchOrchestrator result to the database."""
        row = ResearchRunRow(
            run_id=result.run_id,
            ticker=result.ticker,
            entity_id=result.entity_id,
            period="FY2024",  # Default
            status="completed",
            meta_facts_json=_safe_json(result.meta_facts),
            computed_metrics_json=_safe_json(result.computed_metrics),
            dcf_per_share=result.dcf_per_share,
            scenarios_json=_safe_json(result.scenarios),
            implied_growth=result.implied_growth,
            publishing_status=getattr(result.decision, "publishing_status", None),
            confidence_bucket=getattr(result.decision, "confidence_bucket", None),
            direction=getattr(result.signal, "direction", None),
            conviction=getattr(result.signal, "conviction", None),
            agent_count=7,
            critic_count=7,
            gate_passed=getattr(result.decision, "publishable", None),
            html_report_path=result.html_path,
            pipeline_log_json=result.pipeline_log,
            completed_at=datetime.now(timezone.utc),
        )

        with self._session() as session:
            # Upsert
            existing = session.query(ResearchRunRow).filter_by(run_id=result.run_id).first()
            if existing:
                for col in ResearchRunRow.__table__.columns:
                    if col.name != "id":
                        setattr(existing, col.name, getattr(row, col.name))
            else:
                session.add(row)
            session.commit()
            return row

    def get_run(self, run_id: str) -> ResearchRunRow | None:
        with self._session() as session:
            return session.query(ResearchRunRow).filter_by(run_id=run_id).first()

    def get_runs_for_entity(self, entity_id: str, limit: int = 20) -> list[ResearchRunRow]:
        with self._session() as session:
            return (
                session.query(ResearchRunRow)
                .filter_by(entity_id=entity_id)
                .order_by(ResearchRunRow.created_at.desc())
                .limit(limit)
                .all()
            )

    def get_latest_run(self, ticker: str) -> ResearchRunRow | None:
        with self._session() as session:
            return (
                session.query(ResearchRunRow)
                .filter_by(ticker=ticker)
                .order_by(ResearchRunRow.created_at.desc())
                .first()
            )

    def get_all_runs(self, limit: int = 50) -> list[ResearchRunRow]:
        with self._session() as session:
            return (
                session.query(ResearchRunRow)
                .order_by(ResearchRunRow.created_at.desc())
                .limit(limit)
                .all()
            )

    # ── Portfolio Signals ────────────────────────────────────────────

    def save_signal(self, signal: Any, entity_id: str, run_id: str) -> PortfolioSignalRow:
        row = PortfolioSignalRow(
            entity_id=entity_id,
            run_id=run_id,
            direction=getattr(signal, "direction", "no_signal"),
            conviction=getattr(signal, "conviction", "very_low"),
            sizing_tier=getattr(signal, "sizing_tier", None),
            data_quality_tier=getattr(signal, "data_quality_tier", None),
            thesis_horizon=getattr(signal, "thesis_horizon", None),
            expected_value_per_share=getattr(signal, "expected_value_per_share", None),
            risk_reward_ratio=getattr(signal, "risk_reward_ratio", None),
            review_date=str(getattr(signal, "review_date", "")) or None,
        )

        with self._session() as session:
            session.add(row)
            session.commit()
            return row

    def get_signals(self, entity_id: str | None = None) -> list[PortfolioSignalRow]:
        with self._session() as session:
            q = session.query(PortfolioSignalRow)
            if entity_id:
                q = q.filter_by(entity_id=entity_id)
            return q.order_by(PortfolioSignalRow.created_at.desc()).all()

    # ── Predictions ──────────────────────────────────────────────────

    def save_prediction(self, pred: Any) -> PredictionRow:
        row = PredictionRow(
            thesis_id=pred.thesis_id,
            entity_id=pred.entity_id,
            run_id=pred.run_id,
            publish_date=pred.publish_date,
            confidence_bucket=pred.confidence_bucket,
            bear_value=pred.bear_value,
            base_value=pred.base_value,
            bull_value=pred.bull_value,
            current_price=pred.current_price,
            implied_growth=pred.implied_growth,
            edge_type=pred.edge_type,
            direction=pred.direction,
            conviction=pred.conviction,
            review_date=pred.review_date,
            status=pred.status,
        )
        with self._session() as session:
            session.add(row)
            session.commit()
            return row

    def get_active_predictions(self) -> list[PredictionRow]:
        with self._session() as session:
            return (
                session.query(PredictionRow)
                .filter_by(status="active")
                .order_by(PredictionRow.created_at.desc())
                .all()
            )

    # ── Post-Mortems ─────────────────────────────────────────────────

    def save_postmortem(self, pm: Any) -> PostMortemRow:
        row = PostMortemRow(
            postmortem_id=pm.postmortem_id,
            thesis_id=pm.thesis_id,
            entity_id=pm.entity_id,
            review_date=pm.review_date,
            price_at_thesis=pm.price_at_thesis,
            price_at_review=pm.price_at_review,
            total_return=pm.total_return,
            thesis_survived=pm.thesis_survived,
            variant_realized=pm.variant_realized,
            edge_realized=pm.edge_realized,
            original_confidence_bucket=pm.original_confidence_bucket,
            error_labels_json=pm.error_labels,
            lessons_json=pm.lessons,
        )
        with self._session() as session:
            session.add(row)
            session.commit()
            return row

    # ── Consensus Snapshots ─────────────────────────────────────────

    def save_consensus_snapshot(
        self,
        snapshot_id: str,
        entity_id: str,
        snapshot_timestamp: datetime,
        metric_id: str,
        period: str,
        consensus_mean: float,
        *,
        period_type: str = "annual",
        consensus_median: float | None = None,
        estimate_count: int = 0,
        high_estimate: float | None = None,
        low_estimate: float | None = None,
        standard_deviation: float | None = None,
        revision_1w: float | None = None,
        revision_1m: float | None = None,
        revision_3m: float | None = None,
        revision_6m: float | None = None,
        source: str = "yfinance",
        run_id: str | None = None,
    ) -> ConsensusSnapshotRow:
        """Persist a consensus snapshot. Upserts on snapshot_id."""
        row = ConsensusSnapshotRow(
            snapshot_id=snapshot_id,
            entity_id=entity_id,
            snapshot_timestamp=snapshot_timestamp,
            metric_id=metric_id,
            period=period,
            period_type=period_type,
            consensus_mean=consensus_mean,
            consensus_median=consensus_median,
            estimate_count=estimate_count,
            high_estimate=high_estimate,
            low_estimate=low_estimate,
            standard_deviation=standard_deviation,
            revision_1w=revision_1w,
            revision_1m=revision_1m,
            revision_3m=revision_3m,
            revision_6m=revision_6m,
            source=source,
            run_id=run_id,
        )
        with self._session() as session:
            existing = session.query(ConsensusSnapshotRow).filter_by(
                snapshot_id=snapshot_id,
            ).first()
            if existing:
                for col in ConsensusSnapshotRow.__table__.columns:
                    if col.name != "id":
                        setattr(existing, col.name, getattr(row, col.name))
            else:
                session.add(row)
            session.commit()
            return row

    def save_consensus_batch(
        self,
        snapshots: list[dict[str, Any]],
    ) -> int:
        """Save multiple consensus snapshots in a single transaction.

        Each dict must contain at minimum: snapshot_id, entity_id,
        snapshot_timestamp, metric_id, period, consensus_mean.
        Returns count saved.
        """
        count = 0
        with self._session() as session:
            for snap in snapshots:
                sid = snap["snapshot_id"]
                existing = session.query(ConsensusSnapshotRow).filter_by(
                    snapshot_id=sid,
                ).first()
                if existing:
                    for k, v in snap.items():
                        if k != "id" and hasattr(existing, k):
                            setattr(existing, k, v)
                else:
                    session.add(ConsensusSnapshotRow(**snap))
                count += 1
            session.commit()
        return count

    def get_consensus_for_entity(
        self,
        entity_id: str,
        metric_id: str | None = None,
        period: str | None = None,
        limit: int = 50,
    ) -> list[ConsensusSnapshotRow]:
        """Retrieve consensus snapshots for an entity, newest first."""
        with self._session() as session:
            q = session.query(ConsensusSnapshotRow).filter_by(entity_id=entity_id)
            if metric_id:
                q = q.filter_by(metric_id=metric_id)
            if period:
                q = q.filter_by(period=period)
            return q.order_by(ConsensusSnapshotRow.snapshot_timestamp.desc()).limit(limit).all()

    def get_latest_consensus(
        self, entity_id: str, metric_id: str, period: str,
    ) -> ConsensusSnapshotRow | None:
        """Get the most recent consensus snapshot for a specific metric+period."""
        with self._session() as session:
            return (
                session.query(ConsensusSnapshotRow)
                .filter_by(entity_id=entity_id, metric_id=metric_id, period=period)
                .order_by(ConsensusSnapshotRow.snapshot_timestamp.desc())
                .first()
            )

    def get_consensus_for_run(self, run_id: str) -> list[ConsensusSnapshotRow]:
        """Get all consensus snapshots linked to a research run."""
        with self._session() as session:
            return (
                session.query(ConsensusSnapshotRow)
                .filter_by(run_id=run_id)
                .order_by(ConsensusSnapshotRow.metric_id, ConsensusSnapshotRow.period)
                .all()
            )

    # ── Stats ────────────────────────────────────────────────────────

    def get_stats(self) -> dict[str, int]:
        """Get summary statistics for the dashboard."""
        with self._session() as session:
            return {
                "total_runs": session.query(ResearchRunRow).count(),
                "completed_runs": session.query(ResearchRunRow).filter_by(status="completed").count(),
                "active_predictions": session.query(PredictionRow).filter_by(status="active").count(),
                "total_signals": session.query(PortfolioSignalRow).count(),
                "total_postmortems": session.query(PostMortemRow).count(),
                "consensus_snapshots": session.query(ConsensusSnapshotRow).count(),
            }


def _safe_json(obj: Any) -> Any:
    """Convert to JSON-safe representation."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return {k: _safe_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_safe_json(v) for v in obj]
    if isinstance(obj, (int, float, str, bool)):
        return obj
    return str(obj)
