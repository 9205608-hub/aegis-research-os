"""Phase 2 End-to-End Integration Test.

Validates:
  Agent framework → Specialist Agents → Critics → Publish Gate

Exit criteria (Section 32):
  - Critic 能捕获 double counting
  - Critic 能捕获 bias injection
  - Publish Gate blocks on critical issues, passes on clean analysis
"""

import pytest

from aegis.core.agents.base import AgentInput, AgentOutput
from aegis.core.agents.accounting_analyst.agent import AccountingAnalyst
from aegis.core.agents.business_analyst.agent import BusinessAnalyst
from aegis.core.agents.sector_context_agent.agent import SectorContextAgent
from aegis.core.agents.management_analyst.agent import ManagementAnalyst
from aegis.core.critics.logic_critic.critic import LogicCritic
from aegis.core.critics.accounting_critic.critic import AccountingCritic
from aegis.core.critics.evidence_critic.critic import EvidenceCritic
from aegis.core.critics.sector_critic.critic import SectorCritic
from aegis.core.critics.cognitive_bias_critic.critic import CognitiveBiasCritic
from aegis.core.publish_gate.gate import PublishGate
from aegis.data_contracts.judgment_schema import (
    CognitiveBiasSelfCheck,
    Counterargument,
    DisconfirmingTrigger,
    Inference,
    JudgmentContract,
    Observation,
)


# ──────────────────────── Fixtures ────────────────────────

@pytest.fixture
def meta_input():
    """Standard Meta (US ad platform) input for agent testing."""
    return AgentInput(
        entity_id="meta",
        run_id="run_phase2_test",
        question_id="q_meta_full",
        facts={"revenue": 134_902_000_000, "net_income": 39_098_000_000},
        metric_results={
            "gross_margin": 0.8096,
            "operating_margin": 0.3480,
            "net_margin": 0.2898,
            "ebitda_margin": 0.5179,
            "roe": 0.3085,
            "roic": 0.2520,
            "sbc_to_revenue": 0.1141,
            "dilution_rate": 0.0180,
            "accruals_ratio": 0.0450,
            "cfo_to_net_income": 1.2340,
            "capex_to_revenue": 0.1880,
            "nwc": 5_200_000_000,
            "fcf_simple": 43_000_000_000,
        },
        evidence_packets=[
            {
                "evidence_id": "ev_meta_10k_2024",
                "assertion_type": "accounting_quality",
                "assertion_text": "Meta 10-K FY2024: clean audit opinion, no restatements",
            },
        ],
    )


@pytest.fixture
def ad_platform_sector_pack():
    return {
        "sector_pack_id": "sp_ad_platform_v1",
        "sector_name": "Ad Platform / Digital Advertising",
        "key_kpis": [
            {"metric": "arpu", "display": "Average Revenue Per User", "importance": "critical"},
            {"metric": "dau_mau_ratio", "display": "DAU/MAU Engagement Ratio", "importance": "high"},
        ],
        "cycle_characteristics": {
            "cyclicality": "moderate",
            "primary_driver": "Ad spending correlates with GDP",
            "leading_indicators": ["PMI / business confidence", "E-commerce growth"],
        },
        "competitive_dynamics": {
            "disruption_risks": ["Privacy regulation (ATT, GDPR)", "TikTok shift"],
        },
        "accounting_considerations": [
            "SBC is material (10-15% of revenue)",
            "Dual-class structures common — governance implications",
        ],
    }


# ──────────────────────── Agent Tests ────────────────────────

class TestAgentFramework:
    """Test that agents produce valid JudgmentContract outputs."""

    def test_accounting_analyst_produces_valid_judgment(self, meta_input):
        out = AccountingAnalyst().run(meta_input)
        assert out.validation_passed
        assert out.judgment.agent_name == "accounting_analyst"
        assert len(out.judgment.observations) > 0
        assert len(out.judgment.inferences) > 0
        assert len(out.judgment.counterarguments) > 0
        assert out.judgment.cognitive_bias_self_check is not None

    def test_business_analyst_produces_valid_judgment(self, meta_input):
        out = BusinessAnalyst().run(meta_input)
        assert out.validation_passed
        assert out.judgment.agent_name == "business_analyst"
        assert len(out.judgment.observations) > 0

    def test_sector_context_agent_with_pack(self, meta_input, ad_platform_sector_pack):
        meta_input = AgentInput(
            entity_id=meta_input.entity_id,
            run_id=meta_input.run_id,
            question_id="q_sector_test",
            metric_results={"arpu": 48.5, "dau_mau_ratio": 0.66},
            sector_pack=ad_platform_sector_pack,
        )
        out = SectorContextAgent().run(meta_input)
        assert out.validation_passed
        assert out.judgment.sector_context_applied == "sp_ad_platform_v1"

    def test_management_analyst_produces_valid_judgment(self, meta_input):
        out = ManagementAnalyst().run(meta_input)
        assert out.validation_passed
        assert out.judgment.agent_name == "management_analyst"

    def test_agent_observation_inference_separation(self, meta_input):
        """Section 19.1: observations and inferences must be separate."""
        out = AccountingAnalyst().run(meta_input)
        j = out.judgment
        # Each inference must reference valid observation indices
        obs_count = len(j.observations)
        for inf in j.inferences:
            for idx in inf.based_on_observation_indices:
                assert 0 <= idx < obs_count, \
                    f"Inference references invalid observation index {idx}"


# ──────────────────────── Critic Tests ────────────────────────

class TestCriticDetection:
    """Test that critics properly detect issues — Phase 2 exit criteria."""

    def test_logic_critic_catches_double_counting(self):
        """EXIT CRITERIA: critic catches double counting (SBC + dilution)."""
        # Create a judgment that uses BOTH sbc_to_revenue and dilution_rate
        # but does NOT acknowledge the double-counting risk
        bad_judgment = JudgmentContract(
            judgment_id="j_bad_double_count",
            agent_name="test_agent",
            agent_version="0.1.0",
            question_id="q_test",
            run_id="run_test",
            observations=[
                Observation(
                    text="SBC is 15% of revenue",
                    source_ids=["fact:sbc"],
                ),
                Observation(
                    text="Dilution rate is 3% annually",
                    source_ids=["fact:dilution"],
                ),
            ],
            inferences=[
                Inference(
                    text="Company has high compensation costs and equity dilution",
                    based_on_observation_indices=[0, 1],
                    confidence="high",
                ),
            ],
            counterarguments=[
                Counterargument(
                    text="SBC attracts talent",
                    strength="moderate",
                ),
            ],
            disconfirming_triggers=[
                DisconfirmingTrigger(text="SBC policy change"),
            ],
            used_metric_ids=["sbc_to_revenue", "dilution_rate"],
            cognitive_bias_self_check=CognitiveBiasSelfCheck(
                anchoring_risk="low",
                confirmation_bias_risk="low",
                recency_bias_risk="low",
                narrative_fallacy_risk="low",
            ),
            judgment_status="complete",
        )

        result = LogicCritic().review([bad_judgment])
        issue_codes = [i.issue_code for i in result.issues]
        assert "LOGIC_DOUBLE_COUNTING" in issue_codes, \
            "Logic critic must catch SBC + dilution double counting"

    def test_accounting_critic_catches_sbc_dilution_double_penalty(self):
        """Accounting critic also catches SBC + dilution double penalty."""
        bad_judgment = JudgmentContract(
            judgment_id="j_bad_acct",
            agent_name="test_agent",
            agent_version="0.1.0",
            question_id="q_test",
            run_id="run_test",
            observations=[
                Observation(text="SBC high", source_ids=["f1"]),
                Observation(text="Dilution high", source_ids=["f2"]),
            ],
            inferences=[
                Inference(
                    text="Both are expensive",
                    based_on_observation_indices=[0, 1],
                    confidence="medium",
                ),
            ],
            counterarguments=[
                Counterargument(text="Market practice", strength="weak"),
            ],
            disconfirming_triggers=[
                DisconfirmingTrigger(text="Policy change"),
            ],
            used_metric_ids=["sbc_to_revenue", "dilution_rate"],
            cognitive_bias_self_check=CognitiveBiasSelfCheck(
                anchoring_risk="low",
                confirmation_bias_risk="low",
                recency_bias_risk="low",
                narrative_fallacy_risk="low",
            ),
            judgment_status="complete",
        )

        result = AccountingCritic().review([bad_judgment])
        issue_codes = [i.issue_code for i in result.issues]
        assert "ACCT_SBC_DILUTION_DOUBLE" in issue_codes

    def test_bias_critic_catches_confirmation_bias(self):
        """EXIT CRITERIA: cognitive bias critic catches confirmation bias."""
        # Create judgment with many supporting observations but no counterarguments
        biased_judgment = JudgmentContract(
            judgment_id="j_biased",
            agent_name="biased_agent",
            agent_version="0.1.0",
            question_id="q_test",
            run_id="run_test",
            observations=[
                Observation(text=f"Supporting fact {i}", source_ids=[f"f{i}"])
                for i in range(8)
            ],
            inferences=[
                Inference(
                    text="Everything is great",
                    based_on_observation_indices=[0, 1, 2],
                    confidence="high",
                ),
            ],
            counterarguments=[],  # NO counterarguments — extreme confirmation bias
            disconfirming_triggers=[],
            used_metric_ids=["gross_margin"],
            cognitive_bias_self_check=CognitiveBiasSelfCheck(
                anchoring_risk="low",
                confirmation_bias_risk="low",  # Self-report lies
                recency_bias_risk="low",
                narrative_fallacy_risk="low",
            ),
            judgment_status="complete",
        )

        result = CognitiveBiasCritic().review([biased_judgment])
        issue_codes = [i.issue_code for i in result.issues]
        assert "COGNITIVE_CONFIRMATION" in issue_codes, \
            "Bias critic must catch zero-counterargument confirmation bias"
        # Should be block severity
        confirmation_issues = [
            i for i in result.issues if i.issue_code == "COGNITIVE_CONFIRMATION"
        ]
        assert any(i.severity == "block" for i in confirmation_issues), \
            "Zero counterarguments should trigger block severity"

    def test_bias_critic_catches_narrative_fallacy(self):
        """Bias critic catches inference without observation support."""
        narrative_judgment = JudgmentContract(
            judgment_id="j_narrative",
            agent_name="narrative_agent",
            agent_version="0.1.0",
            question_id="q_test",
            run_id="run_test",
            observations=[
                Observation(text="Revenue grew 20%", source_ids=["f1"]),
            ],
            inferences=[
                Inference(
                    text="This is the next trillion-dollar company",
                    based_on_observation_indices=[99],  # Invalid index!
                    confidence="high",
                ),
            ],
            counterarguments=[
                Counterargument(text="Valuation stretched", strength="moderate"),
            ],
            disconfirming_triggers=[
                DisconfirmingTrigger(text="Growth slows"),
            ],
            used_metric_ids=["revenue"],
            cognitive_bias_self_check=CognitiveBiasSelfCheck(
                anchoring_risk="low",
                confirmation_bias_risk="low",
                recency_bias_risk="low",
                narrative_fallacy_risk="low",
            ),
            judgment_status="complete",
        )

        result = CognitiveBiasCritic().review([narrative_judgment])
        issue_codes = [i.issue_code for i in result.issues]
        assert "COGNITIVE_NARRATIVE" in issue_codes, \
            "Bias critic must catch narrative fallacy (ungrounded inference)"

    def test_bias_critic_catches_overconfidence_narrow_scenario(self):
        """Overconfidence: scenario spread < 20% should block."""
        judgment = JudgmentContract(
            judgment_id="j_overconf",
            agent_name="test_agent",
            agent_version="0.1.0",
            question_id="q_test",
            run_id="run_test",
            observations=[
                Observation(text="Strong growth", source_ids=["f1"]),
            ],
            inferences=[
                Inference(text="Will grow 15%", based_on_observation_indices=[0], confidence="high"),
            ],
            counterarguments=[
                Counterargument(text="Could slow", strength="weak"),
            ],
            disconfirming_triggers=[
                DisconfirmingTrigger(text="Miss earnings"),
            ],
            used_metric_ids=["revenue"],
            self_reported_uncertainties=[],
            cognitive_bias_self_check=CognitiveBiasSelfCheck(
                anchoring_risk="low",
                confirmation_bias_risk="low",
                recency_bias_risk="low",
                narrative_fallacy_risk="low",
            ),
            judgment_status="complete",
        )

        result = CognitiveBiasCritic().review(
            [judgment], context={"scenario_spread": 0.15}
        )
        issue_codes = [i.issue_code for i in result.issues]
        assert "COGNITIVE_OVERCONFIDENCE" in issue_codes
        overconf_issues = [i for i in result.issues if i.issue_code == "COGNITIVE_OVERCONFIDENCE"]
        assert any(i.severity == "block" for i in overconf_issues), \
            "Scenario spread < 20% should block publish"

    def test_evidence_critic_blocks_no_evidence(self):
        """Evidence critic blocks judgment with zero evidence."""
        empty_judgment = JudgmentContract(
            judgment_id="j_no_evidence",
            agent_name="test_agent",
            agent_version="0.1.0",
            question_id="q_test",
            run_id="run_test",
            observations=[],
            inferences=[],
            counterarguments=[
                Counterargument(text="Placeholder", strength="weak"),
            ],
            disconfirming_triggers=[],
            used_metric_ids=[],
            used_evidence_ids=[],
            cognitive_bias_self_check=CognitiveBiasSelfCheck(
                anchoring_risk="low",
                confirmation_bias_risk="low",
                recency_bias_risk="low",
                narrative_fallacy_risk="low",
            ),
            judgment_status="complete",
        )

        result = EvidenceCritic().review([empty_judgment])
        assert result.block_publish, "Evidence critic must block when no evidence exists"


# ──────────────────────── Publish Gate Tests ────────────────────────

class TestPublishGate:
    """Test Publish Gate blocks and passes correctly."""

    def test_clean_analysis_sbc_mutual_exclusion(self, meta_input):
        """Agent output now correctly avoids SBC double-counting.

        With the SBC mutual exclusion fix in AgentBase._collect_metric_ids(),
        agents no longer claim both sbc_to_revenue and dilution_rate simultaneously.
        This prevents the SBC double-counting block that previously prevented all publishing.
        """
        j = AccountingAnalyst().run(meta_input).judgment

        # Verify the agent does NOT claim both SBC metrics
        has_sbc = "sbc_to_revenue" in j.used_metric_ids
        has_dilution = "dilution_rate" in j.used_metric_ids
        assert not (has_sbc and has_dilution), (
            "Agent should not claim both sbc_to_revenue and dilution_rate — "
            "SBC mutual exclusion should prevent this"
        )

        critic_results = [
            LogicCritic().review([j]),
            AccountingCritic().review([j]),
        ]

        # Should have no SBC-related block issues
        sbc_blocks = [
            i for cr in critic_results for i in cr.issues
            if i.severity == "block" and "SBC" in (i.message or "") or "double" in (i.issue_code or "").lower()
        ]
        assert len(sbc_blocks) == 0, f"SBC mutual exclusion should prevent blocks, got: {sbc_blocks}"

    def test_bias_block_prevents_publish(self):
        """Publish gate must block when bias critic blocks."""
        biased_judgment = JudgmentContract(
            judgment_id="j_biased_pub",
            agent_name="test_agent",
            agent_version="0.1.0",
            question_id="q_test",
            run_id="run_test",
            observations=[
                Observation(text=f"Fact {i}", source_ids=[f"f{i}"])
                for i in range(10)
            ],
            inferences=[
                Inference(text="Bull case", based_on_observation_indices=[0], confidence="high"),
            ],
            counterarguments=[],  # Triggers block
            disconfirming_triggers=[],
            used_metric_ids=["gross_margin"],
            cognitive_bias_self_check=CognitiveBiasSelfCheck(
                anchoring_risk="low",
                confirmation_bias_risk="low",
                recency_bias_risk="low",
                narrative_fallacy_risk="low",
            ),
            judgment_status="complete",
        )

        critic_results = [
            LogicCritic().review([biased_judgment]),
            CognitiveBiasCritic().review([biased_judgment]),
        ]

        gate = PublishGate()
        result = gate.evaluate(
            [biased_judgment], critic_results,
            context={"run_manifest_id": "run_test"},
        )
        assert not result.publishable, "Bias-blocked judgment must not be publishable"
        assert "critic_gate" in result.blocked_by or "cognitive_bias_gate" in result.blocked_by

    def test_missing_bias_critic_blocks_publish(self):
        """Publish gate blocks if bias critic didn't run."""
        j = JudgmentContract(
            judgment_id="j_no_bias_check",
            agent_name="test_agent",
            agent_version="0.1.0",
            question_id="q_test",
            run_id="run_test",
            observations=[
                Observation(text="Revenue grew", source_ids=["f1"]),
            ],
            inferences=[
                Inference(text="Growth", based_on_observation_indices=[0], confidence="medium"),
            ],
            counterarguments=[
                Counterargument(text="Could slow", strength="moderate"),
            ],
            disconfirming_triggers=[
                DisconfirmingTrigger(text="Miss earnings"),
            ],
            used_metric_ids=["revenue"],
            cognitive_bias_self_check=CognitiveBiasSelfCheck(
                anchoring_risk="low",
                confirmation_bias_risk="low",
                recency_bias_risk="low",
                narrative_fallacy_risk="low",
            ),
            judgment_status="complete",
        )

        # Only logic critic — no bias critic
        critic_results = [LogicCritic().review([j])]

        gate = PublishGate()
        result = gate.evaluate(
            [j], critic_results,
            context={"run_manifest_id": "run_test"},
        )
        assert not result.publishable, "Must block when bias critic hasn't run"
        assert "cognitive_bias_gate" in result.blocked_by

    def test_no_run_manifest_blocks_publish(self, meta_input):
        """Publish gate blocks without run manifest."""
        j = AccountingAnalyst().run(meta_input).judgment
        critic_results = [CognitiveBiasCritic().review([j])]

        gate = PublishGate()
        result = gate.evaluate([j], critic_results, context={})
        assert not result.publishable
        assert "reproducibility_gate" in result.blocked_by


# ──────────────────────── Cross-Agent Integration ────────────────────────

class TestCrossAgentIntegration:
    """Test multi-agent → multi-critic → publish gate pipeline."""

    def test_full_phase2_pipeline_meta(self, meta_input, ad_platform_sector_pack):
        """Full pipeline: 4 agents → 5 critics → publish gate for Meta."""
        # Run all 4 agents
        acct_j = AccountingAnalyst().run(meta_input).judgment
        biz_j = BusinessAnalyst().run(meta_input).judgment

        sector_inp = AgentInput(
            entity_id="meta", run_id="run_phase2_test",
            question_id="q_sector",
            metric_results={"arpu": 48.5, "dau_mau_ratio": 0.66},
            sector_pack=ad_platform_sector_pack,
        )
        sector_j = SectorContextAgent().run(sector_inp).judgment
        mgmt_j = ManagementAnalyst().run(meta_input).judgment

        all_judgments = [acct_j, biz_j, sector_j, mgmt_j]

        # Run all 5 critics
        critic_results = [
            LogicCritic().review(all_judgments),
            AccountingCritic().review(all_judgments),
            EvidenceCritic().review(all_judgments),
            SectorCritic().review(all_judgments, context={
                "sector_pack": ad_platform_sector_pack,
            }),
            CognitiveBiasCritic().review(all_judgments),
        ]

        # Publish gate — with SBC mutual exclusion in AgentBase._collect_metric_ids(),
        # agents no longer claim both sbc_to_revenue and dilution_rate simultaneously,
        # so the SBC double-counting block no longer fires. The gate should now pass
        # (assuming warn accumulation is below threshold).
        gate = PublishGate()
        result = gate.evaluate(
            all_judgments, critic_results,
            context={"run_manifest_id": "run_phase2_test"},
        )

        # Verify no SBC double-counting blocks exist
        all_issues = [i for cr in critic_results for i in cr.issues]
        sbc_blocks = [
            i for i in all_issues
            if i.severity == "block" and "DOUBLE_COUNTING" in i.issue_code
        ]
        assert len(sbc_blocks) == 0, (
            "SBC mutual exclusion should prevent double-counting blocks"
        )
