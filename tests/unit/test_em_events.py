"""Tests for the A-share recent-events slice connector (em_events_connector).

DESIGN_2.0 Phase 0 task B coverage:
- 业绩预告 parsing: forecast type / value range / notice date / latest-period filter
- 公告流: date-desc ordering, 90-day window filter, 30-row cap, cninfo fallback
- 一致预期: 设计红线 5 coverage gate (≥3 orgs and ≤90 days) in all directions
- graceful degradation when every source is down
- to_prompt_block Chinese formatting + fixed disclaimer header
- live network smoke test for 002669 (skipped unless AEGIS_RUN_NETWORK_TESTS=1)
"""

from __future__ import annotations

from datetime import date

import pytest

from conftest import require_network
from aegis.core.acquisition.connectors import em_events_connector as em
from aegis.core.acquisition.connectors.em_events_connector import (
    Announcement,
    ConsensusForecast,
    ConsensusYear,
    EarningsForecast,
    RecentEvents,
    fetch_announcements,
    fetch_consensus,
    fetch_earnings_forecasts,
    fetch_recent_events,
)

AS_OF = date(2026, 7, 10)


# ============================================================
# HTTP stubbing helpers
# ============================================================

class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _dc(rows):
    """eastmoney datacenter envelope."""
    return {"result": {"data": rows, "pages": 1}, "success": True}


YJYG_ROW_KOUFEI = {
    "SECUCODE": "002669.SZ",
    "SECURITY_CODE": "002669",
    "NOTICE_DATE": "2026-01-21 00:00:00",
    "REPORT_DATE": "2025-12-31 00:00:00",
    "PREDICT_FINANCE": "扣除非经常性损益后的净利润",
    "PREDICT_AMT_LOWER": 21000000,
    "PREDICT_AMT_UPPER": 31000000,
    "ADD_AMP_LOWER": 106.81,
    "ADD_AMP_UPPER": 110.06,
    "PREDICT_TYPE": "扭亏",
    "PREYEAR_SAME_PERIOD": -308264200,
    "IS_LATEST": "T",
}

YJYG_ROW_OLD_PERIOD = {
    **YJYG_ROW_KOUFEI,
    "NOTICE_DATE": "2025-07-12 00:00:00",
    "REPORT_DATE": "2025-06-30 00:00:00",
    "PREDICT_TYPE": "预减",
    "PREDICT_AMT_LOWER": -5000000,
    "PREDICT_AMT_UPPER": -1000000,
}

YJYG_ROW_STALE_REVISION = {
    **YJYG_ROW_KOUFEI,
    "IS_LATEST": "F",
    "PREDICT_AMT_LOWER": 1.0,
    "PREDICT_AMT_UPPER": 2.0,
}


def _ann_row(title, dt, cols=("董事会决议公告",)):
    return {
        "title": title,
        "notice_date": f"{dt} 00:00:00",
        "columns": [{"column_name": c} for c in cols],
    }


def _install_get(monkeypatch, handler):
    """Route requests.get through *handler(url, params) -> payload dict*."""
    import requests

    def fake_get(url, params=None, headers=None, timeout=None, **kw):
        assert timeout is not None and timeout <= 10, "timeout must be ≤10s"
        return FakeResponse(handler(url, params or {}))

    monkeypatch.setattr(requests, "get", fake_get)


def _install_post(monkeypatch, handler):
    import requests

    def fake_post(url, data=None, headers=None, timeout=None, **kw):
        assert timeout is not None and timeout <= 10, "timeout must be ≤10s"
        return FakeResponse(handler(url, data or {}))

    monkeypatch.setattr(requests, "post", fake_post)


def _boom(monkeypatch):
    """Make every HTTP verb raise — simulates all sources down."""
    import requests

    def die(*a, **kw):
        raise ConnectionError("network down")

    monkeypatch.setattr(requests, "get", die)
    monkeypatch.setattr(requests, "post", die)


# ============================================================
# 业绩预告 parsing
# ============================================================

class TestEarningsForecasts:

    def test_parses_range_type_and_dates(self, monkeypatch):
        _install_get(monkeypatch, lambda url, p: _dc([YJYG_ROW_KOUFEI]))
        out = fetch_earnings_forecasts("002669")
        assert len(out) == 1
        f = out[0]
        assert f.forecast_type == "扭亏"
        assert f.indicator == "扣除非经常性损益后的净利润"
        assert f.value_low == 21000000
        assert f.value_high == 31000000
        assert f.change_pct_low == pytest.approx(106.81)
        assert f.notice_date == "2026-01-21"
        assert f.report_period == "2025-12-31"
        assert f.prev_year_value == pytest.approx(-308264200)

    def test_keeps_only_newest_report_period(self, monkeypatch):
        _install_get(monkeypatch,
                     lambda url, p: _dc([YJYG_ROW_KOUFEI, YJYG_ROW_OLD_PERIOD]))
        out = fetch_earnings_forecasts("002669")
        assert len(out) == 1
        assert out[0].report_period == "2025-12-31"

    def test_prefers_is_latest_rows(self, monkeypatch):
        _install_get(monkeypatch,
                     lambda url, p: _dc([YJYG_ROW_STALE_REVISION, YJYG_ROW_KOUFEI]))
        out = fetch_earnings_forecasts("002669")
        assert len(out) == 1
        assert out[0].value_low == 21000000  # not the stale 1.0 revision

    def test_filter_uses_bare_security_code(self, monkeypatch):
        seen = {}

        def handler(url, p):
            seen.update(p)
            return _dc([])

        _install_get(monkeypatch, handler)
        fetch_earnings_forecasts("002669.SZ")
        assert seen["filter"] == '(SECURITY_CODE="002669")'
        assert seen["reportName"] == "RPT_PUBLIC_OP_NEWPREDICT"

    def test_http_failure_returns_empty(self, monkeypatch):
        _boom(monkeypatch)
        assert fetch_earnings_forecasts("002669") == []


# ============================================================
# 公告流: ordering / window / cap / fallback
# ============================================================

class TestAnnouncements:

    def test_sorted_desc_and_window_filtered(self, monkeypatch):
        rows = [
            _ann_row("旧公告-超窗口", "2026-03-01"),   # >90d before as_of → dropped
            _ann_row("公告A", "2026-06-18"),
            _ann_row("公告B", "2026-07-02"),
            _ann_row("公告C", "2026-05-11"),
        ]

        def handler(url, p):
            assert "np-anotice" in url
            return {"data": {"list": rows}, "success": 1}

        _install_get(monkeypatch, handler)
        out = fetch_announcements("002669", days=90, as_of=AS_OF)
        assert [a.title for a in out] == ["公告B", "公告A", "公告C"]
        assert out[0].date == "2026-07-02"
        assert out[0].category == "董事会决议公告"
        assert all(a.source == "eastmoney" for a in out)

    def test_caps_at_30(self, monkeypatch):
        rows = [_ann_row(f"公告{i}", "2026-07-01") for i in range(45)]
        _install_get(monkeypatch, lambda url, p: {"data": {"list": rows}})
        out = fetch_announcements("002669", as_of=AS_OF)
        assert len(out) == 30

    def test_cninfo_fallback_when_em_fails(self, monkeypatch):
        import requests

        def fake_get(*a, **kw):
            raise ConnectionError("em down")

        def fake_post(url, data=None, headers=None, timeout=None, **kw):
            assert "cninfo" in url
            assert data["stock"] == "002669,"
            assert data["column"] == "szse"
            return FakeResponse({"announcements": [
                {"announcementTitle": "康达新材:<em>重组</em>进展公告",
                 # 2026-06-30 00:00 local (UTC+8) → epoch ms, inside 90d window
                 "announcementTime": 1782748800000},
            ]})

        monkeypatch.setattr(requests, "get", fake_get)
        monkeypatch.setattr(requests, "post", fake_post)
        out = fetch_announcements("002669", as_of=AS_OF)
        assert len(out) == 1
        assert out[0].title == "康达新材:重组进展公告"  # <em> tags stripped
        assert out[0].source == "cninfo"

    def test_cninfo_uses_sse_column_for_shanghai(self, monkeypatch):
        import requests
        seen = {}

        monkeypatch.setattr(requests, "get",
                            lambda *a, **kw: (_ for _ in ()).throw(ConnectionError()))

        def fake_post(url, data=None, **kw):
            seen.update(data)
            return FakeResponse({"announcements": []})

        monkeypatch.setattr(requests, "post", fake_post)
        fetch_announcements("600519", as_of=AS_OF)
        assert seen["column"] == "sse"

    def test_both_sources_down_returns_empty(self, monkeypatch):
        _boom(monkeypatch)
        assert fetch_announcements("002669", as_of=AS_OF) == []


# ============================================================
# 一致预期: 设计红线 5 coverage gate
# ============================================================

def _consensus_handler(org_count, latest_report, predictions_rows):
    def handler(url, p):
        if "reportapi" in url:
            data = ([{"publishDate": f"{latest_report} 00:00:00.000"}]
                    if latest_report else [])
            return {"data": data, "hits": len(data)}
        rn = p.get("reportName", "")
        if rn == "RPT_WEB_RESPREDICT":
            return _dc([{"RATING_ORG_NUM": org_count}])
        if rn == "RPT_RES_PROFITPREDICT":
            return _dc(predictions_rows)
        raise AssertionError(f"unexpected call: {url} {rn}")
    return handler


PREDICT_ROWS = [
    {"PREDICT_YEAR": 2027, "EPS": 0.66, "PARENT_NETPROFIT": 199850000,
     "TOTAL_OPERATE_INCOME": 6538850000},
    {"PREDICT_YEAR": 2026, "EPS": 0.43, "PARENT_NETPROFIT": 130030000,
     "TOTAL_OPERATE_INCOME": 5839400000},
]


class TestConsensusGate:

    def test_sufficient_coverage_passes_gate(self, monkeypatch):
        _install_get(monkeypatch,
                     _consensus_handler(5, "2026-06-20", PREDICT_ROWS))
        c = fetch_consensus("002669", as_of=AS_OF)
        assert c is not None
        assert c.insufficient_coverage is False
        assert c.org_count == 5
        assert c.latest_report_date == "2026-06-20"
        # predictions sorted by year ascending
        assert [p.year for p in c.predictions] == [2026, 2027]
        assert c.predictions[0].eps == pytest.approx(0.43)

    def test_too_few_orgs_fails_gate(self, monkeypatch):
        _install_get(monkeypatch,
                     _consensus_handler(1, "2026-06-20", PREDICT_ROWS))
        c = fetch_consensus("002669", as_of=AS_OF)
        assert c is not None
        assert c.insufficient_coverage is True
        assert c.org_count == 1

    def test_exactly_three_orgs_passes(self, monkeypatch):
        _install_get(monkeypatch,
                     _consensus_handler(3, "2026-06-20", PREDICT_ROWS))
        c = fetch_consensus("002669", as_of=AS_OF)
        assert c.insufficient_coverage is False

    def test_stale_report_fails_gate(self, monkeypatch):
        # 2026-03-01 is 131 days before 2026-07-10 → stale (>90d)
        _install_get(monkeypatch,
                     _consensus_handler(5, "2026-03-01", PREDICT_ROWS))
        c = fetch_consensus("002669", as_of=AS_OF)
        assert c.insufficient_coverage is True

    def test_no_report_at_all_fails_gate(self, monkeypatch):
        _install_get(monkeypatch, _consensus_handler(5, None, PREDICT_ROWS))
        c = fetch_consensus("002669", as_of=AS_OF)
        assert c.insufficient_coverage is True
        assert c.latest_report_date is None

    def test_no_predictions_fails_gate(self, monkeypatch):
        _install_get(monkeypatch, _consensus_handler(5, "2026-06-20", []))
        c = fetch_consensus("002669", as_of=AS_OF)
        assert c.insufficient_coverage is True

    def test_all_subcalls_down_returns_none(self, monkeypatch):
        _boom(monkeypatch)
        assert fetch_consensus("002669", as_of=AS_OF) is None


# ============================================================
# 全源失败优雅降级
# ============================================================

class TestGracefulDegradation:

    def test_all_sources_down_still_returns_events(self, monkeypatch):
        _boom(monkeypatch)
        ev = fetch_recent_events("002669", as_of=AS_OF)
        assert isinstance(ev, RecentEvents)
        assert ev.stock_code == "002669"
        assert ev.as_of == "2026-07-10"
        assert ev.announcements == []
        assert ev.forecasts == []
        assert ev.consensus is None
        block = ev.to_prompt_block()
        # Still renders a valid Chinese block with unavailability markers
        assert "以下为公开披露事实（截至 2026-07-10）" in block
        assert "暂无业绩预告" in block
        assert "一致预期数据不可用" in block
        assert "暂无公告" in block


# ============================================================
# to_prompt_block 中文格式
# ============================================================

class TestPromptBlock:

    def _events(self, consensus):
        return RecentEvents(
            stock_code="002669",
            as_of="2026-07-10",
            announcements=[
                Announcement(title="康达新材:第六届董事会第二十五次会议决议公告",
                             date="2026-07-02", category="董事会决议公告"),
                Announcement(title="康达新材:关于为子公司提供担保事项的进展公告",
                             date="2026-06-18", category="提供/对外担保公告"),
            ],
            forecasts=[
                EarningsForecast(
                    report_period="2025-12-31", forecast_type="扭亏",
                    indicator="扣除非经常性损益后的净利润",
                    value_low=21000000, value_high=31000000,
                    change_pct_low=106.81, change_pct_high=110.06,
                    notice_date="2026-01-21"),
            ],
            consensus=consensus,
        )

    def test_disclaimer_header_is_first_line(self):
        block = self._events(None).to_prompt_block()
        first = block.splitlines()[0]
        assert first == ("以下为公开披露事实（截至 2026-07-10），"
                         "分析必须以此为准，禁止引用未在此列出的催化剂或传闻。")

    def test_forecast_line_chinese_units(self):
        block = self._events(None).to_prompt_block()
        assert "报告期 2025-12-31" in block
        assert "类型: 扭亏" in block
        assert "2100万元 ~ 3100万元" in block
        assert "+106.81% ~ +110.06%" in block
        assert "公告日期: 2026-01-21" in block

    def test_announcements_listed_with_dates(self):
        block = self._events(None).to_prompt_block()
        assert "- 2026-07-02 康达新材:第六届董事会第二十五次会议决议公告 [董事会决议公告]" in block
        assert "共 2 条" in block

    def test_insufficient_consensus_shows_gate_not_numbers(self):
        c = ConsensusForecast(
            org_count=1, latest_report_date=None, insufficient_coverage=True,
            predictions=[ConsensusYear(year=2026, eps=0.43,
                                       net_profit=130030000, revenue=5839400000)],
        )
        block = self._events(c).to_prompt_block()
        assert "无有效一致预期" in block
        assert "覆盖机构 1 家" in block
        assert "禁止在分析中引用任何一致预期数字" in block
        # 红线 5: gated numbers must NOT leak into the prompt
        assert "0.43" not in block
        assert "1.30亿元" not in block

    def test_sufficient_consensus_shows_metadata_and_numbers(self):
        c = ConsensusForecast(
            org_count=8, latest_report_date="2026-06-20",
            insufficient_coverage=False,
            predictions=[ConsensusYear(year=2026, eps=0.43,
                                       net_profit=130030000, revenue=5839400000)],
        )
        block = self._events(c).to_prompt_block()
        assert "覆盖机构 8 家" in block
        assert "最近研报日期 2026-06-20" in block
        assert "2026E: EPS 0.43 元" in block
        assert "归母净利润 1.30亿元" in block
        assert "营业总收入 58.39亿元" in block

    def test_no_english_sentences_in_block(self):
        c = ConsensusForecast(org_count=8, latest_report_date="2026-06-20",
                              insufficient_coverage=False,
                              predictions=[ConsensusYear(2026, 0.43, None, None)])
        block = self._events(c).to_prompt_block()
        # Natural-language content must be Chinese: no ASCII words other than
        # allowed abbreviations (EPS) / dates / numbers.
        import re
        words = set(re.findall(r"[A-Za-z]{2,}", block))
        assert words <= {"EPS", "E"}, f"unexpected English words: {words}"


class TestFormatHelpers:

    def test_format_cny_units(self):
        assert em._format_cny(21000000) == "2100万元"
        assert em._format_cny(130030000) == "1.30亿元"
        assert em._format_cny(-308264200) == "-3.08亿元"
        assert em._format_cny(5000) == "5000元"
        # Per-share magnitudes keep decimals (live 002669: EPS 预告 0.06 元
        # must not collapse to "0元")
        assert em._format_cny(0.06) == "0.06元"
        assert em._format_cny(None) == "未披露"

    def test_format_range_missing_sides(self):
        assert em._format_range(None, None) == "未披露"
        assert em._format_range(21000000, None) == "2100万元"
        assert em._format_range(30000000, 30000000) == "3000万元"

    def test_clean_code_variants(self):
        assert em._clean_code("002669.SZ") == "002669"
        assert em._clean_code("SZ002669") == "002669"
        assert em._clean_code("600519.SS") == "600519"
        assert em._secucode("002669") == "002669.SZ"
        assert em._secucode("600519") == "600519.SH"


# ============================================================
# 真实网络冒烟 (AEGIS_RUN_NETWORK_TESTS=1)
# ============================================================

class TestLiveSmoke:

    def test_002669_live(self):
        require_network()
        ev = fetch_recent_events("002669")
        # Announcements: an active listed company always has filings in 90d
        assert len(ev.announcements) > 0
        assert ev.announcements == sorted(
            ev.announcements, key=lambda a: a.date, reverse=True)
        # Forecast: FY2025 扭亏预告 verified live 2026-07-10
        assert any(f.forecast_type for f in ev.forecasts)
        # Consensus: 002669 has 1 org / 0 recent reports → gate must trip
        assert ev.consensus is not None
        assert ev.consensus.insufficient_coverage is True
        block = ev.to_prompt_block()
        assert "以下为公开披露事实" in block
        assert "无有效一致预期" in block
