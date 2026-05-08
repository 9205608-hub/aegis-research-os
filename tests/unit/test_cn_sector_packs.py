"""Tests for A-share specific sector packs.

Round 26 — A股专属 Sector Packs.
Tests cover:
- YAML file loading and schema validation
- Revenue driver tree completeness
- TICKER_SECTOR_MAP A-share mappings
- _load_sector_pack() integration
- _build_driver_tree() for CN packs
"""

import pytest
from pathlib import Path

import yaml


SECTOR_PACKS_DIR = Path("configs/sector_packs")

CN_PACKS = [
    "sp_baijiu_cn_v1",
    "sp_banking_cn_v1",
    "sp_new_energy_cn_v1",
    "sp_pharma_cn_v1",
]

# Required top-level fields for every sector pack
REQUIRED_FIELDS = [
    "sector_pack_id",
    "sector_name",
    "version",
    "key_kpis",
    "cycle_characteristics",
    "competitive_dynamics",
    "valuation_framework",
    "cost_structure",
    "revenue_drivers",
    "accounting_considerations",
]


# ============================================================
# YAML File Existence
# ============================================================

class TestCNPackFilesExist:

    @pytest.mark.parametrize("pack_id", CN_PACKS)
    def test_yaml_file_exists(self, pack_id):
        path = SECTOR_PACKS_DIR / f"{pack_id}.yaml"
        assert path.exists(), f"{pack_id}.yaml not found in {SECTOR_PACKS_DIR}"

    @pytest.mark.parametrize("pack_id", CN_PACKS)
    def test_yaml_file_parseable(self, pack_id):
        path = SECTOR_PACKS_DIR / f"{pack_id}.yaml"
        with open(path) as f:
            data = yaml.safe_load(f)
        assert isinstance(data, dict)
        assert data.get("sector_pack_id") == pack_id


# ============================================================
# Schema Validation
# ============================================================

class TestCNPackSchema:

    @pytest.fixture(params=CN_PACKS)
    def pack_data(self, request):
        path = SECTOR_PACKS_DIR / f"{request.param}.yaml"
        with open(path) as f:
            return yaml.safe_load(f)

    def test_required_fields_present(self, pack_data):
        for field in REQUIRED_FIELDS:
            assert field in pack_data, f"Missing required field: {field}"

    def test_key_kpis_is_list(self, pack_data):
        kpis = pack_data["key_kpis"]
        assert isinstance(kpis, list)
        assert len(kpis) >= 3, "At least 3 key KPIs expected"

    def test_cycle_characteristics_has_cyclicality(self, pack_data):
        cc = pack_data["cycle_characteristics"]
        assert "cyclicality" in cc
        assert cc["cyclicality"] in (
            "very_low", "low", "low_to_moderate", "moderate", "high", "very_high"
        )

    def test_valuation_framework_has_margins(self, pack_data):
        vf = pack_data["valuation_framework"]
        assert "typical_operating_margin_range" in vf
        margins = vf["typical_operating_margin_range"]
        assert len(margins) == 2
        assert margins[0] < margins[1]

    def test_cost_structure_has_typical_margins(self, pack_data):
        cs = pack_data["cost_structure"]
        assert "typical_margins" in cs
        tm = cs["typical_margins"]
        assert "gross_margin" in tm
        assert "operating_margin" in tm

    def test_accounting_considerations_is_list(self, pack_data):
        ac = pack_data["accounting_considerations"]
        assert isinstance(ac, list)
        assert len(ac) >= 3


# ============================================================
# Revenue Driver Tree Validation
# ============================================================

class TestCNPackRevenueDrivers:

    @pytest.fixture(params=CN_PACKS)
    def pack_data(self, request):
        path = SECTOR_PACKS_DIR / f"{request.param}.yaml"
        with open(path) as f:
            return yaml.safe_load(f)

    def test_revenue_drivers_has_decomposition(self, pack_data):
        rd = pack_data["revenue_drivers"]
        assert "decomposition" in rd

    def test_decomposition_has_formula_and_tree(self, pack_data):
        decomp = pack_data["revenue_drivers"]["decomposition"]
        assert "formula" in decomp
        assert "tree" in decomp
        assert isinstance(decomp["formula"], str)
        assert len(decomp["formula"]) > 10

    def test_tree_has_enough_drivers(self, pack_data):
        tree = pack_data["revenue_drivers"]["decomposition"]["tree"]
        assert isinstance(tree, list)
        assert len(tree) >= 3, f"Need at least 3 drivers, got {len(tree)}"

    def test_driver_node_fields(self, pack_data):
        tree = pack_data["revenue_drivers"]["decomposition"]["tree"]
        required_driver_fields = ["name", "unit", "base_value", "near_growth", "long_growth", "growth_driver"]
        for node in tree:
            for field in required_driver_fields:
                assert field in node, f"Driver '{node.get('name', '?')}' missing field: {field}"

    def test_driver_base_values_positive(self, pack_data):
        tree = pack_data["revenue_drivers"]["decomposition"]["tree"]
        for node in tree:
            # base_value should be positive (or zero for indices that start at 1.0)
            assert node["base_value"] >= 0, f"Driver '{node['name']}' has negative base_value"

    def test_driver_growth_rates_reasonable(self, pack_data):
        tree = pack_data["revenue_drivers"]["decomposition"]["tree"]
        for node in tree:
            ng = node["near_growth"]
            lg = node["long_growth"]
            # Growth rates should be between -50% and +100%
            assert -0.5 <= ng <= 1.0, f"Driver '{node['name']}' near_growth {ng} out of range"
            assert -0.5 <= lg <= 1.0, f"Driver '{node['name']}' long_growth {lg} out of range"


# ============================================================
# TICKER_SECTOR_MAP Mappings
# ============================================================

class TestTickerSectorMap:

    def test_baijiu_tickers_mapped(self):
        from aegis.core.orchestrator.auto_research import AutoResearchOrchestrator
        tsm = AutoResearchOrchestrator.TICKER_SECTOR_MAP
        assert tsm.get("600519") == "sp_baijiu_cn_v1"  # 茅台
        assert tsm.get("000858") == "sp_baijiu_cn_v1"  # 五粮液

    def test_banking_cn_tickers_mapped(self):
        from aegis.core.orchestrator.auto_research import AutoResearchOrchestrator
        tsm = AutoResearchOrchestrator.TICKER_SECTOR_MAP
        assert tsm.get("600036") == "sp_banking_cn_v1"  # 招商银行
        assert tsm.get("601398") == "sp_banking_cn_v1"  # 工商银行

    def test_new_energy_tickers_mapped(self):
        from aegis.core.orchestrator.auto_research import AutoResearchOrchestrator
        tsm = AutoResearchOrchestrator.TICKER_SECTOR_MAP
        assert tsm.get("300750") == "sp_new_energy_cn_v1"  # 宁德时代
        assert tsm.get("002594") == "sp_new_energy_cn_v1"  # 比亚迪

    def test_pharma_cn_tickers_mapped(self):
        from aegis.core.orchestrator.auto_research import AutoResearchOrchestrator
        tsm = AutoResearchOrchestrator.TICKER_SECTOR_MAP
        assert tsm.get("600276") == "sp_pharma_cn_v1"  # 恒瑞
        assert tsm.get("300015") == "sp_pharma_cn_v1"  # 爱尔眼科

    def test_us_banking_still_uses_us_pack(self):
        """Ensure US banking tickers still map to US pack, not CN pack."""
        from aegis.core.orchestrator.auto_research import AutoResearchOrchestrator
        tsm = AutoResearchOrchestrator.TICKER_SECTOR_MAP
        assert tsm.get("JPM") == "sp_banking_v1"
        assert tsm.get("BAC") == "sp_banking_v1"


# ============================================================
# Pack Loading Integration
# ============================================================

class TestPackLoading:

    @pytest.mark.parametrize("pack_id", CN_PACKS)
    def test_load_sector_pack(self, pack_id):
        from aegis.core.orchestrator.auto_research import AutoResearchOrchestrator
        orch = AutoResearchOrchestrator.__new__(AutoResearchOrchestrator)
        pack = orch._load_sector_pack(pack_id)
        assert pack.get("sector_pack_id") == pack_id
        assert "revenue_drivers" in pack

    @pytest.mark.parametrize("ticker,expected_pack", [
        ("600519", "sp_baijiu_cn_v1"),
        ("300750", "sp_new_energy_cn_v1"),
        ("600036", "sp_banking_cn_v1"),
        ("600276", "sp_pharma_cn_v1"),
    ])
    def test_load_by_ticker(self, ticker, expected_pack):
        from aegis.core.orchestrator.auto_research import AutoResearchOrchestrator
        orch = AutoResearchOrchestrator.__new__(AutoResearchOrchestrator)
        pack = orch._load_sector_pack(None, ticker=ticker)
        assert pack.get("sector_pack_id") == expected_pack
