"""Tests for critic remediation actions (P1-3).

Tests that block-level critic issues include structured Remediation objects
with specific fix steps, target components, and auto-fix flags.
"""

import pytest
from aegis.data_contracts.critic_result_schema import CriticIssue, CriticResult, Remediation
from aegis.data_contracts.judgment_schema import (
    JudgmentContract, Observation, Inference, Counterargument,
    DisconfirmingTrigger, CognitiveBiasSelfCheck,
)
from aegis.core.critics.logic_critic.critic import LogicCritic
from aegis.core.critics.accounting_critic.critic import AccountingCritic
from aegis.core.critics.evidence_critic.critic import EvidenceCritic
from aegis.core.critics.market_critic.critic import MarketCritic


def _make_judgment(
    agent_name: str = "test_agent",
    observations: list[Observation] | None = None,
    inferences: list[Inference] | None = None,
    counterarguments: list[Counterargument] | None = None,
    disconfirming_triggers: list[DisconfirmingTrigger] | None = None,
    used_metric_ids: list[str] | None = None,
) -> JudgmentContract:
    return JudgmentContract(
        judgment_id="j_test_001",
        agent_name=agent_name,
        agent_version="v1_test",
        question_id="q_test",
        run_id="run_test",
        judgment_status="complete",
        observations=observations or [
            Observation(text="Revenue grew 15%", source_ids=["fact:revenue"]),
        ],
        inferences=inferences or [
            Inference(
                text="Strong revenue growth",
                confidence="medium",
                based_on_observation_indices=[0],
            ),
        ],
        counterarguments=counterarguments if counterarguments is not None else [
            Counterargument(text="Competition increasing", strength="moderate"),
        ],
        disconfirming_triggers=disconfirming_triggers if disconfirming_triggers is not None else [
            DisconfirmingTrigger(text="Revenue growth drops below 5%"),
        ],
        used_metric_ids=used_metric_ids or [],
        cognitive_bias_self_check=CognitiveBiasSelfCheck(
            anchoring_risk="low",
            confirmation_bias_risk="low",
            recency_bias_risk="low",
            narrative_fallacy_risk="low",
        ),
    )


class TestRemediationSchema:

    def test_remediation_dataclass(self):
        r = Remediation(
            steps=["Step 1", "Step 2"],
            target_component="dcf_engine",
            target_field="sbc_treatment",
            auto_fixable=True,
            rerun_required=True,
        )
        assert len(r.steps) == 2
        assert r.auto_fixable is True

    def test_critic_issue_with_remediation(self):
        issue = CriticIssue(
            issue_code="TEST_ISSUE",
            severity="block",
            message="Test block issue",
            remediation=Remediation(
                steps=["Fix the thing"],
                target_component="test",
                target_field="field",
            ),
        )
        assert issue.remediation is not None
        assert issue.remediation.steps == ["Fix the thing"]

    def test_critic_issue_without_remediation(self):
        issue = CriticIssue(
            issue_code="TEST_INFO",
            severity="info",
            message="Just info",
        )
        assert issue.remediation is None

    def test_remediation_defaults(self):
        r = Remediation()
        assert r.steps == []
        assert r.target_component == ""
        assert r.auto_fixable is False
        assert r.rerun_required is True


class TestLogicCriticRemediation:

    def test_double_counting_block_has_remediation(self):
        """SBC + dilution double-counting should have a remediation with auto_fixable=True."""
        j = _make_judgment(
            used_metric_ids=["sbc_to_revenue", "dilution_rate"],
        )
        result = LogicCritic().review([j])
        double_issues = [i for i in result.issues if i.issue_code == "LOGIC_DOUBLE_COUNTING"]
        assert len(double_issues) >= 1
        dc = double_issues[0]
        assert dc.severity == "block"
        assert dc.remediation is not None
        assert dc.remediation.target_component == "dcf_engine"
        assert dc.remediation.target_field == "sbc_treatment"
        assert dc.remediation.auto_fixable is True
        assert any("SBC" in step or "sbc" in step for step in dc.remediation.steps)

    def test_double_counting_acknowledged_no_remediation(self):
        """Acknowledged double-counting is a warn, so no block remediation needed."""
        j = _make_judgment(
            inferences=[
                Inference(
                    text="SBC and dilution both applied — double counting risk acknowledged",
                    confidence="medium",
                    based_on_observation_indices=[0],
                ),
            ],
            used_metric_ids=["sbc_to_revenue", "dilution_rate"],
        )
        result = LogicCritic().review([j])
        double_issues = [i for i in result.issues if i.issue_code == "LOGIC_DOUBLE_COUNTING"]
        assert len(double_issues) >= 1
        assert double_issues[0].severity == "warn"
        # Warn-level: no remediation needed
        assert double_issues[0].remediation is None

    def test_ungrounded_inference_has_remediation(self):
        """Inference referencing out-of-bounds observation should have remediation."""
        j = _make_judgment(
            observations=[
                Observation(text="Revenue grew 15%", source_ids=["fact:revenue"]),
            ],
            inferences=[
                Inference(
                    text="Growth is strong",
                    confidence="medium",
                    based_on_observation_indices=[5],  # Out of bounds
                ),
            ],
        )
        result = LogicCritic().review([j])
        ungrounded = [i for i in result.issues if i.issue_code == "LOGIC_UNGROUNDED_INFERENCE"]
        assert len(ungrounded) >= 1
        assert ungrounded[0].remediation is not None
        assert ungrounded[0].remediation.target_component == "test_agent"
        assert "based_on_observation_indices" in ungrounded[0].remediation.target_field
        assert ungrounded[0].remediation.rerun_required is True


class TestMarketCriticRemediation:

    def test_no_priced_in_has_remediation(self):
        j = _make_judgment()
        result = MarketCritic().review([j], context={})
        pi_issues = [i for i in result.issues if i.issue_code == "MARKET_NO_PRICED_IN"]
        assert len(pi_issues) == 1
        assert pi_issues[0].remediation is not None
        assert pi_issues[0].remediation.auto_fixable is True
        assert "ReverseDCFSolver" in pi_issues[0].remediation.steps[0]
        assert pi_issues[0].remediation.target_component == "market_expectations"

    def test_no_edge_has_remediation(self):
        j = _make_judgment()
        result = MarketCritic().review([j], context={"priced_in": {"growth": 0.1}})
        edge_issues = [i for i in result.issues if i.issue_code == "MARKET_NO_EDGE"]
        assert len(edge_issues) == 1
        assert edge_issues[0].remediation is not None
        assert edge_issues[0].remediation.target_field == "edge_assessment"
        assert edge_issues[0].remediation.auto_fixable is False

    def test_no_scenarios_has_remediation(self):
        j = _make_judgment()
        result = MarketCritic().review([j], context={
            "priced_in": {"growth": 0.1},
            "edge_assessment": {
                "edge_decay_trigger": "test",
                "why_market_is_wrong": "test",
            },
        })
        scenario_issues = [i for i in result.issues if i.issue_code == "MARKET_NO_SCENARIOS"]
        assert len(scenario_issues) == 1
        assert scenario_issues[0].remediation is not None
        assert scenario_issues[0].remediation.auto_fixable is True
        assert "DCFInput" in scenario_issues[0].remediation.steps[0]

    def test_edge_no_why_has_remediation(self):
        j = _make_judgment()
        result = MarketCritic().review([j], context={
            "priced_in": {"growth": 0.1},
            "edge_assessment": {
                "edge_decay_trigger": "test",
                # Missing why_market_is_wrong
            },
        })
        why_issues = [i for i in result.issues if i.issue_code == "MARKET_EDGE_NO_WHY"]
        assert len(why_issues) == 1
        assert why_issues[0].remediation is not None
        assert "why_market_is_wrong" in why_issues[0].remediation.target_field


class TestEvidenceCriticRemediation:

    def test_no_counterargument_has_remediation(self):
        j = _make_judgment(counterarguments=[])
        result = EvidenceCritic().review([j])
        counter_issues = [i for i in result.issues if i.issue_code == "EVIDENCE_NO_COUNTERARGUMENT"]
        assert len(counter_issues) == 1
        assert counter_issues[0].remediation is not None
        assert counter_issues[0].remediation.target_field == "counterarguments"
        assert any("Counterargument" in step for step in counter_issues[0].remediation.steps)
        assert counter_issues[0].remediation.rerun_required is True


class TestAccountingCriticRemediation:

    def test_sbc_double_count_block_has_remediation(self):
        j = _make_judgment(
            used_metric_ids=["sbc_to_revenue", "dilution_rate"],
        )
        result = AccountingCritic().review([j])
        sbc_issues = [i for i in result.issues if i.issue_code == "ACCT_SBC_DILUTION_DOUBLE"]
        assert len(sbc_issues) == 1
        assert sbc_issues[0].severity == "block"
        assert sbc_issues[0].remediation is not None
        assert sbc_issues[0].remediation.target_field == "sbc_treatment"
        assert sbc_issues[0].remediation.auto_fixable is True
        assert len(sbc_issues[0].remediation.steps) >= 2
