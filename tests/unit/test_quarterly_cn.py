"""A 股季报连接器回归 — 解析层纯函数 + 摄取端契约（网络零依赖）.

覆盖：
1. parse_statement_facts：东财行样 → ParsedFact（期末节点过滤、缺值跳过、
   capex 翻负号、unaudited 判定、meta 溯源字段）；
2. 红线 #3：_is_backfill 阈值判定（披露日缺失保守不标）；
3. 红线 #8：EXTRA_CONCEPTS 注册后全部 concept 可入库（含新增的
   accounts_receivable / inventory / total_liabilities 验证点科目）；
4. ingest_quarterly：mock 网络层 → 全链入库（as_of/announce_date 双时间戳
   落库非空）；单表失败静默降级不打断其余报表；永不 raise。
5. 工具：_clean_code / _secucode 后缀与交易所推断。
"""

from datetime import date

import pytest

from aegis.core.acquisition.connectors import quarterly_cn as q
from aegis.pit.store import PITStore

# ── 东财 F10 行样（2026-07-10 实测 002669 字段名/量级） ────────────────
INCOME_ROW_Q1 = {
    "REPORT_DATE": "2026-03-31 00:00:00",
    "NOTICE_DATE": "2026-04-28 00:00:00",
    "OPINION_TYPE": None,
    "REPORT_TYPE": "一季报",
    "TOTAL_OPERATE_INCOME": 1_155_707_868.0,
    "PARENT_NETPROFIT": 6_864_387.0,
    "DEDUCT_PARENT_NETPROFIT": 4_490_057.29,
    "OPERATE_PROFIT": 9_000_000.0,
}
INCOME_ROW_FY = {
    "REPORT_DATE": "2025-12-31 00:00:00",
    "NOTICE_DATE": "2026-04-25 00:00:00",
    "OPINION_TYPE": "标准无保留意见",
    "REPORT_TYPE": "年报",
    "TOTAL_OPERATE_INCOME": 5_236_874_842.0,
    "PARENT_NETPROFIT": 125_439_848.0,
    "DEDUCT_PARENT_NETPROFIT": 16_723_356.61,
    "OPERATE_PROFIT": 100_000_000.0,
}
CASHFLOW_ROW = {
    "REPORT_DATE": "2026-03-31 00:00:00",
    "NOTICE_DATE": "2026-04-28 00:00:00",
    "OPINION_TYPE": None,
    "NETCASH_OPERATE": -1_199_000_000.0,
    "CONSTRUCT_LONG_ASSET": 361_000_000.0,   # 东财存正的流出额
}
BALANCE_ROW = {
    "REPORT_DATE": "2026-03-31 00:00:00",
    "NOTICE_DATE": "2026-04-28 00:00:00",
    "OPINION_TYPE": None,
    "MONETARYFUNDS": 1_500_000_000.0,
    "TOTAL_ASSETS": 9_635_219_067.88,
    "TOTAL_PARENT_EQUITY": 2_800_000_000.0,
    "ACCOUNTS_RECE": 2_598_006_671.45,
    "INVENTORY": 740_745_120.91,
    "TOTAL_LIABILITIES": 6_644_308_774.59,
}


# ─────────────────────────────────────────────────────────────────
# 1. 解析层
# ─────────────────────────────────────────────────────────────────

class TestParseStatementFacts:
    def test_income_row_parses_all_concepts(self):
        facts = q.parse_statement_facts(
            [INCOME_ROW_Q1], q.INCOME_CONCEPTS, "em_f10_income")
        by_concept = {f.concept: f for f in facts}
        assert set(by_concept) == set(q.INCOME_CONCEPTS)
        rev = by_concept["revenue"]
        assert rev.value == pytest.approx(1_155_707_868.0)
        assert rev.period == "2026-03-31"
        assert rev.fiscal_period == "Q1"
        assert rev.announce_date == "2026-04-28"
        assert rev.unaudited is True          # 季报无审计意见
        assert rev.meta["em_column"] == "TOTAL_OPERATE_INCOME"
        assert rev.meta["report_type"] == "一季报"

    def test_fy_row_is_audited(self):
        facts = q.parse_statement_facts(
            [INCOME_ROW_FY], q.INCOME_CONCEPTS, "em_f10_income")
        assert all(f.unaudited is False for f in facts)
        assert all(f.fiscal_period == "FY" for f in facts)
        assert facts[0].meta["opinion_type"] == "标准无保留意见"

    def test_capex_sign_flip(self):
        """东财存正的现金流出额 → 带符号口径为负（SIGN_FLIP_CAPEX）。"""
        facts = q.parse_statement_facts(
            [CASHFLOW_ROW], q.CASHFLOW_CONCEPTS, "em_f10_cashflow")
        by_concept = {f.concept: f for f in facts}
        assert by_concept["capex_ppe"].value == pytest.approx(-361_000_000.0)
        assert by_concept["cfo"].value == pytest.approx(-1_199_000_000.0)  # 原值已带号

    def test_balance_row_covers_verification_concepts(self):
        """新增三科目（应收/存货/负债合计）解析进 PIT——验证点核验依赖。"""
        facts = q.parse_statement_facts(
            [BALANCE_ROW], q.BALANCE_CONCEPTS, "em_f10_balance")
        by_concept = {f.concept: f.value for f in facts}
        assert by_concept["accounts_receivable"] == pytest.approx(2_598_006_671.45)
        assert by_concept["inventory"] == pytest.approx(740_745_120.91)
        assert by_concept["total_liabilities"] == pytest.approx(6_644_308_774.59)

    def test_non_disclosure_period_skipped(self):
        row = dict(INCOME_ROW_Q1, REPORT_DATE="2026-02-28 00:00:00")
        assert q.parse_statement_facts([row], q.INCOME_CONCEPTS, "s") == []

    def test_missing_values_skipped_not_null(self):
        row = dict(INCOME_ROW_Q1)
        row["TOTAL_OPERATE_INCOME"] = None
        facts = q.parse_statement_facts([row], q.INCOME_CONCEPTS, "s")
        assert "revenue" not in {f.concept for f in facts}
        assert len(facts) == len(q.INCOME_CONCEPTS) - 1

    def test_malformed_report_date_skipped(self):
        assert q.parse_statement_facts(
            [{"REPORT_DATE": "garbage"}], q.INCOME_CONCEPTS, "s") == []


# ─────────────────────────────────────────────────────────────────
# 2. 红线 #3：backfill 判定
# ─────────────────────────────────────────────────────────────────

class TestBackfill:
    TODAY = date(2026, 7, 10)

    def test_old_announce_is_backfill(self):
        assert q._is_backfill("2026-04-28", self.TODAY, 30) is True

    def test_recent_announce_not_backfill(self):
        assert q._is_backfill("2026-07-01", self.TODAY, 30) is False

    def test_missing_announce_conservatively_not_flagged(self):
        assert q._is_backfill(None, self.TODAY, 30) is False
        assert q._is_backfill("not-a-date", self.TODAY, 30) is False


# ─────────────────────────────────────────────────────────────────
# 3. 红线 #8：概念词表
# ─────────────────────────────────────────────────────────────────

class TestConceptGovernance:
    def test_all_concepts_known_after_registration(self, tmp_path):
        store = PITStore(tmp_path / "pit.db")
        q.register_quarterly_concepts(store)
        for cmap in (q.INCOME_CONCEPTS, q.CASHFLOW_CONCEPTS, q.BALANCE_CONCEPTS):
            for concept in cmap:
                assert store.is_known_concept(concept), concept
        store.close()

    def test_extra_concepts_cover_registry_gaps(self):
        """EXTRA_CONCEPTS 与三表映射的差集自洽（漏注册在 CI 就炸）。"""
        from aegis.core.truth.registry.seed_metrics import create_seeded_registry
        registry_vocab: set[str] = set()
        for d in create_seeded_registry().list_all():
            registry_vocab.add(d.metric_name)
            registry_vocab.update(d.allowed_inputs)
        all_concepts = (set(q.INCOME_CONCEPTS) | set(q.CASHFLOW_CONCEPTS)
                        | set(q.BALANCE_CONCEPTS))
        assert all_concepts - registry_vocab == set(q.EXTRA_CONCEPTS)


# ─────────────────────────────────────────────────────────────────
# 4. 摄取端（mock 网络层）
# ─────────────────────────────────────────────────────────────────

@pytest.fixture
def store(tmp_path):
    s = PITStore(tmp_path / "pit.db")
    yield s
    s.close()


def _mock_fetch(rows_by_report):
    def fetch(report_name, secucode, n_periods):
        rows = rows_by_report.get(report_name)
        if isinstance(rows, Exception):
            raise rows
        return rows or []
    return fetch


class TestIngestQuarterly:
    def test_full_ingest_dual_timestamps(self, store, monkeypatch):
        monkeypatch.setattr(q, "_fetch_statement_rows", _mock_fetch({
            "RPT_F10_FINANCE_GINCOME": [INCOME_ROW_Q1, INCOME_ROW_FY],
            "RPT_F10_FINANCE_GCASHFLOW": [CASHFLOW_ROW],
            "RPT_F10_FINANCE_GBALANCE": [BALANCE_ROW],
        }))
        res = q.ingest_quarterly(store, "002669.SZ")
        assert res.stock_code == "002669"
        assert res.facts_written == len(q.INCOME_CONCEPTS) * 2 + 2 + 6
        assert res.periods == ["2026-03-31", "2025-12-31"]
        assert sorted(res.sources_ok) == [
            "em_f10_balance", "em_f10_cashflow", "em_f10_income"]
        # 红线 #3：双时间戳全部非空；披露日早于摄取日 >30 天 → backfilled
        for f in store.get_facts("002669"):
            assert f.as_of, "as_of 摄取时间戳必须非空"
            assert f.announce_date, "announce_date 披露日必须非空"
            assert f.backfilled is True  # 2026-04-28 披露，注入远晚于 30 天

    def test_recent_announce_not_backfilled(self, store, monkeypatch):
        monkeypatch.setattr(q, "_fetch_statement_rows", _mock_fetch({
            "RPT_F10_FINANCE_GINCOME": [INCOME_ROW_Q1],
        }))
        q.ingest_quarterly(store, "002669", as_of="2026-04-30")
        facts = store.get_facts("002669", "revenue")
        assert facts and all(f.backfilled is False for f in facts)

    def test_single_statement_failure_degrades_silently(self, store, monkeypatch):
        monkeypatch.setattr(q, "_fetch_statement_rows", _mock_fetch({
            "RPT_F10_FINANCE_GINCOME": TimeoutError("connect timeout"),
            "RPT_F10_FINANCE_GCASHFLOW": [CASHFLOW_ROW],
            "RPT_F10_FINANCE_GBALANCE": [BALANCE_ROW],
        }))
        res = q.ingest_quarterly(store, "002669")   # 不 raise
        assert "em_f10_income" not in res.sources_ok
        assert any("em_f10_income" in e for e in res.errors)
        assert res.facts_written == 2 + 6           # 其余两表照常入库

    def test_all_failures_never_raise(self, store, monkeypatch):
        boom = ConnectionError("network down")
        monkeypatch.setattr(q, "_fetch_statement_rows", _mock_fetch({
            "RPT_F10_FINANCE_GINCOME": boom,
            "RPT_F10_FINANCE_GCASHFLOW": boom,
            "RPT_F10_FINANCE_GBALANCE": boom,
        }))
        res = q.ingest_quarterly(store, "002669")
        assert res.facts_written == 0
        assert len(res.errors) == 3

    def test_reingest_idempotent(self, store, monkeypatch):
        monkeypatch.setattr(q, "_fetch_statement_rows", _mock_fetch({
            "RPT_F10_FINANCE_GINCOME": [INCOME_ROW_Q1],
        }))
        q.ingest_quarterly(store, "002669")
        q.ingest_quarterly(store, "002669")   # 同值重录
        facts = store.get_facts("002669", "revenue")
        assert len(facts) == 1                # store 幂等去重


# ─────────────────────────────────────────────────────────────────
# 5. 工具
# ─────────────────────────────────────────────────────────────────

class TestUtils:
    @pytest.mark.parametrize("raw,expected", [
        ("002669", "002669"), ("002669.SZ", "002669"), ("SZ002669", "002669"),
        ("600519.SS", "600519"), ("600519.SH", "600519"), ("sh600519", "600519"),
    ])
    def test_clean_code(self, raw, expected):
        assert q._clean_code(raw) == expected

    def test_secucode_exchange_inference(self):
        assert q._secucode("600519") == "600519.SH"
        assert q._secucode("002669") == "002669.SZ"
        assert q._secucode("300661") == "300661.SZ"

    def test_timeout_hard_cap(self):
        """铁律：网络超时 ≤10s。"""
        assert q._TIMEOUT <= 10
