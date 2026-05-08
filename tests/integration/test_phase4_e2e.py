"""Phase 4 End-to-End Integration Test.

Validates:
  Research Mode Router → Multi-Entity Orchestration → Comparative Analyst
  → Cross-Entity Critic → Event Bus → Portfolio Integration

Exit criteria (Section 32):
  - 可执行 thematic screening
  - 可执行 pair trade analysis
"""

import pytest
from datetime import datetime, timezone

from aegis.data_contracts.common import ResearchMode

from aegis.core.planner import ResearchModeRouter, ResearchRequest
from aegis.core.agents.comparative_analyst.agent import ComparativeAnalyst, ComparativeInput
from aegis.core.agents.base import AgentInput
from aegis.core.agents.valuation_analyst.agent import ValuationAnalyst
from aegis.core.agents.risk_analyst.agent import RiskAnalyst
from aegis.core.agents.business_analyst.agent import BusinessAnalyst
from aegis.core.critics import CrossEntityCritic, LogicCritic, CognitiveBiasCritic
from aegis.core.events import EventBus
from aegis.core.portfolio import PortfolioIntegration
from aegis.core.decision_engine import DecisionEngine
from aegis.core.publish_gate import PublishGate
from aegis.data_contracts.edge_assessment_schema import EdgeAssessment


# ──────────────────────── Fixtures ────────────────────────

@pytest.fixture
def semiconductor_entities():
    return {
        "nvidia_corp": {
            "gross_margin": 0.75, "roic": 0.45, "roe": 0.90,
            "ev_to_revenue": 25.3, "ev_to_ebitda": 45.0, "pe_ratio": 55.0,
            "net_debt": -15_000_000_000, "net_debt_to_ebitda": -0.3,
            "capex_to_revenue": 0.08, "fcf_simple": 30_000_000_000,
        },
        "tsmc_ltd": {
            "gross_margin": 0.55, "roic": 0.25, "roe": 0.28,
            "ev_to_revenue": 8.2, "ev_to_ebitda": 14.0, "pe_ratio": 22.0,
            "net_debt": 5_000_000_000, "net_debt_to_ebitda": 0.2,
            "capex_to_revenue": 0.35, "fcf_simple": 15_000_000_000,
        },
        "amd_inc": {
            "gross_margin": 0.50, "roic": 0.15, "roe": 0.12,
            "ev_to_revenue": 8.5, "ev_to_ebitda": 28.0, "pe_ratio": 40.0,
            "net_debt": -3_000_000_000, "net_debt_to_ebitda": -0.5,
            "capex_to_revenue": 0.05, "fcf_simple": 4_000_000_000,
        },
        "broadcom_inc": {
            "gross_margin": 0.65, "roic": 0.18, "roe": 0.35,
            "ev_to_revenue": 11.8, "ev_to_ebitda": 18.0, "pe_ratio": 30.0,
            "net_debt": 25_000_000_000, "net_debt_to_ebitda": 2.5,
            "capex_to_revenue": 0.04, "fcf_simple": 18_000_000_000,
        },
        "asml_holding": {
            "gross_margin": 0.52, "roic": 0.30, "roe": 0.70,
            "ev_to_revenue": 12.1, "ev_to_ebitda": 30.0, "pe_ratio": 38.0,
            "net_debt": -5_000_000_000, "net_debt_to_ebitda": -0.5,
            "capex_to_revenue": 0.06, "fcf_simple": 8_000_000_000,
        },
    }


# ──────────────────────── Research Mode Router Tests ────────────────────────

class TestResearchModeRouter:
    def test_single_entity_routing(self):
        router = ResearchModeRouter()
        plan = router.route(ResearchRequest(
            research_mode=ResearchMode.SINGLE_ENTITY,
            entity_ids=["nvidia_corp"],
        ))
        assert plan.research_mode == ResearchMode.SINGLE_ENTITY
        assert len(plan.phases) == 1
        assert "valuation_analyst" in plan.phases[0].agents_to_run

    def test_multi_entity_routing(self):
        router = ResearchModeRouter()
        plan = router.route(ResearchRequest(
            research_mode=ResearchMode.MULTI_ENTITY,
            entity_ids=["nvidia_corp", "tsmc_ltd", "amd_inc"],
        ))
        assert len(plan.phases) == 2
        assert plan.phases[1].cross_entity_agents == ["comparative_analyst"]
        assert "cross_entity_critic" in plan.phases[1].critics_to_run

    def test_thematic_routing(self):
        """EXIT CRITERIA: can execute thematic screening."""
        router = ResearchModeRouter()
        plan = router.route(ResearchRequest(
            research_mode=ResearchMode.THEMATIC,
            entity_ids=["nvidia_corp", "tsmc_ltd", "amd_inc", "broadcom_inc", "asml_holding"],
            theme="AI infrastructure beneficiaries",
            screening_filters=[
                {"metric": "gross_margin", "condition": "> 0.40"},
            ],
        ))
        assert plan.research_mode == ResearchMode.THEMATIC
        assert len(plan.phases) == 3  # screening → ranking → deep dive
        assert plan.shared_context["theme"] == "AI infrastructure beneficiaries"
        assert "comparative_analyst" in plan.phases[1].cross_entity_agents

    def test_pair_trade_routing(self):
        """EXIT CRITERIA: can execute pair trade analysis."""
        router = ResearchModeRouter()
        plan = router.route(ResearchRequest(
            research_mode=ResearchMode.PAIR_TRADE,
            entity_ids=["asml_holding", "amd_inc"],
        ))
        assert plan.research_mode == ResearchMode.PAIR_TRADE
        assert len(plan.entity_ids) == 2
        assert len(plan.phases) == 2
        # Phase 1: full analysis for both
        assert len(plan.phases[0].agents_to_run) == 7
        # Phase 2: comparative
        assert "comparative_analyst" in plan.phases[1].cross_entity_agents

    def test_pair_trade_rejects_wrong_entity_count(self):
        router = ResearchModeRouter()
        with pytest.raises(ValueError, match="exactly 2"):
            router.route(ResearchRequest(
                research_mode=ResearchMode.PAIR_TRADE,
                entity_ids=["a", "b", "c"],
            ))

    def test_event_impact_routing(self):
        router = ResearchModeRouter()
        plan = router.route(ResearchRequest(
            research_mode=ResearchMode.EVENT_IMPACT,
            entity_ids=["tsmc_ltd", "nvidia_corp", "amd_inc"],
            event_description="China restricts rare earth exports",
        ))
        assert "risk_analyst" in plan.phases[0].agents_to_run

    def test_supply_chain_routing(self):
        router = ResearchModeRouter()
        plan = router.route(ResearchRequest(
            research_mode=ResearchMode.SUPPLY_CHAIN,
            entity_ids=["tsmc_ltd", "nvidia_corp", "amd_inc"],
        ))
        assert "comparative_analyst" in plan.phases[0].cross_entity_agents


# ──────────────────────── Comparative Analyst Tests ────────────────────────

class TestComparativeAnalyst:
    def test_comparison_matrix_production(self, semiconductor_entities):
        ci = ComparativeInput(
            entity_ids=list(semiconductor_entities.keys()),
            run_id="run_p4_test",
            theme="AI semiconductor",
            per_entity_metrics=semiconductor_entities,
            comparison_dimensions=["gross_margin", "roic", "ev_to_revenue"],
        )
        cm = ComparativeAnalyst().analyze(ci)

        assert len(cm.entity_ids) == 5
        assert len(cm.dimensions) >= 2
        assert len(cm.top_picks) > 0
        assert cm.relative_valuation.metric == "ev_to_revenue"

        # NVIDIA should rank #1 in gross_margin and roic
        gm_dim = next((d for d in cm.dimensions if d.dimension == "gross_margin"), None)
        assert gm_dim is not None
        assert gm_dim.rankings["nvidia_corp"] == 1

    def test_cross_entity_risk_detection(self):
        ci = ComparativeInput(
            entity_ids=["nvidia_corp", "amd_inc", "broadcom_inc"],
            run_id="run_test",
            per_entity_metrics={
                "nvidia_corp": {"gross_margin": 0.75},
                "amd_inc": {"gross_margin": 0.50},
                "broadcom_inc": {"gross_margin": 0.65},
            },
            entity_relationships=[
                {"relationship_type": "supply_chain.supplier_to", "entity_a": "tsmc_ltd", "entity_b": "nvidia_corp"},
                {"relationship_type": "supply_chain.supplier_to", "entity_a": "tsmc_ltd", "entity_b": "amd_inc"},
            ],
        )
        cm = ComparativeAnalyst().analyze(ci)
        # Should detect shared TSMC supplier
        assert any("tsmc_ltd" in r.lower() for r in cm.cross_entity_risks)

    def test_pair_trade_comparison(self, semiconductor_entities):
        """Pair trade: long ASML vs short AMD."""
        ci = ComparativeInput(
            entity_ids=["asml_holding", "amd_inc"],
            run_id="run_pair",
            theme="Equipment cycle mismatch",
            per_entity_metrics={
                k: semiconductor_entities[k]
                for k in ["asml_holding", "amd_inc"]
            },
            comparison_dimensions=["gross_margin", "roic", "fcf_simple"],
        )
        cm = ComparativeAnalyst().analyze(ci)
        assert len(cm.entity_ids) == 2
        assert len(cm.top_picks) >= 1


# ──────────────────────── Event Bus Tests ────────────────────────

class TestEventBus:
    def test_register_and_query_monitorables(self):
        bus = EventBus()
        mid = bus.register_monitorable(
            "th_001", "nvidia_corp", "Margin decline >300bps", "quarterly", "risk_analyst"
        )
        active = bus.get_active_monitorables("nvidia_corp")
        assert len(active) == 1
        assert active[0].monitorable_id == mid

    def test_register_kill_criteria(self):
        bus = EventBus()
        kid = bus.register_kill_criterion(
            "th_001", "nvidia_corp", "Revenue miss >10%", ">10%", "quarterly"
        )
        criteria = bus.get_active_kill_criteria("th_001")
        assert len(criteria) == 1

    def test_register_edge_decay(self):
        bus = EventBus()
        did = bus.register_edge_decay(
            "th_001", "nvidia_corp", "analytical", "Sell-side catches up"
        )
        bus.mark_edge_decayed(did)
        reg = bus.get_registrations_for_thesis("th_001")
        assert reg["edge_decays"] == 1

    def test_filing_event_triggers_full_rerun(self):
        bus = EventBus()
        bus.register_monitorable("th_001", "nvidia_corp", "test", "quarterly", "test")
        evt = bus.emit_event("A", "nvidia_corp", "10-K annual filing")
        assert "full_rerun" in evt.triggered_actions
        assert "th_001" in evt.affected_thesis_ids

    def test_edge_decay_event(self):
        bus = EventBus()
        bus.register_edge_decay("th_002", "meta", "analytical", "Sell-side coverage")
        evt = bus.emit_event("E", "meta", "Sell-side published AI ROI model")
        assert "edge_reassessment" in evt.triggered_actions
        assert "th_002" in evt.affected_thesis_ids

    def test_event_handler_callback(self):
        bus = EventBus()
        events_received = []
        bus.on_event("B", lambda e: events_received.append(e))
        bus.emit_event("B", "nvidia_corp", "Price dropped 5%")
        assert len(events_received) == 1

    def test_full_thesis_registration(self):
        """All monitorables, kill criteria, and edge decays must be registered."""
        bus = EventBus()
        # Simulate decision engine registering from thesis
        bus.register_monitorable("th_meta", "meta", "Gross margin decline", "quarterly", "risk_analyst")
        bus.register_monitorable("th_meta", "meta", "Customer loss", "quarterly", "business_analyst")
        bus.register_kill_criterion("th_meta", "meta", "Revenue miss >10%", ">10%", "quarterly")
        bus.register_edge_decay("th_meta", "meta", "analytical", "Sell-side AI models")

        reg = bus.get_registrations_for_thesis("th_meta")
        assert reg["monitorables"] == 2
        assert reg["kill_criteria"] == 1
        assert reg["edge_decays"] == 1


# ──────────────────────── Cross-Entity Critic Tests ────────────────────────

class TestCrossEntityCritic:
    def test_blocks_cross_standard_without_bridge(self):
        """Cross-standard comparison without bridge must block."""
        from aegis.data_contracts.judgment_schema import (
            JudgmentContract, Observation, Inference, Counterargument,
            DisconfirmingTrigger, CognitiveBiasSelfCheck,
        )
        j = JudgmentContract(
            judgment_id="j_cross", agent_name="comparative_analyst",
            agent_version="0.1.0", question_id="q", run_id="r",
            observations=[Observation(text="NVIDIA vs TSMC comparison", source_ids=["f1"])],
            inferences=[Inference(text="NVIDIA has higher margins", based_on_observation_indices=[0], confidence="medium")],
            counterarguments=[Counterargument(text="Different models", strength="moderate")],
            disconfirming_triggers=[DisconfirmingTrigger(text="Margin compression")],
            cognitive_bias_self_check=CognitiveBiasSelfCheck(
                anchoring_risk="low", confirmation_bias_risk="low",
                recency_bias_risk="low", narrative_fallacy_risk="low",
            ),
            judgment_status="complete",
        )
        result = CrossEntityCritic().review([j], context={
            "entity_standards": {"nvidia_corp": "US_GAAP", "tsmc_ltd": "IFRS"},
        })
        codes = [i.issue_code for i in result.issues]
        assert "CROSS_ENTITY_NO_BRIDGE" in codes
        assert result.block_publish

    def test_passes_same_standard(self):
        from aegis.data_contracts.judgment_schema import (
            JudgmentContract, Observation, Inference, Counterargument,
            DisconfirmingTrigger, CognitiveBiasSelfCheck,
        )
        j = JudgmentContract(
            judgment_id="j_same", agent_name="test", agent_version="0.1.0",
            question_id="q", run_id="r",
            observations=[Observation(text="Comparison", source_ids=["f1"])],
            inferences=[Inference(text="Similar", based_on_observation_indices=[0], confidence="medium")],
            counterarguments=[Counterargument(text="Different scale", strength="moderate")],
            disconfirming_triggers=[DisconfirmingTrigger(text="Test")],
            cognitive_bias_self_check=CognitiveBiasSelfCheck(
                anchoring_risk="low", confirmation_bias_risk="low",
                recency_bias_risk="low", narrative_fallacy_risk="low",
            ),
            judgment_status="complete",
        )
        result = CrossEntityCritic().review([j], context={
            "entity_standards": {"nvidia_corp": "US_GAAP", "amd_inc": "US_GAAP"},
        })
        assert not result.block_publish


# ──────────────────────── Portfolio Integration Tests ────────────────────────

class TestPortfolioIntegration:
    def test_signal_generation_from_decision(self):
        de = DecisionEngine()
        from aegis.core.agents.base import AgentInput
        inp = AgentInput(
            entity_id="meta", run_id="run_sig", question_id="q_val",
            metric_results={"pe_ratio": 25.0, "gross_margin": 0.81, "roic": 0.25,
                            "net_debt": -20e9, "net_debt_to_ebitda": -0.3},
            macro_context={
                "priced_in": {"implied_revenue_growth": 0.15},
                "scenarios": {"bear_value": 420, "base_value": 580, "bull_value": 750},
                "current_price": 520,
            },
        )
        val_j = ValuationAnalyst().run(inp).judgment
        var_j = BusinessAnalyst().run(inp).judgment

        edge = EdgeAssessment(
            edge_assessment_id="ea_meta", thesis_id="th_meta",
            primary_edge_type="analytical", edge_source="AI capex underestimated",
            edge_durability="medium_term",
            edge_decay_trigger="Sell-side catches up",
            edge_confidence="medium",
            why_market_is_wrong="Simple revenue/capex model misses segment ROIC",
            what_would_change_my_mind="Management AI revenue breakdown matches consensus",
            edge_uniqueness="moderate",
        )

        critic_results = [CognitiveBiasCritic().review([val_j, var_j])]
        gate = PublishGate()
        gate_result = gate.evaluate([val_j, var_j], critic_results, context={"run_manifest_id": "run_sig"})

        decision = de.decide("meta", "run_sig", [val_j, var_j], critic_results, gate_result.publishable,
            context={"edge_assessment": edge,
                     "scenarios": {"bear_value": 420, "base_value": 580, "bull_value": 750}})

        pi = PortfolioIntegration()
        signal = pi.generate_signal(decision)
        assert signal.entity_id == "meta"
        assert signal.sizing_tier in ("full_position", "standard_position", "starter_position", "no_position")
        assert "analytical" in str(signal.edge_type).lower()
        assert signal.thesis_horizon != ""

    def test_risk_interaction_check(self):
        """Section 22.2: risk interaction check is mandatory."""
        from types import SimpleNamespace
        td = SimpleNamespace(
            entity_id="nvidia_corp", run_id="run_test",
            publishable=True, publishing_status="published",
            my_variant="upside from AI", confidence_bucket="medium",
            base_case_value=580, bear_case_value=420, bull_case_value=750,
            edge_assessment=SimpleNamespace(
                primary_edge_type="analytical", edge_durability="medium_term",
            ),
            variant_magnitude="15% upside",
            fragility_points=["AI capex timing"],
            sector_cycle_position="semiconductor",
        )

        pi = PortfolioIntegration()
        signal = pi.generate_signal(
            td,
            existing_positions=[{"entity_id": "amd_inc", "sector": "semiconductor"}],
            entity_relationships=[
                {"relationship_type": "supply_chain.supplier_to",
                 "entity_a": "tsmc_ltd", "entity_b": "nvidia_corp"},
            ],
        )
        # Should detect sector concentration
        assert any(ri.interaction_type == "sector_concentration" for ri in signal.risk_interactions)


# ──────────────────────── Full Pipeline Tests ────────────────────────

class TestFullPipelinePhase4:
    def test_thematic_screening_pipeline(self, semiconductor_entities):
        """EXIT CRITERIA: can execute thematic screening."""
        # 1. Route
        router = ResearchModeRouter()
        plan = router.route(ResearchRequest(
            research_mode=ResearchMode.THEMATIC,
            entity_ids=list(semiconductor_entities.keys()),
            theme="AI infrastructure beneficiaries",
        ))
        assert plan.research_mode == ResearchMode.THEMATIC

        # 2. Run comparative analysis
        ci = ComparativeInput(
            entity_ids=plan.entity_ids,
            run_id=plan.run_id,
            theme="AI infrastructure beneficiaries",
            per_entity_metrics=semiconductor_entities,
            comparison_dimensions=["gross_margin", "roic", "fcf_simple"],
        )
        cm = ComparativeAnalyst().analyze(ci)
        assert len(cm.top_picks) > 0
        assert cm.theme == "AI infrastructure beneficiaries"

        # 3. Register results on event bus
        bus = EventBus()
        for pick in cm.top_picks:
            bus.register_monitorable(
                f"th_{pick}", pick, "Thematic screening result — monitor for thesis creation",
                "monthly", "comparative_analyst",
            )
        assert len(bus.get_active_monitorables()) == len(cm.top_picks)

    def test_pair_trade_pipeline(self, semiconductor_entities):
        """EXIT CRITERIA: can execute pair trade analysis."""
        # 1. Route
        router = ResearchModeRouter()
        plan = router.route(ResearchRequest(
            research_mode=ResearchMode.PAIR_TRADE,
            entity_ids=["asml_holding", "amd_inc"],
        ))
        assert len(plan.entity_ids) == 2

        # 2. Per-entity analysis
        judgments_by_entity: dict[str, list] = {}
        for eid in plan.entity_ids:
            inp = AgentInput(
                entity_id=eid, run_id=plan.run_id,
                question_id=f"q_{eid}_pair",
                metric_results=semiconductor_entities[eid],
            )
            biz_j = BusinessAnalyst().run(inp).judgment
            risk_j = RiskAnalyst().run(inp).judgment
            judgments_by_entity[eid] = [biz_j, risk_j]

        # 3. Comparative analysis
        ci = ComparativeInput(
            entity_ids=plan.entity_ids,
            run_id=plan.run_id,
            theme="Equipment cycle mismatch pair trade",
            per_entity_metrics={
                k: semiconductor_entities[k] for k in plan.entity_ids
            },
            comparison_dimensions=["gross_margin", "roic"],
        )
        cm = ComparativeAnalyst().analyze(ci)
        assert len(cm.entity_ids) == 2

        # 4. Cross-entity critic
        all_judgments = [j for jlist in judgments_by_entity.values() for j in jlist]
        cross_result = CrossEntityCritic().review(all_judgments, context={
            "entity_standards": {"asml_holding": "IFRS", "amd_inc": "US_GAAP"},
        })
        # Should flag cross-standard issue since ASML is IFRS
        codes = [i.issue_code for i in cross_result.issues]
        assert "CROSS_ENTITY_NO_BRIDGE" in codes

        # 5. Event bus registration
        bus = EventBus()
        for eid in plan.entity_ids:
            bus.register_monitorable(
                f"th_pair_{eid}", eid,
                "Pair trade component — monitor relative performance",
                "weekly", "variant_analyst",
            )
            bus.register_kill_criterion(
                f"th_pair_{eid}", eid,
                "Pair spread inverts",
                "spread < 0",
                "daily",
            )
        assert bus.get_registrations_for_thesis("th_pair_asml_holding")["monitorables"] == 1
        assert bus.get_registrations_for_thesis("th_pair_amd_inc")["kill_criteria"] == 1
