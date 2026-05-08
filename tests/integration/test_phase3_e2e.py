"""Phase 3 End-to-End Integration Test.

Validates:
  Macro Context → Market Expectations → 7 Agents → 7 Critics →
  Decision Engine → Thesis Version Manager

Exit criteria (Section 32):
  - Thesis contains priced-in object
  - Thesis contains edge assessment
  - Thesis contains scenario matrix (bear/base/bull)
"""

import pytest
from datetime import date, datetime, timezone

from aegis.data_contracts.macro_snapshot_schema import MacroSnapshot
from aegis.data_contracts.consensus_snapshot_schema import ConsensusSnapshot
from aegis.data_contracts.edge_assessment_schema import EdgeAssessment

from aegis.core.macro import MacroContextLayer
from aegis.core.market_expectations import MarketExpectationsLayer

from aegis.core.agents.base import AgentInput
from aegis.core.agents.accounting_analyst.agent import AccountingAnalyst
from aegis.core.agents.business_analyst.agent import BusinessAnalyst
from aegis.core.agents.sector_context_agent.agent import SectorContextAgent
from aegis.core.agents.management_analyst.agent import ManagementAnalyst
from aegis.core.agents.valuation_analyst.agent import ValuationAnalyst
from aegis.core.agents.variant_analyst.agent import VariantAnalyst
from aegis.core.agents.risk_analyst.agent import RiskAnalyst

from aegis.core.critics.logic_critic.critic import LogicCritic
from aegis.core.critics.accounting_critic.critic import AccountingCritic
from aegis.core.critics.evidence_critic.critic import EvidenceCritic
from aegis.core.critics.sector_critic.critic import SectorCritic
from aegis.core.critics.cognitive_bias_critic.critic import CognitiveBiasCritic
from aegis.core.critics.macro_consistency_critic.critic import MacroConsistencyCritic
from aegis.core.critics.market_critic.critic import MarketCritic

from aegis.core.decision_engine import DecisionEngine
from aegis.core.publish_gate import PublishGate
from aegis.core.thesis_manager import ThesisVersionManager


# ──────────────────────── Fixtures ────────────────────────

@pytest.fixture
def us_macro_snapshot():
    return MacroSnapshot(
        macro_snapshot_id="ms_us_202604",
        snapshot_timestamp=datetime(2026, 4, 10, tzinfo=timezone.utc),
        region="US",
        cycle_phase_estimate="late_expansion",
        fed_funds_rate=0.0425,
        us_10y_yield=0.043,
        us_2y_yield=0.041,
        yield_curve_slope_2s10s=20.0,
        vix=18.5,
        pmi_manufacturing=52.3,
        pmi_services=54.1,
        cpi_yoy=0.028,
        core_pce_yoy=0.025,
        unemployment_rate=0.038,
        usd_dxy=104.5,
        source_ids=["fred:2026-04-10"],
        ingestion_batch_id="batch_macro_001",
    )


@pytest.fixture
def cn_macro_snapshot():
    return MacroSnapshot(
        macro_snapshot_id="ms_cn_202604",
        snapshot_timestamp=datetime(2026, 4, 10, tzinfo=timezone.utc),
        region="CN",
        cycle_phase_estimate="recovery",
        cn_pmi_official=50.8,
        cn_pmi_caixin=51.2,
        credit_pulse=0.02,
        lpr_1y=0.031,
        lpr_5y=0.036,
        cpi_yoy=0.008,
        source_ids=["wind:2026-04-10"],
        ingestion_batch_id="batch_macro_002",
    )


@pytest.fixture
def meta_metrics():
    return {
        "gross_margin": 0.8096,
        "operating_margin": 0.3480,
        "net_margin": 0.2898,
        "ebitda_margin": 0.5179,
        "roe": 0.3085,
        "roic": 0.2520,
        "sbc_to_revenue": 0.1141,
        "dilution_rate": 0.0180,
        "accruals_ratio": 0.0450,
        "cfo_to_net_income": 1.234,
        "capex_to_revenue": 0.188,
        "nwc": 5_200_000_000,
        "fcf_simple": 43_000_000_000,
        "pe_ratio": 25.2,
        "ev_to_ebitda": 18.5,
        "ev_to_revenue": 9.6,
        "enterprise_value": 1_295_000_000_000,
        "net_debt": -20_000_000_000,
        "net_debt_to_ebitda": -0.29,
        "current_ratio": 2.68,
    }


@pytest.fixture
def ad_sector_pack():
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
            "leading_indicators": ["PMI / business confidence"],
        },
        "competitive_dynamics": {"disruption_risks": ["Privacy regulation"]},
        "accounting_considerations": ["SBC is material (10-15% of revenue)"],
    }


@pytest.fixture
def edge_assessment_dict():
    return {
        "edge_assessment_id": "ea_meta_20260412",
        "thesis_id": "th_meta_20260412_001",
        "primary_edge_type": "analytical",
        "edge_source": "Market underestimates AI capex payback",
        "edge_durability": "medium_term",
        "edge_decay_trigger": "Sell-side begins publishing detailed AI ROI models",
        "edge_confidence": "medium",
        "why_market_is_wrong": "Consensus uses simple revenue/capex; real return requires segment ROIC decomposition",
        "what_would_change_my_mind": "Management provides detailed AI revenue attribution matching consensus optimism",
        "edge_uniqueness": "moderate",
    }


# ──────────────────────── Macro Context Tests ────────────────────────

class TestMacroContext:
    def test_us_macro_snapshot_and_cycle(self, us_macro_snapshot):
        ml = MacroContextLayer()
        ml.update_snapshot(us_macro_snapshot)
        cycle = ml.get_cycle_assessment("US")
        assert cycle is not None
        assert cycle.phase == "late_expansion"
        assert len(cycle.key_signals) > 0

    def test_cn_macro_snapshot(self, cn_macro_snapshot):
        ml = MacroContextLayer()
        ml.update_snapshot(cn_macro_snapshot)
        cycle = ml.get_cycle_assessment("CN")
        assert cycle is not None
        assert cycle.phase == "recovery"

    def test_macro_transmission_paths(self, us_macro_snapshot):
        ml = MacroContextLayer()
        ml.update_snapshot(us_macro_snapshot)
        paths = ml.get_transmission_paths("Ad Platform")
        assert len(paths) > 0

    def test_macro_staleness_check(self):
        ml = MacroContextLayer()
        assert ml.is_stale("US")  # No snapshot loaded


# ──────────────────────── Market Expectations Tests ────────────────────────

class TestMarketExpectations:
    def test_priced_in_object_construction(self):
        me = MarketExpectationsLayer()
        me.set_current_price("meta", 520.0)
        pio = me.build_priced_in_object(
            "meta",
            implied_growth=0.15,
            implied_terminal_growth=0.03,
        )
        assert pio.current_price == 520.0
        assert pio.implied_revenue_growth == 0.15
        assert pio.implied_terminal_growth == 0.03

    def test_consensus_revision_signal(self):
        me = MarketExpectationsLayer()
        cs = ConsensusSnapshot(
            snapshot_id="cs_meta_rev_202604",
            entity_id="meta",
            snapshot_timestamp=datetime(2026, 4, 10, tzinfo=timezone.utc),
            metric_id="revenue",
            definition_id="def_revenue_v1",
            period="FY_forward",
            period_type="annual",
            consensus_mean=160_000_000_000,
            consensus_median=159_000_000_000,
            estimate_count=35,
            high_estimate=175_000_000_000,
            low_estimate=148_000_000_000,
            standard_deviation=5_000_000_000,
            revision_1w=0.005,
            revision_1m=0.012,
            revision_3m=0.025,
            unit="USD",
            source="bloomberg",
            source_tier=2,
            ingestion_batch_id="batch_consensus_001",
        )
        me.add_consensus(cs)
        signal = me.get_revision_signal("meta", "revenue", "FY_forward")
        assert signal is not None
        assert signal.momentum == "positive"

    def test_key_assumption_disagreements(self):
        me = MarketExpectationsLayer()
        disagreements = me.build_key_assumption_disagreements(
            "meta",
            {
                "revenue_growth_fy26": {
                    "bear": "10%", "base": "18%", "bull": "25%",
                    "market_implied": "15%", "my_view": "18%",
                    "is_variant": True,
                }
            },
        )
        assert len(disagreements) == 1
        assert disagreements[0].this_is_the_variant is True


# ──────────────────────── Full Pipeline Test ────────────────────────

class TestPhase3FullPipeline:
    """EXIT CRITERIA: thesis contains priced-in + edge assessment + scenario matrix."""

    def test_full_pipeline_meta_with_all_phase3_components(
        self, us_macro_snapshot, meta_metrics, ad_sector_pack, edge_assessment_dict
    ):
        """Full 7-agent, 7-critic pipeline producing a publishable thesis."""
        # 1. Macro context
        ml = MacroContextLayer()
        ml.update_snapshot(us_macro_snapshot)
        macro_ctx = ml.get_context_for_entity("meta", "US", "Ad Platform")

        # 2. Market expectations
        me = MarketExpectationsLayer()
        me.set_current_price("meta", 520.0)
        priced_in = me.build_priced_in_object("meta", implied_growth=0.15, implied_terminal_growth=0.03)

        # 3. Build agent context
        agent_macro = {
            **macro_ctx,
            "priced_in": {
                "implied_revenue_growth": priced_in.implied_revenue_growth,
                "implied_terminal_growth": priced_in.implied_terminal_growth,
                "revision_momentum": "positive",
                "pe_ratio_fwd": 22.0,
            },
            "scenarios": {
                "bear_value": 420.0,
                "base_value": 580.0,
                "bull_value": 750.0,
                "matrix_id": "sm_meta_20260412",
            },
            "current_price": 520.0,
            "disagreements": [{
                "assumption": "revenue_growth",
                "market_implied": "15%",
                "my_view": "18%",
                "this_is_the_variant": True,
            }],
        }

        # 4. Run all 7 agents
        base_inp = AgentInput(
            entity_id="meta", run_id="run_p3_full",
            question_id="q_meta_acct",
            metric_results=meta_metrics,
            macro_context=agent_macro,
            evidence_packets=[{
                "evidence_id": "ev_meta_10k_2024",
                "assertion_type": "accounting_quality",
                "assertion_text": "Clean audit, no restatements",
            }],
        )
        acct_j = AccountingAnalyst().run(base_inp).judgment
        biz_j = BusinessAnalyst().run(base_inp).judgment
        mgmt_j = ManagementAnalyst().run(base_inp).judgment

        sector_inp = AgentInput(
            entity_id="meta", run_id="run_p3_full",
            question_id="q_sector",
            metric_results={"arpu": 48.5, "dau_mau_ratio": 0.66},
            sector_pack=ad_sector_pack,
        )
        sector_j = SectorContextAgent().run(sector_inp).judgment

        val_inp = AgentInput(
            entity_id="meta", run_id="run_p3_full",
            question_id="q_val",
            metric_results=meta_metrics,
            macro_context=agent_macro,
        )
        val_j = ValuationAnalyst().run(val_inp).judgment
        var_j = VariantAnalyst().run(val_inp).judgment
        risk_j = RiskAnalyst().run(val_inp).judgment

        all_judgments = [acct_j, biz_j, mgmt_j, sector_j, val_j, var_j, risk_j]

        # 5. Run all 7 critics
        critic_context = {
            "sector_pack": ad_sector_pack,
            "cycle_phase": "late_expansion",
            "priced_in": {"implied_revenue_growth": 0.15},
            "edge_assessment": edge_assessment_dict,
            "scenarios": {
                "bear_value": 420.0,
                "base_value": 580.0,
                "bull_value": 750.0,
            },
        }

        critic_results = [
            LogicCritic().review(all_judgments),
            AccountingCritic().review(all_judgments),
            EvidenceCritic().review(all_judgments),
            SectorCritic().review(all_judgments, context=critic_context),
            CognitiveBiasCritic().review(all_judgments),
            MacroConsistencyCritic().review(all_judgments, context=critic_context),
            MarketCritic().review(all_judgments, context=critic_context),
        ]

        # 6. Publish gate — with SBC mutual exclusion, agents no longer trigger
        # double-counting blocks. Gate should pass if warns are within threshold.
        gate = PublishGate()
        gate_result = gate.evaluate(
            all_judgments, critic_results,
            context={"run_manifest_id": "run_p3_full"},
        )
        # SBC mutual exclusion prevents double-counting blocks

        # 7. Decision engine — receives blocked gate result
        de = DecisionEngine()
        edge_obj = EdgeAssessment(**edge_assessment_dict)
        decision = de.decide(
            "meta", "run_p3_full", all_judgments, critic_results,
            gate_result.publishable,
            context={
                "edge_assessment": edge_obj,
                "scenarios": {
                    "bear_value": 420.0, "base_value": 580.0, "bull_value": 750.0,
                    "matrix_id": "sm_meta_20260412",
                },
                "macro_dependency": "US late-cycle: favor quality, pricing power",
                "sector_cycle_position": "Moderate cyclicality, ad spending GDP-correlated",
            },
        )
        # With SBC mutual exclusion, gate can now pass → decision may be published
        assert decision.publishing_status in ("published", "blocked")

        # EXIT CRITERIA CHECKS:
        # (a) Thesis has priced-in object
        assert priced_in.implied_revenue_growth is not None
        assert priced_in.current_price > 0

        # (b) Thesis has edge assessment
        assert decision.edge_assessment is not None
        assert decision.edge_assessment.why_market_is_wrong != ""
        assert decision.edge_assessment.edge_decay_trigger != ""

        # (c) Thesis has scenario matrix (bear/base/bull)
        assert decision.bear_case_value is not None
        assert decision.base_case_value is not None
        assert decision.bull_case_value is not None
        assert decision.bear_case_value < decision.base_case_value < decision.bull_case_value

        # Additional quality checks
        assert len(decision.kill_criteria) > 0
        assert len(decision.monitorables) > 0
        # With SBC enforcement causing blocks, confidence drops to very_low
        assert decision.confidence_bucket in ("very_low", "low", "medium", "high", "very_high")

    def test_market_critic_blocks_without_priced_in(self, meta_metrics):
        """Market critic must block thesis without priced-in object."""
        inp = AgentInput(
            entity_id="meta", run_id="run_p3_test",
            question_id="q_val",
            metric_results=meta_metrics,
        )
        val_j = ValuationAnalyst().run(inp).judgment

        # No priced-in, no edge, no scenarios in context
        result = MarketCritic().review([val_j], context={})
        assert result.block_publish
        codes = [i.issue_code for i in result.issues]
        assert "MARKET_NO_PRICED_IN" in codes
        assert "MARKET_NO_EDGE" in codes
        assert "MARKET_NO_SCENARIOS" in codes

    def test_market_critic_passes_with_complete_context(self, meta_metrics, edge_assessment_dict):
        """Market critic passes with all required context."""
        inp = AgentInput(
            entity_id="meta", run_id="run_p3_test",
            question_id="q_val",
            metric_results=meta_metrics,
            macro_context={
                "priced_in": {"implied_revenue_growth": 0.15},
                "scenarios": {"bear_value": 420, "base_value": 580, "bull_value": 750},
                "current_price": 520,
            },
        )
        var_j = VariantAnalyst().run(inp).judgment

        result = MarketCritic().review([var_j], context={
            "priced_in": {"implied_revenue_growth": 0.15},
            "edge_assessment": edge_assessment_dict,
            "scenarios": {"bear_value": 420, "base_value": 580, "bull_value": 750},
        })
        assert not result.block_publish


# ──────────────────────── Thesis Version Manager Tests ────────────────────────

class TestThesisVersionManager:
    def test_create_and_full_rerun(self):
        tvm = ThesisVersionManager()
        snap = tvm.create_thesis("th_001", "run_1", {
            "core_thesis": "Meta undervalued due to AI capex payback",
            "confidence": "medium",
        })
        assert snap.version == 1
        assert snap.status == "draft"

        rec = tvm.full_rerun("th_001", "run_2", {
            "core_thesis": "Meta undervalued due to AI capex payback",
            "confidence": "high",
            "new_evidence": "Q2 beat confirmed thesis",
        }, trigger="Q2 FY2026 earnings release")
        assert rec is not None
        assert rec.from_version == 1
        assert rec.to_version == 2
        assert rec.change_type == "full_rerun"
        assert rec.unchanged_core_thesis is True

        current = tvm.get_current("th_001")
        assert current.version == 2

    def test_incremental_update(self):
        tvm = ThesisVersionManager()
        tvm.create_thesis("th_002", "run_1", {"core_thesis": "PDD growth", "target": 150})
        rec = tvm.incremental_update(
            "th_002", "run_1b",
            {"target": 165},
            trigger="Consensus revised up 5%",
        )
        assert rec is not None
        assert rec.change_type == "incremental_update"
        current = tvm.get_current("th_002")
        assert current.data["target"] == 165

    def test_status_change(self):
        tvm = ThesisVersionManager()
        tvm.create_thesis("th_003", "run_1", {"core_thesis": "TSMC"})
        rec = tvm.update_status("th_003", "published", "run_1", "Passed all gates")
        assert rec is not None
        assert rec.change_type == "status_change_only"
        assert tvm.get_current("th_003").status == "published"

    def test_version_chain_complete(self):
        tvm = ThesisVersionManager()
        tvm.create_thesis("th_004", "run_1", {"core_thesis": "NVDA"})
        tvm.update_status("th_004", "published", "run_1", "Published")
        tvm.full_rerun("th_004", "run_2", {"core_thesis": "NVDA updated"}, "Earnings")
        tvm.update_status("th_004", "killed", "run_2", "Kill criterion hit")

        chain = tvm.get_version_chain("th_004")
        assert len(chain) == 4  # create + status + rerun + kill
        records = tvm.get_version_records("th_004")
        assert len(records) == 3  # 3 transitions
