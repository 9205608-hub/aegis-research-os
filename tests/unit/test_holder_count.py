"""L1 Wave 4（2026-08-01）：A 股股东户数摄取回归测试。

fixture 取自 2026-08-01 真实接口返回形态（``stock_zh_a_gdhs_detail_em``
东财股东户数明细，301358 经真实网络调用抓取），锁定行为：

① 解析与契约装配：户数/截止日/公告日/户均持股市值、最多 8 期序列；
② 上市前静态登记行过滤（301358 实测 IPO 前恒为 30 户——混入趋势
   会制造假"筹码集中"信号）；
③ 环比 % 重算（源表增减比例为百分数口径，但跨被过滤行会失真）；
④ 趋势推断：连续两期下降=集中 / 上升=分散 / 死区±1%内或方向互现=平稳；
⑤ 坏输入 / 网络失败永不 raise；
⑥ holder_count_sanctioned_pcts 容错提取（红线 9）。
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date

import pandas as pd
import pytest

from aegis.core.acquisition.connectors.holder_count import (
    _normalize,
    _trend,
    fetch_holder_count,
    holder_count_sanctioned_pcts,
)


def _gdhs_301358() -> pd.DataFrame:
    """2026-08-01 实测 stock_zh_a_gdhs_detail_em("301358") 缩样。

    含上市前发起人静态登记行（恒 30 户）与 8 期真实户数序列；
    行序按截止日升序（最新在表尾），与实测一致。
    """
    return pd.DataFrame({
        "股东户数统计截止日": [
            date(2021, 3, 31), date(2021, 6, 30),
            date(2024, 9, 30), date(2024, 12, 31), date(2025, 2, 28),
            date(2025, 3, 31), date(2025, 6, 30), date(2025, 9, 30),
            date(2025, 12, 31), date(2026, 3, 31),
        ],
        "区间涨跌幅": [None, None, None, -6.83, 8.68, -20.52, 4.27,
                  -17.20, 24.05, 23.11],
        "股东户数-本次": [30, 30, 29736, 21633, 23510, 25888, 36766,
                    32441, 40243, 49543],
        "股东户数-上次": [0, 30, 30791, 29736, 21633, 23510, 25888,
                    36766, 32441, 40243],
        "股东户数-增减": [30, 0, -1055, -8103, 1877, 2378, 10878,
                    -4325, 7802, 9300],
        "股东户数-增减比例": [None, 0.0, -3.426326, -27.249798, 8.676559,
                      10.114845, 42.019468, -11.763586, 24.049814,
                      23.109609],
        "户均持股市值": [None, None, 954970.1, 1586405.0, 1379547.0,
                   1043095.0, 642406.7, 1416963.0, 1222411.0,
                   1160638.0],
        "户均持股数量": [None, None, 25467.0, 35005.0, 32210.0, 29252.0,
                   20596.0, 23343.0, 18817.0, 15284.0],
        "总市值": [None, None, 2.839699e10, 3.431871e10, 3.243315e10,
                2.700364e10, 2.361872e10, 4.596770e10, 4.919350e10,
                5.750147e10],
        "总股本": [567939870] * 2 + [757253070] * 8,
        "股本变动": [0] * 10,
        "股本变动原因": [None] * 10,
        "股东户数公告日期": [
            date(2021, 9, 28), date(2021, 12, 30),
            date(2024, 10, 29), date(2025, 3, 15), date(2025, 3, 15),
            date(2025, 4, 29), date(2025, 8, 26), date(2025, 10, 28),
            date(2026, 4, 23), date(2026, 4, 28),
        ],
        "代码": ["301358"] * 10,
        "名称": ["湖南裕能"] * 10,
    })


class TestNormalizeContract:

    def test_latest_fields(self):
        out = _normalize(_gdhs_301358())
        assert out["latest_holder_count"] == 49543
        assert out["latest_period"] == "2026-03-31"
        assert out["latest_announce_date"] == "2026-04-28"
        assert out["latest_change_pct"] == pytest.approx(23.11)
        assert out["avg_holding_value"] == pytest.approx(1160638.0)
        assert out["source"] == "eastmoney_gdhs"
        assert "2026-03-31" in out["source_note"]

    def test_preipo_rows_filtered_and_max_periods(self):
        out = _normalize(_gdhs_301358())
        counts = [p["holder_count"] for p in out["periods"]]
        # 上市前 30 户静态行不得混入
        assert 30 not in counts
        assert len(out["periods"]) == 8
        assert out["periods"][0]["period"] == "2024-09-30"

    def test_change_pct_recomputed(self):
        out = _normalize(_gdhs_301358())
        by_period = {p["period"]: p["change_pct"] for p in out["periods"]}
        # 首个保留期无前值 → None
        assert by_period["2024-09-30"] is None
        assert by_period["2024-12-31"] == pytest.approx(-27.25)
        assert by_period["2025-06-30"] == pytest.approx(42.02)
        assert by_period["2026-03-31"] == pytest.approx(23.11)

    def test_lines_zh(self):
        out = _normalize(_gdhs_301358())
        blob = "\n".join(out["lines_zh"])
        assert "最新披露股东户数 49,543 户" in blob
        assert "截至 2026-03-31" in blob
        assert "公告 2026-04-28" in blob
        assert "+23.11%" in blob
        assert "户均持股市值约 116.1 万元" in blob
        # 序列行只列最近 5 期（噪声控制）
        assert "近 5 期户数序列" in blob
        assert "2025-03-31 25,888 户" in blob
        assert "2024-09-30" not in blob.split("[股东户数] 近 5 期")[1]

    def test_trend_dispersing_line(self):
        # 301358 实测最近两期 +24.05% / +23.11% → 分散
        out = _normalize(_gdhs_301358())
        assert out["holder_count_trend"] == "分散"
        blob = "\n".join(out["lines_zh"])
        assert "户数连续两期上升（筹码趋于分散）" in blob

    def test_sanctioned_pcts_abs_and_dedup(self):
        out = _normalize(_gdhs_301358())
        # lines 里出现的变化 % 以绝对值注册（文本写"下降 11.76%"）
        for v in (23.11, 10.11, 42.02, 11.76, 24.05):
            assert v in out["sanctioned_pcts"]
        # 去重：23.11 同时出现在首行与序列行，只注册一次
        assert out["sanctioned_pcts"].count(23.11) == 1

    def test_missing_columns(self):
        assert _normalize(pd.DataFrame({"foo": [1]})) is None

    def test_all_rows_preipo(self):
        df = _gdhs_301358().iloc[0:2]
        assert _normalize(df) is None

    def test_unsorted_input_defensive(self):
        df = _gdhs_301358().sample(frac=1.0, random_state=7)
        out = _normalize(df)
        assert out["latest_period"] == "2026-03-31"
        assert out["periods"][0]["period"] == "2024-09-30"


class TestTrend:

    def test_concentrating(self):
        assert _trend([None, -5.0, -3.2]) == "集中"

    def test_dispersing(self):
        assert _trend([2.0, 8.0, 3.5]) == "分散"

    def test_flat_within_deadband(self):
        # ±1% 死区内的抖动不定向（登记口径噪声）
        assert _trend([-5.0, -0.8, -0.5]) == "平稳"

    def test_mixed_directions(self):
        assert _trend([-15.0, 8.0, -6.0]) == "平稳"
        assert _trend([8.0, -6.0, 8.0]) == "平稳"

    def test_insufficient_data(self):
        assert _trend([None, 5.0]) is None
        assert _trend([]) is None

    def test_concentrating_line(self):
        df = _gdhs_301358()
        df.loc[df.index[-2], "股东户数-本次"] = 30000   # 40243 → 30000
        df.loc[df.index[-1], "股东户数-本次"] = 25000   # → 连续两期下降
        out = _normalize(df)
        assert out["holder_count_trend"] == "集中"
        blob = "\n".join(out["lines_zh"])
        assert "筹码趋于集中" in blob
        assert "筹码向少数账户集中" in blob

    def test_two_periods_no_trend_line(self):
        df = _gdhs_301358().iloc[-2:]
        out = _normalize(df)
        assert out["holder_count_trend"] is None
        assert not any("趋于" in ln or "平稳" in ln for ln in out["lines_zh"])


class TestFetchNeverRaises:

    def test_network_failure_degrades(self, monkeypatch):
        import aegis.core.acquisition.connectors.holder_count as hc

        @contextmanager
        def _boom():
            raise RuntimeError("net down")
            yield  # pragma: no cover

        monkeypatch.setattr(hc, "_no_proxy", _boom)
        assert fetch_holder_count("301358") is None

    def test_normalize_raise_degrades(self, monkeypatch):
        import sys
        import types
        import aegis.core.acquisition.connectors.holder_count as hc
        fake = types.ModuleType("akshare")
        fake.stock_zh_a_gdhs_detail_em = lambda symbol: _gdhs_301358()
        monkeypatch.setitem(sys.modules, "akshare", fake)
        monkeypatch.setattr(
            hc, "_normalize",
            lambda df: (_ for _ in ()).throw(RuntimeError("boom")))
        assert fetch_holder_count("301358") is None

    def test_empty_frame(self, monkeypatch):
        import sys
        import types
        fake = types.ModuleType("akshare")
        fake.stock_zh_a_gdhs_detail_em = (
            lambda symbol: _gdhs_301358().iloc[0:0]
        )
        monkeypatch.setitem(sys.modules, "akshare", fake)
        assert fetch_holder_count("301358") is None

    def test_fetch_end_to_end_with_fake_akshare(self, monkeypatch):
        import sys
        import types
        seen: dict = {}
        fake = types.ModuleType("akshare")

        def _stub(symbol):
            seen["symbol"] = symbol
            return _gdhs_301358()

        fake.stock_zh_a_gdhs_detail_em = _stub
        monkeypatch.setitem(sys.modules, "akshare", fake)
        out = fetch_holder_count("301358.SZ")
        assert seen["symbol"] == "301358"  # 后缀剥离
        assert out["latest_holder_count"] == 49543
        assert out["holder_count_trend"] == "分散"


class TestSanctionedPctsExtraction:

    def test_tolerant_extraction(self):
        out = _normalize(_gdhs_301358())
        assert holder_count_sanctioned_pcts(out) == out["sanctioned_pcts"]
        assert holder_count_sanctioned_pcts(None) == []
        assert holder_count_sanctioned_pcts({"sanctioned_pcts": "junk"}) == []
        assert holder_count_sanctioned_pcts("not-a-dict") == []
