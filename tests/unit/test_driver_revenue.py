"""Tests for driver-based revenue modeling.

Round 20 — Revenue modeling upgrade (driver-based).
Tests cover:
- RevenueDriver / RevenueDriverTree dataclasses
- resolve_driver_revenue() — driver tree → growth path conversion
- apply_driver_deltas() — scenario-level driver adjustments
- ScenarioCase driver_deltas integration
- Sector pack YAML loading with driver definitions
- Orchestrator _build_driver_tree() integration
"""

import math
import pytest
from dataclasses import dataclass
from pathlib import Path

from aegis.core.truth.scenario_engine.dcf_engine import (
    DCFEngine,
    DCFInput,
    RevenueDriver,
    RevenueDriverTree,
    resolve_driver_revenue,
    apply_driver_deltas,
)
from aegis.core.chief_analyst.scenario_architect import ScenarioCase


# ============================================================
# RevenueDriver & RevenueDriverTree Dataclass Tests
# ============================================================

class TestRevenueDriverDataclasses:

    def test_driver_creation(self):
        d = RevenueDriver(
            name="DAU",
            base_value=3.3e9,
            growth_path=[0.02] * 10,
            unit="billions",
        )
        assert d.name == "DAU"
        assert d.base_value == 3.3e9
        assert len(d.growth_path) == 10

    def test_driver_tree_creation(self):
        tree = RevenueDriverTree(
            entity_id="meta",
            sector_pack_id="sp_ad_platform_v1",
            decomposition_formula="Revenue = DAU × Sessions × Ads × CPM",
            drivers=[
                RevenueDriver("DAU", 3.3e9, [0.02] * 10, "billions"),
                RevenueDriver("Sessions", 6.5, [0.01] * 10, "per_day"),
                RevenueDriver("Ads", 4.2, [0.03] * 10, "per_session"),
                RevenueDriver("CPM", 11.5, [0.05] * 10, "$/1000"),
            ],
        )
        assert len(tree.drivers) == 4
        assert tree.horizon_years == 10


# ============================================================
# resolve_driver_revenue() Tests
# ============================================================

class TestResolveDriverRevenue:

    def _simple_tree(self, growths_a, growths_b):
        """Two-driver tree for clean math verification."""
        return RevenueDriverTree(
            entity_id="test",
            sector_pack_id="sp_test",
            decomposition_formula="Revenue = A × B",
            drivers=[
                RevenueDriver("A", 100.0, growths_a),
                RevenueDriver("B", 200.0, growths_b),
            ],
            horizon_years=len(growths_a),
        )

    def test_flat_growth_all_drivers(self):
        """If both drivers grow at 0%, revenue growth should be 0%."""
        tree = self._simple_tree([0.0] * 5, [0.0] * 5)
        base_rev = 100.0 * 200.0  # 20,000 — scale factor = 1.0
        # But base_rev is the actual company revenue, which may differ
        # Let's use base_rev = 20000 so scale = 1.0
        growth, projections = resolve_driver_revenue(20000.0, tree)
        assert len(growth) == 5
        assert all(abs(g) < 1e-6 for g in growth)

    def test_single_driver_growth(self):
        """If A grows 10%, B flat → revenue should grow ~10%."""
        tree = self._simple_tree([0.10] * 3, [0.0] * 3)
        growth, projections = resolve_driver_revenue(20000.0, tree)
        assert len(growth) == 3
        assert growth[0] == pytest.approx(0.10, abs=0.001)
        assert growth[1] == pytest.approx(0.10, abs=0.001)

    def test_multiplicative_composition(self):
        """A grows 10%, B grows 5% → revenue grows ~15.5% (multiplicative)."""
        tree = self._simple_tree([0.10] * 3, [0.05] * 3)
        growth, projections = resolve_driver_revenue(20000.0, tree)
        # (1.10 * 1.05) - 1 = 0.155
        assert growth[0] == pytest.approx(0.155, abs=0.001)

    def test_scaling_preserves_base_revenue(self):
        """Revenue at Y0 matches base_revenue regardless of driver product."""
        tree = self._simple_tree([0.10] * 5, [0.05] * 5)
        base_rev = 150e9  # Doesn't match 100*200
        growth, projections = resolve_driver_revenue(base_rev, tree)
        # Reconstruct Y1 revenue
        y1_rev = base_rev * (1 + growth[0])
        # Should equal: scale * A_y1 * B_y1
        scale = base_rev / (100.0 * 200.0)
        expected_y1 = scale * (100.0 * 1.10) * (200.0 * 1.05)
        assert y1_rev == pytest.approx(expected_y1, rel=1e-6)

    def test_negative_growth_works(self):
        """Negative driver growth should produce negative revenue growth."""
        tree = self._simple_tree([-0.05] * 3, [-0.03] * 3)
        growth, projections = resolve_driver_revenue(20000.0, tree)
        # (0.95 * 0.97) - 1 = -0.0785
        assert growth[0] == pytest.approx(-0.0785, abs=0.001)

    def test_projections_track_driver_values(self):
        """Driver projections should track individual driver trajectories."""
        tree = self._simple_tree([0.10] * 3, [0.05] * 3)
        growth, projections = resolve_driver_revenue(20000.0, tree)
        assert len(projections) == 3
        assert projections[0]["A"] == pytest.approx(110.0, rel=1e-6)
        assert projections[0]["B"] == pytest.approx(210.0, rel=1e-6)
        assert projections[1]["A"] == pytest.approx(121.0, rel=1e-6)

    def test_empty_drivers_returns_default(self):
        tree = RevenueDriverTree(
            entity_id="test",
            sector_pack_id="sp_test",
            decomposition_formula="Revenue = ?",
            drivers=[],
            horizon_years=5,
        )
        growth, projections = resolve_driver_revenue(100.0, tree)
        assert len(growth) == 5
        assert growth[0] == 0.03  # Default

    def test_mismatched_growth_path_raises(self):
        """Driver growth_path shorter than horizon_years should raise."""
        tree = RevenueDriverTree(
            entity_id="test",
            sector_pack_id="sp_test",
            decomposition_formula="Revenue = A × B",
            drivers=[
                RevenueDriver("A", 100.0, [0.10] * 3),  # Only 3 years
                RevenueDriver("B", 200.0, [0.05] * 5),  # 5 years
            ],
            horizon_years=5,
        )
        with pytest.raises(ValueError, match="Driver 'A'"):
            resolve_driver_revenue(20000.0, tree)

    def test_four_driver_meta_model(self):
        """Realistic 4-driver model matching ad platform decomposition."""
        tree = RevenueDriverTree(
            entity_id="meta",
            sector_pack_id="sp_ad_platform_v1",
            decomposition_formula="Revenue = DAU × Sessions/DAU × Ads/Session × CPM/1000",
            drivers=[
                RevenueDriver("DAU", 3.3e9, [0.02] * 10, "billions"),
                RevenueDriver("Sessions_per_DAU", 6.5, [0.01] * 10, "sessions/day"),
                RevenueDriver("Ads_per_Session", 4.2, [0.03] * 10, "ads"),
                RevenueDriver("CPM", 11.5, [0.05] * 10, "$/1000"),
            ],
        )
        # base product = 3.3e9 * 6.5 * 4.2 * 11.5 = huge number
        # But actual revenue is ~160B, so scale factor handles the mismatch
        base_rev = 160e9
        growth, projections = resolve_driver_revenue(base_rev, tree)
        assert len(growth) == 10
        # Y1 growth ≈ (1.02 * 1.01 * 1.03 * 1.05) - 1 ≈ 0.1135
        expected_y1 = (1.02 * 1.01 * 1.03 * 1.05) - 1
        assert growth[0] == pytest.approx(expected_y1, abs=0.002)


# ============================================================
# apply_driver_deltas() Tests
# ============================================================

class TestApplyDriverDeltas:

    def _base_tree(self):
        return RevenueDriverTree(
            entity_id="meta",
            sector_pack_id="sp_test",
            decomposition_formula="Revenue = DAU × CPM",
            drivers=[
                RevenueDriver("DAU", 3.3e9, [0.02] * 10),
                RevenueDriver("CPM", 11.5, [0.05] * 10),
            ],
        )

    def test_bear_scenario_reduces_growth(self):
        tree = self._base_tree()
        deltas = {"CPM": [-0.03] * 10}  # CPM growth reduced by 3%
        bear_tree = apply_driver_deltas(tree, deltas)
        assert bear_tree.drivers[1].growth_path[0] == pytest.approx(0.02)  # 0.05 - 0.03

    def test_bull_scenario_increases_growth(self):
        tree = self._base_tree()
        deltas = {"DAU": [0.01] * 10, "CPM": [0.02] * 10}
        bull_tree = apply_driver_deltas(tree, deltas)
        assert bull_tree.drivers[0].growth_path[0] == pytest.approx(0.03)  # 0.02 + 0.01
        assert bull_tree.drivers[1].growth_path[0] == pytest.approx(0.07)  # 0.05 + 0.02

    def test_unmentioned_drivers_unchanged(self):
        tree = self._base_tree()
        deltas = {"CPM": [-0.03] * 10}
        adjusted = apply_driver_deltas(tree, deltas)
        # DAU should be unchanged
        assert adjusted.drivers[0].growth_path == tree.drivers[0].growth_path

    def test_empty_deltas_returns_equivalent(self):
        tree = self._base_tree()
        adjusted = apply_driver_deltas(tree, {})
        growth_orig, _ = resolve_driver_revenue(160e9, tree)
        growth_adj, _ = resolve_driver_revenue(160e9, adjusted)
        for g1, g2 in zip(growth_orig, growth_adj):
            assert g1 == pytest.approx(g2, abs=1e-8)

    def test_driver_delta_produces_different_revenue_growth(self):
        tree = self._base_tree()
        base_growth, _ = resolve_driver_revenue(160e9, tree)

        bear_tree = apply_driver_deltas(tree, {"CPM": [-0.04] * 10})
        bear_growth, _ = resolve_driver_revenue(160e9, bear_tree)

        # Bear growth should be lower than base
        for bg, bearig in zip(base_growth, bear_growth):
            assert bearig < bg

    def test_partial_delta_length_pads_with_original(self):
        tree = self._base_tree()
        # Delta only 5 years, tree has 10
        deltas = {"CPM": [-0.02] * 5}
        adjusted = apply_driver_deltas(tree, deltas)
        # First 5 years adjusted
        assert adjusted.drivers[1].growth_path[4] == pytest.approx(0.03)  # 0.05 - 0.02
        # Year 6+ unchanged
        assert adjusted.drivers[1].growth_path[5] == pytest.approx(0.05)


# ============================================================
# ScenarioCase driver_deltas Integration
# ============================================================

class TestScenarioCaseDriverDeltas:

    def test_scenario_case_with_driver_deltas(self):
        case = ScenarioCase(
            name="bear",
            probability=0.25,
            narrative="CPM pressure from privacy regulation",
            key_driver="CPM decline",
            revenue_growth_delta=[-0.04] * 10,
            margin_delta=[-0.02] * 10,
            driver_deltas={
                "DAU": [-0.005] * 10,
                "CPM": [-0.03] * 10,
            },
        )
        assert "DAU" in case.driver_deltas
        assert len(case.driver_deltas["CPM"]) == 10
        assert case.driver_deltas["CPM"][0] == -0.03

    def test_scenario_case_without_driver_deltas(self):
        """Backward compatibility — driver_deltas defaults to empty dict."""
        case = ScenarioCase(
            name="base",
            probability=0.50,
            narrative="Base case",
            key_driver="",
            revenue_growth_delta=[0.0] * 10,
            margin_delta=[0.0] * 10,
        )
        assert case.driver_deltas == {}

    def test_driver_deltas_override_aggregate(self):
        """When driver_deltas exist, they should produce a different growth path
        than the aggregate revenue_growth_delta."""
        tree = RevenueDriverTree(
            entity_id="meta",
            sector_pack_id="sp_test",
            decomposition_formula="Revenue = DAU × CPM",
            drivers=[
                RevenueDriver("DAU", 3.3e9, [0.02] * 10),
                RevenueDriver("CPM", 11.5, [0.05] * 10),
            ],
        )

        case = ScenarioCase(
            name="bear",
            probability=0.25,
            narrative="CPM pressure",
            key_driver="CPM",
            revenue_growth_delta=[-0.04] * 10,  # aggregate fallback
            margin_delta=[-0.02] * 10,
            driver_deltas={"CPM": [-0.03] * 10},  # only CPM drops
        )

        # Apply driver deltas
        adjusted_tree = apply_driver_deltas(tree, case.driver_deltas)
        driver_growth, _ = resolve_driver_revenue(160e9, adjusted_tree)

        # Compare to base
        base_growth, _ = resolve_driver_revenue(160e9, tree)

        # Driver-based bear growth should be less negative than aggregate -4%
        # because only CPM is affected, not DAU
        # Base Y1: (1.02 * 1.05) - 1 ≈ 0.071
        # Bear Y1: (1.02 * 1.02) - 1 ≈ 0.0404 (CPM growth reduced to 0.02)
        assert driver_growth[0] > base_growth[0] - 0.04
        assert driver_growth[0] < base_growth[0]


# ============================================================
# Sector Pack YAML Loading Tests
# ============================================================

class TestSectorPackYAML:

    def test_ad_platform_pack_loads(self):
        import yaml
        path = Path("configs/sector_packs/sp_ad_platform_v1.yaml")
        if not path.exists():
            pytest.skip("Sector pack YAML not found")
        with open(path) as f:
            pack = yaml.safe_load(f)
        assert pack["sector_pack_id"] == "sp_ad_platform_v1"
        decomp = pack["revenue_drivers"]["decomposition"]
        assert "DAU" in decomp["formula"]
        assert len(decomp["tree"]) >= 4
        for node in decomp["tree"]:
            assert "name" in node
            assert "base_value" in node
            assert node["base_value"] > 0

    def test_semiconductor_pack_loads(self):
        import yaml
        path = Path("configs/sector_packs/sp_semiconductor_v1.yaml")
        if not path.exists():
            pytest.skip("Sector pack YAML not found")
        with open(path) as f:
            pack = yaml.safe_load(f)
        assert pack["sector_pack_id"] == "sp_semiconductor_v1"
        decomp = pack["revenue_drivers"]["decomposition"]
        assert len(decomp["tree"]) >= 3


# ============================================================
# Orchestrator _build_driver_tree() Integration
# ============================================================

class TestBuildDriverTree:
    """Test orchestrator's driver tree construction from sector pack."""

    def test_build_from_sector_pack(self):
        from aegis.core.orchestrator.auto_research import AutoResearchOrchestrator
        orch = AutoResearchOrchestrator.__new__(AutoResearchOrchestrator)

        sector_pack = {
            "sector_pack_id": "sp_test",
            "revenue_drivers": {
                "decomposition": {
                    "formula": "Revenue = Units × Price",
                    "tree": [
                        {"name": "Units", "base_value": 1000, "near_growth": 0.10,
                         "long_growth": 0.03, "unit": "thousands"},
                        {"name": "Price", "base_value": 50, "near_growth": 0.05,
                         "long_growth": 0.02, "unit": "$/unit"},
                    ],
                }
            }
        }
        tree = orch._build_driver_tree(
            sector_pack, {"revenue": 50000}, {}, "test_entity",
        )
        assert tree is not None
        assert len(tree.drivers) == 2
        assert tree.drivers[0].name == "Units"
        assert tree.drivers[1].name == "Price"
        assert len(tree.drivers[0].growth_path) == 10
        # Near-term growth should be close to 0.10
        assert tree.drivers[0].growth_path[0] == pytest.approx(0.10, abs=0.01)
        # Long-term growth should converge toward 0.03
        assert tree.drivers[0].growth_path[9] == pytest.approx(0.03, abs=0.01)

    def test_no_revenue_drivers_returns_none(self):
        from aegis.core.orchestrator.auto_research import AutoResearchOrchestrator
        orch = AutoResearchOrchestrator.__new__(AutoResearchOrchestrator)

        tree = orch._build_driver_tree(
            {"sector_pack_id": "sp_generic"}, {"revenue": 50000}, {}, "test",
        )
        assert tree is None

    def test_single_driver_returns_none(self):
        """Need at least 2 drivers for decomposition to be meaningful."""
        from aegis.core.orchestrator.auto_research import AutoResearchOrchestrator
        orch = AutoResearchOrchestrator.__new__(AutoResearchOrchestrator)

        sector_pack = {
            "revenue_drivers": {
                "decomposition": {
                    "formula": "Revenue = Units",
                    "tree": [
                        {"name": "Units", "base_value": 1000, "near_growth": 0.10,
                         "long_growth": 0.03},
                    ],
                }
            }
        }
        tree = orch._build_driver_tree(sector_pack, {"revenue": 50000}, {}, "test")
        assert tree is None

    def test_driver_tree_integrates_into_dcf_input(self):
        """When driver tree is provided, _build_dcf_input should use it."""
        from aegis.core.orchestrator.auto_research import AutoResearchOrchestrator, ResearchConfig
        orch = AutoResearchOrchestrator.__new__(AutoResearchOrchestrator)

        sector_pack = {
            "sector_pack_id": "sp_test",
            "revenue_drivers": {
                "decomposition": {
                    "formula": "Revenue = Units × Price",
                    "tree": [
                        {"name": "Units", "base_value": 100, "near_growth": 0.10,
                         "long_growth": 0.03, "unit": "k"},
                        {"name": "Price", "base_value": 500, "near_growth": 0.05,
                         "long_growth": 0.02, "unit": "$"},
                    ],
                }
            },
        }
        config = ResearchConfig(ticker="TEST", period="FY2025")
        facts = {"revenue": 50_000_000_000, "diluted_shares": 2_000_000_000, "depreciation_amortization": 0}
        metrics = {"net_debt": 0, "operating_margin": 0.20, "capex_to_revenue": 0.05,
                   "dilution_rate": 0.02}
        market_data = {"current_price": 100, "market_cap": 200_000_000_000}

        tree = orch._build_driver_tree(sector_pack, facts, metrics, "test")
        dcf_input = orch._build_dcf_input(
            config, facts, metrics, market_data, sector_pack,
            driver_tree=tree,
        )

        # Revenue growth should come from drivers, not consensus/CAGR
        # Y1 growth ≈ (1.10 * 1.05) - 1 = 0.155
        assert dcf_input.revenue_growth_path[0] == pytest.approx(0.155, abs=0.005)


# ============================================================
# End-to-End: Driver Tree → DCF → Value
# ============================================================

class TestDriverTreeEndToEnd:

    def test_driver_dcf_produces_reasonable_value(self):
        """Full pipeline: driver tree → growth path → DCFInput → DCF value."""
        tree = RevenueDriverTree(
            entity_id="meta",
            sector_pack_id="sp_ad_platform_v1",
            decomposition_formula="Revenue = DAU × Sessions × Ads × CPM",
            drivers=[
                RevenueDriver("DAU", 3.3e9, [0.02] * 10),
                RevenueDriver("Sessions", 6.5, [0.01] * 10),
                RevenueDriver("Ads", 4.2, [0.03] * 10),
                RevenueDriver("CPM", 11.5, [0.05] * 10),
            ],
        )
        base_rev = 160e9
        growth_path, _ = resolve_driver_revenue(base_rev, tree)

        dcf_input = DCFInput(
            base_revenue=base_rev,
            revenue_growth_path=growth_path,
            operating_margin_path=[0.38] * 10,
            capex_to_revenue_path=[0.15] * 10,
            effective_tax_rate=0.15,
            nwc_to_revenue_delta=0.01,
            terminal_growth_rate=0.03,
            wacc=0.095,
            sbc_to_revenue=0.0,
            dilution_rate_annual=0.02,
            shares_outstanding=2.5e9,
            net_debt=-50e9,  # Net cash
            horizon_years=10,
        )
        engine = DCFEngine()
        output = engine.compute_dcf(dcf_input)
        assert output.per_share_value > 0
        assert output.per_share_value < 10000  # Sanity: not astronomical

    def test_bear_bull_via_driver_deltas(self):
        """Bear/bull via driver_deltas should produce different values."""
        tree = RevenueDriverTree(
            entity_id="test",
            sector_pack_id="sp_test",
            decomposition_formula="Revenue = Units × Price",
            drivers=[
                RevenueDriver("Units", 1000, [0.08] * 10),
                RevenueDriver("Price", 50, [0.03] * 10),
            ],
        )
        base_rev = 50000
        base_growth, _ = resolve_driver_revenue(base_rev, tree)

        bear_deltas = {"Units": [-0.03] * 10, "Price": [-0.02] * 10}
        bear_tree = apply_driver_deltas(tree, bear_deltas)
        bear_growth, _ = resolve_driver_revenue(base_rev, bear_tree)

        bull_deltas = {"Units": [0.02] * 10, "Price": [0.01] * 10}
        bull_tree = apply_driver_deltas(tree, bull_deltas)
        bull_growth, _ = resolve_driver_revenue(base_rev, bull_tree)

        # Verify ordering: bear < base < bull for every year
        for yr in range(10):
            assert bear_growth[yr] < base_growth[yr]
            assert bull_growth[yr] > base_growth[yr]

    def test_driver_vs_aggregate_delta_difference(self):
        """Driver-level deltas should produce different results than aggregate."""
        tree = RevenueDriverTree(
            entity_id="test",
            sector_pack_id="sp_test",
            decomposition_formula="Revenue = A × B × C",
            drivers=[
                RevenueDriver("A", 100, [0.10] * 10),
                RevenueDriver("B", 50, [0.05] * 10),
                RevenueDriver("C", 20, [0.02] * 10),
            ],
        )
        base_rev = 100000
        base_growth, _ = resolve_driver_revenue(base_rev, tree)

        # Scenario: only A drops by 5% — but aggregate delta would be spread
        driver_bear = apply_driver_deltas(tree, {"A": [-0.05] * 10})
        driver_growth, _ = resolve_driver_revenue(base_rev, driver_bear)

        # Aggregate approach: same total impact but spread uniformly
        aggregate_delta = [-0.05] * 10  # Would apply to all growth equally
        agg_growth = [g + d for g, d in zip(base_growth, aggregate_delta)]

        # They should be different — driver-level is more precise
        # Because only A is affected, B and C still grow normally
        assert driver_growth[0] != pytest.approx(agg_growth[0], abs=0.001)
