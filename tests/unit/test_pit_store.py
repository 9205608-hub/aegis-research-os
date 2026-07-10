"""PIT 时点库单测 — DESIGN_2.0 红线 #3 (双时间戳) / #8 (concept 词表) / #10 (sqlite3).

全库最重要的断言：晚摄取的事实在早 as_of 查询下不可见（knowledge-time 语义）。
"""

import json
import threading
from datetime import datetime, timezone

import pytest

from aegis.pit import PITFact, PITStore, PITStoreError, UnknownConceptError

T1 = "2026-04-10T08:00:00+00:00"
T2 = "2026-04-20T08:00:00+00:00"
T3 = "2026-05-05T08:00:00+00:00"
T_BETWEEN_1_2 = "2026-04-15T00:00:00+00:00"
T_BETWEEN_2_3 = "2026-04-25T00:00:00+00:00"


@pytest.fixture()
def store(tmp_path):
    s = PITStore(tmp_path / "pit.db")
    yield s
    s.close()


# =====================================================================
# as-of 正确性（红线 #3，全库最重要的断言）
# =====================================================================


class TestAsOfKnowledgeTime:
    def test_late_ingest_invisible_at_early_as_of(self, store):
        """晚摄取的事实在早 as_of 查询下必须不可见。"""
        store.record_fact(
            entity_id="002669", concept="revenue", period="2026-03-31",
            fiscal_period="Q1", value=1.156e9, source="em_lrb", as_of=T2,
        )
        # T1 < T2：查询者当时还不知道这条事实
        assert store.get_facts("002669", "revenue", as_of=T1) == []
        assert store.latest_value("002669", "revenue", as_of=T1) is None
        # T2 及之后可见
        assert len(store.get_facts("002669", "revenue", as_of=T2)) == 1
        got = store.latest_value("002669", "revenue", as_of=T3)
        assert got is not None and got.value == pytest.approx(1.156e9)

    def test_as_of_boundary_inclusive(self, store):
        """as_of 恰等于摄取时刻 → 可见（≤ 语义）。"""
        store.record_fact(
            entity_id="002669", concept="revenue", period="2025-12-31",
            fiscal_period="FY", value=4.0e9, source="em_lrb", as_of=T1,
        )
        assert len(store.get_facts("002669", "revenue", as_of=T1)) == 1

    def test_as_of_none_sees_everything(self, store):
        store.record_fact(
            entity_id="002669", concept="revenue", period="2025-12-31",
            fiscal_period="FY", value=4.0e9, source="em_lrb", as_of=T3,
        )
        assert len(store.get_facts("002669", "revenue")) == 1

    def test_earlier_period_ingested_later_still_invisible(self, store):
        """描述更早报告期、但更晚才摄取（如回填）的事实同样受 as_of 约束。"""
        store.record_fact(
            entity_id="002669", concept="revenue", period="2024-12-31",
            fiscal_period="FY", value=3.0e9, source="em_lrb",
            as_of=T3, backfilled=True,
        )
        assert store.get_facts("002669", "revenue", as_of=T1) == []


# =====================================================================
# 重述链：新版本 + 旧版本可查
# =====================================================================


class TestRestatementVersioning:
    def _record_two_versions(self, store):
        id1 = store.record_fact(
            entity_id="002669", concept="net_income", period="2025-12-31",
            fiscal_period="FY", value=1.25e8, source="em_lrb", as_of=T1,
        )
        id2 = store.record_fact(
            entity_id="002669", concept="net_income", period="2025-12-31",
            fiscal_period="FY", value=1.10e8, source="em_lrb", as_of=T2,
        )
        return id1, id2

    def test_restatement_creates_new_version(self, store):
        id1, id2 = self._record_two_versions(store)
        assert id1 != id2
        facts = store.get_facts("002669", "net_income")
        assert [f.fact_version for f in facts] == [1, 2]
        v2 = facts[1]
        assert v2.restatement_of == id1
        assert v2.value == pytest.approx(1.10e8)

    def test_old_version_still_queryable(self, store):
        id1, _ = self._record_two_versions(store)
        v1 = [f for f in store.get_facts("002669", "net_income") if f.id == id1]
        assert len(v1) == 1 and v1[0].value == pytest.approx(1.25e8)

    def test_latest_value_returns_newest_version(self, store):
        self._record_two_versions(store)
        got = store.latest_value("002669", "net_income")
        assert got.fact_version == 2 and got.value == pytest.approx(1.10e8)

    def test_as_of_between_versions_returns_old_value(self, store):
        """knowledge-time 回放：重述发生前的视角必须看到旧值。"""
        self._record_two_versions(store)
        got = store.latest_value("002669", "net_income", as_of=T_BETWEEN_1_2)
        assert got.fact_version == 1 and got.value == pytest.approx(1.25e8)

    def test_same_value_rerecord_is_idempotent(self, store):
        id1 = store.record_fact(
            entity_id="002669", concept="net_income", period="2025-12-31",
            fiscal_period="FY", value=1.25e8, source="em_lrb", as_of=T1,
        )
        id_again = store.record_fact(
            entity_id="002669", concept="net_income", period="2025-12-31",
            fiscal_period="FY", value=1.25e8, source="em_lrb", as_of=T2,
        )
        assert id_again == id1
        facts = store.get_facts("002669", "net_income")
        assert len(facts) == 1
        # 幂等重录不得篡改原始摄取时间（库内格式带微秒，按前缀比对）
        assert facts[0].as_of == "2026-04-10T08:00:00.000000+00:00"

    def test_different_source_is_separate_chain(self, store):
        """不同 source 各自维护版本链，不互相触发重述。"""
        store.record_fact(
            entity_id="002669", concept="net_income", period="2025-12-31",
            fiscal_period="FY", value=1.25e8, source="em_lrb", as_of=T1,
        )
        store.record_fact(
            entity_id="002669", concept="net_income", period="2025-12-31",
            fiscal_period="FY", value=1.20e8, source="cninfo", as_of=T2,
        )
        facts = store.get_facts("002669", "net_income")
        assert [f.fact_version for f in facts] == [1, 1]
        assert all(f.restatement_of is None for f in facts)


# =====================================================================
# 区间型事实（业绩预告）
# =====================================================================


class TestRangeFacts:
    def test_forecast_range_roundtrip(self, store):
        store.record_fact(
            entity_id="002669", concept="net_income", period="2026-06-30",
            fiscal_period="H1", value=None, value_low=2.0e7, value_high=3.5e7,
            source="em_forecast", as_of=T1, announce_date="2026-04-09",
            unaudited=True, meta={"forecast_type": "预增"},
        )
        got = store.latest_value("002669", "net_income", prefer_audited=False)
        assert got.value is None
        assert got.value_low == pytest.approx(2.0e7)
        assert got.value_high == pytest.approx(3.5e7)
        assert got.announce_date == "2026-04-09"
        assert got.meta == {"forecast_type": "预增"}

    def test_value_and_range_both_none_raises(self, store):
        with pytest.raises(PITStoreError):
            store.record_fact(
                entity_id="002669", concept="net_income", period="2026-06-30",
                fiscal_period="H1", source="em_forecast",
            )


# =====================================================================
# registry 校验（红线 #8：禁自由字符串）
# =====================================================================


class TestConceptRegistry:
    def test_unregistered_concept_raises(self, store):
        with pytest.raises(UnknownConceptError):
            store.record_fact(
                entity_id="002669", concept="made_up_concept_xyz",
                period="2025-12-31", value=1.0, source="em_lrb",
            )

    def test_seeded_registry_concepts_pass(self, store):
        # metric_name 与 allowed_inputs 两个来源都要在词表内
        for concept in ("revenue", "net_income", "cfo", "gross_margin", "total_debt"):
            store.record_fact(
                entity_id="002669", concept=concept,
                period="2025-12-31", fiscal_period="FY", value=1.0, source="em_lrb",
            )

    def test_register_concept_escape_hatch(self, store):
        store.register_concept("deducted_net_income")  # 扣非归母（A 股双轨）
        fact_id = store.record_fact(
            entity_id="002669", concept="deducted_net_income",
            period="2025-12-31", fiscal_period="FY", value=1.672e7, source="em_lrb",
        )
        assert fact_id > 0

    def test_register_concept_rejects_empty(self, store):
        with pytest.raises(PITStoreError):
            store.register_concept("")


# =====================================================================
# 标志位往返 + 序列化
# =====================================================================


class TestFlagsAndSerialization:
    def test_backfilled_unaudited_roundtrip(self, store):
        store.record_fact(
            entity_id="002669", concept="revenue", period="2023-12-31",
            fiscal_period="FY", value=2.8e9, source="em_lrb",
            backfilled=True, unaudited=True,
        )
        got = store.latest_value("002669", "revenue", prefer_audited=False)
        assert got.backfilled is True
        assert got.unaudited is True

    def test_flags_default_false(self, store):
        store.record_fact(
            entity_id="002669", concept="revenue", period="2025-12-31",
            fiscal_period="FY", value=4.0e9, source="em_lrb",
        )
        got = store.latest_value("002669", "revenue")
        assert got.backfilled is False and got.unaudited is False

    def test_to_dict_json_serializable(self, store):
        store.record_fact(
            entity_id="002669", concept="revenue", period="2025-12-31",
            fiscal_period="FY", value=4.0e9, source="em_lrb",
            announce_date="2026-04-20", meta={"note": "年报", "rows": 3},
        )
        got = store.latest_value("002669", "revenue")
        assert isinstance(got, PITFact)
        payload = json.dumps(got.to_dict(), ensure_ascii=False)
        back = json.loads(payload)
        assert back["value"] == pytest.approx(4.0e9)
        assert back["meta"] == {"note": "年报", "rows": 3}
        assert back["backfilled"] is False


# =====================================================================
# latest_value 选取规则
# =====================================================================


class TestLatestValue:
    def test_picks_latest_period(self, store):
        store.record_fact(
            entity_id="002669", concept="revenue", period="2025-12-31",
            fiscal_period="FY", value=4.0e9, source="em_lrb", as_of=T1,
        )
        store.record_fact(
            entity_id="002669", concept="revenue", period="2026-03-31",
            fiscal_period="Q1", value=1.156e9, source="em_lrb", as_of=T1,
        )
        got = store.latest_value("002669", "revenue")
        assert got.period == "2026-03-31"

    def test_prefer_audited_over_flash_report(self, store):
        """快报（unaudited）先到，正式年报后到：默认偏好审计值。"""
        store.record_fact(  # 业绩快报
            entity_id="002669", concept="net_income", period="2025-12-31",
            fiscal_period="FY", value=1.30e8, source="em_kuaibao",
            as_of=T1, unaudited=True,
        )
        store.record_fact(  # 正式年报
            entity_id="002669", concept="net_income", period="2025-12-31",
            fiscal_period="FY", value=1.25e8, source="em_lrb", as_of=T2,
        )
        store.record_fact(  # 快报的更晚修正（仍未审计）
            entity_id="002669", concept="net_income", period="2025-12-31",
            fiscal_period="FY", value=1.28e8, source="em_kuaibao",
            as_of=T3, unaudited=True,
        )
        audited = store.latest_value("002669", "net_income", prefer_audited=True)
        assert audited.value == pytest.approx(1.25e8) and audited.unaudited is False
        any_latest = store.latest_value("002669", "net_income", prefer_audited=False)
        assert any_latest.value == pytest.approx(1.28e8)

    def test_prefer_audited_falls_back_when_only_flash(self, store):
        store.record_fact(
            entity_id="002669", concept="net_income", period="2025-12-31",
            fiscal_period="FY", value=1.30e8, source="em_kuaibao",
            as_of=T1, unaudited=True,
        )
        got = store.latest_value("002669", "net_income", prefer_audited=True)
        assert got is not None and got.value == pytest.approx(1.30e8)

    def test_missing_entity_returns_none(self, store):
        assert store.latest_value("600519", "revenue") is None


# =====================================================================
# 输入校验
# =====================================================================


class TestValidation:
    def test_invalid_fiscal_period_raises(self, store):
        with pytest.raises(PITStoreError):
            store.record_fact(
                entity_id="002669", concept="revenue", period="2026-06-30",
                fiscal_period="Q2", value=1.0, source="em_lrb",
            )

    def test_invalid_period_raises(self, store):
        with pytest.raises(PITStoreError):
            store.record_fact(
                entity_id="002669", concept="revenue", period="not-a-date",
                value=1.0, source="em_lrb",
            )

    def test_empty_entity_or_source_raises(self, store):
        with pytest.raises(PITStoreError):
            store.record_fact(
                entity_id="", concept="revenue", period="2025-12-31",
                value=1.0, source="em_lrb",
            )
        with pytest.raises(PITStoreError):
            store.record_fact(
                entity_id="002669", concept="revenue", period="2025-12-31",
                value=1.0, source="",
            )

    def test_naive_and_aware_as_of_normalized(self, store):
        """naive datetime 视为 UTC，与带时区输入落在同一可比坐标系。"""
        store.record_fact(
            entity_id="002669", concept="revenue", period="2025-12-31",
            fiscal_period="FY", value=4.0e9, source="em_lrb",
            as_of=datetime(2026, 4, 10, 8, 0, 0),  # naive
        )
        got = store.latest_value(
            "002669", "revenue",
            as_of=datetime(2026, 4, 10, 8, 0, 0, tzinfo=timezone.utc),
        )
        assert got is not None
        assert got.as_of.endswith("+00:00")


# =====================================================================
# 并发写
# =====================================================================


class TestConcurrency:
    def test_concurrent_writes_do_not_corrupt(self, store, tmp_path):
        n_threads, n_each = 8, 25
        errors = []

        def writer(tid: int) -> None:
            try:
                for i in range(n_each):
                    store.record_fact(
                        entity_id=f"E{tid}", concept="revenue",
                        period=f"20{10 + i}-12-31", fiscal_period="FY",
                        value=float(tid * 1000 + i), source="em_lrb",
                    )
            except Exception as exc:  # noqa: BLE001 - 收集断言用
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        total = sum(len(store.get_facts(f"E{t}", "revenue")) for t in range(n_threads))
        assert total == n_threads * n_each
