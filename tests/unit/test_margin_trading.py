"""L1 Wave 4（2026-08-01）：A 股融资融券余额摄取回归测试。

fixture 取自 2026-08-01 真实接口返回形态（东财 ``RPTA_WEB_RZRQ_GGMX``
个股两融明细，300502 经真实网络调用抓取），锁定行为：

① 行解析：余额单位元直通、``RZYEZB`` 百分数口径直取（4.49 = 4.49%）；
② 占流通市值比防御：RZYEZB 缺失 / 疑似小数口径 → 用 SZ 重算，
   离谱值（>100%）丢弃；
③ 近 20 交易日余额变化 %：足窗直取、窗口不足按实际期数、单行降 None；
④ 非两融标的（接口空结果码 9201）→ is_margin_eligible=False 干净降级块；
⑤ 网络失败 → None、装配 raise → None、坏输入永不 raise；
⑥ margin_sanctioned_pcts 容错提取（红线 9）。
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest

from aegis.core.acquisition.connectors.margin_trading import (
    _assemble,
    _fetch_ggmx,
    _not_eligible_block,
    _parse_rows,
    _pct_of_float,
    fetch_margin_trading,
    margin_sanctioned_pcts,
)


def _row(d: str, rzye: float, rzyezb: float | None = None,
         sz: float | None = None, rqye: float | None = None) -> dict:
    """真实返回行缩样（完整行 45 键，仅保留解析所需 + 干扰键）。"""
    return {
        "DATE": f"{d} 00:00:00", "SCODE": "300502", "SECNAME": "新易盛",
        "RZYE": rzye, "RZYEZB": rzyezb, "SZ": sz, "RQYE": rqye,
        "MARKET": "融资融券_深证", "TRADE_MARKET": "深交所创业板",
    }


def _rows_300502() -> list[dict]:
    """2026-08-01 实测 300502 最近三行（元 / 百分数口径原样）。"""
    return [
        _row("2026-07-30", 20893993369, 4.48899124, 465449635933.8, 83727582),
        _row("2026-07-29", 23029561885, 4.35928811, 528287218149.6, 98594494),
        _row("2026-07-28", 24201335255, 4.74209115, 510351540990.2, 98502352),
    ]


def _rows_n(n: int, latest: float = 2.0e10, step: float = -1.0e8) -> list[dict]:
    """倒序生成 n 行：rows[0] 最新余额 latest，往前每日 -step。"""
    return [
        _row(f"2026-07-{30 - i:02d}" if i < 30 else f"2026-06-{60 - i:02d}",
             latest - step * i, 4.0, 5.0e11, 8.0e7)
        for i in range(n)
    ]


class TestParseRows:

    def test_units_passthrough_and_desc_sort(self):
        parsed = _parse_rows(list(reversed(_rows_300502())))  # 打乱为升序
        assert parsed[0]["date"] == "2026-07-30"
        assert parsed[0]["rzye"] == pytest.approx(20893993369)  # 元直通
        assert parsed[0]["rzyezb"] == pytest.approx(4.48899124)
        assert parsed[0]["sz"] == pytest.approx(465449635933.8)

    def test_bad_rows_skipped(self):
        rows = _rows_300502() + [
            {"DATE": None, "RZYE": 1.0},
            {"DATE": "2026-07-27 00:00:00", "RZYE": None},
            {"DATE": "2026-07-26 00:00:00", "RZYE": -5.0},
        ]
        assert len(_parse_rows(rows)) == 3


class TestPctOfFloat:

    def test_percent_scale_taken_directly(self):
        assert _pct_of_float(
            {"rzyezb": 4.48899124, "rzye": 2.09e10, "sz": 4.65e11}
        ) == pytest.approx(4.49)

    def test_missing_recomputed_from_float_cap(self):
        out = _pct_of_float(
            {"rzyezb": None, "rzye": 20893993369, "sz": 465449635933.8}
        )
        assert out == pytest.approx(4.49)

    def test_decimal_scale_anomaly_recomputed(self):
        # 源头若改发小数（0.0449），经 SZ 重算恢复百分数口径
        out = _pct_of_float(
            {"rzyezb": 0.0449, "rzye": 20893993369, "sz": 465449635933.8}
        )
        assert out == pytest.approx(4.49)

    def test_decimal_scale_without_float_cap_kept(self):
        assert _pct_of_float(
            {"rzyezb": 0.9, "rzye": 1.0e9, "sz": None}
        ) == pytest.approx(0.9)

    def test_absurd_value_dropped(self):
        assert _pct_of_float(
            {"rzyezb": 150.0, "rzye": 1.0e9, "sz": None}
        ) is None


class TestAssembleContract:

    def test_latest_fields(self):
        out = _assemble(_rows_300502(), "300502")
        assert out["is_margin_eligible"] is True
        assert out["latest_date"] == "2026-07-30"
        assert out["margin_balance"] == pytest.approx(20893993369)
        assert out["short_balance"] == pytest.approx(83727582)
        assert out["margin_balance_pct_of_float"] == pytest.approx(4.49)
        assert out["float_market_cap"] == pytest.approx(465449635933.8)
        assert "300502" in out["source_note"]
        blob = "\n".join(out["lines_zh"])
        assert "融资余额 208.94 亿元" in blob
        assert "截至 2026-07-30" in blob
        assert "约占流通市值 4.49%" in blob
        assert "融券余额 8373 万元" in blob

    def test_full_window_20d_change(self):
        rows = _rows_n(25)
        out = _assemble(rows, "300502")
        assert out["chg_window_days"] == 20
        base = rows[20]["RZYE"]
        expect = round((rows[0]["RZYE"] / base - 1) * 100.0, 2)
        assert out["balance_chg_pct"] == pytest.approx(expect)

    def test_short_window_uses_oldest(self):
        rows = _rows_n(5)
        out = _assemble(rows, "300502")
        assert out["chg_window_days"] == 4
        assert out["balance_chg_pct"] is not None

    def test_single_row_no_change(self):
        out = _assemble(_rows_n(1), "300502")
        assert out["balance_chg_pct"] is None
        assert out["chg_window_days"] is None
        assert len(out["lines_zh"]) == 1

    def test_drop_direction_wording(self):
        # 2.0e10 → 1.6e10：-20% 退潮
        rows = _rows_n(21, latest=1.6e10, step=(1.6e10 - 2.0e10) / 20)
        out = _assemble(rows, "300502")
        assert out["balance_chg_pct"] == pytest.approx(-20.0)
        blob = "\n".join(out["lines_zh"])
        assert "融资余额下降 20.00%" in blob
        assert "杠杆资金退潮" in blob

    def test_rise_direction_wording(self):
        rows = _rows_n(21, latest=2.4e10, step=(2.4e10 - 2.0e10) / 20)
        out = _assemble(rows, "300502")
        assert out["balance_chg_pct"] == pytest.approx(20.0)
        blob = "\n".join(out["lines_zh"])
        assert "融资余额上升 20.00%" in blob
        assert "杠杆资金加码" in blob

    def test_flat_within_threshold(self):
        rows = _rows_n(21, latest=2.04e10, step=(2.04e10 - 2.0e10) / 20)
        out = _assemble(rows, "300502")
        assert out["balance_chg_pct"] == pytest.approx(2.0)
        blob = "\n".join(out["lines_zh"])
        assert "变动 +2.00%" in blob
        assert "大体平稳" in blob

    def test_sanctioned_pcts(self):
        rows = _rows_n(21, latest=1.6e10, step=(1.6e10 - 2.0e10) / 20)
        out = _assemble(rows, "300502")
        # 占流通市值 % 与变化 %（绝对值）注册进白名单
        pct_float = out["margin_balance_pct_of_float"]
        assert pct_float in out["sanctioned_pcts"]
        assert 20.0 in out["sanctioned_pcts"]

    def test_all_rows_bad(self):
        assert _assemble([{"DATE": None, "RZYE": None}], "300502") is None


class TestNotEligible:

    def test_block_shape(self):
        out = _not_eligible_block("600000")
        assert out["is_margin_eligible"] is False
        assert out["margin_balance"] is None
        assert out["sanctioned_pcts"] == []
        blob = "\n".join(out["lines_zh"])
        assert "不在融资融券标的范围内" in blob
        assert "非两融标的" in out["source_note"]

    def test_fetch_empty_rows_returns_block(self, monkeypatch):
        import aegis.core.acquisition.connectors.margin_trading as mt
        monkeypatch.setattr(mt, "_fetch_ggmx", lambda c: [])
        out = fetch_margin_trading("600000")
        assert out["is_margin_eligible"] is False


class TestFetchGgmx:

    def _fake_requests(self, monkeypatch, payload: Any):
        import sys
        import types
        fake = types.ModuleType("requests")

        class _Resp:
            def json(self):
                if isinstance(payload, Exception):
                    raise payload
                return payload

        fake.get = lambda *a, **k: _Resp()
        monkeypatch.setitem(sys.modules, "requests", fake)

    def test_success_payload(self, monkeypatch):
        self._fake_requests(monkeypatch, {
            "success": True, "result": {"data": _rows_300502(), "count": 3},
        })
        rows = _fetch_ggmx("300502")
        assert len(rows) == 3

    def test_empty_code_9201_means_not_eligible(self, monkeypatch):
        # 2026-08-01 实测非标的/不存在代码的真实返回形态
        self._fake_requests(monkeypatch, {
            "version": None, "result": None, "success": False,
            "message": "返回数据为空", "code": 9201,
        })
        assert _fetch_ggmx("999999") == []

    def test_other_failure_code_is_unknown(self, monkeypatch):
        self._fake_requests(monkeypatch, {
            "success": False, "message": "rate limited", "code": 500,
        })
        assert _fetch_ggmx("300502") is None

    def test_malformed_json_is_unknown(self, monkeypatch):
        self._fake_requests(monkeypatch, ValueError("bad json"))
        assert _fetch_ggmx("300502") is None
        self._fake_requests(monkeypatch, ["not-a-dict"])
        assert _fetch_ggmx("300502") is None


class TestFetchNeverRaises:

    def test_network_failure_degrades(self, monkeypatch):
        import aegis.core.acquisition.connectors.margin_trading as mt

        @contextmanager
        def _boom():
            raise RuntimeError("net down")
            yield  # pragma: no cover

        monkeypatch.setattr(mt, "_no_proxy", _boom)
        assert fetch_margin_trading("300502") is None

    def test_assemble_raise_degrades(self, monkeypatch):
        import aegis.core.acquisition.connectors.margin_trading as mt
        monkeypatch.setattr(mt, "_fetch_ggmx", lambda c: _rows_300502())
        monkeypatch.setattr(
            mt, "_assemble",
            lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
        assert fetch_margin_trading("300502") is None

    def test_fetch_end_to_end(self, monkeypatch):
        import aegis.core.acquisition.connectors.margin_trading as mt
        seen: dict = {}

        def _stub(code):
            seen["code"] = code
            return _rows_300502()

        monkeypatch.setattr(mt, "_fetch_ggmx", _stub)
        out = fetch_margin_trading("300502.SZ")
        assert seen["code"] == "300502"  # 后缀剥离
        assert out["margin_balance_pct_of_float"] == pytest.approx(4.49)


class TestSanctionedPctsExtraction:

    def test_tolerant_extraction(self):
        out = _assemble(_rows_300502(), "300502")
        assert margin_sanctioned_pcts(out) == out["sanctioned_pcts"]
        assert margin_sanctioned_pcts(None) == []
        assert margin_sanctioned_pcts({"sanctioned_pcts": "junk"}) == []
        assert margin_sanctioned_pcts("not-a-dict") == []
