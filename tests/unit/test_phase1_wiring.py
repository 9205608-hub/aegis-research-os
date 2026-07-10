"""Aegis 2.0 Phase 1 接线回归 — 季报 PIT / TTM / 相对估值 / 验证点核验进管线与报告.

覆盖（任务 D5）：
1. run_quarterly_pit_step：mock PIT+TTM 走通 → meta_facts 固定键
   （红线 8 豁免的 ttm_* 三键 + __data_freshness）；ingest/TTM 失败静默
   降级（无 ttm 键、store 照常返回）；ticker 后缀清洗。
2. quarterly 失败优雅回退：orchestrator 降级路径 build_data_freshness
   年报口径（A 股 FY 固定 12-31 期末）。
3. verification 封闭目录检查器：真实 PITStore（tmp db）上的
   通过/未通过/数据不足 三态；CFO Q1 噪声 gate；扣非/归母康达型未通过；
   预告 vs 一致预期（红线 5 覆盖 gate）；store=None 永不 raise。
4. annotate_verification_focus：verification_focus 文案 → 已核验·通过/
   未通过 + 依据；无命中维持「未核验」；落单检查器追加条目。
5. 渲染层：报告 dict 的 pricedIn.relative（同业倍数分位小节 + 红线 5
   样本不足披露）、verification 三态、dataAsOf 数据截至行（季报/年报
   口径、时效天数按渲染时点重算）；end-to-end HTML 内联断言。
6. KEY FINANCIALS 注入 TTM 三行 + 数据截至（zh/en 双语，任务 D4）；
   ttm 键缺席时不产生 TTM 行。
7. 红线 9：relative_valuation_sanctioned_pcts 白名单生成器（样本不足
   → 空表，禁止引用）。
"""

import json
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from aegis.core.agents.base import AgentInput
from aegis.core.agents.llm_agent_base import LLMAgentBase
from aegis.core.chief_analyst.thesis_synthesizer import (
    relative_valuation_sanctioned_pcts,
)
from aegis.core.orchestrator.auto_research import (
    ResearchConfig,
    TTM_META_KEYS,
    _latest_announce_date,
    build_data_freshness,
    run_quarterly_pit_step,
)
from aegis.core.reports import html_report_v2 as v2
from aegis.core.truth.verification import (
    VerificationResult,
    annotate_verification_focus,
    run_verification,
)
from aegis.pit.store import PITStore

from test_phase0_wiring import (
    MOCK_EVENTS,
    MOCK_FRONTIER,
    MOCK_REGIME,
    _decision,
)


# ─────────────────────────────────────────────────────────────────
# 共享 fixture
# ─────────────────────────────────────────────────────────────────

_CN_SCENARIOS = {
    "bear_value": 1.50, "base_value": 2.15, "bull_value": 3.00,
    "probability_weighted_value": 2.15, "currency": "CNY",
    "bear_probability": 0.25, "base_probability": 0.50,
    "bull_probability": 0.25,
}

#: 康达型相对估值结果（RelativeValuation.to_dict 同构，实测数字口径）。
MOCK_RELVAL = {
    "stock_code": "002669",
    "industry": "化学制品",
    "data_date": "2026-07-10",
    "peer_source": "industry_board",
    "target_pe_ttm": 32.6,
    "target_pb": 1.48,
    "peer_pe_median": 33.3,
    "peer_pb_median": 2.01,
    "pe_percentile": 44.0,
    "pb_percentile": 30.0,
    "peer_count": 10,
    "pe_sample_size": 9,
    "pb_sample_size": 10,
    "loss_making_count": 1,
    "universe_size": 181,
    "insufficient_peers": False,
    "peers": [],
}

MOCK_RELVAL_INSUFFICIENT = {
    **MOCK_RELVAL,
    "peer_pe_median": None, "peer_pb_median": None,
    "pe_percentile": None, "pb_percentile": None,
    "pe_sample_size": 2, "pb_sample_size": 1,
    "insufficient_peers": True,
}


class _FakeIngestResult(SimpleNamespace):
    pass


def _fake_ingest_ok(store, code):
    return _FakeIngestResult(
        stock_code=code, facts_written=18,
        periods=["2026-03-31", "2025-12-31", "2025-03-31"],
        sources_ok=["em_f10_income"], errors=[],
    )


def _fake_ingest_fail(store, code):
    raise ConnectionError("datacenter unreachable")


def _fake_ttm(store, code):
    return {
        "ttm_revenue": 4.565e9,
        "ttm_net_income": 1.25e8,
        "ttm_net_income_deducted": 1.672e7,
        "latest_period": "2026-03-31",
    }


class _FakeTTMDataclass:
    """带 to_dict 的 TTM 快照形态（Wave 3 引擎的 dataclass 合同）。"""

    def to_dict(self):
        return _fake_ttm(None, None)


@pytest.fixture
def pit(tmp_path):
    store = PITStore(tmp_path / "pit.db")
    for concept in ("net_income_deducted", "total_assets", "total_liabilities",
                    "accounts_receivable", "inventory"):
        store.register_concept(concept)
    yield store
    store.close()


def _seed(store, concept, values, *, fiscal=None, unaudited=True):
    """values: {period: value}。"""
    for period, value in values.items():
        fp = fiscal or {"03-31": "Q1", "06-30": "H1",
                        "09-30": "Q3", "12-31": "FY"}[period[5:]]
        store.record_fact(
            entity_id="002669", concept=concept, period=period,
            value=value, fiscal_period=fp, source="em_f10_income",
            unaudited=unaudited, announce_date=f"{period[:4]}-04-28",
        )


# ═════════════════════════════════════════════════════════════════
# 1. run_quarterly_pit_step（任务 D1 的可测缝）
# ═════════════════════════════════════════════════════════════════

class TestQuarterlyPitStep:

    def test_happy_path_ttm_keys_and_freshness(self, pit):
        logs = []
        store, updates = run_quarterly_pit_step(
            "002669.SZ", store=pit, ingest_fn=_fake_ingest_ok,
            ttm_fn=_fake_ttm, log=logs.append,
        )
        assert store is pit
        assert updates["ttm_revenue"] == pytest.approx(4.565e9)
        assert updates["ttm_net_income"] == pytest.approx(1.25e8)
        assert updates["ttm_net_income_deducted"] == pytest.approx(1.672e7)
        fresh = updates["__data_freshness"]
        assert fresh["latest_period"] == "2026-03-31"
        assert fresh["basis"] == "quarterly"
        # 红线 8：除 3 个固定 ttm 键 + __data_freshness 外不加任何自由键
        assert set(updates) == {*TTM_META_KEYS, "__data_freshness"}

    def test_ttm_dataclass_form_coerced(self, pit):
        _, updates = run_quarterly_pit_step(
            "002669", store=pit, ingest_fn=_fake_ingest_ok,
            ttm_fn=lambda s, c: _FakeTTMDataclass(),
        )
        assert updates["ttm_revenue"] == pytest.approx(4.565e9)

    def test_ingest_failure_degrades_silently(self, pit):
        logs = []
        store, updates = run_quarterly_pit_step(
            "002669", store=pit, ingest_fn=_fake_ingest_fail,
            ttm_fn=lambda s, c: None, log=logs.append,
        )
        assert store is pit          # store 照常返回（验证点核验仍可读库）
        assert updates == {}         # 无 TTM、无 freshness → 年报口径回退
        assert any("Quarterly ingest failed" in m for m in logs)

    def test_ttm_engine_missing_uses_ingested_periods_for_freshness(self, pit):
        # Wave 3 引擎缺席（ttm_fn 返回 None）→ ttm 键缺省，但摄取到的
        # 最新报告期仍派生 __data_freshness（数据截至行不留空）。
        _, updates = run_quarterly_pit_step(
            "002669", store=pit, ingest_fn=_fake_ingest_ok,
            ttm_fn=lambda s, c: None,
        )
        assert not any(k in updates for k in TTM_META_KEYS)
        assert updates["__data_freshness"]["latest_period"] == "2026-03-31"

    def test_ttm_fn_crash_degrades(self, pit):
        def _boom(s, c):
            raise RuntimeError("ttm crash")
        logs = []
        _, updates = run_quarterly_pit_step(
            "002669", store=pit, ingest_fn=_fake_ingest_ok,
            ttm_fn=_boom, log=logs.append,
        )
        assert not any(k in updates for k in TTM_META_KEYS)
        assert any("TTM snapshot failed" in m for m in logs)

    def test_ticker_suffix_cleaned(self, pit):
        seen = {}

        def _spy(store, code):
            seen["code"] = code
            return _fake_ingest_ok(store, code)

        run_quarterly_pit_step("600519.SS", store=pit,
                               ingest_fn=_spy, ttm_fn=lambda s, c: None)
        assert seen["code"] == "600519"

    def test_never_raises_on_store_factory_failure(self, monkeypatch):
        # 默认建库路径失败（如只读文件系统）→ (None, {})，主流程不受影响
        import aegis.pit.store as pit_store_mod

        def _boom(*a, **k):
            raise OSError("read-only fs")

        monkeypatch.setattr(pit_store_mod, "PITStore", _boom)
        store, updates = run_quarterly_pit_step("002669")
        assert store is None and updates == {}

    def test_config_flag_default_on(self):
        assert ResearchConfig(ticker="X").enable_quarterly is True

    def test_annual_fallback_freshness(self):
        fresh = build_data_freshness("2025-12-31", basis="annual")
        assert fresh["basis"] == "annual"
        assert fresh["latest_period"] == "2025-12-31"
        assert fresh["days_since"] == (date.today() - date(2025, 12, 31)).days

    def test_freshness_bad_date_degrades_to_none_days(self):
        fresh = build_data_freshness("FY2025", basis="annual")
        assert fresh["days_since"] is None

    # ── 披露日时效口径（红线 #3：announce_date 优先） ────────────────

    def test_freshness_prefers_announce_date(self):
        """时效按披露日计：期末 +90 天到下次披露之间不产生日历假警报。"""
        ad = (date.today() - timedelta(days=73)).isoformat()
        fresh = build_data_freshness(
            "2026-03-31", basis="quarterly", announce_date=ad)
        assert fresh["announce_date"] == ad
        assert fresh["days_since"] == 73
        assert fresh["latest_period"] == "2026-03-31"  # 期末照旧展示

    def test_freshness_bad_announce_falls_back_to_period_end(self):
        fresh = build_data_freshness(
            "2026-03-31", basis="quarterly", announce_date="待披露")
        assert fresh["days_since"] == (date.today() - date(2026, 3, 31)).days

    def test_latest_announce_date_reads_store(self, pit):
        _seed(pit, "revenue", {"2026-03-31": 1.0, "2025-12-31": 2.0})
        assert _latest_announce_date(pit, "002669", "2026-03-31") == "2026-04-28"
        assert _latest_announce_date(pit, "002669", "2024-03-31") is None
        assert _latest_announce_date(None, "002669", "2026-03-31") is None

    def test_step_freshness_carries_announce_date(self, pit):
        """全链：库里有披露日 → __data_freshness 带 announce_date 且据其计天。"""
        _seed(pit, "revenue", {"2026-03-31": 1.0})
        _, updates = run_quarterly_pit_step(
            "002669", store=pit, ingest_fn=_fake_ingest_ok, ttm_fn=_fake_ttm)
        fresh = updates["__data_freshness"]
        assert fresh["announce_date"] == "2026-04-28"
        assert fresh["days_since"] == (date.today() - date(2026, 4, 28)).days


# ═════════════════════════════════════════════════════════════════
# 2. 验证点核验器（任务 D2，LLM 不参与的纯数据规则）
# ═════════════════════════════════════════════════════════════════

class TestVerificationChecks:

    def _by_id(self, results):
        return {r.check_id: r for r in results}

    def test_store_none_never_raises_all_pit_checks_insufficient(self):
        results = run_verification(store=None, entity_id="002669",
                                   recent_events=None)
        assert len(results) == 6
        assert all(r.status == "insufficient" for r in results)
        # to_dict 全 JSON 可序列化（meta_facts / replay 前提）
        json.dumps([r.to_dict() for r in results], ensure_ascii=False)

    def test_cfo_to_ni_fail_on_kanda_pattern(self, pit):
        # 康达型：归母为正、经营现金流大幅为负 → 未通过
        _seed(pit, "net_income", {"2025-12-31": 1.25e8})
        _seed(pit, "cfo", {"2025-12-31": -1.2e9})
        r = self._by_id(run_verification(store=pit, entity_id="002669"))[
            "cfo_to_net_income"]
        assert r.status == "fail"
        assert "红旗线" in r.detail_zh
        assert r.evidence["ratio"] < 0.5

    def test_cfo_to_ni_pass(self, pit):
        _seed(pit, "net_income", {"2025-12-31": 1.0e8})
        _seed(pit, "cfo", {"2025-12-31": 1.1e8})
        r = self._by_id(run_verification(store=pit, entity_id="002669"))[
            "cfo_to_net_income"]
        assert r.status == "pass"

    def test_cfo_to_ni_q1_only_insufficient(self, pit):
        # 仅 Q1 累计数据 → 季节性噪声过大，不予判定
        _seed(pit, "net_income", {"2026-03-31": 3.0e7})
        _seed(pit, "cfo", {"2026-03-31": -5.0e7})
        r = self._by_id(run_verification(store=pit, entity_id="002669"))[
            "cfo_to_net_income"]
        assert r.status == "insufficient"
        assert "一季度" in r.detail_zh

    def test_cfo_to_ni_negative_ni_insufficient(self, pit):
        _seed(pit, "net_income", {"2025-12-31": -2.0e8})
        _seed(pit, "cfo", {"2025-12-31": 1.0e8})
        r = self._by_id(run_verification(store=pit, entity_id="002669"))[
            "cfo_to_net_income"]
        assert r.status == "insufficient"
        assert "口径失效" in r.detail_zh

    def test_deducted_ratio_fail_kanda(self, pit):
        # 康达基准：归母 1.25 亿 vs 扣非 1672 万 → 0.13 < 0.5 未通过
        _seed(pit, "net_income", {"2025-12-31": 1.25e8})
        _seed(pit, "net_income_deducted", {"2025-12-31": 1.672e7})
        r = self._by_id(run_verification(store=pit, entity_id="002669"))[
            "deducted_to_attributable"]
        assert r.status == "fail"
        assert "非经常性损益" in r.detail_zh

    def test_deducted_ratio_pass(self, pit):
        _seed(pit, "net_income", {"2025-12-31": 1.0e8})
        _seed(pit, "net_income_deducted", {"2025-12-31": 9.0e7})
        r = self._by_id(run_verification(store=pit, entity_id="002669"))[
            "deducted_to_attributable"]
        assert r.status == "pass"

    def test_receivables_growth_gap_fail(self, pit):
        # 应收同比 +80% vs 营收同比 +10% → 缺口 70pp 未通过（累计同比口径）
        _seed(pit, "revenue", {"2026-03-31": 1.1e9, "2025-03-31": 1.0e9})
        _seed(pit, "accounts_receivable",
              {"2026-03-31": 1.8e9, "2025-03-31": 1.0e9})
        r = self._by_id(run_verification(store=pit, entity_id="002669"))[
            "receivables_vs_revenue"]
        assert r.status == "fail"
        assert r.evidence["gap_pp"] == pytest.approx(70.0)

    def test_receivables_growth_gap_pass(self, pit):
        _seed(pit, "revenue", {"2026-03-31": 1.2e9, "2025-03-31": 1.0e9})
        _seed(pit, "accounts_receivable",
              {"2026-03-31": 1.25e9, "2025-03-31": 1.0e9})
        r = self._by_id(run_verification(store=pit, entity_id="002669"))[
            "receivables_vs_revenue"]
        assert r.status == "pass"

    def test_receivables_missing_concept_insufficient(self, pit):
        _seed(pit, "revenue", {"2026-03-31": 1.1e9, "2025-03-31": 1.0e9})
        r = self._by_id(run_verification(store=pit, entity_id="002669"))[
            "receivables_vs_revenue"]
        assert r.status == "insufficient"
        assert "摄取端未覆盖" in r.detail_zh

    def test_inventory_gap_uses_wider_threshold(self, pit):
        # 存货缺口 22pp：低于 25pp 阈值 → 通过（应收同阈值会未通过）
        _seed(pit, "revenue", {"2026-03-31": 1.0e9, "2025-03-31": 1.0e9})
        _seed(pit, "inventory", {"2026-03-31": 1.22e9, "2025-03-31": 1.0e9})
        r = self._by_id(run_verification(store=pit, entity_id="002669"))[
            "inventory_vs_revenue"]
        assert r.status == "pass"

    def test_leverage_trend_fail_on_rise(self, pit):
        _seed(pit, "total_assets",
              {"2026-03-31": 10e9, "2025-03-31": 10e9})
        _seed(pit, "total_liabilities",
              {"2026-03-31": 7.0e9, "2025-03-31": 6.0e9})
        r = self._by_id(run_verification(store=pit, entity_id="002669"))[
            "leverage_trend"]
        assert r.status == "fail"
        assert r.evidence["delta_pp"] == pytest.approx(10.0)

    def test_forecast_vs_consensus_gate_insufficient(self):
        # 红线 5：一致预期覆盖 gate 不过 → 该项数据不足
        r = [x for x in run_verification(recent_events=MOCK_EVENTS)
             if x.check_id == "forecast_vs_consensus"][0]
        assert r.status == "insufficient"
        assert "未达使用门槛" in r.detail_zh

    def test_forecast_vs_consensus_fail_on_gap(self):
        events = {
            **MOCK_EVENTS,
            "consensus": {
                "org_count": 5, "latest_report_date": "2026-06-20",
                "insufficient_coverage": False,
                "predictions": [
                    {"year": 2025, "eps": 0.5, "net_profit": 2.0e8,
                     "revenue": 5.0e9},
                ],
            },
        }
        r = [x for x in run_verification(recent_events=events)
             if x.check_id == "forecast_vs_consensus"][0]
        # 预告中值 1.30 亿 vs 一致预期 2.0 亿 → 缺口 -35% 未通过
        assert r.status == "fail"
        assert "低于一致预期" in r.detail_zh
        assert r.evidence["gap_pct"] == pytest.approx(-35.0)

    def test_forecast_vs_consensus_pass_within_threshold(self):
        events = {
            **MOCK_EVENTS,
            "consensus": {
                "org_count": 5, "latest_report_date": "2026-06-20",
                "insufficient_coverage": False,
                "predictions": [
                    {"year": 2025, "eps": 0.5, "net_profit": 1.25e8,
                     "revenue": 5.0e9},
                ],
            },
        }
        r = [x for x in run_verification(recent_events=events)
             if x.check_id == "forecast_vs_consensus"][0]
        assert r.status == "pass"


class TestAnnotateVerificationFocus:

    RESULTS = [
        VerificationResult(
            check_id="cfo_to_net_income", name_zh="经营现金流 / 归母净利润",
            status="fail", detail_zh="CFO/归母 = -9.60，低于 0.5 红旗线",
            evidence={"ratio": -9.6},
        ),
        VerificationResult(
            check_id="deducted_to_attributable", name_zh="扣非 / 归母净利润比",
            status="fail", detail_zh="扣非仅为归母的 13%",
        ),
        VerificationResult(
            check_id="leverage_trend", name_zh="资产负债率趋势",
            status="insufficient", detail_zh="PIT 库无负债合计数据",
        ),
        VerificationResult(
            check_id="forecast_vs_consensus", name_zh="业绩预告 vs 一致预期",
            status="pass", detail_zh="缺口 +3%，在阈值内",
        ),
    ]

    def test_focus_gets_verified_fail_with_evidence(self):
        out = annotate_verification_focus(
            ["盈利质量修复信号（CFO/净利比、应计项目占比回落）"], self.RESULTS)
        item = out[0]
        assert item["status"] == "已核验·未通过"
        assert item["state"] == "fail"
        assert "红旗线" in item["evidence"]
        assert "13%" in item["evidence"]  # 两个命中检查器的依据都在

    def test_focus_without_matching_check_stays_unverified(self):
        out = annotate_verification_focus(
            ["题材催化剂的可证伪时间点（公告、订单、政策节点）"], [])
        assert out[0]["status"] == "未核验"
        assert out[0]["state"] == "unverified"

    def test_focus_matched_but_insufficient(self):
        out = annotate_verification_focus(
            ["债务与流动性压力（净负债/EBITDA、再融资安排）"], self.RESULTS)
        assert out[0]["status"] == "数据不足"
        assert out[0]["state"] == "insufficient"

    def test_unmatched_determinate_check_appended(self):
        # 预告 vs 一致预期没有被任何文案吸收 → 追加独立条目，核验不丢失
        out = annotate_verification_focus(
            ["盈利质量修复信号（CFO/净利比）"], self.RESULTS)
        extras = [o for o in out if o["text"] == "业绩预告 vs 一致预期"]
        assert len(extras) == 1
        assert extras[0]["status"] == "已核验·通过"

    def test_accepts_dict_form_results(self):
        # replay 路径从 meta_facts 读到的是 to_dict 后的 dict
        dicts = [r.to_dict() for r in self.RESULTS]
        out = annotate_verification_focus(["盈利质量修复信号"], dicts)
        assert out[0]["state"] == "fail"


# ═════════════════════════════════════════════════════════════════
# 3. 渲染层（任务 D3）：relative / verification 三态 / dataAsOf
# ═════════════════════════════════════════════════════════════════

def _meta_facts_phase1(**over):
    mf = {
        "ebitda": 5e8, "operating_income": 4e8, "revenue": 5e9,
        "net_income": 3e8, "total_equity": 2e9, "shares_outstanding": 4e8,
        "ttm_revenue": 4.565e9, "ttm_net_income": 1.25e8,
        "ttm_net_income_deducted": 1.672e7,
        "__expectations_frontier": MOCK_FRONTIER,
        "__pricing_regime": MOCK_REGIME,
        "__recent_events": MOCK_EVENTS,
        "__relative_valuation": dict(MOCK_RELVAL),
        "__verification": [
            {"check_id": "cfo_to_net_income",
             "name_zh": "经营现金流 / 归母净利润", "status": "fail",
             "status_zh": "未通过",
             "detail_zh": "2025-12-31 期 CFO/归母 = -9.60，低于 0.5 红旗线",
             "evidence": {"ratio": -9.6}},
            {"check_id": "forecast_vs_consensus",
             "name_zh": "业绩预告 vs 一致预期", "status": "insufficient",
             "status_zh": "数据不足",
             "detail_zh": "无有效一致预期", "evidence": {}},
        ],
        "__data_freshness": {"latest_period": "2026-03-31",
                             "basis": "quarterly", "days_since": 101},
    }
    mf.update(over)
    return mf


def _build_cn_report(**meta_over):
    return v2.build_report_dict(
        decision=_decision(),
        market_data={"current_price": 13.76},
        meta_facts=_meta_facts_phase1(**meta_over),
        scenarios=dict(_CN_SCENARIOS),
        entity_id="002669",
        entity_name="康达新材",
        entity_name_clean="康达新材",
        period="FY2025",
    )


class TestRelativeValuationBlock:

    def test_rows_and_percentiles(self):
        rel = _build_cn_report()["pricedIn"]["relative"]
        assert rel["insufficient"] is False
        assert "化学制品" in rel["note"]
        assert "181" in rel["note"]
        assert "1 家亏损已剔除 PE 样本" in rel["note"]
        pe = next(r for r in rel["rows"] if r["lbl"] == "PE(TTM)")
        assert pe["target"] == "32.6×"
        assert pe["median"] == "33.3×"
        assert pe["pct"] == "第 44 分位"
        assert pe["sample"] == "有效样本 9 家"
        pb = next(r for r in rel["rows"] if r["lbl"] == "PB")
        assert pb["target"] == "1.48×"
        assert pb["median"] == "2.01×"

    def test_insufficient_sample_gated(self):
        rel = _build_cn_report(
            __relative_valuation=dict(MOCK_RELVAL_INSUFFICIENT),
        )["pricedIn"]["relative"]
        assert rel["insufficient"] is True
        assert rel["rows"] == []
        assert "同业样本不足" in rel["note"]

    def test_single_metric_gate_disclosed(self):
        # PE 样本不足但 PB 可用 → PE 行披露样本不足、PB 行正常（红线 5）
        relval = dict(MOCK_RELVAL)
        relval.update({"peer_pe_median": None, "pe_percentile": None,
                       "pe_sample_size": 2})
        rel = _build_cn_report(__relative_valuation=relval)["pricedIn"]["relative"]
        pe = next(r for r in rel["rows"] if r["lbl"] == "PE(TTM)")
        assert "该指标样本不足" in pe["sample"]
        assert pe["median"] == "暂无"
        pb = next(r for r in rel["rows"] if r["lbl"] == "PB")
        assert pb["median"] == "2.01×"

    def test_missing_relval_block_none(self):
        p = _build_cn_report(__relative_valuation=None)["pricedIn"]
        assert p["relative"] is None  # 前端显示「暂无」


class TestVerificationRendering:

    def test_three_states_rendered(self):
        ver = _build_cn_report()["pricedIn"]["verification"]
        by_status = {v["status"] for v in ver}
        # MOCK_REGIME focus: 盈利质量修复信号（命中 cfo → 已核验·未通过）
        # + 重组/并购整合（无检查器 → 未核验）；预告检查落单 insufficient
        # 不追加（只追加有数据的），forecast insufficient 被「预告」关键词?
        # —— MOCK_REGIME 无预告文案，落单且 insufficient → 不追加。
        assert "已核验·未通过" in by_status
        assert "未核验" in by_status
        fail_item = next(v for v in ver if v["status"] == "已核验·未通过")
        assert "红旗线" in fail_item["evidence"]
        assert fail_item["state"] == "fail"

    def test_no_verification_keeps_phase0_unverified(self):
        ver = _build_cn_report(__verification=None)["pricedIn"]["verification"]
        assert ver, "验证点清单非空"
        assert all(v["status"] == "未核验" for v in ver)

    def test_insufficient_state_when_only_gated_data(self):
        mf_ver = [{
            "check_id": "cfo_to_net_income",
            "name_zh": "经营现金流 / 归母净利润",
            "status": "insufficient", "status_zh": "数据不足",
            "detail_zh": "PIT 库缺经营现金流或归母净利润数据", "evidence": {},
        }]
        ver = _build_cn_report(__verification=mf_ver)["pricedIn"]["verification"]
        quality = next(v for v in ver if "盈利质量" in v["text"])
        assert quality["status"] == "数据不足"
        assert quality["state"] == "insufficient"


class TestDataAsOf:

    def test_quarterly_basis_line(self):
        rep = _build_cn_report()
        d = rep["dataAsOf"]
        assert d["latestPeriod"] == "2026-03-31"
        assert d["basis"] == "quarterly"
        # 天数按渲染时点重算（不是缓存里的 101）
        expected_days = (date.today() - date(2026, 3, 31)).days
        assert d["days"] == expected_days
        assert d["line"] == (
            f"数据截至 2026-03-31（季报口径 · 时效 {expected_days} 天）")

    def test_annual_fallback_line(self):
        rep = _build_cn_report(
            __data_freshness={"latest_period": "2025-12-31",
                              "basis": "annual", "days_since": 190},
        )
        assert "年报口径" in rep["dataAsOf"]["line"]

    def test_missing_freshness_none(self):
        rep = _build_cn_report(__data_freshness=None)
        assert rep["dataAsOf"] is None  # 前端隐藏该行（缺数据不显示烂值）

    def test_freshness_under_90_days_for_acceptance(self):
        # Phase 1 验收线：季报可得时数据时效 <90 天。构造 60 天前的报告期
        # 走完整渲染链，确保 days 语义是「距报告期天数」而非其它口径。
        lp = (date.today() - timedelta(days=60)).isoformat()
        rep = _build_cn_report(
            __data_freshness={"latest_period": lp, "basis": "quarterly",
                              "days_since": None},
        )
        assert rep["dataAsOf"]["days"] == 60
        assert rep["dataAsOf"]["days"] < 90

    def test_days_measured_from_announce_date_when_available(self):
        """红线 #3 披露日口径：期末 101 天前但披露 73 天前 → 时效 73 天。

        季报期末 +90 天到下次披露之间，期末口径必然 >90 天（日历假警报）；
        披露日口径下「手里最新公开信息」仍然新鲜，验收线才有意义。
        """
        lp = (date.today() - timedelta(days=101)).isoformat()
        ad = (date.today() - timedelta(days=73)).isoformat()
        rep = _build_cn_report(
            __data_freshness={"latest_period": lp, "basis": "quarterly",
                              "days_since": None, "announce_date": ad},
        )
        d = rep["dataAsOf"]
        assert d["days"] == 73
        assert d["days"] < 90
        assert d["announceDate"] == ad
        assert d["latestPeriod"] == lp            # 期末照旧展示
        assert f"披露于 {ad}" in d["line"]
        assert "时效 73 天" in d["line"]


class TestEndToEndHtml:

    def test_html_contains_phase1_sections(self, monkeypatch):
        monkeypatch.setenv("AEGIS_SKIP_SPARKLINE", "1")
        html = v2.generate_html_report(
            decision=_decision(),
            computed_metrics={},
            market_data={"current_price": 13.76},
            agent_judgments=[], critic_results=[],
            meta_facts=_meta_facts_phase1(),
            scenarios=dict(_CN_SCENARIOS),
            entity_name="康达新材", entity_name_clean="康达新材",
            period="FY2025",
        )
        marker = "window.REPORT = "
        start = html.index(marker) + len(marker)
        end = html.index(";</script>", start)
        rep = json.loads(html[start:end].replace("<\\/", "</"))
        assert rep["dataAsOf"]["latestPeriod"] == "2026-03-31"
        assert rep["pricedIn"]["relative"]["rows"]
        assert any(v["state"] == "fail" for v in rep["pricedIn"]["verification"])
        # jsx 已内联 Phase 1 界面元素
        assert "相对估值（同业倍数分位）" in html
        assert "数据截至" in html


# ═════════════════════════════════════════════════════════════════
# 4. KEY FINANCIALS 注入 TTM（任务 D4，zh/en 双语）
# ═════════════════════════════════════════════════════════════════

def _agent_msg(zh: bool, with_ttm: bool = True):
    facts = {
        "revenue": 4.0e9, "net_income": 1.0e8,
    }
    if zh:
        facts["__display"] = {
            "symbol": "¥", "scale": 1e8, "unit": "亿",
            "big_scale": 1e12, "big_unit": "万亿", "currency": "CNY",
        }
    if with_ttm:
        facts.update({
            "ttm_revenue": 4.565e9,
            "ttm_net_income": 1.25e8,
            "ttm_net_income_deducted": 1.672e7,
            "__data_freshness": {"latest_period": "2026-03-31",
                                 "basis": "quarterly", "days_since": 101},
        })
    mc = {"cycle_phase": "mid"}
    if zh:
        mc["language"] = "zh-CN"
        mc["market_id"] = "cn"
    inp = AgentInput(
        entity_id="002669" if zh else "us_test",
        run_id="run_test", question_id="q_test",
        facts=facts, macro_context=mc,
    )
    agent = LLMAgentBase.__new__(LLMAgentBase)  # _build_user_message 不用 self
    return agent._build_user_message(inp)


class TestKeyFinancialsTTM:

    def test_zh_ttm_rows_and_freshness(self):
        msg = _agent_msg(zh=True)
        assert "=== KEY FINANCIALS SUMMARY ===" in msg
        assert "TTM营收（最近4季滚动）: ¥45.6亿" in msg
        assert "TTM归母净利润: ¥1.2亿" in msg
        assert "TTM扣非归母净利润: ¥0.2亿" in msg
        assert "数据截至: 2026-03-31（距今 101 天）" in msg

    def test_en_ttm_rows_and_freshness(self):
        msg = _agent_msg(zh=False)
        assert "TTM Revenue (trailing 4 quarters): $4.6B" in msg
        assert "TTM Net Income (attributable): $0.1B" in msg
        assert "TTM Net Income (ex non-recurring): $0.0B" in msg
        assert "Data as of: 2026-03-31 (101 days old)" in msg

    def test_no_ttm_keys_no_rows(self):
        msg = _agent_msg(zh=True, with_ttm=False)
        assert "TTM营收" not in msg
        assert "数据截至" not in msg


# ═════════════════════════════════════════════════════════════════
# 5. 红线 9：相对估值白名单生成器
# ═════════════════════════════════════════════════════════════════

class TestRelvalSanctionedPcts:

    def test_numbers_match_display_rounding(self):
        pcts = relative_valuation_sanctioned_pcts(MOCK_RELVAL)
        # PE/PB 目标与中位 + 两个分位，展示口径四舍五入
        for expected in (32.6, 33.3, 1.48, 2.01, 44.0, 30.0):
            assert expected in pcts

    def test_insufficient_returns_empty(self):
        # 样本不足 → zh_lines 禁止引用任何同业数字，白名单不发
        assert relative_valuation_sanctioned_pcts(MOCK_RELVAL_INSUFFICIENT) == []

    def test_none_and_garbage_safe(self):
        assert relative_valuation_sanctioned_pcts(None) == []
        assert relative_valuation_sanctioned_pcts({"insufficient_peers": True}) == []
