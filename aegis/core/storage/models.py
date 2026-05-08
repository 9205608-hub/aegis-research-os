"""SQLAlchemy ORM Models — persistent storage for research pipeline data.

Uses SQLite for development, PostgreSQL for production.
Schema mirrors the data contracts in aegis/data_contracts/.

Tables:
  - research_runs: Research run metadata and status
  - predictions: Thesis predictions at publish time
  - postmortems: Post-mortem evaluations
  - portfolio_signals: Generated portfolio signals
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    """SQLAlchemy declarative base for all ORM models."""
    pass


class ResearchRunRow(Base):
    """A single research run — from initiation to report generation."""

    __tablename__ = "research_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String(64), unique=True, nullable=False, index=True)
    ticker = Column(String(10), nullable=False, index=True)
    entity_id = Column(String(64), nullable=False, index=True)
    period = Column(String(10), nullable=False)  # "FY2024"
    status = Column(String(20), nullable=False, default="pending")  # pending/running/completed/failed
    research_mode = Column(String(20), default="single_entity")

    # Config
    current_price = Column(Float, nullable=True)
    market_cap = Column(Float, nullable=True)
    wacc = Column(Float, default=0.095)
    terminal_growth_rate = Column(Float, default=0.03)
    sector_pack_id = Column(String(64), nullable=True)

    # Results (stored as JSON)
    meta_facts_json = Column(JSON, nullable=True)
    computed_metrics_json = Column(JSON, nullable=True)
    dcf_per_share = Column(Float, nullable=True)
    scenarios_json = Column(JSON, nullable=True)  # {"bear_value": ..., "base_value": ..., "bull_value": ...}
    implied_growth = Column(Float, nullable=True)

    # Decision
    publishing_status = Column(String(20), nullable=True)
    confidence_bucket = Column(String(20), nullable=True)
    direction = Column(String(20), nullable=True)
    conviction = Column(String(20), nullable=True)

    # Pipeline metadata
    agent_count = Column(Integer, nullable=True)
    critic_count = Column(Integer, nullable=True)
    gate_passed = Column(Boolean, nullable=True)
    html_report_path = Column(Text, nullable=True)
    pipeline_log_json = Column(JSON, nullable=True)

    # Timestamps
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    completed_at = Column(DateTime, nullable=True)
    error_message = Column(Text, nullable=True)


class PredictionRow(Base):
    """A thesis prediction snapshot at publish time."""

    __tablename__ = "predictions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    thesis_id = Column(String(128), unique=True, nullable=False, index=True)
    entity_id = Column(String(64), nullable=False, index=True)
    run_id = Column(String(64), nullable=False)
    publish_date = Column(String(10), nullable=False)  # ISO date
    confidence_bucket = Column(String(20), nullable=False)
    bear_value = Column(Float, nullable=False)
    base_value = Column(Float, nullable=False)
    bull_value = Column(Float, nullable=False)
    current_price = Column(Float, nullable=False)
    implied_growth = Column(Float, default=0.0)
    edge_type = Column(String(20), default="analytical")
    direction = Column(String(20), default="no_signal")
    conviction = Column(String(20), default="very_low")
    review_date = Column(String(10), nullable=True)
    status = Column(String(20), default="active")  # active/reviewed/killed
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class PostMortemRow(Base):
    """A post-mortem evaluation after thesis horizon expires."""

    __tablename__ = "postmortems"

    id = Column(Integer, primary_key=True, autoincrement=True)
    postmortem_id = Column(String(64), unique=True, nullable=False)
    thesis_id = Column(String(128), nullable=False, index=True)
    entity_id = Column(String(64), nullable=False, index=True)
    review_date = Column(String(10), nullable=False)
    price_at_thesis = Column(Float, nullable=False)
    price_at_review = Column(Float, nullable=False)
    total_return = Column(Float, nullable=False)
    thesis_survived = Column(Boolean, nullable=False)
    variant_realized = Column(Boolean, default=False)
    edge_realized = Column(Boolean, default=False)
    original_confidence_bucket = Column(String(20), nullable=False)
    error_labels_json = Column(JSON, default=list)
    lessons_json = Column(JSON, default=list)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class ConsensusSnapshotRow(Base):
    """Point-in-time consensus snapshot persisted for revision tracking."""

    __tablename__ = "consensus_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    snapshot_id = Column(String(128), unique=True, nullable=False, index=True)
    entity_id = Column(String(64), nullable=False, index=True)
    snapshot_timestamp = Column(DateTime, nullable=False)
    metric_id = Column(String(64), nullable=False)  # "revenue", "eps", "ebitda"
    period = Column(String(20), nullable=False)  # "FY_Current", "FY_Next"
    period_type = Column(String(20), nullable=False, default="annual")
    consensus_mean = Column(Float, nullable=False)
    consensus_median = Column(Float, nullable=True)
    estimate_count = Column(Integer, default=0)
    high_estimate = Column(Float, nullable=True)
    low_estimate = Column(Float, nullable=True)
    standard_deviation = Column(Float, nullable=True)
    revision_1w = Column(Float, nullable=True)
    revision_1m = Column(Float, nullable=True)
    revision_3m = Column(Float, nullable=True)
    revision_6m = Column(Float, nullable=True)
    source = Column(String(64), nullable=False, default="yfinance")
    run_id = Column(String(64), nullable=True, index=True)  # link to research run
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class PortfolioSignalRow(Base):
    """A portfolio signal generated from a research run."""

    __tablename__ = "portfolio_signals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    entity_id = Column(String(64), nullable=False, index=True)
    run_id = Column(String(64), nullable=False)
    direction = Column(String(20), nullable=False)
    conviction = Column(String(20), nullable=False)
    sizing_tier = Column(String(20), nullable=True)
    data_quality_tier = Column(String(10), nullable=True)
    thesis_horizon = Column(String(20), nullable=True)
    expected_value_per_share = Column(Float, nullable=True)
    risk_reward_ratio = Column(Float, nullable=True)
    review_date = Column(String(10), nullable=True)
    kill_criteria_json = Column(JSON, default=list)
    monitorables_json = Column(JSON, default=list)
    catalysts_json = Column(JSON, default=list)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


def create_db_engine(db_url: str = "sqlite:///aegis_research.db"):
    """Create a database engine. Default: SQLite for development."""
    return create_engine(db_url, echo=False)


def init_db(db_url: str = "sqlite:///aegis_research.db") -> sessionmaker:
    """Initialize the database and return a session factory."""
    engine = create_db_engine(db_url)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)
