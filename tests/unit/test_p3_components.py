"""Tests for P3 components: Storage, API, Dashboard."""

import pytest
from datetime import datetime, timezone
from pathlib import Path

from aegis.core.storage.models import (
    Base,
    ResearchRunRow,
    PredictionRow,
    PostMortemRow,
    PortfolioSignalRow,
    init_db,
)
from aegis.core.storage.repository import ResearchRepository


class TestStorageModels:

    def test_create_tables_sqlite(self, tmp_path):
        """Tables should be created in SQLite without errors."""
        db_path = tmp_path / "test.db"
        session_factory = init_db(f"sqlite:///{db_path}")
        assert db_path.exists()

    def test_insert_research_run(self, tmp_path):
        db_url = f"sqlite:///{tmp_path / 'test.db'}"
        session_factory = init_db(db_url)
        session = session_factory()

        row = ResearchRunRow(
            run_id="run_test_001",
            ticker="META",
            entity_id="meta_platforms",
            period="FY2024",
            status="completed",
            dcf_per_share=600.0,
            scenarios_json={"bear_value": 400, "base_value": 600, "bull_value": 800},
            direction="long",
            confidence_bucket="medium",
        )
        session.add(row)
        session.commit()

        retrieved = session.query(ResearchRunRow).filter_by(run_id="run_test_001").first()
        assert retrieved is not None
        assert retrieved.ticker == "META"
        assert retrieved.dcf_per_share == 600.0
        assert retrieved.scenarios_json["bear_value"] == 400
        session.close()

    def test_insert_prediction(self, tmp_path):
        db_url = f"sqlite:///{tmp_path / 'test.db'}"
        session_factory = init_db(db_url)
        session = session_factory()

        row = PredictionRow(
            thesis_id="th_meta_001",
            entity_id="meta_platforms",
            run_id="run_001",
            publish_date="2025-01-15",
            confidence_bucket="medium",
            bear_value=400, base_value=600, bull_value=800,
            current_price=585.0,
        )
        session.add(row)
        session.commit()

        retrieved = session.query(PredictionRow).filter_by(thesis_id="th_meta_001").first()
        assert retrieved is not None
        assert retrieved.current_price == 585.0
        session.close()

    def test_insert_postmortem(self, tmp_path):
        db_url = f"sqlite:///{tmp_path / 'test.db'}"
        session_factory = init_db(db_url)
        session = session_factory()

        row = PostMortemRow(
            postmortem_id="pm_001",
            thesis_id="th_meta_001",
            entity_id="meta_platforms",
            review_date="2025-07-15",
            price_at_thesis=585.0,
            price_at_review=650.0,
            total_return=0.11,
            thesis_survived=True,
            variant_realized=False,
            edge_realized=True,
            original_confidence_bucket="medium",
        )
        session.add(row)
        session.commit()
        assert session.query(PostMortemRow).count() == 1
        session.close()

    def test_insert_signal(self, tmp_path):
        db_url = f"sqlite:///{tmp_path / 'test.db'}"
        session_factory = init_db(db_url)
        session = session_factory()

        row = PortfolioSignalRow(
            entity_id="meta_platforms",
            run_id="run_001",
            direction="long",
            conviction="medium",
            sizing_tier="standard",
        )
        session.add(row)
        session.commit()
        assert session.query(PortfolioSignalRow).count() == 1
        session.close()


class TestResearchRepository:

    def test_save_and_retrieve_run(self, tmp_path):
        db_url = f"sqlite:///{tmp_path / 'test.db'}"
        repo = ResearchRepository(db_url)

        # Create a mock result object
        class MockResult:
            run_id = "run_repo_test"
            ticker = "AAPL"
            entity_id = "aapl"
            meta_facts = {"revenue": 100}
            computed_metrics = {"gross_margin": 0.45}
            dcf_per_share = 200.0
            scenarios = {"bear_value": 150, "base_value": 200, "bull_value": 250}
            implied_growth = 0.08
            html_path = None
            pipeline_log = ["step1", "step2"]

            class decision:
                publishing_status = "published"
                confidence_bucket = "medium"
                publishable = True

            class signal:
                direction = "long"
                conviction = "medium"

        repo.save_research_run(MockResult())

        latest = repo.get_latest_run("AAPL")
        assert latest is not None
        assert latest.ticker == "AAPL"
        assert latest.dcf_per_share == 200.0

    def test_get_all_runs(self, tmp_path):
        db_url = f"sqlite:///{tmp_path / 'test.db'}"
        repo = ResearchRepository(db_url)

        class MockResult:
            ticker = "META"
            entity_id = "meta"
            meta_facts = {}
            computed_metrics = {}
            dcf_per_share = 600.0
            scenarios = {}
            implied_growth = 0.1
            html_path = None
            pipeline_log = []
            class decision:
                publishing_status = "published"
                confidence_bucket = "high"
                publishable = True
            class signal:
                direction = "long"
                conviction = "high"

        for i in range(3):
            MockResult.run_id = f"run_{i}"
            repo.save_research_run(MockResult())

        all_runs = repo.get_all_runs()
        assert len(all_runs) == 3

    def test_get_stats(self, tmp_path):
        db_url = f"sqlite:///{tmp_path / 'test.db'}"
        repo = ResearchRepository(db_url)
        stats = repo.get_stats()
        assert "total_runs" in stats
        assert "total_signals" in stats
        assert stats["total_runs"] == 0


class TestAPIApp:

    def test_app_imports(self):
        """FastAPI app should import without errors."""
        from aegis.api.rest.app import app
        assert app.title == "Aegis Research OS"

    def test_health_endpoint(self):
        from fastapi.testclient import TestClient
        from aegis.api.rest.app import app
        client = TestClient(app)
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_dashboard_endpoint(self):
        from fastapi.testclient import TestClient
        from aegis.api.rest.app import app
        client = TestClient(app)
        resp = client.get("/dashboard")
        assert resp.status_code == 200
        assert "Aegis Research OS" in resp.text

    def test_list_runs_empty(self):
        from fastapi.testclient import TestClient
        from aegis.api.rest.app import app
        client = TestClient(app)
        resp = client.get("/api/v1/research/runs")
        assert resp.status_code == 200

    def test_portfolio_stats(self):
        from fastapi.testclient import TestClient
        from aegis.api.rest.app import app
        client = TestClient(app)
        resp = client.get("/api/v1/portfolio/stats")
        assert resp.status_code == 200


class TestDashboardHTML:

    def test_dashboard_file_exists(self):
        html_path = Path(__file__).resolve().parent.parent.parent / "aegis" / "dashboard" / "index.html"
        assert html_path.exists()

    def test_dashboard_contains_key_elements(self):
        html_path = Path(__file__).resolve().parent.parent.parent / "aegis" / "dashboard" / "index.html"
        content = html_path.read_text()
        assert "Aegis Research OS" in content
        assert "Quick Research" in content
        assert "Portfolio Signals" in content
        assert "chart.js" in content.lower() or "Chart" in content
