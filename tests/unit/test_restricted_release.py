"""L1 Wave 3（2026-08-01）：A 股限售解禁日历摄取回归测试。

fixture 取自 2026-08-01 真实接口返回形态（``stock_restricted_release_queue_em``，
301358 含未来批次 / 300502 全历史批次，经真实网络调用抓取），锁定行为：

① 契约装配：next_release_* / upcoming_12m / total_pending_pct /
   recent_3m_released_pct 与中文行；
② 比例字段小数 → 百分数换算 + 源头已是百分数的防御；
③ 无未来批次时的干净表述（负面证据同样注入）；近 3 个月回看窗口；
④ 多批未来批次的汇总行与截断；
⑤ 坏输入（缺列/空表/垃圾日期）永不 raise；fetch 网络失败降级 None；
⑥ restricted_sanctioned_pcts 容错提取（红线 9）。
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date

import pandas as pd
import pytest

from aegis.core.acquisition.connectors.restricted_release import (
    _normalize,
    fetch_restricted_release,
    restricted_sanctioned_pcts,
)

NAN = float("nan")


def _queue_301358() -> pd.DataFrame:
    """2026-08-01 实测 stock_restricted_release_queue_em("301358") 形态。"""
    return pd.DataFrame({
        "序号": [1, 2, 3, 4, 5],
        "解禁时间": [date(2026, 10, 9), date(2026, 2, 9), date(2024, 3, 1),
                 date(2024, 2, 19), date(2023, 8, 9)],
        "解禁股东数": [18, 15, 1, 26, 1],
        "解禁数量": [82537493.0, 373819924.0, 55500.0,
                 250858406.0, 7954528.0],
        "实际解禁数量": [82537493.0, 373819924.0, 55500.0,
                   250858406.0, 7954528.0],
        "未解禁数量": [864590.0, 864590.0, 373819924.0,
                  373875424.0, 623329630.0],
        "实际解禁数量市值": [5.227925e9, 2.363290e10, 1.532910e6,
                     6.587542e9, 3.486470e8],
        "占总市值比例": [0.097363, 0.491349, 0.000073, 0.331274, 0.010504],
        "占流通市值比例": [0.107987, 0.491908, 0.000145, 0.654338, 0.059396],
        "解禁前一交易日收盘价": [63.34, 63.22, 27.62, 26.26, 43.83],
        "限售股类型": ["定向增发机构配售股份", "首发原股东限售股份", "首发战略配售股份",
                  "首发原股东限售股份,首发战略配售股份", "首发机构配售股份"],
        "解禁前20日涨跌幅": [NAN, 1.368140, -2.843103, -16.484185, -10.196635],
        "解禁后20日涨跌幅": [NAN, 24.387217, 11.669075, 5.680991, -8.239278],
    })


def _queue_300502() -> pd.DataFrame:
    """2026-08-01 实测 stock_restricted_release_queue_em("300502") 缩样
    （全部批次已在历史上解禁）。"""
    return pd.DataFrame({
        "序号": [1, 2],
        "解禁时间": [date(2025, 6, 13), date(2021, 6, 28)],
        "解禁股东数": [6, 57],
        "解禁数量": [1534197.0, 43724777.0],
        "实际解禁数量": [1534197.0, 43724777.0],
        "未解禁数量": [108527827.0, 135565546.0],
        "实际解禁数量市值": [1.534964e8, 1.370772e9],
        "占总市值比例": [0.001546, 0.086228],
        "占流通市值比例": [0.001736, 0.117691],
        "解禁前一交易日收盘价": [100.05, 31.35],
        "限售股类型": ["股权激励限售股份", "定向增发机构配售股份"],
        "解禁前20日涨跌幅": [19.230308, -7.783065],
        "解禁后20日涨跌幅": [31.980954, 27.819305],
    })


class TestNormalizeContract:

    def test_future_batch_301358(self):
        out = _normalize(_queue_301358(), date(2026, 8, 1))
        assert out["next_release_date"] == "2026-10-09"
        assert out["next_release_pct"] == pytest.approx(9.74)
        assert len(out["upcoming_12m"]) == 1
        assert out["upcoming_12m"][0]["share_type"] == "定向增发机构配售股份"
        assert out["upcoming_12m"][0]["holder_num"] == 18
        assert out["upcoming_12m_pct"] == pytest.approx(9.74)
        assert out["total_pending_pct"] == pytest.approx(9.74)
        assert out["recent_3m_batches"] == 0
        blob = "\n".join(out["lines_zh"])
        assert "下一批解禁 2026-10-09" in blob
        assert "8254万股" in blob
        assert "占总市值 9.74%" in blob
        assert "占流通市值 10.80%" in blob
        # 红线 9：lines 里出现的 % 全部注册进白名单
        for v in (9.74, 10.8):
            assert v in out["sanctioned_pcts"]

    def test_recent_3m_window(self):
        # 2026-04-01 视角：2026-02-09 批（49.13%）落在近 3 个月内
        out = _normalize(_queue_301358(), date(2026, 4, 1))
        assert out["recent_3m_batches"] == 1
        assert out["recent_3m_released_pct"] == pytest.approx(49.13)
        assert out["next_release_date"] == "2026-10-09"
        blob = "\n".join(out["lines_zh"])
        assert "近 3 个月已解禁 1 批" in blob
        assert 49.13 in out["sanctioned_pcts"]

    def test_no_future_batches_300502(self):
        out = _normalize(_queue_300502(), date(2026, 8, 1))
        assert out["next_release_date"] is None
        assert out["next_release_pct"] is None
        assert out["upcoming_12m"] == []
        assert out["total_pending_pct"] == pytest.approx(0.0)
        blob = "\n".join(out["lines_zh"])
        assert "未来 12 个月无已公告解禁批次" in blob
        assert "最近一批 2025-06-13 已解禁" in blob
        assert 0.15 in out["sanctioned_pcts"]

    def test_multi_upcoming_summary_line(self):
        df = _queue_301358().copy()
        # 追加两批未来 12 个月内的解禁
        extra = pd.DataFrame({
            "序号": [6, 7],
            "解禁时间": [date(2026, 12, 1), date(2027, 3, 15)],
            "解禁股东数": [2, 3],
            "解禁数量": [1_000_000.0, 2_000_000.0],
            "实际解禁数量": [NAN, NAN],
            "未解禁数量": [NAN, NAN],
            "实际解禁数量市值": [NAN, NAN],
            "占总市值比例": [0.02, 0.03],
            "占流通市值比例": [0.025, 0.035],
            "解禁前一交易日收盘价": [NAN, NAN],
            "限售股类型": ["股权激励限售股份", "定向增发机构配售股份"],
            "解禁前20日涨跌幅": [NAN, NAN],
            "解禁后20日涨跌幅": [NAN, NAN],
        })
        df = pd.concat([df, extra], ignore_index=True)
        out = _normalize(df, date(2026, 8, 1))
        assert len(out["upcoming_12m"]) == 3
        assert out["upcoming_12m_pct"] == pytest.approx(9.74 + 2.0 + 3.0)
        blob = "\n".join(out["lines_zh"])
        assert "未来 12 个月共 3 批待解禁" in blob
        assert "14.74%" in blob
        assert 14.74 in out["sanctioned_pcts"]

    def test_pct_defensive_when_source_already_percent(self):
        # 防御 akshare 未来改口径：值 >1.0 视为已是百分数
        df = _queue_301358()
        df.loc[0, "占总市值比例"] = 9.74
        out = _normalize(df, date(2026, 8, 1))
        assert out["next_release_pct"] == pytest.approx(9.74)


class TestBadInputsNeverRaise:

    def test_missing_columns(self):
        df = pd.DataFrame({"foo": [1], "bar": [2]})
        assert _normalize(df, date(2026, 8, 1)) is None

    def test_empty_frame(self):
        df = _queue_301358().iloc[0:0]
        assert _normalize(df, date(2026, 8, 1)) is None

    def test_garbage_dates(self):
        df = _queue_301358()
        df["解禁时间"] = ["垃圾", None, "??", "", "not-a-date"]
        assert _normalize(df, date(2026, 8, 1)) is None


class TestFetchNeverRaises:

    def test_network_failure_degrades(self, monkeypatch):
        import aegis.core.acquisition.connectors.restricted_release as rr

        @contextmanager
        def _boom():
            raise RuntimeError("net down")
            yield  # pragma: no cover

        monkeypatch.setattr(rr, "_no_proxy", _boom)
        assert fetch_restricted_release("301358") is None

    def test_fetch_normalizes_akshare_frame(self, monkeypatch):
        import sys
        import types
        seen: list[str] = []
        fake = types.ModuleType("akshare")

        def _fake_queue(symbol: str) -> pd.DataFrame:
            seen.append(symbol)
            return _queue_301358()

        fake.stock_restricted_release_queue_em = _fake_queue
        monkeypatch.setitem(sys.modules, "akshare", fake)
        out = fetch_restricted_release("301358.SZ", today=date(2026, 8, 1))
        assert seen == ["301358"]  # 6 位代码裁剪
        assert out["next_release_date"] == "2026-10-09"
        assert out["source"] == "eastmoney_restricted_release"

    def test_fetch_empty_frame_degrades(self, monkeypatch):
        import sys
        import types
        fake = types.ModuleType("akshare")
        fake.stock_restricted_release_queue_em = (
            lambda symbol: pd.DataFrame()
        )
        monkeypatch.setitem(sys.modules, "akshare", fake)
        assert fetch_restricted_release("301358") is None


class TestSanctionedPctsExtraction:

    def test_tolerant_extraction(self):
        out = _normalize(_queue_301358(), date(2026, 8, 1))
        assert restricted_sanctioned_pcts(out) == out["sanctioned_pcts"]
        assert restricted_sanctioned_pcts(None) == []
        assert restricted_sanctioned_pcts({"sanctioned_pcts": "junk"}) == []
        assert restricted_sanctioned_pcts("not-a-dict") == []
