"""Tests for multi-entity comparison pipeline (P1-4)."""

import pytest

from aegis.core.agents.comparative_analyst.agent import (
    ComparativeAnalyst,
    ComparativeInput,
)
from aegis.core.critics.cross_entity_critic.critic import CrossEntityCritic
from aegis.core.reports import ReportSerializer


@pytest.fixture
def three_entity_metrics():
    return {
        "meta_platforms": {
            "gross_margin": 0.83,
            "operating_margin": 0.42,
            "fcf_margin": 0.32,
            "roic": 0.40,
            "ev_to_revenue": 9.0,
            "sbc_to_revenue": 0.12,
        },
        "googl": {
            "gross_margin": 0.58,
            "operating_margin": 0.32,
            "fcf_margin": 0.17,
            "roic": 0.38,
            "ev_to_revenue": 6.3,
            "sbc_to_revenue": 0.07,
        },
        "snap": {
            "gross_margin": 0.53,
            "operating_margin": -0.14,
            "fcf_margin": 0.01,
            "roic": -0.15,
            "ev_to_revenue": 4.4,
            "sbc_to_revenue": 0.29,
        },
    }


class TestComparativeAnalystPipeline:

    def test_three_entity_comparison(self, three_entity_metrics):
        inp = ComparativeInput(
            entity_ids=["meta_platforms", "googl", "snap"],
            run_id="run_test",
            theme="Ad Platform",
            per_entity_metrics=three_entity_metrics,
            comparison_dimensions=["gross_margin", "operating_margin", "fcf_margin", "roic"],
            valuation_metric="ev_to_revenue",
        )

        analyst = ComparativeAnalyst()
        result = analyst.analyze(inp)

        assert len(result.entity_ids) == 3
        assert len(result.dimensions) == 4

        # META should rank #1 in most dimensions
        for dim in result.dimensions:
            assert dim.rankings["meta_platforms"] <= 2  # Top 2 in all

        # Top picks should include META
        assert "meta_platforms" in result.top_picks
        assert len(result.top_picks) <= 2

    def test_relative_valuation(self, three_entity_metrics):
        inp = ComparativeInput(
            entity_ids=["meta_platforms", "googl", "snap"],
            run_id="run_test",
            per_entity_metrics=three_entity_metrics,
            valuation_metric="ev_to_revenue",
        )
        result = ComparativeAnalyst().analyze(inp)

        assert result.relative_valuation.metric == "ev_to_revenue"
        assert result.relative_valuation.sector_median > 0
        assert len(result.relative_valuation.values) == 3

    def test_comparison_table_serialization(self, three_entity_metrics):
        inp = ComparativeInput(
            entity_ids=["meta_platforms", "googl"],
            run_id="run_test",
            per_entity_metrics=three_entity_metrics,
        )
        result = ComparativeAnalyst().analyze(inp)

        serializer = ReportSerializer()
        table = serializer.comparison_table(result)

        assert table["report_type"] == "sector_comparison_table"
        assert len(table["entities"]) == 2
        assert len(table["top_picks"]) >= 1

    def test_cross_entity_critic_same_standard(self, three_entity_metrics):
        """Same accounting standard → no cross-standard block."""
        # Use empty judgments — critic checks context, not judgments
        critic = CrossEntityCritic()
        result = critic.review([], context={
            "entity_standards": {
                "meta_platforms": "US_GAAP",
                "googl": "US_GAAP",
                "snap": "US_GAAP",
            },
            "entity_currencies": {
                "meta_platforms": "USD",
                "googl": "USD",
                "snap": "USD",
            },
        })
        # Same standard + currency → should pass
        assert not result.block_publish

    def test_two_entity_pair(self, three_entity_metrics):
        """Pair comparison (exactly 2 entities)."""
        inp = ComparativeInput(
            entity_ids=["meta_platforms", "googl"],
            run_id="run_test",
            theme="Pair Trade",
            per_entity_metrics=three_entity_metrics,
            comparison_dimensions=["operating_margin", "roic"],
        )
        result = ComparativeAnalyst().analyze(inp)

        assert len(result.entity_ids) == 2
        assert len(result.top_picks) >= 1
        assert result.top_pick_rationale != ""
