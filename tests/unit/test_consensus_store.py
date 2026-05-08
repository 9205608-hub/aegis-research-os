"""Tests for ConsensusStore persistence and MarketExpectationsLayer integration.

Round 19 — ConsensusStore 正式填充 + VariantAnalyst revision momentum deepening.
"""

import pytest
from datetime import datetime, timezone

from aegis.core.storage.models import (
    Base,
    ConsensusSnapshotRow,
    create_db_engine,
)
from aegis.core.storage.repository import ResearchRepository
from aegis.core.market_expectations.expectations import (
    ConsensusRevisionSignal,
    MarketExpectationsLayer,
)
from aegis.data_contracts.consensus_snapshot_schema import ConsensusSnapshot
from aegis.core.agents.variant_analyst.agent import VariantAnalyst
from aegis.core.agents.base import AgentInput


# ============================================================
# ConsensusSnapshotRow ORM Tests
# ============================================================

class TestConsensusSnapshotRow:
    """ORM model for consensus snapshot persistence."""

    def test_row_creation(self):
        row = ConsensusSnapshotRow(
            snapshot_id="cs_meta_rev_FY_Current",
            entity_id="meta_platforms",
            snapshot_timestamp=datetime(2026, 4, 13, tzinfo=timezone.utc),
            metric_id="revenue",
            period="FY_Current",
            consensus_mean=165_000_000_000.0,
            source="yfinance",
        )
        assert row.snapshot_id == "cs_meta_rev_FY_Current"
        assert row.entity_id == "meta_platforms"
        assert row.metric_id == "revenue"
        assert row.consensus_mean == 165_000_000_000.0

    def test_row_optional_fields(self):
        row = ConsensusSnapshotRow(
            snapshot_id="cs_test",
            entity_id="test",
            snapshot_timestamp=datetime.now(timezone.utc),
            metric_id="eps",
            period="FY_Next",
            period_type="annual",
            consensus_mean=5.0,
            source="fmp",
        )
        assert row.period_type == "annual"
        assert row.revision_1w is None
        assert row.revision_3m is None
        assert row.high_estimate is None

    def test_row_with_revisions(self):
        row = ConsensusSnapshotRow(
            snapshot_id="cs_rev",
            entity_id="meta",
            snapshot_timestamp=datetime.now(timezone.utc),
            metric_id="eps",
            period="FY_Current",
            consensus_mean=22.5,
            revision_1w=0.02,
            revision_1m=0.05,
            revision_3m=0.08,
            revision_6m=0.12,
            source="yfinance",
        )
        assert row.revision_1w == 0.02
        assert row.revision_1m == 0.05
        assert row.revision_3m == 0.08
        assert row.revision_6m == 0.12


# ============================================================
# Repository Consensus Methods Tests
# ============================================================

class TestRepositoryConsensusMethods:
    """Tests for consensus save/query repository methods."""

    @pytest.fixture
    def repo(self, tmp_path):
        db_url = f"sqlite:///{tmp_path / 'test.db'}"
        return ResearchRepository(db_url)

    def test_save_and_retrieve(self, repo):
        repo.save_consensus_snapshot(
            snapshot_id="cs_1",
            entity_id="meta",
            snapshot_timestamp=datetime(2026, 4, 13, tzinfo=timezone.utc),
            metric_id="revenue",
            period="FY_Current",
            consensus_mean=165e9,
            source="yfinance",
            estimate_count=30,
        )
        rows = repo.get_consensus_for_entity("meta")
        assert len(rows) == 1
        assert rows[0].snapshot_id == "cs_1"
        assert rows[0].consensus_mean == 165e9
        assert rows[0].estimate_count == 30

    def test_save_consensus_batch(self, repo):
        batch = [
            {
                "snapshot_id": "cs_rev_1",
                "entity_id": "meta",
                "snapshot_timestamp": datetime(2026, 4, 13, tzinfo=timezone.utc),
                "metric_id": "revenue",
                "period": "FY_Current",
                "consensus_mean": 165e9,
                "source": "yfinance",
            },
            {
                "snapshot_id": "cs_eps_1",
                "entity_id": "meta",
                "snapshot_timestamp": datetime(2026, 4, 13, tzinfo=timezone.utc),
                "metric_id": "eps",
                "period": "FY_Current",
                "consensus_mean": 22.5,
                "source": "yfinance",
            },
        ]
        count = repo.save_consensus_batch(batch)
        assert count == 2
        rows = repo.get_consensus_for_entity("meta")
        assert len(rows) == 2

    def test_get_latest_consensus(self, repo):
        # Save two snapshots for same metric/period, different timestamps
        repo.save_consensus_snapshot(
            snapshot_id="cs_old",
            entity_id="meta",
            snapshot_timestamp=datetime(2026, 3, 1, tzinfo=timezone.utc),
            metric_id="revenue",
            period="FY_Current",
            consensus_mean=160e9,
            source="yfinance",
        )
        repo.save_consensus_snapshot(
            snapshot_id="cs_new",
            entity_id="meta",
            snapshot_timestamp=datetime(2026, 4, 13, tzinfo=timezone.utc),
            metric_id="revenue",
            period="FY_Current",
            consensus_mean=165e9,
            source="yfinance",
        )
        latest = repo.get_latest_consensus("meta", "revenue", "FY_Current")
        assert latest is not None
        assert latest.snapshot_id == "cs_new"
        assert latest.consensus_mean == 165e9

    def test_get_consensus_for_run(self, repo):
        repo.save_consensus_snapshot(
            snapshot_id="cs_run_1",
            entity_id="meta",
            snapshot_timestamp=datetime.now(timezone.utc),
            metric_id="revenue",
            period="FY_Current",
            consensus_mean=165e9,
            source="yfinance",
            run_id="run_abc123",
        )
        repo.save_consensus_snapshot(
            snapshot_id="cs_run_2",
            entity_id="meta",
            snapshot_timestamp=datetime.now(timezone.utc),
            metric_id="eps",
            period="FY_Current",
            consensus_mean=22.5,
            source="yfinance",
            run_id="run_abc123",
        )
        results = repo.get_consensus_for_run("run_abc123")
        assert len(results) == 2

    def test_upsert_consensus(self, repo):
        repo.save_consensus_snapshot(
            snapshot_id="cs_upsert",
            entity_id="meta",
            snapshot_timestamp=datetime.now(timezone.utc),
            metric_id="revenue",
            period="FY_Current",
            consensus_mean=160e9,
            source="yfinance",
        )
        # Update same snapshot_id
        repo.save_consensus_snapshot(
            snapshot_id="cs_upsert",
            entity_id="meta",
            snapshot_timestamp=datetime.now(timezone.utc),
            metric_id="revenue",
            period="FY_Current",
            consensus_mean=165e9,
            source="yfinance",
        )
        rows = repo.get_consensus_for_entity("meta")
        assert len(rows) == 1
        assert rows[0].consensus_mean == 165e9

    def test_filter_by_metric(self, repo):
        repo.save_consensus_snapshot(
            snapshot_id="cs_rev",
            entity_id="meta",
            snapshot_timestamp=datetime.now(timezone.utc),
            metric_id="revenue",
            period="FY_Current",
            consensus_mean=165e9,
            source="yfinance",
        )
        repo.save_consensus_snapshot(
            snapshot_id="cs_eps",
            entity_id="meta",
            snapshot_timestamp=datetime.now(timezone.utc),
            metric_id="eps",
            period="FY_Current",
            consensus_mean=22.5,
            source="yfinance",
        )
        rev_only = repo.get_consensus_for_entity("meta", metric_id="revenue")
        assert len(rev_only) == 1
        assert rev_only[0].metric_id == "revenue"

    def test_stats_include_consensus(self, repo):
        repo.save_consensus_snapshot(
            snapshot_id="cs_stat",
            entity_id="meta",
            snapshot_timestamp=datetime.now(timezone.utc),
            metric_id="revenue",
            period="FY_Current",
            consensus_mean=165e9,
            source="yfinance",
        )
        stats = repo.get_stats()
        assert stats["consensus_snapshots"] == 1


# ============================================================
# MarketExpectationsLayer — Ingestion & Revision Signals
# ============================================================

class _FakeConsensusEstimate:
    """Minimal stand-in for OpenBB ConsensusEstimate dataclass."""
    def __init__(self, metric, period, mean, high, low, count,
                 source="yfinance", period_type="annual", fetched_at="2026-04-13"):
        self.metric = metric
        self.period = period
        self.consensus_mean = mean
        self.consensus_high = high
        self.consensus_low = low
        self.analyst_count = count
        self.source = source
        self.period_type = period_type
        self.fetched_at = fetched_at


class TestMarketExpectationsIngestion:
    """Tests for consensus ingestion into MarketExpectationsLayer."""

    def test_ingest_consensus_estimates(self):
        me = MarketExpectationsLayer()
        estimates = [
            _FakeConsensusEstimate("revenue", "FY_Current", 165e9, 175e9, 155e9, 30),
            _FakeConsensusEstimate("eps", "FY_Current", 22.5, 25.0, 20.0, 28),
            _FakeConsensusEstimate("revenue", "FY_Next", 190e9, 200e9, 180e9, 25),
        ]
        count = me.ingest_consensus_estimates("meta", estimates)
        assert count == 3

        # Can retrieve ingested data
        rev = me.get_consensus_for_metric("meta", "revenue", "FY_Current")
        assert rev is not None
        assert rev.consensus_mean == 165e9

    def test_priced_in_with_ingested_consensus(self):
        me = MarketExpectationsLayer()
        me.set_current_price("meta", 580.0)
        estimates = [
            _FakeConsensusEstimate("revenue", "FY_forward", 165e9, 175e9, 155e9, 30),
            _FakeConsensusEstimate("eps", "FY_forward", 22.5, 25.0, 20.0, 28),
        ]
        me.ingest_consensus_estimates("meta", estimates)

        priced_in = me.build_priced_in_object("meta", implied_growth=0.15)
        assert priced_in.consensus_eps_fwd == 22.5
        assert priced_in.consensus_revenue_fwd == 165e9
        assert priced_in.pe_ratio_fwd == pytest.approx(580.0 / 22.5, rel=1e-3)


class TestRevisionSignalEnhanced:
    """Tests for enhanced revision signal with acceleration and breadth."""

    def _make_snapshot(self, metric, period, rev_1w=None, rev_1m=None, rev_3m=None):
        return ConsensusSnapshot(
            snapshot_id=f"cs_{metric}_{period}",
            entity_id="meta",
            snapshot_timestamp=datetime(2026, 4, 13, tzinfo=timezone.utc),
            metric_id=metric,
            definition_id=f"def_{metric}",
            period=period,
            period_type="annual",
            consensus_mean=100.0,
            consensus_median=100.0,
            estimate_count=30,
            high_estimate=110.0,
            low_estimate=90.0,
            standard_deviation=5.0,
            revision_1w=rev_1w,
            revision_1m=rev_1m,
            revision_3m=rev_3m,
            unit="USD",
            source="yfinance",
            source_tier=2,
            ingestion_batch_id="batch_test",
        )

    def test_acceleration_detected(self):
        me = MarketExpectationsLayer()
        # Weekly revision is much larger than monthly → accelerating
        me.add_consensus(self._make_snapshot("revenue", "FY_Current",
                                              rev_1w=0.03, rev_1m=0.04))
        sig = me.get_revision_signal("meta", "revenue", "FY_Current")
        assert sig is not None
        # 0.03 * 4 = 0.12 vs 0.04 * 1.2 = 0.048 → accelerating
        assert sig.acceleration == "accelerating"

    def test_deceleration_detected(self):
        me = MarketExpectationsLayer()
        # Weekly revision is tiny compared to monthly → decelerating
        me.add_consensus(self._make_snapshot("revenue", "FY_Current",
                                              rev_1w=0.005, rev_1m=0.06))
        sig = me.get_revision_signal("meta", "revenue", "FY_Current")
        assert sig is not None
        # 0.005 * 4 = 0.02 vs 0.06 * 0.5 = 0.03 → 0.02 < 0.03 → decelerating
        assert sig.acceleration == "decelerating"

    def test_stable_pace(self):
        me = MarketExpectationsLayer()
        me.add_consensus(self._make_snapshot("revenue", "FY_Current",
                                              rev_1w=0.01, rev_1m=0.04))
        sig = me.get_revision_signal("meta", "revenue", "FY_Current")
        assert sig is not None
        # 0.01 * 4 = 0.04 vs 0.04 * 1.2 = 0.048, 0.04 * 0.5 = 0.02
        # 0.04 is between 0.02 and 0.048 → stable
        assert sig.acceleration == "stable"

    def test_aggregate_broad_upgrade(self):
        me = MarketExpectationsLayer()
        # All metrics positive
        me.add_consensus(self._make_snapshot("revenue", "FY_Current",
                                              rev_1w=0.02, rev_1m=0.05, rev_3m=0.08))
        me.add_consensus(self._make_snapshot("eps", "FY_Current",
                                              rev_1w=0.01, rev_1m=0.03, rev_3m=0.06))
        me.add_consensus(self._make_snapshot("ebitda", "FY_Current",
                                              rev_1w=0.01, rev_1m=0.04, rev_3m=0.07))

        agg = me.get_aggregate_revision_signal("meta")
        assert agg is not None
        assert agg.breadth == "broad_upgrade"
        assert agg.momentum == "positive"

    def test_aggregate_mixed(self):
        me = MarketExpectationsLayer()
        # Revenue positive, EPS negative
        me.add_consensus(self._make_snapshot("revenue", "FY_Current",
                                              rev_1w=0.02, rev_1m=0.05, rev_3m=0.08))
        me.add_consensus(self._make_snapshot("eps", "FY_Current",
                                              rev_1w=-0.01, rev_1m=-0.03, rev_3m=-0.06))

        agg = me.get_aggregate_revision_signal("meta")
        assert agg is not None
        assert agg.breadth == "mixed"

    def test_aggregate_broad_downgrade(self):
        me = MarketExpectationsLayer()
        me.add_consensus(self._make_snapshot("revenue", "FY_Current",
                                              rev_1w=-0.02, rev_1m=-0.05, rev_3m=-0.08))
        me.add_consensus(self._make_snapshot("eps", "FY_Current",
                                              rev_1w=-0.01, rev_1m=-0.03, rev_3m=-0.06))
        me.add_consensus(self._make_snapshot("ebitda", "FY_Current",
                                              rev_1w=-0.01, rev_1m=-0.04, rev_3m=-0.07))

        agg = me.get_aggregate_revision_signal("meta")
        assert agg is not None
        assert agg.breadth == "broad_downgrade"
        assert agg.momentum == "negative"

    def test_no_data_returns_none(self):
        me = MarketExpectationsLayer()
        assert me.get_aggregate_revision_signal("nonexistent") is None


# ============================================================
# VariantAnalyst Deepening Tests
# ============================================================

class TestVariantAnalystRevisionMomentum:
    """Tests for VariantAnalyst's enhanced revision signal usage."""

    def _make_input(self, revision_signal=None, scenarios=None, price=520.0):
        priced_in = {
            "implied_revenue_growth": 0.15,
            "implied_terminal_growth": 0.03,
            "pe_ratio_fwd": 25.0,
            "revision_momentum": "neutral",
        }
        if revision_signal:
            priced_in["revision_signal"] = revision_signal
            priced_in["revision_momentum"] = revision_signal.get("momentum", "neutral")

        if scenarios is None:
            scenarios = {
                "bear_value": 400, "base_value": 600, "bull_value": 800,
            }

        return AgentInput(
            entity_id="meta_platforms",
            run_id="test_run",
            question_id="q_variant",
            facts={"revenue": 150e9},
            evidence_packets=[],
            prior_judgments=[],
            macro_context={
                "priced_in": priced_in,
                "scenarios": scenarios,
                "current_price": price,
                "sensitivity_rankings": [],
            },
        )

    def test_rich_revision_observation(self):
        inp = self._make_input(revision_signal={
            "momentum": "positive",
            "breadth": "broad_upgrade",
            "acceleration": "accelerating",
            "revision_1w_pct": 0.02,
            "revision_1m_pct": 0.05,
            "revision_3m_pct": 0.08,
        })
        va = VariantAnalyst()
        result = va.run(inp)
        j = result.judgment

        # Check revision observation is rich, not just "positive"
        rev_obs = [o for o in j.observations if "revision momentum" in o.text.lower()]
        assert len(rev_obs) >= 1
        obs_text = rev_obs[0].text
        assert "broad_upgrade" in obs_text
        assert "accelerating" in obs_text
        assert "1m revision" in obs_text

    def test_bullish_positive_momentum_favorable(self):
        """Bullish variant + positive momentum = FAVORABLE alignment."""
        inp = self._make_input(
            revision_signal={
                "momentum": "positive",
                "breadth": "broad_upgrade",
                "acceleration": "stable",
            },
            scenarios={"bear_value": 400, "base_value": 700, "bull_value": 900},
            price=520.0,  # base > price → bullish
        )
        va = VariantAnalyst()
        j = va.run(inp).judgment

        alignment_inf = [i for i in j.inferences if "momentum alignment" in i.text.lower()]
        assert len(alignment_inf) >= 1
        assert "FAVORABLE" in alignment_inf[0].text

    def test_bullish_negative_momentum_contrarian(self):
        """Bullish variant + negative momentum = CONTRARIAN."""
        inp = self._make_input(
            revision_signal={
                "momentum": "negative",
                "breadth": "broad_downgrade",
                "acceleration": "stable",
            },
            scenarios={"bear_value": 400, "base_value": 700, "bull_value": 900},
            price=520.0,
        )
        va = VariantAnalyst()
        j = va.run(inp).judgment

        alignment_inf = [i for i in j.inferences if "momentum alignment" in i.text.lower()]
        assert len(alignment_inf) >= 1
        assert "CONTRARIAN" in alignment_inf[0].text

    def test_bearish_positive_momentum_contrarian(self):
        """Bearish variant + positive momentum = CONTRARIAN."""
        inp = self._make_input(
            revision_signal={
                "momentum": "positive",
                "breadth": "broad_upgrade",
                "acceleration": "stable",
            },
            scenarios={"bear_value": 400, "base_value": 450, "bull_value": 600},
            price=520.0,  # base < price → bearish
        )
        va = VariantAnalyst()
        j = va.run(inp).judgment

        alignment_inf = [i for i in j.inferences if "momentum alignment" in i.text.lower()]
        assert len(alignment_inf) >= 1
        assert "CONTRARIAN" in alignment_inf[0].text

    def test_acceleration_noted_in_inference(self):
        """Acceleration should appear in the momentum alignment inference."""
        inp = self._make_input(
            revision_signal={
                "momentum": "positive",
                "breadth": "broad_upgrade",
                "acceleration": "accelerating",
            },
            scenarios={"bear_value": 400, "base_value": 700, "bull_value": 900},
            price=520.0,
        )
        va = VariantAnalyst()
        j = va.run(inp).judgment

        alignment_inf = [i for i in j.inferences if "momentum alignment" in i.text.lower()]
        assert any("ACCELERATING" in i.text for i in alignment_inf)

    def test_revision_reversal_kill_trigger(self):
        """VariantAnalyst should have a revision reversal disconfirming trigger."""
        inp = self._make_input()
        va = VariantAnalyst()
        j = va.run(inp).judgment

        trigger_texts = [t.text for t in j.disconfirming_triggers]
        assert any("revision momentum reverses" in t.lower() for t in trigger_texts)

    def test_fallback_to_simple_momentum_string(self):
        """Without revision_signal, falls back to simple momentum observation."""
        inp = self._make_input()  # No revision_signal
        inp.macro_context["priced_in"]["revision_momentum"] = "positive"
        va = VariantAnalyst()
        j = va.run(inp).judgment

        rev_obs = [o for o in j.observations if "revision momentum" in o.text.lower()]
        assert len(rev_obs) >= 1
        assert "positive" in rev_obs[0].text.lower()

    def test_flat_momentum_variant_depends_on_catalyst(self):
        """Flat momentum → variant depends on new info or catalyst."""
        inp = self._make_input(
            revision_signal={
                "momentum": "flat",
                "breadth": "mixed",
                "acceleration": "stable",
            },
            scenarios={"bear_value": 400, "base_value": 700, "bull_value": 900},
            price=520.0,
        )
        va = VariantAnalyst()
        j = va.run(inp).judgment

        alignment_inf = [i for i in j.inferences if "momentum" in i.text.lower() and "catalyst" in i.text.lower()]
        assert len(alignment_inf) >= 1

    def test_four_disconfirming_triggers(self):
        """Should now have 4 triggers (original 3 + revision reversal)."""
        inp = self._make_input()
        va = VariantAnalyst()
        j = va.run(inp).judgment
        assert len(j.disconfirming_triggers) == 4
