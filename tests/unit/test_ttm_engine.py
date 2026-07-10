"""TTM 引擎回归 — 红线 #4（只流量科目 / 累计差分 / 归母扣非双轨）+ 边界.

覆盖（DESIGN_2.0 §三.B TTM 引擎规格逐条锁定）：
1. 累计差分正确性：TTM = FY_prev + YTD_cur − YTD_prev_same，
   用 2026-07-10 东财 F10 实测的康达(002669)/茅台(600519)数字做 golden；
2. FY 边界：最新期 = 年报 → TTM 即 FY 值本身（专项回归，设计文档点名）；
3. 跨重述窗口：同期重录新版本 → TTM 吃最新版本（专项回归，设计文档点名）；
4. 快报替换：同期 unaudited 快报与审计年报并存 → 审计值优先；
5. 降级语义：三元组缺任一期 → None + 中文原因（宁缺勿假，禁年化外推）；
6. 双轨：ttm_net_income 与 ttm_net_income_deducted 并行独立计算；
7. 封闭目录：TTM_FLOW_CONCEPTS 不含任何时点科目（红线 #4 结构性保障）；
8. 永不 raise：store=None / get_facts 抛错 / NaN 值。
"""

import math

import pytest

from aegis.core.truth.ttm_engine import (
    TTM_FLOW_CONCEPTS,
    TTMSnapshot,
    _ttm_for_concept,
    ttm_snapshot,
)
from aegis.pit.store import PITStore

# ── 2026-07-10 东财 F10 实测数字（golden 对账基准） ──────────────────
# 康达新材 002669
KD = {
    ("revenue", "2026-03-31"): 1_155_707_868.0,
    ("revenue", "2025-12-31"): 5_236_874_842.0,
    ("revenue", "2025-03-31"): 877_010_302.0,
    ("net_income", "2026-03-31"): 6_864_387.0,
    ("net_income", "2025-12-31"): 125_439_848.0,
    ("net_income", "2025-03-31"): 6_371_843.0,
    ("net_income_deducted", "2026-03-31"): 4_490_057.29,
    ("net_income_deducted", "2025-12-31"): 16_723_356.61,
    ("net_income_deducted", "2025-03-31"): 3_363_744.0,
}
#: 手工对账：TTM 营收 = 52.37亿 + 11.56亿 − 8.77亿 ≈ 55.16亿
KD_TTM_REVENUE = 5_236_874_842.0 + 1_155_707_868.0 - 877_010_302.0
KD_TTM_NI = 125_439_848.0 + 6_864_387.0 - 6_371_843.0
KD_TTM_DED = 16_723_356.61 + 4_490_057.29 - 3_363_744.0


@pytest.fixture
def store(tmp_path):
    s = PITStore(tmp_path / "pit.db")
    s.register_concept("net_income_deducted")
    yield s
    s.close()


def _load(store, entity, table, *, fiscal_by_mmdd=None, unaudited=True):
    fiscal_by_mmdd = fiscal_by_mmdd or {
        "03-31": "Q1", "06-30": "H1", "09-30": "Q3", "12-31": "FY"}
    for (concept, period), value in table.items():
        store.record_fact(
            entity_id=entity, concept=concept, period=period,
            fiscal_period=fiscal_by_mmdd[period[5:]], value=value,
            announce_date=period, source="em_f10_income",
            unaudited=(unaudited and period[5:] != "12-31"),
        )


# ─────────────────────────────────────────────────────────────────
# 1. 累计差分 golden（康达实测数字）
# ─────────────────────────────────────────────────────────────────

class TestCumulativeDiff:
    def test_kd_golden_revenue(self, store):
        _load(store, "002669", KD)
        snap = ttm_snapshot(store, "002669")
        assert snap.ttm_revenue == pytest.approx(KD_TTM_REVENUE)
        # 数量级 sanity：55.16 亿
        assert snap.ttm_revenue == pytest.approx(5.5156e9, rel=1e-3)

    def test_kd_golden_dual_track(self, store):
        """红线 #4 双轨：归母 与 扣非归母 并行独立（康达差 7 倍）。"""
        _load(store, "002669", KD)
        snap = ttm_snapshot(store, "002669")
        assert snap.ttm_net_income == pytest.approx(KD_TTM_NI)
        assert snap.ttm_net_income_deducted == pytest.approx(KD_TTM_DED)
        # 成色差异保真：扣非 TTM 远小于归母 TTM
        assert snap.ttm_net_income_deducted < snap.ttm_net_income / 5

    def test_moutai_golden_revenue(self, store):
        _load(store, "600519", {
            ("revenue", "2026-03-31"): 54_702_912_385.0,
            ("revenue", "2025-12-31"): 172_054_171_891.0,
            ("revenue", "2025-03-31"): 51_443_450_584.0,
        })
        snap = ttm_snapshot(store, "600519")
        assert snap.ttm_revenue == pytest.approx(
            172_054_171_891.0 + 54_702_912_385.0 - 51_443_450_584.0)

    def test_h1_and_q3_periods_also_diff(self, store):
        """差分公式对 H1/Q3 累计期同样成立（不是只会算 Q1）。"""
        _load(store, "X", {
            ("revenue", "2026-09-30"): 30.0,
            ("revenue", "2025-12-31"): 40.0,
            ("revenue", "2025-09-30"): 28.0,
        })
        snap = ttm_snapshot(store, "X")
        assert snap.ttm_revenue == pytest.approx(40.0 + 30.0 - 28.0)

    def test_latest_period_and_basis_provenance(self, store):
        _load(store, "002669", KD)
        snap = ttm_snapshot(store, "002669")
        assert snap.latest_period == "2026-03-31"
        b = snap.basis["ttm_revenue"]
        assert b["method"] == "cumulative_diff"
        assert set(b["periods"]) == {"2025-12-31", "2026-03-31", "2025-03-31"}

    def test_negative_values_flow_through(self, store):
        """亏损期（负累计值）照常差分，不做任何符号夹逼。"""
        _load(store, "X", {
            ("net_income", "2026-03-31"): -5.0,
            ("net_income", "2025-12-31"): -100.0,
            ("net_income", "2025-03-31"): 10.0,
        })
        snap = ttm_snapshot(store, "X")
        assert snap.ttm_net_income == pytest.approx(-100.0 - 5.0 - 10.0)


# ─────────────────────────────────────────────────────────────────
# 2. FY 边界（设计文档点名的专项回归）
# ─────────────────────────────────────────────────────────────────

class TestFYBoundary:
    def test_latest_is_fy_uses_fy_directly(self, store):
        _load(store, "002669", {
            ("revenue", "2025-12-31"): 5_236_874_842.0,
            ("revenue", "2025-09-30"): 3_749_809_890.0,
            ("revenue", "2024-12-31"): 3_101_062_179.0,
        })
        snap = ttm_snapshot(store, "002669")
        assert snap.ttm_revenue == pytest.approx(5_236_874_842.0)
        assert snap.basis["ttm_revenue"]["method"] == "fy_direct"
        assert snap.latest_period == "2025-12-31"

    def test_fy_direct_does_not_need_prior_year(self, store):
        """FY 直取不依赖上年数据（只入库一期年报也能出 TTM）。"""
        _load(store, "X", {("revenue", "2025-12-31"): 42.0})
        snap = ttm_snapshot(store, "X")
        assert snap.ttm_revenue == pytest.approx(42.0)


# ─────────────────────────────────────────────────────────────────
# 3. 跨重述窗口 + 快报替换（PIT 版本链纪律）
# ─────────────────────────────────────────────────────────────────

class TestRestatementWindow:
    def test_restated_prior_fy_uses_latest_version(self, store):
        """上年 FY 被重述 → TTM 吃重述后的最新版本（设计文档点名回归）。"""
        _load(store, "X", {
            ("revenue", "2026-03-31"): 10.0,
            ("revenue", "2025-12-31"): 100.0,
            ("revenue", "2025-03-31"): 8.0,
        })
        # 年报重述：100 → 90（同键不同值 = 新 fact_version，旧版保留）
        store.record_fact(
            entity_id="X", concept="revenue", period="2025-12-31",
            fiscal_period="FY", value=90.0, announce_date="2026-04-30",
            source="em_f10_income",
        )
        snap = ttm_snapshot(store, "X")
        assert snap.ttm_revenue == pytest.approx(90.0 + 10.0 - 8.0)

    def test_unaudited_flash_superseded_by_audited(self, store):
        """快报（unaudited）与正式年报并存 → 审计值优先。"""
        store.record_fact(
            entity_id="X", concept="revenue", period="2025-12-31",
            fiscal_period="FY", value=105.0, announce_date="2026-02-25",
            source="em_flash", unaudited=True,
        )
        store.record_fact(
            entity_id="X", concept="revenue", period="2025-12-31",
            fiscal_period="FY", value=100.0, announce_date="2026-04-25",
            source="em_f10_income", unaudited=False,
        )
        snap = ttm_snapshot(store, "X")
        assert snap.ttm_revenue == pytest.approx(100.0)


# ─────────────────────────────────────────────────────────────────
# 4. 降级语义（宁缺勿假）
# ─────────────────────────────────────────────────────────────────

class TestDegradation:
    def test_missing_prior_same_period_gives_none(self, store):
        _load(store, "X", {
            ("revenue", "2026-03-31"): 10.0,
            ("revenue", "2025-12-31"): 100.0,
            # 缺 2025-03-31
        })
        snap = ttm_snapshot(store, "X")
        assert snap.ttm_revenue is None
        assert "2025-03-31" in snap.basis["ttm_revenue"]["reason_zh"]
        # 数据在库这一事实不丢：latest_period 仍然可用于数据截至行
        assert snap.latest_period == "2026-03-31"

    def test_missing_prior_fy_gives_none(self, store):
        _load(store, "X", {
            ("revenue", "2026-03-31"): 10.0,
            ("revenue", "2025-03-31"): 8.0,
        })
        snap = ttm_snapshot(store, "X")
        assert snap.ttm_revenue is None
        assert "2025-12-31" in snap.basis["ttm_revenue"]["reason_zh"]

    def test_empty_store_all_none(self, store):
        snap = ttm_snapshot(store, "NOBODY")
        assert isinstance(snap, TTMSnapshot)
        assert snap.ttm_revenue is None
        assert snap.ttm_net_income is None
        assert snap.ttm_net_income_deducted is None
        assert snap.latest_period is None

    def test_tracks_degrade_independently(self, store):
        """revenue 齐 / 扣非缺 → 前者出值后者 None（双轨互不拖累）。"""
        _load(store, "X", {
            ("revenue", "2026-03-31"): 10.0,
            ("revenue", "2025-12-31"): 100.0,
            ("revenue", "2025-03-31"): 8.0,
            ("net_income_deducted", "2026-03-31"): 1.0,
        })
        snap = ttm_snapshot(store, "X")
        assert snap.ttm_revenue == pytest.approx(102.0)
        assert snap.ttm_net_income_deducted is None

    def test_store_none_never_raises(self):
        snap = ttm_snapshot(None, "002669")
        assert snap.ttm_revenue is None
        assert snap.latest_period is None

    def test_broken_store_never_raises(self):
        class Boom:
            def get_facts(self, *a, **k):
                raise RuntimeError("db locked")
        snap = ttm_snapshot(Boom(), "002669")
        assert snap.ttm_revenue is None

    def test_nan_value_treated_as_missing(self):
        """NaN 穿透有前科（红线 5 同族）：非有限数按缺失处理。"""
        ttm, why = _ttm_for_concept({})
        assert ttm is None and "reason_zh" in why

        class F:
            def __init__(self, period, value):
                self.period, self.value = period, value
                self.unaudited, self.as_of = False, "t"
                self.fact_version, self.id = 1, 1
        by_period = {
            "2026-03-31": F("2026-03-31", 10.0),
            "2025-12-31": F("2025-12-31", math.nan),
            "2025-03-31": F("2025-03-31", 8.0),
        }
        ttm, why = _ttm_for_concept(by_period)
        assert ttm is None
        assert "2025-12-31" in why["reason_zh"]


# ─────────────────────────────────────────────────────────────────
# 5. 结构性红线 + 序列化
# ─────────────────────────────────────────────────────────────────

class TestStructuralRedlines:
    def test_flow_catalog_contains_no_balance_concepts(self):
        """红线 #4：封闭目录只有流量科目——时点科目结构上进不了 TTM。"""
        point_in_time = {
            "total_assets", "total_liabilities", "total_equity_attributable",
            "cash_and_equivalents", "inventory", "accounts_receivable",
        }
        assert point_in_time.isdisjoint(set(TTM_FLOW_CONCEPTS.values()))

    def test_catalog_covers_exactly_the_meta_keys(self):
        """引擎输出键与 orchestrator 红线 8 豁免键一一对应。"""
        from aegis.core.orchestrator.auto_research import TTM_META_KEYS
        assert set(TTM_FLOW_CONCEPTS) == set(TTM_META_KEYS)

    def test_to_dict_json_serializable(self, store):
        import json
        _load(store, "002669", KD)
        d = ttm_snapshot(store, "002669").to_dict()
        json.dumps(d)  # 不抛即通过
        assert d["ttm_revenue"] == pytest.approx(KD_TTM_REVENUE)
        assert d["latest_period"] == "2026-03-31"

    def test_orchestrator_seam_discovers_engine(self, store):
        """_default_ttm_snapshot 能通过 import + 函数名发现本引擎。"""
        from aegis.core.orchestrator.auto_research import _default_ttm_snapshot
        _load(store, "002669", KD)
        snap = _default_ttm_snapshot(store, "002669")
        assert snap is not None
        assert snap.ttm_revenue == pytest.approx(KD_TTM_REVENUE)

    def test_full_wiring_meta_updates(self, store):
        """run_quarterly_pit_step（注入真引擎 + 假摄取）→ meta_facts 三键齐。"""
        from aegis.core.orchestrator.auto_research import run_quarterly_pit_step
        _load(store, "002669", KD)
        _, updates = run_quarterly_pit_step(
            "002669", store=store,
            ingest_fn=lambda s, c: None,   # 摄取已完成（上面 _load）
            ttm_fn=ttm_snapshot,
        )
        assert updates["ttm_revenue"] == pytest.approx(KD_TTM_REVENUE)
        assert updates["ttm_net_income"] == pytest.approx(KD_TTM_NI)
        assert updates["ttm_net_income_deducted"] == pytest.approx(KD_TTM_DED)
        assert updates["__data_freshness"]["latest_period"] == "2026-03-31"
        assert updates["__data_freshness"]["basis"] == "quarterly"
