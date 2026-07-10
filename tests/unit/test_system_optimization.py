"""Tests for Round 24 system optimization.

Covers:
- All 11 sector packs load and validate
- A-share OpenBB guard prevents US-only calls
- Report currency symbol propagation
- A-share peer mapping coverage
"""

import pytest
import yaml
from pathlib import Path

from aegis.core.orchestrator.auto_research import AutoResearchOrchestrator


# ============================================================
# Sector Pack Completeness Tests
# ============================================================

class TestSectorPackCompleteness:
    """All referenced sector packs must exist and be valid."""

    EXPECTED_PACKS = [
        "sp_ad_platform_v1",
        "sp_semiconductor_v1",
        "sp_saas_v1",
        "sp_banking_v1",
        "sp_biotech_pharma_v1",
        "sp_consumer_electronics_v1",
        "sp_energy_v1",
        "sp_industrial_v1",
        "sp_reits_v1",
        "sp_ecommerce_v1",
        "sp_consumer_staples_v1",
    ]

    def test_all_packs_exist(self):
        pack_dir = Path("configs/sector_packs")
        for pack_id in self.EXPECTED_PACKS:
            path = pack_dir / f"{pack_id}.yaml"
            assert path.exists(), f"Missing sector pack: {pack_id}"

    def test_all_packs_parse(self):
        pack_dir = Path("configs/sector_packs")
        for pack_id in self.EXPECTED_PACKS:
            path = pack_dir / f"{pack_id}.yaml"
            with open(path) as f:
                pack = yaml.safe_load(f)
            assert pack["sector_pack_id"] == pack_id
            assert "sector_name" in pack
            assert "key_kpis" in pack
            assert len(pack["key_kpis"]) >= 3

    def test_all_packs_have_revenue_drivers(self):
        """Every sector pack should have revenue driver decomposition."""
        pack_dir = Path("configs/sector_packs")
        for pack_id in self.EXPECTED_PACKS:
            path = pack_dir / f"{pack_id}.yaml"
            with open(path) as f:
                pack = yaml.safe_load(f)
            rd = pack.get("revenue_drivers", {})
            decomp = rd.get("decomposition", {})
            assert decomp.get("formula"), f"{pack_id} missing revenue driver formula"
            tree = decomp.get("tree", [])
            assert len(tree) >= 2, f"{pack_id} needs at least 2 driver nodes"

    def test_driver_nodes_have_required_fields(self):
        pack_dir = Path("configs/sector_packs")
        for pack_id in self.EXPECTED_PACKS:
            path = pack_dir / f"{pack_id}.yaml"
            with open(path) as f:
                pack = yaml.safe_load(f)
            tree = pack.get("revenue_drivers", {}).get("decomposition", {}).get("tree", [])
            for node in tree:
                assert "name" in node, f"{pack_id}: node missing 'name'"
                assert "base_value" in node, f"{pack_id}/{node.get('name')}: missing 'base_value'"
                assert node["base_value"] > 0, f"{pack_id}/{node['name']}: base_value must be > 0"

    def test_all_mapped_tickers_have_packs(self):
        """Every ticker in TICKER_SECTOR_MAP should reference an existing pack."""
        orch = AutoResearchOrchestrator.__new__(AutoResearchOrchestrator)
        pack_dir = Path("configs/sector_packs")
        missing = []
        for ticker, pack_id in orch.TICKER_SECTOR_MAP.items():
            path = pack_dir / f"{pack_id}.yaml"
            if not path.exists():
                missing.append(f"{ticker} → {pack_id}")
        assert len(missing) == 0, f"Tickers with missing packs: {missing}"

    def test_valuation_framework_ranges(self):
        """Margin and growth ranges should be realistic."""
        pack_dir = Path("configs/sector_packs")
        for pack_id in self.EXPECTED_PACKS:
            path = pack_dir / f"{pack_id}.yaml"
            with open(path) as f:
                pack = yaml.safe_load(f)
            vf = pack.get("valuation_framework", {})
            om_range = vf.get("typical_operating_margin_range", [])
            if om_range:
                assert len(om_range) == 2, f"{pack_id}: OM range needs [low, high]"
                assert om_range[0] < om_range[1]
                assert om_range[0] >= -0.2  # Some sectors (biotech pre-revenue) can be negative
                assert om_range[1] <= 0.80


# ============================================================
# A-share OpenBB Guard Tests
# ============================================================

class TestAShareOpenBBGuard:
    """Verify A-share pipeline gets yfinance data but skips US-only features."""

    def test_a_share_gets_consensus_via_yfinance(self):
        """A-share research should get consensus estimates via yfinance (not skipped)."""
        import inspect
        src = inspect.getsource(AutoResearchOrchestrator.run)
        # Consensus block should be under `if config.enable_openbb:` (not `not is_a_share`)
        # and use yf_symbol for A-share ticker conversion
        assert "yf_symbol" in src
        assert "_to_yfinance_symbol" in src

    def test_a_share_skips_us_only_features(self):
        """A-share should skip price target and FRED macro (US-only)."""
        import inspect
        src = inspect.getsource(AutoResearchOrchestrator.run)
        # Price target and FRED macro should still have `not is_a_share` guard
        idx = src.index("US-only features")
        nearby = src[idx:idx+300]
        assert "not is_a_share" in nearby

    def test_a_share_skips_earnings_call(self):
        """A-share should not attempt FMP earnings call fetch."""
        import inspect
        src = inspect.getsource(AutoResearchOrchestrator.run)
        idx = src.index("Earnings Call Transcript")
        nearby = src[idx:idx+200]
        assert "not is_a_share" in nearby

    def test_to_yfinance_symbol_conversion(self):
        """Verify A-share ticker conversion to yfinance format."""
        assert AutoResearchOrchestrator._to_yfinance_symbol("600519") == "600519.SS"
        assert AutoResearchOrchestrator._to_yfinance_symbol("000858") == "000858.SZ"
        assert AutoResearchOrchestrator._to_yfinance_symbol("300750") == "300750.SZ"
        assert AutoResearchOrchestrator._to_yfinance_symbol("688981") == "688981.SS"
        assert AutoResearchOrchestrator._to_yfinance_symbol("AAPL") == "AAPL"
        assert AutoResearchOrchestrator._to_yfinance_symbol("600519.SS") == "600519.SS"


# ============================================================
# A-share Peer Mapping Tests
# ============================================================

class TestASharePeerMapping:

    def test_major_a_shares_have_peers(self):
        from aegis.core.acquisition.connectors.openbb_connector import OpenBBConnector
        conn = OpenBBConnector()
        for code in ["600519", "000858", "601318", "300750", "002594", "688981"]:
            peers = conn.get_sector_peers(code, limit=5)
            assert len(peers) >= 3, f"A-share {code} has too few peers: {peers}"

    def test_peer_format_is_yfinance(self):
        """A-share peers should use .SS/.SZ format for yfinance compatibility."""
        from aegis.core.acquisition.connectors.openbb_connector import OpenBBConnector
        conn = OpenBBConnector()
        peers = conn.get_sector_peers("600519")
        for p in peers:
            assert p.endswith(".SS") or p.endswith(".SZ"), \
                f"A-share peer {p} should use .SS/.SZ suffix"


# ============================================================
# Report Currency Symbol Tests
# ============================================================

class TestReportCurrencySymbol:
    """AUDIT-E3 (2026-07): this suite previously compared two hardcoded
    literal dicts against each other and never called production code —
    it could not fail. It now exercises the real currency derivation used
    by the v2 renderer (_detect_market / _resolve_display_ctx)."""

    def test_symbol_follows_scenarios_currency(self):
        from aegis.core.reports.html_report_v2 import _detect_market
        expected = {"USD": "$", "CNY": "¥", "EUR": "€", "GBP": "£",
                    "JPY": "¥", "HKD": "HK$"}
        for code, sym in expected.items():
            currency, symbol, _market = _detect_market("XYZ", {"currency": code})
            assert currency == code
            assert symbol == sym

    def test_entity_id_heuristic(self):
        """No currency metadata → 6-digit numeric id = A-share (CNY)."""
        from aegis.core.reports.html_report_v2 import _detect_market
        assert _detect_market("600519", None) == ("CNY", "¥", "CN")
        assert _detect_market("301358.SZ", None) == ("CNY", "¥", "CN")
        assert _detect_market("AAPL", None) == ("USD", "$", "US")

    def test_meta_facts_display_takes_precedence(self):
        """Filing-level currency (fact_bridge __display) beats scenarios."""
        from aegis.core.reports.html_report_v2 import _detect_market
        meta = {"__display": {"currency": "USD"}}
        assert _detect_market("600519", {"currency": "CNY"}, meta)[0] == "USD"

    def test_display_ctx_units(self):
        """CNY renders in 亿 (1e8); USD in B (1e9)."""
        from aegis.core.reports.html_report_v2 import _resolve_display_ctx
        cny = _resolve_display_ctx(None, "CNY")
        assert cny["unit"] == "亿"
        assert cny["scale"] == 1e8
        usd = _resolve_display_ctx(None, "USD")
        assert usd["unit"] == "B"
        assert usd["scale"] == 1e9
