"""L1 Wave 4（2026-08-01）：A 股龙虎榜活跃度摄取回归测试。

fixture 取自 2026-08-01 真实接口返回形态（``stock_lhb_stock_statistic_em``
("近三月") 全市场表，300502 命中行 / 301358 缺席，经真实网络调用抓取），
锁定行为：

① 命中提取：上榜次数/最近上榜日/净买额（元）/机构席位次数与净额；
② 缺席 = 近三个月未上榜 → 干净 None（多数票的常态，不盖章不注入）；
③ 净卖出方向措辞、机构席位行的有无；
④ 坏输入 / 网络失败永不 raise；
⑤ 本块无百分数：sanctioned_pcts 恒空（红线 9 无须接白名单）。
"""

from __future__ import annotations

from contextlib import contextmanager

import pandas as pd
import pytest

from aegis.core.acquisition.connectors.lhb_activity import (
    _extract,
    fetch_lhb_activity,
)


def _statistic_df() -> pd.DataFrame:
    """2026-08-01 实测 stock_lhb_stock_statistic_em("近三月") 缩样。"""
    return pd.DataFrame({
        "序号": [1, 2, 1134],
        "代码": ["603459", "301139", "300502"],
        "名称": ["红板科技", "*ST元道", "新易盛"],
        "最近上榜日": ["2026-07-30", "2026-07-30", "2026-07-28"],
        "收盘价": [85.10, 3.23, 406.9],
        "涨跌幅": [-10.0042, -13.6364, -17.1283],
        "上榜次数": [47, 30, 1],
        "龙虎榜净买额": [9.811340e8, -2.673112e7, 1.927535e9],
        "龙虎榜买入额": [1.348869e10, 2.791324e8, 6.213416e9],
        "龙虎榜卖出额": [1.250756e10, 3.058636e8, 4.285881e9],
        "龙虎榜总成交额": [2.599625e10, 5.849960e8, 1.049930e10],
        "买方机构次数": [24, 36, 5],
        "卖方机构次数": [17, 43, 6],
        "机构买入净额": [1.267201e9, -9.768917e5, 1.632697e9],
        "机构买入总额": [2.228608e9, 7.965077e7, 3.291549e9],
        "机构卖出总额": [9.614064e8, 8.062766e7, 1.658852e9],
        "近1个月涨跌幅": [-16.576806, -24.532710, -28.109541],
    })


class TestExtract:

    def test_hit_contract(self):
        out = _extract(_statistic_df(), "300502")
        assert out["times_on_list"] == 1
        assert out["latest_list_date"] == "2026-07-28"
        assert out["net_buy"] == pytest.approx(1.927535e9)  # 元直通
        assert out["inst_buy_times"] == 5
        assert out["inst_sell_times"] == 6
        assert out["inst_net_buy"] == pytest.approx(1.632697e9)
        assert out["sanctioned_pcts"] == []  # 本块无百分数
        assert out["source"] == "eastmoney_lhb"

    def test_hit_lines(self):
        out = _extract(_statistic_df(), "300502")
        blob = "\n".join(out["lines_zh"])
        assert "近三个月上榜 1 次" in blob
        assert "最近 2026-07-28" in blob
        assert "净买入 19.28 亿元" in blob
        assert "榜上总成交 104.99 亿元" in blob
        assert "买方 5 次 / 卖方 6 次" in blob
        assert "机构净买入 16.33 亿元" in blob

    def test_net_sell_wording(self):
        # 301139 实测为净卖出（负值）
        out = _extract(_statistic_df(), "301139")
        blob = "\n".join(out["lines_zh"])
        assert "净卖出 2673 万元" in blob
        assert "机构净卖出 98 万元" in blob

    def test_absent_means_none(self):
        # 301358 实测缺席：多数票近三个月不上榜 → 干净 None
        assert _extract(_statistic_df(), "301358") is None

    def test_zero_times_means_none(self):
        df = _statistic_df()
        df.loc[df["代码"] == "300502", "上榜次数"] = 0
        assert _extract(df, "300502") is None

    def test_no_inst_activity_no_inst_line(self):
        df = _statistic_df()
        mask = df["代码"] == "300502"
        df.loc[mask, "买方机构次数"] = 0
        df.loc[mask, "卖方机构次数"] = 0
        out = _extract(df, "300502")
        assert len(out["lines_zh"]) == 1
        assert "机构专用席位" not in out["lines_zh"][0]

    def test_missing_columns(self):
        assert _extract(pd.DataFrame({"foo": [1]}), "300502") is None


class TestFetchNeverRaises:

    def test_network_failure_degrades(self, monkeypatch):
        import aegis.core.acquisition.connectors.lhb_activity as lhb

        @contextmanager
        def _boom():
            raise RuntimeError("net down")
            yield  # pragma: no cover

        monkeypatch.setattr(lhb, "_no_proxy", _boom)
        assert fetch_lhb_activity("300502") is None

    def test_empty_frame(self, monkeypatch):
        import sys
        import types
        fake = types.ModuleType("akshare")
        fake.stock_lhb_stock_statistic_em = (
            lambda symbol: _statistic_df().iloc[0:0]
        )
        monkeypatch.setitem(sys.modules, "akshare", fake)
        assert fetch_lhb_activity("300502") is None

    def test_extract_raise_degrades(self, monkeypatch):
        import sys
        import types
        import aegis.core.acquisition.connectors.lhb_activity as lhb
        fake = types.ModuleType("akshare")
        fake.stock_lhb_stock_statistic_em = lambda symbol: _statistic_df()
        monkeypatch.setitem(sys.modules, "akshare", fake)
        monkeypatch.setattr(
            lhb, "_extract",
            lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
        assert fetch_lhb_activity("300502") is None

    def test_fetch_end_to_end_with_fake_akshare(self, monkeypatch):
        import sys
        import types
        seen: dict = {}
        fake = types.ModuleType("akshare")

        def _stub(symbol):
            seen["symbol"] = symbol
            return _statistic_df()

        fake.stock_lhb_stock_statistic_em = _stub
        monkeypatch.setitem(sys.modules, "akshare", fake)
        out = fetch_lhb_activity("300502.SZ")
        assert seen["symbol"] == "近三月"
        assert out["times_on_list"] == 1
        assert out["latest_list_date"] == "2026-07-28"
