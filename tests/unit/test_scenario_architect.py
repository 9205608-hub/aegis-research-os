"""Tests for ScenarioArchitect — narrative-driven scenario construction."""

import pytest
from unittest.mock import MagicMock

from aegis.core.chief_analyst.scenario_architect import (
    ScenarioArchitect,
    ScenarioBlueprint,
    ScenarioCase,
)


def _mock_llm_response():
    """Return a realistic ScenarioArchitect LLM response."""
    return {
        "scenarios": [
            {
                "name": "bear",
                "probability": 0.25,
                "narrative": "Ad revenue growth slows to mid-single-digit as TikTok captures incremental share in short-form video. Reality Labs losses widen to $20B as hardware adoption stalls.",
                "key_driver": "Short-form video competitive intensity",
                "revenue_growth_delta": [-0.05, -0.04, -0.03, -0.02, -0.02, -0.01, -0.01, -0.01, 0.0, 0.0],
                "margin_delta": [-0.04, -0.03, -0.03, -0.02, -0.02, -0.01, -0.01, 0.0, 0.0, 0.0],
            },
            {
                "name": "base",
                "probability": 0.50,
                "narrative": "Reels monetization continues closing the gap with Feed, driving 12-15% ad revenue growth. Reality Labs losses stabilize at $15B as Quest ecosystem matures.",
                "key_driver": "Reels monetization trajectory",
                "revenue_growth_delta": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                "margin_delta": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            },
            {
                "name": "bull",
                "probability": 0.25,
                "narrative": "AI-powered ad targeting restores post-ATT efficiency, driving 20%+ CPM increases. WhatsApp Business and Threads emerge as meaningful new revenue streams.",
                "key_driver": "AI advertising efficiency breakthrough",
                "revenue_growth_delta": [0.04, 0.03, 0.03, 0.02, 0.02, 0.01, 0.01, 0.01, 0.0, 0.0],
                "margin_delta": [0.03, 0.02, 0.02, 0.02, 0.01, 0.01, 0.01, 0.0, 0.0, 0.0],
            },
        ],
        "key_disagreements": [
            "Market underestimates AI-driven ad targeting improvement post-ATT",
            "Consensus overweights Reality Labs losses relative to core ad business quality",
        ],
        "primary_swing_factor": "AI advertising efficiency timeline",
    }


class TestScenarioArchitectParsing:
    """Test ScenarioBlueprint parsing from LLM output."""

    def test_parse_valid_response(self):
        mock_llm = MagicMock()
        mock_llm.call_structured.return_value = _mock_llm_response()

        architect = ScenarioArchitect(llm_client=mock_llm)
        bp = architect.architect(
            entity_id="0001326801",
            entity_name="META",
            base_dcf_assumptions={"revenue_growth_path": [0.12] * 10, "operating_margin_path": [0.35] * 10},
            meta_facts={"revenue": 160_000_000_000},
            computed_metrics={"operating_margin": 0.35},
        )

        assert isinstance(bp, ScenarioBlueprint)
        assert len(bp.scenarios) == 3
        assert bp.primary_swing_factor == "AI advertising efficiency timeline"
        assert len(bp.key_disagreements) == 2

    def test_scenario_names(self):
        mock_llm = MagicMock()
        mock_llm.call_structured.return_value = _mock_llm_response()

        architect = ScenarioArchitect(llm_client=mock_llm)
        bp = architect.architect(
            entity_id="test", entity_name="TEST",
            base_dcf_assumptions={"revenue_growth_path": [0.10] * 10},
            meta_facts={}, computed_metrics={},
        )

        names = [s.name for s in bp.scenarios]
        assert "bear" in names
        assert "base" in names
        assert "bull" in names

    def test_get_case(self):
        mock_llm = MagicMock()
        mock_llm.call_structured.return_value = _mock_llm_response()

        architect = ScenarioArchitect(llm_client=mock_llm)
        bp = architect.architect(
            entity_id="test", entity_name="TEST",
            base_dcf_assumptions={}, meta_facts={}, computed_metrics={},
        )

        bear = bp.get_case("bear")
        assert bear is not None
        assert bear.name == "bear"
        assert bear.probability == 0.25
        assert len(bear.revenue_growth_delta) == 10
        assert bear.revenue_growth_delta[0] == -0.05

        assert bp.get_case("nonexistent") is None

    def test_base_deltas_forced_to_zero(self):
        """Even if LLM returns non-zero base deltas, they should be zeroed."""
        resp = _mock_llm_response()
        resp["scenarios"][1]["revenue_growth_delta"] = [0.01] * 10  # non-zero
        resp["scenarios"][1]["margin_delta"] = [0.02] * 10

        mock_llm = MagicMock()
        mock_llm.call_structured.return_value = resp

        architect = ScenarioArchitect(llm_client=mock_llm)
        bp = architect.architect(
            entity_id="test", entity_name="TEST",
            base_dcf_assumptions={}, meta_facts={}, computed_metrics={},
        )

        base = bp.get_case("base")
        assert all(d == 0.0 for d in base.revenue_growth_delta)
        assert all(d == 0.0 for d in base.margin_delta)


class TestProbabilityNormalization:
    """Test probability weight normalization."""

    def test_probabilities_sum_to_one(self):
        mock_llm = MagicMock()
        mock_llm.call_structured.return_value = _mock_llm_response()

        architect = ScenarioArchitect(llm_client=mock_llm)
        bp = architect.architect(
            entity_id="test", entity_name="TEST",
            base_dcf_assumptions={}, meta_facts={}, computed_metrics={},
        )

        total = sum(s.probability for s in bp.scenarios)
        assert abs(total - 1.0) < 0.01

    def test_probabilities_normalized_when_not_summing(self):
        resp = _mock_llm_response()
        # Make probabilities sum to 0.8 instead of 1.0
        resp["scenarios"][0]["probability"] = 0.20
        resp["scenarios"][1]["probability"] = 0.40
        resp["scenarios"][2]["probability"] = 0.20

        mock_llm = MagicMock()
        mock_llm.call_structured.return_value = resp

        architect = ScenarioArchitect(llm_client=mock_llm)
        bp = architect.architect(
            entity_id="test", entity_name="TEST",
            base_dcf_assumptions={}, meta_facts={}, computed_metrics={},
        )

        total = sum(s.probability for s in bp.scenarios)
        assert abs(total - 1.0) < 0.01


class TestScenarioDeltaApplication:
    """Test that scenario deltas integrate correctly with DCFInput."""

    def test_bear_deltas_reduce_growth(self):
        mock_llm = MagicMock()
        mock_llm.call_structured.return_value = _mock_llm_response()

        architect = ScenarioArchitect(llm_client=mock_llm)
        bp = architect.architect(
            entity_id="test", entity_name="TEST",
            base_dcf_assumptions={"revenue_growth_path": [0.12] * 10},
            meta_facts={}, computed_metrics={},
        )

        bear = bp.get_case("bear")
        base_growth = [0.12] * 10
        adjusted = [g + d for g, d in zip(base_growth, bear.revenue_growth_delta)]

        # Y1: 0.12 + (-0.05) = 0.07
        assert abs(adjusted[0] - 0.07) < 0.001
        # Y10: 0.12 + 0.0 = 0.12 (converges back)
        assert abs(adjusted[9] - 0.12) < 0.001

    def test_bull_deltas_increase_growth(self):
        mock_llm = MagicMock()
        mock_llm.call_structured.return_value = _mock_llm_response()

        architect = ScenarioArchitect(llm_client=mock_llm)
        bp = architect.architect(
            entity_id="test", entity_name="TEST",
            base_dcf_assumptions={"revenue_growth_path": [0.12] * 10},
            meta_facts={}, computed_metrics={},
        )

        bull = bp.get_case("bull")
        base_growth = [0.12] * 10
        adjusted = [g + d for g, d in zip(base_growth, bull.revenue_growth_delta)]

        # Y1: 0.12 + 0.04 = 0.16
        assert abs(adjusted[0] - 0.16) < 0.001


class TestScenarioNarratives:
    """Test narrative content is preserved."""

    def test_narratives_non_empty(self):
        mock_llm = MagicMock()
        mock_llm.call_structured.return_value = _mock_llm_response()

        architect = ScenarioArchitect(llm_client=mock_llm)
        bp = architect.architect(
            entity_id="test", entity_name="TEST",
            base_dcf_assumptions={}, meta_facts={}, computed_metrics={},
        )

        for s in bp.scenarios:
            assert len(s.narrative) > 20, f"{s.name} narrative too short"
            assert len(s.key_driver) > 5, f"{s.name} key_driver too short"


class TestMessageBuilding:
    """Test that _build_message produces valid context."""

    def test_message_includes_dcf_assumptions(self):
        architect = ScenarioArchitect(llm_client=MagicMock())
        msg = architect._build_message(
            entity_id="test",
            entity_name="TEST",
            base_dcf_assumptions={
                "revenue_growth_path": [0.12, 0.11, 0.10],
                "operating_margin_path": [0.35, 0.36, 0.37],
                "wacc": 0.095,
                "terminal_growth_rate": 0.03,
            },
            meta_facts={"revenue": 100_000_000_000},
            computed_metrics={"operating_margin": 0.35},
            market_data={"current_price": 500, "market_cap": 1_300_000_000_000},
            sector_pack=None, consensus_data=None,
            segment_detail=None, macro_context=None,
        )

        assert "BASE CASE DCF ASSUMPTIONS" in msg
        assert "12.0%" in msg
        assert "WACC" in msg
        assert "KEY FINANCIALS" in msg

    def test_message_handles_empty_data(self):
        architect = ScenarioArchitect(llm_client=MagicMock())
        msg = architect._build_message(
            entity_id="test", entity_name="TEST",
            base_dcf_assumptions={}, meta_facts={}, computed_metrics={},
            market_data=None, sector_pack=None, consensus_data=None,
            segment_detail=None, macro_context=None,
        )
        assert "ENTITY: TEST" in msg


class TestDecisionEngineIntegration:
    """Test that scenario data flows correctly to ThesisDecision."""

    def test_scenarios_dict_has_new_fields(self):
        """Verify the scenarios dict format matches what DecisionEngine expects."""
        scenarios = {
            "bear_value": 150.0,
            "base_value": 200.0,
            "bull_value": 280.0,
            "matrix_id": "sm_test_001",
            "bear_narrative": "Growth slows due to competition",
            "base_narrative": "Steady execution on current trajectory",
            "bull_narrative": "AI breakthrough accelerates growth",
            "bear_probability": 0.25,
            "base_probability": 0.50,
            "bull_probability": 0.25,
            "probability_weighted_value": 207.5,
            "primary_swing_factor": "AI monetization timeline",
        }

        # Verify all expected keys exist
        assert "bear_narrative" in scenarios
        assert "base_probability" in scenarios
        assert "probability_weighted_value" in scenarios
        assert "primary_swing_factor" in scenarios

        # Verify PW calculation
        pw = (scenarios["bear_probability"] * scenarios["bear_value"]
              + scenarios["base_probability"] * scenarios["base_value"]
              + scenarios["bull_probability"] * scenarios["bull_value"])
        assert abs(pw - scenarios["probability_weighted_value"]) < 1.0
