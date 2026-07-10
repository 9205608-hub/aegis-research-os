"""Unit tests for the production HTML renderer — html_report_v2.

AUDIT-E3 (2026-07): `html_report_v2.generate_html_report` is the sole
production entry point (every report since the v2 cutover), yet it had
zero direct test coverage — the legacy tests import re-exported helpers
from the `html_report` shim that hard-route to html_report_legacy dead
code. These tests import the v2 module directly.

Covers:
- _derive_rating thresholds (both languages, zero-price guard)
- publishing_status branching in build_report_dict:
  published / blocked / needs_review / downgraded (AUDIT-C2 regression)
- _sanitize_floats on inf / -inf / nan in nested containers (BUG-Y39)
- generate_html_report end-to-end: injected window.REPORT JSON parses,
  contains no Infinity/NaN tokens, and no literal "</" that would
  terminate the <script> block early (AUDIT </script> escape)
- Chinese headline fallback uses abs() — no "-23.8%下行空间" double
  negative (AUDIT headline regression)
- US long-form unit suffix reads "USD billions", not "usd bs"
- Staleness banner month math (month granularity + real period end)
"""

import json
import math
from datetime import datetime
from types import SimpleNamespace

import pytest

from aegis.core.reports import html_report_v2 as v2


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """Keep tests hermetic — skip sparkline / quote-meta network fetches."""
    monkeypatch.setenv("AEGIS_SKIP_SPARKLINE", "1")


# ─────────────────────────────────────────────────────────────────
# Builders
# ─────────────────────────────────────────────────────────────────

def _decision(**over):
    base = dict(
        entity_id="600519",
        publishing_status="published",
        confidence_bucket="medium",
        bias_check_status="通过",
        run_id="run_test",
        period="FY2025",
        open_questions=[],
        dcf_output=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


_CN_SCENARIOS = {
    "currency": "CNY",
    "base_value": 150.0,
    "probability_weighted_value": 150.0,
    "bear_value": 100.0, "bear_probability": 0.25, "bear_narrative": "悲观情形",
    "base_probability": 0.50, "base_narrative": "基准情形",
    "bull_value": 200.0, "bull_probability": 0.25, "bull_narrative": "乐观情形",
}

_CN_META = {
    "ebitda": 5e9, "operating_income": 4e9, "revenue": 2e10,
    "net_income": 3e9, "total_equity": 1e10, "shares_outstanding": 1e9,
}


def _build_cn(**over):
    """build_report_dict with a minimal-but-valid CN (A-share) input set."""
    kw = dict(
        decision=_decision(),
        market_data={"current_price": 100.0},
        meta_facts=dict(_CN_META),
        scenarios=dict(_CN_SCENARIOS),
        entity_id="600519",
        entity_name="贵州茅台",
        entity_name_clean="贵州茅台",
        period="FY2025",
    )
    kw.update(over)
    return v2.build_report_dict(**kw)


def _generate(decision=None, **over):
    """generate_html_report with the same minimal CN defaults."""
    kw = dict(
        computed_metrics={},
        market_data={"current_price": 100.0},
        agent_judgments=[],
        critic_results=[],
        meta_facts=dict(_CN_META),
        scenarios=dict(_CN_SCENARIOS),
        entity_name="贵州茅台",
        entity_name_clean="贵州茅台",
        period="FY2025",
    )
    kw.update(over)
    return v2.generate_html_report(decision or _decision(), **kw)


def _extract_report_json(html: str) -> str:
    """Pull the inlined window.REPORT JSON out of the rendered page."""
    marker = "window.REPORT = "
    start = html.index(marker) + len(marker)
    end = html.index(";</script>", start)
    return html[start:end]


# ─────────────────────────────────────────────────────────────────
# _derive_rating
# ─────────────────────────────────────────────────────────────────

class TestDeriveRating:

    def test_thresholds_en(self):
        assert v2._derive_rating(130, 100, False) == ("Buy", "buy")
        assert v2._derive_rating(110, 100, False) == ("Overweight", "buy")
        assert v2._derive_rating(100, 100, False) == ("Hold", "hold")
        assert v2._derive_rating(92, 100, False) == ("Reduce", "avoid")
        assert v2._derive_rating(80, 100, False) == ("Avoid", "avoid")

    def test_thresholds_zh(self):
        assert v2._derive_rating(130, 100, True) == ("买入", "buy")
        assert v2._derive_rating(110, 100, True) == ("增持", "buy")
        assert v2._derive_rating(100, 100, True) == ("持有", "hold")
        assert v2._derive_rating(92, 100, True) == ("减持", "avoid")
        assert v2._derive_rating(80, 100, True) == ("回避", "avoid")

    def test_zero_price_falls_back_to_hold(self):
        assert v2._derive_rating(150, 0, True) == ("持有", "hold")
        assert v2._derive_rating(150, -1, False) == ("Hold", "hold")


# ─────────────────────────────────────────────────────────────────
# publishing_status × rating (BUG-Y22 + AUDIT-C2)
# ─────────────────────────────────────────────────────────────────

class TestPublishingStatusRating:

    def test_published_derives_rating_from_target(self):
        rep = _build_cn()  # target 150 vs price 100 → +50% → 买入
        assert rep["rating"]["word"] == "买入"
        assert rep["rating"]["tone"] == "buy"
        assert rep["rating"]["weighted"] == "概率加权"
        assert rep["rating"]["downgraded"] is False

    def test_blocked_shows_not_rated(self):
        rep = _build_cn(decision=_decision(publishing_status="blocked"))
        assert rep["rating"]["word"] == "暂不评级"
        assert rep["rating"]["tone"] == "hold"

    def test_needs_review_shows_under_review(self):
        rep = _build_cn(decision=_decision(publishing_status="needs_review"))
        assert rep["rating"]["word"] == "审核中"
        assert rep["rating"]["tone"] == "hold"

    def test_downgraded_keeps_rating_with_caveat_zh(self):
        # AUDIT-C2: downgraded used to fall through to the plain rating
        # path and render an unqualified "买入".
        rep = _build_cn(decision=_decision(publishing_status="downgraded"))
        assert rep["rating"]["word"] == "买入"  # rating retained
        assert rep["rating"]["downgraded"] is True
        assert rep["rating"]["weighted"] == "评级已降级 · 存在未解决分歧"

    def test_downgraded_keeps_rating_with_caveat_en(self):
        rep = v2.build_report_dict(
            decision=_decision(entity_id="NVDA", publishing_status="downgraded"),
            market_data={"current_price": 100.0},
            meta_facts={"ebitda": 5e9, "operating_income": 4e9},
            scenarios={"currency": "USD", "base_value": 150.0,
                       "probability_weighted_value": 150.0},
            entity_id="NVDA",
            entity_name="NVIDIA",
            entity_name_clean="NVIDIA",
            period="FY2026",
        )
        assert rep["rating"]["word"] == "Buy"
        assert rep["rating"]["downgraded"] is True
        assert rep["rating"]["weighted"] == "Downgraded · unresolved conflicts"


# ─────────────────────────────────────────────────────────────────
# _sanitize_floats (BUG-Y39)
# ─────────────────────────────────────────────────────────────────

class TestSanitizeFloats:

    def test_non_finite_become_none(self):
        assert v2._sanitize_floats(float("inf")) is None
        assert v2._sanitize_floats(float("-inf")) is None
        assert v2._sanitize_floats(float("nan")) is None

    def test_finite_values_pass_through(self):
        assert v2._sanitize_floats(1.5) == 1.5
        assert v2._sanitize_floats(0.0) == 0.0
        assert v2._sanitize_floats(42) == 42          # int untouched
        assert v2._sanitize_floats("nan") == "nan"    # strings untouched

    def test_nested_containers(self):
        dirty = {
            "a": [1.0, float("inf"), {"b": float("nan")}],
            "c": (float("-inf"), "ok"),
        }
        clean = v2._sanitize_floats(dirty)
        assert clean == {"a": [1.0, None, {"b": None}], "c": (None, "ok")}


# ─────────────────────────────────────────────────────────────────
# generate_html_report — injected JSON integrity
# ─────────────────────────────────────────────────────────────────

class TestGeneratedJsonIntegrity:

    def test_inf_nan_inputs_produce_parseable_json(self):
        sc = dict(_CN_SCENARIOS)
        sc["bear_value"] = float("inf")        # → scenarios[0].px
        sc["base_probability"] = float("nan")  # → scenarios[1].prob
        html = _generate(scenarios=sc)
        raw = _extract_report_json(html)
        assert "Infinity" not in raw
        assert "NaN" not in raw
        rep = json.loads(raw)  # must parse cleanly
        bear = next(s for s in rep["scenarios"] if s["key"] == "bear")
        base = next(s for s in rep["scenarios"] if s["key"] == "base")
        assert bear["px"] is None
        assert base["prob"] is None

    def test_script_terminator_in_llm_text_is_escaped(self):
        # AUDIT: a literal "</script>" inside any LLM string used to
        # terminate the inline <script> block and white-screen the report.
        evil = "ROIC<WACC，价值毁灭</script><script>alert(1)</script><!--"
        judgment = {
            "agent_name": "business_analyst",
            "inferences": [{"text": evil, "confidence": "medium"}],
            "observations": [{"text": "观察一"}],
            "counterarguments": [],
        }
        html = _generate(agent_judgments=[judgment])
        raw = _extract_report_json(html)
        assert "</" not in raw          # every "</" escaped as "<\/"
        assert "<!--" not in raw        # comment-open escaped too
        rep = json.loads(raw)           # escapes are JSON-equivalent…
        assert rep["agents"][0]["thesis"] == evil  # …and round-trip

    def test_chinese_labels_spot_check(self):
        html = _generate()
        rep = json.loads(_extract_report_json(html))
        assert rep["rating"]["weighted"] == "概率加权"
        assert rep["rating"]["timeHorizon"] == "12 个月"
        assert rep["dcf"]["title"] == "十年现金流桥接与企业价值拆解"
        assert rep["scenarios"][0]["tag"] == "悲观情景"


# ─────────────────────────────────────────────────────────────────
# Chinese headline fallback (BUG-Y34 + AUDIT abs() regression)
# ─────────────────────────────────────────────────────────────────

class TestChineseHeadlineFallback:

    def test_downside_uses_abs_no_double_negative(self):
        # target 76.2 vs price 100 → implied -23.8%. The old fallback
        # rendered "DCF 基准 -23.8%下行空间" — a double negative.
        sc = dict(_CN_SCENARIOS)
        sc["base_value"] = 76.2
        sc["probability_weighted_value"] = 76.2
        rep = _build_cn(scenarios=sc)
        assert "下行空间" in rep["headline"]
        assert "23.8%" in rep["headline"]
        assert "-23.8" not in rep["headline"]

    def test_upside_keeps_direction_word(self):
        rep = _build_cn()  # target 150 vs 100 → +50%
        assert "上行空间" in rep["headline"]
        assert "50.0%" in rep["headline"]
        assert "贵州茅台" in rep["headline"]

    def test_editor_headline_takes_precedence(self):
        rep = _build_cn(edited_report={"headline": "编辑器标题", "lede": "导语"})
        assert rep["headline"] == "编辑器标题"


# ─────────────────────────────────────────────────────────────────
# US long-form unit suffix ("usd bs" regression)
# ─────────────────────────────────────────────────────────────────

class TestUnitSuffix:

    def test_us_unit_reads_usd_billions(self):
        rep = v2.build_report_dict(
            decision=_decision(entity_id="NVDA"),
            market_data={"current_price": 100.0},
            meta_facts={"ebitda": 5e9, "operating_income": 4e9, "revenue": 6e10},
            scenarios={"currency": "USD", "base_value": 150.0,
                       "probability_weighted_value": 150.0},
            entity_id="NVDA",
            entity_name="NVIDIA",
            entity_name_clean="NVIDIA",
            period="FY2026",
        )
        assert rep["dcf"]["unit"] == "USD billions"
        assert rep["financials"]["revTitle"].endswith("USD billions")
        assert "usd bs" not in json.dumps(rep, default=str)

    def test_cn_unit_unchanged(self):
        rep = _build_cn()
        assert rep["dcf"]["unit"] == "亿元人民币"


# ─────────────────────────────────────────────────────────────────
# Staleness banner month math
# ─────────────────────────────────────────────────────────────────

class TestStaleBanner:

    def test_gap_is_month_granular(self):
        # FY three years back always trips the 15-month threshold; the
        # expected month count uses the same calendar-FY-end approximation
        # the renderer falls back to (fy_year-12).
        now = datetime.now()
        fy = now.year - 3
        gap = (now.year * 12 + now.month) - (fy * 12 + 12)
        rep = _build_cn(period=f"FY{fy}", decision=_decision(period=f"FY{fy}"))
        banner = rep["staleBanner"]
        assert banner is not None
        assert f"约 {gap} 个月" in banner
        assert f"{fy}-12-31" in banner

    def test_real_period_end_preferred(self):
        # A non-calendar fiscal year end (e.g. NVDA-style late January)
        # must drive both the month math and the displayed date.
        now = datetime.now()
        pe_year = now.year - 2
        gap = (now.year * 12 + now.month) - (pe_year * 12 + 1)
        meta = dict(_CN_META)
        meta["fiscal_period_end"] = f"{pe_year}-01-31"
        rep = _build_cn(
            meta_facts=meta,
            period=f"FY{pe_year}",
            decision=_decision(period=f"FY{pe_year}"),
        )
        banner = rep["staleBanner"]
        assert banner is not None
        assert f"{pe_year}-01-31" in banner
        assert f"约 {gap} 个月" in banner

    def test_fresh_period_has_no_banner(self):
        now = datetime.now()
        rep = _build_cn(period=f"FY{now.year}",
                        decision=_decision(period=f"FY{now.year}"))
        assert rep["staleBanner"] is None
