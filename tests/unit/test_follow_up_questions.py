"""Tests for Agent Follow-Up Questions feature."""

import pytest
from aegis.data_contracts.judgment_schema import FollowUpQuestion, JudgmentContract
from aegis.core.orchestrator.auto_research import AutoResearchOrchestrator


class TestFollowUpQuestionSchema:
    """Test FollowUpQuestion Pydantic model."""

    def test_valid_follow_up(self):
        fq = FollowUpQuestion(
            question="What is the gross margin by product segment?",
            data_type="segment",
            data_key="gross_margin_by_segment",
            priority="high",
        )
        assert fq.question == "What is the gross margin by product segment?"
        assert fq.data_type == "segment"
        assert fq.priority == "high"

    def test_default_priority(self):
        fq = FollowUpQuestion(
            question="What is capex trend?",
            data_type="time_series",
            data_key="capex",
        )
        assert fq.priority == "medium"

    def test_invalid_data_type(self):
        with pytest.raises(Exception):
            FollowUpQuestion(
                question="test",
                data_type="invalid_type",
                data_key="test",
            )

    def test_invalid_priority(self):
        with pytest.raises(Exception):
            FollowUpQuestion(
                question="test",
                data_type="metric",
                data_key="test",
                priority="critical",  # invalid
            )


class TestJudgmentContractWithFollowUps:
    """Test JudgmentContract includes follow_up_questions."""

    def test_empty_follow_ups_by_default(self):
        from aegis.data_contracts.judgment_schema import CognitiveBiasSelfCheck
        jc = JudgmentContract(
            judgment_id="j_test_001",
            agent_name="test_agent",
            agent_version="1.0",
            question_id="q_test",
            run_id="r_test",
            cognitive_bias_self_check=CognitiveBiasSelfCheck(
                anchoring_risk="low",
                confirmation_bias_risk="low",
                recency_bias_risk="low",
                narrative_fallacy_risk="low",
            ),
            judgment_status="complete",
        )
        assert jc.follow_up_questions == []

    def test_with_follow_ups(self):
        from aegis.data_contracts.judgment_schema import CognitiveBiasSelfCheck
        fqs = [
            FollowUpQuestion(
                question="What is D&A by segment?",
                data_type="segment",
                data_key="depreciation_by_segment",
                priority="high",
            ),
        ]
        jc = JudgmentContract(
            judgment_id="j_test_002",
            agent_name="accounting_analyst",
            agent_version="1.0",
            question_id="q_test",
            run_id="r_test",
            cognitive_bias_self_check=CognitiveBiasSelfCheck(
                anchoring_risk="low",
                confirmation_bias_risk="low",
                recency_bias_risk="low",
                narrative_fallacy_risk="low",
            ),
            judgment_status="complete",
            follow_up_questions=fqs,
        )
        assert len(jc.follow_up_questions) == 1
        assert jc.follow_up_questions[0].priority == "high"


class TestTryAnswerFollowUp:
    """Test _try_answer_follow_up data lookup."""

    def setup_method(self):
        self.segment_detail = {
            "product": {
                "iphone": {"revenue": 200_000_000_000, "operating_margin": 0.54},
                "services": {"revenue": 85_000_000_000, "operating_margin": 0.72},
            },
            "geographic": {
                "americas": {"revenue": 167_000_000_000},
                "emea": {"revenue": 89_000_000_000},
            },
        }
        self.computed_metrics = {
            "gross_margin": 0.46,
            "operating_margin": 0.35,
            "roic": 0.52,
            "pe_ratio": 28.5,
        }
        self.meta_facts = {
            "revenue": 400_000_000_000,
            "net_income": 100_000_000_000,
            "sbc": 12_000_000_000,
            "capex": 15_000_000_000,
        }
        self.historical_data = {
            2022: {"revenue": 350_000_000_000, "net_income": 85_000_000_000},
            2023: {"revenue": 375_000_000_000, "net_income": 92_000_000_000},
            2024: {"revenue": 400_000_000_000, "net_income": 100_000_000_000},
        }

    def _make_fq(self, data_type, data_key, priority="high"):
        return FollowUpQuestion(
            question="test", data_type=data_type,
            data_key=data_key, priority=priority,
        )

    def test_metric_exact_match(self):
        fq = self._make_fq("metric", "gross_margin")
        result = AutoResearchOrchestrator._try_answer_follow_up(
            fq, self.segment_detail, self.computed_metrics,
            self.meta_facts, self.historical_data,
        )
        assert result == 0.46

    def test_metric_fuzzy_match(self):
        fq = self._make_fq("metric", "margin")
        result = AutoResearchOrchestrator._try_answer_follow_up(
            fq, self.segment_detail, self.computed_metrics,
            self.meta_facts, self.historical_data,
        )
        # Should find gross_margin or operating_margin (fuzzy)
        assert result is not None

    def test_metric_not_found(self):
        fq = self._make_fq("metric", "nonexistent_ratio")
        result = AutoResearchOrchestrator._try_answer_follow_up(
            fq, self.segment_detail, self.computed_metrics,
            self.meta_facts, self.historical_data,
        )
        assert result is None

    def test_segment_lookup(self):
        fq = self._make_fq("segment", "operating_margin_by_product")
        result = AutoResearchOrchestrator._try_answer_follow_up(
            fq, self.segment_detail, self.computed_metrics,
            self.meta_facts, self.historical_data,
        )
        # Should find operating_margin across product segments
        assert result is not None
        assert "iphone" in result
        assert result["iphone"] == 0.54

    def test_fact_exact_match(self):
        fq = self._make_fq("fact", "sbc")
        result = AutoResearchOrchestrator._try_answer_follow_up(
            fq, self.segment_detail, self.computed_metrics,
            self.meta_facts, self.historical_data,
        )
        assert result == 12_000_000_000

    def test_fact_not_found(self):
        fq = self._make_fq("fact", "insider_transactions")
        result = AutoResearchOrchestrator._try_answer_follow_up(
            fq, self.segment_detail, self.computed_metrics,
            self.meta_facts, self.historical_data,
        )
        assert result is None

    def test_time_series_lookup(self):
        fq = self._make_fq("time_series", "revenue")
        result = AutoResearchOrchestrator._try_answer_follow_up(
            fq, self.segment_detail, self.computed_metrics,
            self.meta_facts, self.historical_data,
        )
        assert result is not None
        assert 2024 in result
        assert result[2024] == 400_000_000_000

    def test_time_series_not_found(self):
        fq = self._make_fq("time_series", "dau_count")
        result = AutoResearchOrchestrator._try_answer_follow_up(
            fq, self.segment_detail, self.computed_metrics,
            self.meta_facts, self.historical_data,
        )
        assert result is None

    def test_empty_data_sources(self):
        fq = self._make_fq("metric", "gross_margin")
        result = AutoResearchOrchestrator._try_answer_follow_up(
            fq, {}, {}, {}, {},
        )
        assert result is None


class TestAgentInputSupplementalData:
    """Test AgentInput.supplemental_data field."""

    def test_default_empty(self):
        from aegis.core.agents.base import AgentInput
        inp = AgentInput(entity_id="test", run_id="r1", question_id="q1")
        assert inp.supplemental_data == {}

    def test_with_supplemental_data(self):
        from aegis.core.agents.base import AgentInput
        inp = AgentInput(
            entity_id="test", run_id="r1", question_id="q1",
            supplemental_data={"gross_margin_by_segment": {"iphone": 0.54}},
        )
        assert "gross_margin_by_segment" in inp.supplemental_data
