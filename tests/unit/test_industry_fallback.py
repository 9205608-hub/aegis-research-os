"""Regression tests for the A-share industry fallback chain (AUDIT 2026-07).

Root cause fixed: push2.eastmoney.com (stock_individual_info_em) is
unreachable behind the CN proxy bypass, so any A-share missing from the
orchestrator's NAME_FRAGMENT_TO_INDUSTRY whitelist collapsed to the
General sector pack (Cambricon v2: 25× DCF miscalibration; Kangda
002669 smoke 2026-07-10 reproduced it). The connector now falls back to
the eastmoney datacenter F10 API (different host, verified reachable)
before the name-fragment heuristic gets a say.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from aegis.core.acquisition.connectors.akshare_connector import AkShareConnector
from aegis.core.orchestrator.auto_research import AutoResearchOrchestrator


def _fake_response(payload):
    return SimpleNamespace(json=lambda: payload)


class TestFetchIndustryF10:
    """AkShareConnector._fetch_industry_f10 — datacenter F10 lookup."""

    _PAYLOAD = {
        "result": {
            "data": [
                {
                    "SECUCODE": "002669.SZ",
                    "SECURITY_NAME_ABBR": "康达新材",
                    "EM2016": "基础化工-化学制品-其他化学制品",
                    "INDUSTRYCSRC1": "制造业-化学原料和化学制品制造业",
                }
            ]
        }
    }

    def test_parses_em2016_industry(self):
        with patch("requests.get", return_value=_fake_response(self._PAYLOAD)) as mock_get:
            out = AkShareConnector._fetch_industry_f10("002669")
        assert out is not None
        assert out["industry"] == "基础化工-化学制品-其他化学制品"
        assert out["industry_csrc"] == "制造业-化学原料和化学制品制造业"
        assert out["name"] == "康达新材"
        # SECUCODE suffix derived from the leading digit (0/3 → SZ, 6 → SH)
        assert 'SECUCODE="002669.SZ"' in mock_get.call_args.kwargs["params"]["filter"]

    def test_sh_prefix_for_shanghai_codes(self):
        with patch("requests.get", return_value=_fake_response(self._PAYLOAD)) as mock_get:
            AkShareConnector._fetch_industry_f10("600519")
        assert 'SECUCODE="600519.SH"' in mock_get.call_args.kwargs["params"]["filter"]

    def test_falls_back_to_csrc_when_em2016_empty(self):
        payload = {
            "result": {
                "data": [
                    {
                        "SECURITY_NAME_ABBR": "某公司",
                        "EM2016": "",
                        "INDUSTRYCSRC1": "制造业-专用设备制造业",
                    }
                ]
            }
        }
        with patch("requests.get", return_value=_fake_response(payload)):
            out = AkShareConnector._fetch_industry_f10("300000")
        assert out is not None
        assert out["industry"] == "制造业-专用设备制造业"

    def test_none_on_empty_result(self):
        with patch("requests.get", return_value=_fake_response({"result": None})):
            assert AkShareConnector._fetch_industry_f10("002669") is None

    def test_none_on_network_error_never_raises(self):
        with patch("requests.get", side_effect=OSError("proxy blackhole")):
            assert AkShareConnector._fetch_industry_f10("002669") is None


class TestSectorPackFromF10Industry:
    """The 3-level EM2016 strings must keep working with the orchestrator's
    substring keyword matcher (same taxonomy family as push2's 行业 field)."""

    @staticmethod
    def _infer(industry: str):
        # Pure class-attribute lookup — no orchestrator instance needed.
        return AutoResearchOrchestrator._infer_sector_pack_from_industry(
            AutoResearchOrchestrator, industry
        )

    def test_semiconductor_three_level_string_hits_pack(self):
        assert self._infer("电子-半导体-数字芯片设计") == "sp_semiconductor_v1"

    def test_pharma_three_level_string_hits_pack(self):
        assert self._infer("医药生物-化学制药-原料药") == "sp_pharma_cn_v1"

    def test_chemicals_maps_to_general(self):
        # No chemicals pack exists — General is the intended fallback for
        # Kangda-style specialty-materials names, not a resolution failure.
        assert self._infer("基础化工-化学制品-其他化学制品") is None

    def test_empty_maps_to_general(self):
        assert self._infer("") is None


class TestBackfillQuoteGaps:
    """AkShareConnector._backfill_quote_gaps — price-only Method 2 must not
    leave market_cap=0 downstream (Kangda 002669 run 2026-07-10)."""

    @staticmethod
    def _quote(**kw):
        base = dict(code="002669", name="康达新材", current_price=13.73,
                    market_cap=4.2e9, shares_outstanding=3.03e8)
        base.update(kw)
        return SimpleNamespace(**base)

    def test_price_only_gets_shares_and_cap(self):
        md = {"current_price": 13.73}
        info = {}
        with patch(
            "aegis.core.acquisition.connectors.tencent_sina_quote.fetch_cn_quote",
            return_value=self._quote(),
        ):
            name = AkShareConnector._backfill_quote_gaps("002669", md, None, info)
        assert md["total_shares"] == 3.03e8
        assert md["market_cap"] == 4.2e9
        assert md["current_price"] == 13.73  # existing price untouched
        assert name == "康达新材"

    def test_zero_cap_from_method1_is_treated_as_missing(self):
        # Method 1 writes `_safe_float(...) or 0.0` — a literal 0.0 must be
        # backfilled, not treated as present.
        md = {"current_price": 13.73, "total_shares": 0.0, "market_cap": 0.0}
        with patch(
            "aegis.core.acquisition.connectors.tencent_sina_quote.fetch_cn_quote",
            return_value=self._quote(),
        ):
            AkShareConnector._backfill_quote_gaps("002669", md, "康达新材", {})
        assert md["market_cap"] == 4.2e9
        assert md["total_shares"] == 3.03e8

    def test_noop_when_all_present(self):
        md = {"current_price": 13.9, "total_shares": 3e8, "market_cap": 4.2e9}
        with patch(
            "aegis.core.acquisition.connectors.tencent_sina_quote.fetch_cn_quote",
            side_effect=AssertionError("must not be called"),
        ):
            AkShareConnector._backfill_quote_gaps("002669", md, "康达新材", {})
        assert md == {"current_price": 13.9, "total_shares": 3e8, "market_cap": 4.2e9}

    def test_dead_quote_source_never_raises(self):
        md = {"current_price": 13.73}
        with patch(
            "aegis.core.acquisition.connectors.tencent_sina_quote.fetch_cn_quote",
            side_effect=OSError("network down"),
        ):
            name = AkShareConnector._backfill_quote_gaps("002669", md, None, {})
        assert name is None
        assert "market_cap" not in md
