"""L1 Wave 3（2026-08-01）：A 股股权质押摄取回归测试。

fixture 取自 2026-08-01 真实接口返回形态（``stock_gpzy_pledge_ratio_em``
中登周五快照 / ``stock_gpzy_individual_pledge_ratio_detail_em`` 重要股东
明细，经真实网络调用抓取），锁定行为：

① 中登行提取：命中（百分数口径、万股换算）与缺席（视为 ≈0 的负面证据）；
② 重要股东明细聚合：未解押过滤、按股东求和、公告日期取最新；
③ 对账降级：明细聚合超出中登口径 → detail_stale，股东占比不进白名单
   （300502 实测坑：中登 0 笔 vs 东财明细挂 2018 年"未解押"）；
④ 高质押 / 满仓质押张力阈值（常量口径）；
⑤ 单路失败干净降级、两路全败返回 None、坏输入永不 raise；
⑥ pledge_sanctioned_pcts 容错提取（红线 9）。
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date

import pandas as pd
import pytest

from aegis.core.acquisition.connectors.equity_pledge import (
    _assemble,
    _csdc_row,
    _fetch_csdc_ratio,
    _fetch_csdc_ratio_datacenter,
    _holder_agg,
    _recent_csdc_dates,
    fetch_equity_pledge,
    pledge_sanctioned_pcts,
)


def _csdc_df() -> pd.DataFrame:
    """2026-08-01 实测 stock_gpzy_pledge_ratio_em(date="20260731") 缩样。"""
    return pd.DataFrame({
        "序号": [1, 2, 3],
        "股票代码": ["600370", "301358", "002230"],
        "股票简称": ["*ST三房", "湖南裕能", "科大讯飞"],
        "交易日期": [date(2026, 7, 31)] * 3,
        "所属行业": ["化学纤维", "电池", "软件开发"],
        "质押比例": [78.74, 7.09, 0.28],
        "质押股数": [316745.56, 6008.33, 676.74],
        "质押市值": [500457.9848, 380567.6222, 28571.9628],
        "质押笔数": [34, 6, 1],
        "无限售股质押数": [316745.56, 6008.33, 676.74],
        "限售股质押数": [0.0, 0.0, 0.0],
        "近一年涨跌幅": [-19.796954, 98.1951, -12.584218],
        "所属行业代码": ["471", "1033", "737"],
    })


def _detail_300502() -> pd.DataFrame:
    """2026-08-01 实测 stock_gpzy_individual_pledge_ratio_detail_em
    ("300502") 缩样（含已解押干扰行与 NaT 结束日期）。"""
    return pd.DataFrame({
        "序号": [1, 4, 7],
        "股票代码": ["300502"] * 3,
        "股票简称": ["新易盛"] * 3,
        "股东名称": ["高光荣", "胡学民", "胡学民"],
        "质押股份数量": [9744000, 1760000, 9800000],
        "占所持股份比例": [18.57, 5.86, 32.63],
        "占总股本比例": [1.37, 0.49, 2.96],
        "质押机构": ["华泰证券(上海)资产管理有限公司", "广发证券股份有限公司",
                 "华西证券股份有限公司"],
        "最新价": [396.01] * 3,
        "质押日收盘价": [9.546378, 10.197994, 4.225407],
        "预估平仓线": [6.682464, 7.138596, 2.957785],
        "质押开始日期": [date(2022, 11, 1), date(2021, 4, 27), date(2019, 7, 10)],
        "质押结束日期": [date(2023, 11, 1), pd.NaT, date(2020, 7, 10)],
        "状态": ["已解押", "未解押", "已解押"],
        "公告日期": [date(2023, 11, 3), date(2021, 4, 28), date(2020, 7, 13)],
    })


class TestCsdcRow:

    def test_found(self):
        out = _csdc_row(_csdc_df(), "301358", date(2026, 7, 31))
        assert out["listed"] is True
        assert out["trade_date"] == "2026-07-31"
        assert out["pledge_ratio_total"] == pytest.approx(7.09)
        assert out["pledge_count"] == 6
        # 中登口径质押股数单位万股 → 股
        assert out["pledged_shares"] == pytest.approx(60_083_300.0)

    def test_absent_means_near_zero(self):
        out = _csdc_row(_csdc_df(), "300502", date(2026, 7, 31))
        assert out["listed"] is False
        assert out["pledge_ratio_total"] == pytest.approx(0.0)
        assert out["pledge_count"] == 0

    def test_missing_columns(self):
        df = pd.DataFrame({"foo": [1]})
        assert _csdc_row(df, "300502", date(2026, 7, 31)) is None


class TestHolderAgg:

    def test_active_only_and_fields(self):
        out = _holder_agg(_detail_300502())
        assert out["total_records"] == 3
        assert len(out["active"]) == 1
        h = out["active"][0]
        assert h["name"] == "胡学民"
        assert h["pct_of_total"] == pytest.approx(0.49)
        assert h["pct_of_holding"] == pytest.approx(5.86)
        assert h["records"] == 1
        assert h["latest_notice_date"] == "2021-04-28"

    def test_same_holder_summed_latest_notice_wins(self):
        df = _detail_300502()
        df["状态"] = ["未解押"] * 3
        out = _holder_agg(df)
        by_name = {h["name"]: h for h in out["active"]}
        assert by_name["胡学民"]["pct_of_total"] == pytest.approx(0.49 + 2.96)
        assert by_name["胡学民"]["pct_of_holding"] == pytest.approx(5.86 + 32.63)
        assert by_name["胡学民"]["records"] == 2
        assert by_name["胡学民"]["latest_notice_date"] == "2021-04-28"
        # 排序按占总股本降序
        assert out["active"][0]["name"] == "胡学民"

    def test_empty_frame(self):
        out = _holder_agg(_detail_300502().iloc[0:0])
        assert out == {"total_records": 0, "active": []}

    def test_missing_columns(self):
        assert _holder_agg(pd.DataFrame({"foo": [1]})) is None


class TestAssembleContract:

    def _ratio(self, ratio: float, listed: bool = True) -> dict:
        return {"trade_date": "2026-07-31", "listed": listed,
                "pledge_ratio_total": ratio, "pledge_count": 6,
                "pledged_shares": 60_083_300.0}

    def _holders(self, pct_total: float, pct_holding: float) -> dict:
        return {"total_records": 1, "active": [{
            "name": "张三", "pct_of_total": pct_total,
            "pct_of_holding": pct_holding, "records": 1,
            "latest_notice_date": "2026-06-30",
        }]}

    def test_high_pledge_and_strain(self):
        out = _assemble(self._ratio(40.0), self._holders(30.0, 85.0), "600000")
        assert out["high_pledge_flag"] is True
        assert out["detail_stale"] is False
        assert out["holder_strain_flag"] is True
        blob = "\n".join(out["lines_zh"])
        assert "全股质押比例 40.00%" in blob
        assert "高质押关注区间" in blob
        assert "张三 占总股本 30.00%（约占其持股 85.00%" in blob
        assert "平仓/控制权风险" in blob
        # 红线 9：lines 里出现的 % 全部注册进白名单
        for v in (40.0, 30.0, 85.0):
            assert v in out["sanctioned_pcts"]
        # 阈值字面量不是披露数字，不得进白名单
        assert 30.0 in out["sanctioned_pcts"]  # 30.0 是张三的披露占比
        assert 80.0 not in out["sanctioned_pcts"]

    def test_moderate_pledge_no_flags(self):
        out = _assemble(self._ratio(7.09), self._holders(5.0, 20.0), "301358")
        assert out["high_pledge_flag"] is False
        assert out["holder_strain_flag"] is False
        assert "高质押" not in "\n".join(out["lines_zh"])

    def test_stale_detail_reconciliation(self):
        # 300502 实测坑：中登 0 笔 vs 东财明细仍挂"未解押"
        out = _assemble(self._ratio(0.0, listed=False),
                        self._holders(4.15, 65.13), "300502")
        assert out["detail_stale"] is True
        assert out["holder_strain_flag"] is None
        blob = "\n".join(out["lines_zh"])
        assert "中登质押登记未见该股" in blob
        assert "与中登口径不符" in blob
        assert "占总股本 4.15%" not in blob  # 陈旧明细不注入
        assert out["sanctioned_pcts"] == []  # 陈旧占比不进白名单

    def test_ratio_only(self):
        out = _assemble(self._ratio(7.09), None, "301358")
        assert out["holder_strain_flag"] is None
        assert out["major_holder_pledged_pct_of_total"] is None
        assert len(out["lines_zh"]) == 1

    def test_holders_only(self):
        out = _assemble(None, self._holders(5.0, 20.0), "301358")
        assert out["pledge_ratio_total"] is None
        assert out["high_pledge_flag"] is None
        blob = "\n".join(out["lines_zh"])
        assert "中登" not in blob
        assert "张三 占总股本 5.00%" in blob

    def test_no_active_records_line(self):
        out = _assemble(self._ratio(0.0, listed=False),
                        {"total_records": 3, "active": []}, "300502")
        blob = "\n".join(out["lines_zh"])
        assert "无未解押记录" in blob
        assert out["detail_stale"] is False


class TestRecentCsdcDates:

    def test_saturday_starts_previous_friday(self):
        dates = _recent_csdc_dates(date(2026, 8, 1), 3)  # 周六
        assert dates == [date(2026, 7, 31), date(2026, 7, 24),
                         date(2026, 7, 17)]

    def test_friday_includes_today(self):
        assert _recent_csdc_dates(date(2026, 7, 31), 1) == [date(2026, 7, 31)]


class TestFetchNeverRaises:

    def test_both_paths_fail(self, monkeypatch):
        import aegis.core.acquisition.connectors.equity_pledge as ep
        monkeypatch.setattr(ep, "_fetch_csdc_ratio", lambda c, t: None)
        monkeypatch.setattr(ep, "_fetch_holder_detail", lambda c: None)
        assert fetch_equity_pledge("300502") is None

    def test_network_failure_degrades(self, monkeypatch):
        import aegis.core.acquisition.connectors.equity_pledge as ep

        @contextmanager
        def _boom():
            raise RuntimeError("net down")
            yield  # pragma: no cover

        monkeypatch.setattr(ep, "_no_proxy", _boom)
        assert fetch_equity_pledge("300502") is None

    def test_assemble_raise_degrades(self, monkeypatch):
        import aegis.core.acquisition.connectors.equity_pledge as ep
        monkeypatch.setattr(
            ep, "_fetch_csdc_ratio",
            lambda c, t: {"trade_date": "2026-07-31", "listed": True,
                          "pledge_ratio_total": 1.0, "pledge_count": 1,
                          "pledged_shares": 1.0})
        monkeypatch.setattr(ep, "_fetch_holder_detail", lambda c: None)
        monkeypatch.setattr(
            ep, "_assemble",
            lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
        assert fetch_equity_pledge("300502") is None

    def test_fetch_end_to_end_with_fake_akshare(self, monkeypatch):
        import sys
        import types

        import aegis.core.acquisition.connectors.equity_pledge as ep

        # datacenter 排前：必须 stub，否则本测会打真网
        monkeypatch.setattr(ep, "_fetch_csdc_ratio_datacenter", lambda c, t: None)
        fake = types.ModuleType("akshare")
        fake.stock_gpzy_pledge_ratio_em = lambda date: _csdc_df()
        fake.stock_gpzy_individual_pledge_ratio_detail_em = (
            lambda symbol: _detail_300502()
        )
        monkeypatch.setitem(sys.modules, "akshare", fake)
        out = fetch_equity_pledge("300502", today=date(2026, 8, 1))
        # 中登缺席 → ≈0；明细 0.49% 未超对账容差 → 不判陈旧
        assert out["pledge_ratio_total"] == pytest.approx(0.0)
        assert out["trade_date"] == "2026-07-31"
        assert out["detail_stale"] is False
        blob = "\n".join(out["lines_zh"])
        assert "中登质押登记未见该股" in blob
        assert "胡学民 占总股本 0.49%" in blob
        for v in (0.49, 5.86):
            assert v in out["sanctioned_pcts"]


class TestFetchCsdcRatioDatacenter:
    """datacenter 按码过滤：命中 / 空+补快照日 / 失败回退 / 口径。不打真网。"""

    def _em_row(self, code: str = "301358", name: str = "湖南裕能",
                td: str = "2026-07-31 00:00:00",
                ratio: float = 7.09, shares_wan: float = 6008.33,
                count: int = 6) -> dict:
        """2026-08-13 实测 RPT_CSDC_LIST 行缩样。"""
        return {
            "SECUCODE": f"{code}.SZ",
            "SECURITY_CODE": code,
            "SECURITY_NAME_ABBR": name,
            "TRADE_DATE": td,
            "PLEDGE_RATIO": ratio,
            "REPURCHASE_BALANCE": shares_wan,
            "PLEDGE_DEAL_NUM": count,
        }

    def _ok(self, rows: list, count: int | None = None) -> dict:
        return {
            "success": True, "code": 0, "message": "ok",
            "result": {"data": rows, "count": count if count is not None else len(rows)},
        }

    def _empty_9201(self) -> dict:
        return {
            "version": None, "result": None, "success": False,
            "message": "返回数据为空", "code": 9201,
        }

    def _patch_requests(self, monkeypatch, handler):
        import sys
        import types

        fake = types.ModuleType("requests")

        class _Resp:
            def __init__(self, payload):
                self._payload = payload

            def json(self):
                if isinstance(self._payload, Exception):
                    raise self._payload
                return self._payload

        def get(url, params=None, **kwargs):
            return _Resp(handler(params or {}))

        fake.get = get
        monkeypatch.setitem(sys.modules, "requests", fake)

    def test_hit_percent_passthrough_and_wan_to_shares(self, monkeypatch):
        # today=2026-08-01 → 日历最近周五 2026-07-31，与行对齐，只发一次按码请求
        def handler(params):
            assert params["reportName"] == "RPT_CSDC_LIST"
            assert '(SECURITY_CODE="301358")' in params.get("filter", "")
            return self._ok([self._em_row()])

        self._patch_requests(monkeypatch, handler)
        out = _fetch_csdc_ratio_datacenter("301358", date(2026, 8, 1))
        assert out["listed"] is True
        assert out["trade_date"] == "2026-07-31"
        assert out["pledge_ratio_total"] == pytest.approx(7.09)  # 百分数透传
        assert out["pledge_count"] == 6
        assert out["pledged_shares"] == pytest.approx(60_083_300.0)  # 万股→股

    def test_success_empty_plus_micro_request_snapshot_date(self, monkeypatch):
        calls: list[dict] = []

        def handler(params):
            calls.append(params)
            if params.get("filter"):
                return {"success": True, "code": 0,
                        "result": {"data": [], "count": 0}}
            # pageSize=1 不带码过滤：全市场最新快照日
            assert params.get("pageSize") == "1"
            return self._ok([self._em_row(
                code="600503", name="华丽家族",
                td="2026-08-07 00:00:00", ratio=5.62, shares_wan=9007.58, count=1,
            )])

        self._patch_requests(monkeypatch, handler)
        out = _fetch_csdc_ratio_datacenter("300502", date(2026, 8, 1))
        assert out["listed"] is False
        assert out["trade_date"] == "2026-08-07"
        assert out["pledge_ratio_total"] == pytest.approx(0.0)
        assert out["pledge_count"] == 0
        assert out["pledged_shares"] == pytest.approx(0.0)
        assert len(calls) == 2

    def test_empty_code_9201_plus_micro_request(self, monkeypatch):
        def handler(params):
            if params.get("filter"):
                return self._empty_9201()
            return self._ok([self._em_row(
                code="600503", td="2026-08-07 00:00:00",
            )])

        self._patch_requests(monkeypatch, handler)
        out = _fetch_csdc_ratio_datacenter("999999", date(2026, 8, 1))
        assert out == {
            "trade_date": "2026-08-07", "listed": False,
            "pledge_ratio_total": 0.0, "pledge_count": 0, "pledged_shares": 0.0,
        }

    def test_stale_history_is_absent_from_current_snapshot(self, monkeypatch):
        # 300502 实测形态：按码有行但停在 2023-10-27，当前快照 2026-08-07 未收录
        def handler(params):
            if params.get("filter"):
                return self._ok([self._em_row(
                    code="300502", name="新易盛",
                    td="2023-10-27 00:00:00", ratio=1.37, shares_wan=974.4, count=1,
                )])
            return self._ok([self._em_row(
                code="600503", td="2026-08-07 00:00:00",
            )])

        self._patch_requests(monkeypatch, handler)
        out = _fetch_csdc_ratio_datacenter("300502", date(2026, 8, 1))
        assert out["listed"] is False
        assert out["trade_date"] == "2026-08-07"
        assert out["pledge_ratio_total"] == pytest.approx(0.0)

    def test_datacenter_fail_falls_back_akshare(self, monkeypatch):
        import sys
        import types

        import aegis.core.acquisition.connectors.equity_pledge as ep

        def handler(params):
            raise ConnectionError("net down")

        self._patch_requests(monkeypatch, handler)
        fake = types.ModuleType("akshare")
        fake.stock_gpzy_pledge_ratio_em = lambda date: _csdc_df()
        monkeypatch.setitem(sys.modules, "akshare", fake)
        # 编排器：datacenter None → akshare 全表命中 301358
        out = _fetch_csdc_ratio("301358", date(2026, 8, 1))
        assert out["listed"] is True
        assert out["pledge_ratio_total"] == pytest.approx(7.09)
        assert out["pledged_shares"] == pytest.approx(60_083_300.0)
        assert out["trade_date"] == "2026-07-31"
        # 直接函数也是 None，确认回退发生在编排器而非 datacenter 内部
        assert ep._fetch_csdc_ratio_datacenter("301358", date(2026, 8, 1)) is None

    def test_micro_request_fail_returns_none(self, monkeypatch):
        def handler(params):
            if params.get("filter"):
                return self._empty_9201()
            raise ConnectionError("market date down")

        self._patch_requests(monkeypatch, handler)
        assert _fetch_csdc_ratio_datacenter("300502", date(2026, 8, 1)) is None

    def test_malformed_payload_returns_none(self, monkeypatch):
        self._patch_requests(monkeypatch, lambda p: ["not-a-dict"])
        assert _fetch_csdc_ratio_datacenter("301358", date(2026, 8, 1)) is None


class TestSanctionedPctsExtraction:

    def test_tolerant_extraction(self):
        out = _assemble(
            {"trade_date": "2026-07-31", "listed": True,
             "pledge_ratio_total": 7.09, "pledge_count": 6,
             "pledged_shares": 60_083_300.0},
            {"total_records": 1, "active": [{
                "name": "张三", "pct_of_total": 5.0, "pct_of_holding": 20.0,
                "records": 1, "latest_notice_date": "2026-06-30"}]},
            "301358",
        )
        assert pledge_sanctioned_pcts(out) == out["sanctioned_pcts"]
        assert pledge_sanctioned_pcts(None) == []
        assert pledge_sanctioned_pcts({"sanctioned_pcts": "junk"}) == []
        assert pledge_sanctioned_pcts("not-a-dict") == []
