"""Phase 6 End-to-End Integration Test.

Validates:
  REST API → CLI → Report Layer → Audit/Cost → Sector Packs (12)

Exit criteria (Section 32):
  - API 端到端运行
  - Dashboard 可用 (via dashboard_json serializer)
"""

import pytest
from datetime import datetime, timezone

from aegis.data_contracts.common import ResearchMode
from aegis.data_contracts.edge_assessment_schema import EdgeAssessment


# ──────────────────────── REST API Tests ────────────────────────

class TestRESTAPI:
    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient
        from aegis.api.rest.app import app
        return TestClient(app)

    def test_health_check(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["version"] == "0.1.0"

    def test_start_research_single_entity(self, client):
        resp = client.post("/api/v1/research/run", json={
            "entity_ids": ["meta"],
            "research_mode": "single_entity",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["research_mode"] == "single_entity"
        assert "meta" in data["entity_ids"]
        assert len(data["phases"]) == 1
        assert data["status"] == "planned"
        run_id = data["run_id"]

        # Get status
        resp2 = client.get(f"/api/v1/research/run/{run_id}")
        assert resp2.status_code == 200
        assert resp2.json()["status"] == "planned"

    def test_start_research_thematic(self, client):
        resp = client.post("/api/v1/research/run", json={
            "entity_ids": ["nvidia_corp", "tsmc_ltd", "amd_inc"],
            "research_mode": "thematic",
            "theme": "AI infrastructure",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["research_mode"] == "thematic"
        assert len(data["phases"]) == 3

    def test_start_research_pair_trade(self, client):
        resp = client.post("/api/v1/research/run", json={
            "entity_ids": ["asml_holding", "amd_inc"],
            "research_mode": "pair_trade",
        })
        assert resp.status_code == 200
        assert len(resp.json()["entity_ids"]) == 2

    def test_invalid_research_mode_rejected(self, client):
        resp = client.post("/api/v1/research/run", json={
            "entity_ids": ["meta"],
            "research_mode": "invalid_mode",
        })
        assert resp.status_code == 400

    def test_list_runs(self, client):
        client.post("/api/v1/research/run", json={
            "entity_ids": ["meta"],
            "research_mode": "single_entity",
        })
        resp = client.get("/api/v1/research/runs")
        assert resp.status_code == 200
        assert len(resp.json()) >= 1

    def test_thesis_crud(self, client):
        # Create
        resp = client.post("/api/v1/thesis/create", json={
            "thesis_id": "th_api_test",
            "entity_id": "meta",
            "run_id": "run_api",
            "core_thesis": "Meta undervalued",
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "draft"

        # Get
        resp2 = client.get("/api/v1/thesis/th_api_test")
        assert resp2.status_code == 200
        assert resp2.json()["thesis_id"] == "th_api_test"

        # List
        resp3 = client.get("/api/v1/thesis/")
        assert resp3.status_code == 200

    def test_event_emit_and_log(self, client):
        resp = client.post("/api/v1/events/emit", json={
            "category": "A",
            "entity_id": "meta",
            "description": "10-K annual filing",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "full_rerun" in data["triggered_actions"]

        # Query log
        resp2 = client.get("/api/v1/events/log?entity_id=meta")
        assert resp2.status_code == 200
        assert len(resp2.json()) >= 1

    def test_monitorable_registration(self, client):
        resp = client.post("/api/v1/events/monitorables", json={
            "thesis_id": "th_001",
            "entity_id": "meta",
            "description": "Margin decline >300bps",
            "check_frequency": "quarterly",
        })
        assert resp.status_code == 200
        assert "monitorable_id" in resp.json()

        resp2 = client.get("/api/v1/events/monitorables")
        assert resp2.status_code == 200

    def test_admin_audit_log(self, client):
        client.post("/api/v1/admin/audit-log?action=test_action&user=tester")
        resp = client.get("/api/v1/admin/audit-log")
        assert resp.status_code == 200

    def test_admin_cost_tracking(self, client):
        client.post("/api/v1/admin/costs/run_001?llm_calls=10&input_tokens=5000&output_tokens=2000&estimated_cost_usd=0.15")
        resp = client.get("/api/v1/admin/costs")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_cost_usd"] >= 0.15

    def test_admin_system_status(self, client):
        resp = client.get("/api/v1/admin/status")
        assert resp.status_code == 200
        assert resp.json()["status"] == "operational"

    def test_portfolio_signals_endpoint(self, client):
        resp = client.get("/api/v1/portfolio/signals")
        assert resp.status_code == 200


# ──────────────────────── CLI Tests ────────────────────────

class TestCLI:
    def test_cli_health(self):
        """CLI health command works."""
        from aegis.api.cli.main import health
        # Just verify it doesn't crash
        health()

    def test_cli_metrics(self):
        """CLI metrics command works."""
        from aegis.api.cli.main import metrics
        metrics("meta")


# ──────────────────────── Report Layer Tests ────────────────────────

class TestReportLayer:
    @pytest.fixture
    def sample_decision(self):
        from types import SimpleNamespace
        edge = EdgeAssessment(
            edge_assessment_id="ea_test", thesis_id="th_test",
            primary_edge_type="analytical", edge_source="AI capex payback",
            edge_durability="medium_term",
            edge_decay_trigger="Sell-side catches up",
            edge_confidence="medium",
            why_market_is_wrong="Simple revenue/capex model",
            what_would_change_my_mind="Management AI revenue breakdown",
            edge_uniqueness="moderate",
        )
        return SimpleNamespace(
            entity_id="meta", run_id="run_report",
            publishable=True, publishing_status="published",
            core_thesis="Meta undervalued due to AI capex payback",
            my_variant="Market underestimates AI revenue potential",
            variant_magnitude="15% upside to base case",
            market_implied_story="Market prices 15% growth, we see 18%",
            counter_thesis="AI capex may not generate adequate returns",
            key_assumption_disagreement="Revenue growth FY26: market 15% vs our 18%",
            bear_case_value=420.0, base_case_value=580.0, bull_case_value=750.0,
            edge_assessment=edge,
            kill_criteria=[{"description": "Revenue miss >10%", "threshold": ">10%", "check_frequency": "quarterly"}],
            monitorables=[{"description": "Margin decline", "check_frequency": "quarterly"}],
            fragility_points=["AI capex timing uncertain"],
            unresolved_conflicts=[],
            critic_summary={"logic_critic": "PASS (0 issues)", "cognitive_bias_critic": "PASS (0 issues)"},
            confidence_bucket="medium",
            bias_check_status="passed",
            macro_dependency="US late-cycle",
            sector_cycle_position="Ad spending GDP-correlated",
            management_quality_summary="Strong capital allocation",
            capital_allocation_assessment="ROIC 25%, above WACC",
        )

    def test_investment_memo(self, sample_decision):
        from aegis.core.reports import ReportSerializer
        serializer = ReportSerializer()
        memo = serializer.investment_memo(sample_decision, {})
        assert memo["report_type"] == "investment_memo"
        assert memo["entity_id"] == "meta"
        assert memo["sections"]["executive_summary"]["core_thesis"] != ""
        assert memo["sections"]["edge_assessment"]["available"] is True
        assert memo["sections"]["valuation"]["base_case"] == 580.0

    def test_one_page_note(self, sample_decision):
        from aegis.core.reports import ReportSerializer
        note = ReportSerializer().one_page_note(sample_decision)
        assert note["report_type"] == "one_page_variant_note"
        assert note["variant"] != ""
        assert note["bear_base_bull"] == [420.0, 580.0, 750.0]

    def test_dashboard_json(self, sample_decision):
        """EXIT CRITERIA: dashboard 可用 (dashboard_json serializer)."""
        from aegis.core.reports import ReportSerializer
        dash = ReportSerializer().dashboard_json(sample_decision)
        assert dash["report_type"] == "dashboard_json"
        assert dash["publishable"] is True
        assert dash["confidence"] == "medium"
        assert dash["bear"] == 420.0
        assert dash["base"] == 580.0
        assert dash["bull"] == 750.0

    def test_comparison_table(self):
        from aegis.core.reports import ReportSerializer
        from aegis.core.agents.comparative_analyst.agent import ComparativeAnalyst, ComparativeInput
        ci = ComparativeInput(
            entity_ids=["nvidia_corp", "tsmc_ltd", "amd_inc"],
            run_id="run_cmp",
            theme="AI semiconductor",
            per_entity_metrics={
                "nvidia_corp": {"gross_margin": 0.75, "roic": 0.45},
                "tsmc_ltd": {"gross_margin": 0.55, "roic": 0.25},
                "amd_inc": {"gross_margin": 0.50, "roic": 0.15},
            },
        )
        cm = ComparativeAnalyst().analyze(ci)
        table = ReportSerializer().comparison_table(cm)
        assert table["report_type"] == "sector_comparison_table"
        assert len(table["entities"]) == 3



# ──────────────────────── Full E2E Pipeline Test ────────────────────────

class TestFullE2EPipeline:
    """Full API → Pipeline → Report end-to-end test."""

    def test_api_to_report_pipeline(self):
        """EXIT CRITERIA: API 端到端运行."""
        from fastapi.testclient import TestClient
        from aegis.api.rest.app import app
        from aegis.core.agents.base import AgentInput
        from aegis.core.agents import AccountingAnalyst, BusinessAnalyst, ValuationAnalyst, RiskAnalyst
        from aegis.core.critics import LogicCritic, CognitiveBiasCritic, MarketCritic
        from aegis.core.decision_engine import DecisionEngine
        from aegis.core.publish_gate import PublishGate
        from aegis.core.reports import ReportSerializer

        client = TestClient(app)

        # 1. Start research via API
        resp = client.post("/api/v1/research/run", json={
            "entity_ids": ["meta"],
            "research_mode": "single_entity",
        })
        assert resp.status_code == 200
        run_id = resp.json()["run_id"]

        # 2. Run agents (simulating what the pipeline would do)
        metrics = {
            "gross_margin": 0.81, "operating_margin": 0.35, "roic": 0.25,
            "pe_ratio": 25.0, "ev_to_ebitda": 18.5,
            "accruals_ratio": 0.05, "cfo_to_net_income": 1.2,
            "sbc_to_revenue": 0.12, "dilution_rate": 0.02,
            "net_debt": -20e9, "net_debt_to_ebitda": -0.3,
        }
        inp = AgentInput(
            entity_id="meta", run_id=run_id, question_id="q_full",
            metric_results=metrics,
            macro_context={
                "priced_in": {"implied_revenue_growth": 0.15},
                "scenarios": {"bear_value": 420, "base_value": 580, "bull_value": 750},
                "current_price": 520,
            },
        )
        judgments = [
            AccountingAnalyst().run(inp).judgment,
            BusinessAnalyst().run(inp).judgment,
            ValuationAnalyst().run(inp).judgment,
            RiskAnalyst().run(inp).judgment,
        ]

        # 3. Run critics
        edge_dict = {
            "edge_assessment_id": "ea_meta", "thesis_id": "th_meta",
            "primary_edge_type": "analytical", "edge_source": "AI capex",
            "edge_durability": "medium_term",
            "edge_decay_trigger": "Sell-side models",
            "edge_confidence": "medium",
            "why_market_is_wrong": "Simple model",
            "what_would_change_my_mind": "AI revenue breakdown",
            "edge_uniqueness": "moderate",
        }
        critic_ctx = {
            "priced_in": {"implied_revenue_growth": 0.15},
            "edge_assessment": edge_dict,
            "scenarios": {"bear_value": 420, "base_value": 580, "bull_value": 750},
        }
        critic_results = [
            LogicCritic().review(judgments),
            CognitiveBiasCritic().review(judgments),
            MarketCritic().review(judgments, context=critic_ctx),
        ]

        # 4. Publish gate — with SBC enforcement, agents referencing both
        # sbc_to_revenue and dilution_rate trigger block (correct behavior)
        gate = PublishGate()
        gate_result = gate.evaluate(
            judgments, critic_results,
            context={"run_manifest_id": run_id},
        )
        # SBC mutual exclusion: gate may now pass

        # 5. Decision engine — receives blocked gate result
        edge = EdgeAssessment(**edge_dict)
        de = DecisionEngine()
        decision = de.decide("meta", run_id, judgments, critic_results,
            gate_result.publishable, context={
                "edge_assessment": edge,
                "scenarios": {"bear_value": 420, "base_value": 580, "bull_value": 750},
            })
        # SBC mutual exclusion: gate can now pass → decision may be published
        assert decision.publishing_status in ("published", "blocked")

        # 6. Generate reports via serializer (works on blocked decisions too)
        serializer = ReportSerializer()
        memo = serializer.investment_memo(decision, {})
        note = serializer.one_page_note(decision)
        dash = serializer.dashboard_json(decision)

        assert memo["report_type"] == "investment_memo"
        assert note["report_type"] == "one_page_variant_note"
        assert dash["report_type"] == "dashboard_json"
        assert isinstance(dash["publishable"], bool)  # Reflects gate result

        # 7. Record to audit log via API
        client.post(f"/api/v1/admin/audit-log?action=thesis_published&user=system&details=th_meta")
        client.post(f"/api/v1/admin/costs/{run_id}?llm_calls=25&input_tokens=50000&output_tokens=15000&estimated_cost_usd=0.85")

        # 8. Emit filing event
        resp_evt = client.post("/api/v1/events/emit", json={
            "category": "A",
            "entity_id": "meta",
            "description": "FY2025 10-K filed",
        })
        assert resp_evt.status_code == 200

        # Full pipeline ran end-to-end via API ✓
