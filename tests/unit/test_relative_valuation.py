"""Tests for the A-share relative valuation anchor (relative_valuation).

DESIGN_2.0 Phase 1 task C coverage:
- 主路径：目标估值行 → 同板块成分（同一交易日）→ 市值最接近 ≤10 家
- 分位/中位数计算（midrank 平手、亏损 peer 剔除但计数披露）
- 红线 5 gate：单指标样本 <4 家 → 该指标 None；双指标全无效 → insufficient_peers
- 兜底：板块路径失败 → 内置映射表逐 peer 拉估值；全链失败静默降级（永不 raise）
- zh_lines 中文摘要格式 + sanctioned_numbers（红线 9）+ to_dict
- live network smoke tests for 002669 / 600519 (AEGIS_RUN_NETWORK_TESTS=1)
"""

from __future__ import annotations

import pytest

from conftest import require_network
from aegis.core.truth import relative_valuation as rv
from aegis.core.truth.relative_valuation import (
    MIN_PEER_SAMPLE,
    PeerQuote,
    RelativeValuation,
    _percentile_rank,
    _select_closest_peers,
    compute_relative_valuation,
)


# ============================================================
# HTTP stubbing helpers（照搬 test_em_events 范式）
# ============================================================

class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _dc(rows, count=None):
    """eastmoney datacenter envelope."""
    return {"result": {"data": rows, "count": count if count is not None else len(rows)},
            "success": True}


def _install_get(monkeypatch, handler):
    """handler(url, params) -> payload dict; records every call."""
    import requests
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        assert timeout is not None and timeout <= 10, "timeout must be ≤10s"
        calls.append((url, params or {}))
        return FakeResponse(handler(url, params or {}))

    monkeypatch.setattr(requests, "get", fake_get)
    return calls


def _boom(monkeypatch):
    import requests
    calls = []

    def die(*a, **k):
        calls.append(a)
        raise ConnectionError("network down")

    monkeypatch.setattr(requests, "get", die)
    return calls


def _row(code, name="同业", mcap=1e10, pe=20.0, pb=2.0,
         date="2026-07-10 00:00:00", board_code="016041", board_name="化学制品"):
    return {
        "SECURITY_CODE": code, "SECURITY_NAME_ABBR": name,
        "TOTAL_MARKET_CAP": mcap, "PE_TTM": pe, "PB_MRQ": pb,
        "TRADE_DATE": date, "BOARD_CODE": board_code, "BOARD_NAME": board_name,
    }


TARGET_ROW = _row("002669", name="康达新材", mcap=4.1e9, pe=32.62, pb=1.48)

# 6 家同业：PE 有效样本 [10,20,30,40]（中位 25，目标 32.62 → 第 75 分位），
# PB 有效样本 [1,2,3,4,5]（中位 3，目标 1.48 → 第 20 分位），1 家亏损。
BOARD_ROWS = [
    TARGET_ROW,  # 目标自己也在成分里，必须被剔除
    _row("000001", name="甲", mcap=5e9, pe=10.0, pb=1.0),
    _row("000002", name="乙", mcap=4e9, pe=20.0, pb=2.0),
    _row("000003", name="丙", mcap=3e9, pe=30.0, pb=3.0),
    _row("000004", name="丁", mcap=6e9, pe=40.0, pb=4.0),
    _row("000005", name="戊（亏损）", mcap=4.5e9, pe=-5.0, pb=5.0),
    _row("000006", name="己（缺数）", mcap=2e9, pe=None, pb=None),
]


def _board_handler(url, params):
    """SECURITY_CODE 过滤 → 目标行；BOARD_CODE 过滤 → 板块成分。"""
    flt = params.get("filter", "")
    if 'SECURITY_CODE="002669"' in flt:
        return _dc([TARGET_ROW])
    if 'BOARD_CODE="016041"' in flt:
        return _dc(BOARD_ROWS, count=181)
    return _dc([])


# ============================================================
# 纯计算：分位与市值就近选择
# ============================================================

class TestPercentileRank:
    def test_no_ties(self):
        assert _percentile_rank([10, 20, 30, 40], 35.0) == 75.0

    def test_midrank_ties(self):
        assert _percentile_rank([10, 20, 35, 40], 35.0) == 62.5

    def test_below_all(self):
        assert _percentile_rank([10, 20, 30], 5.0) == 0.0

    def test_above_all(self):
        assert _percentile_rank([10, 20, 30], 99.0) == 100.0


class TestSelectClosestPeers:
    def test_picks_closest_by_log_market_cap(self):
        near = [PeerQuote(code=f"{i:06d}", market_cap=1e10 * (1 + i / 10))
                for i in range(1, 11)]
        far = [PeerQuote(code="900001", market_cap=1e13),
               PeerQuote(code="900002", market_cap=1e6)]
        picked = _select_closest_peers(near + far, target_mcap=1e10, max_peers=10)
        assert len(picked) == 10
        assert {q.code for q in picked} == {q.code for q in near}

    def test_unknown_target_mcap_prefers_largest(self):
        quotes = [PeerQuote(code="1", market_cap=1e9),
                  PeerQuote(code="2", market_cap=5e10),
                  PeerQuote(code="3", market_cap=2e10)]
        picked = _select_closest_peers(quotes, target_mcap=None, max_peers=2)
        assert [q.code for q in picked] == ["2", "3"]

    def test_missing_mcap_peers_sorted_last(self):
        quotes = [PeerQuote(code="nomcap", market_cap=None),
                  PeerQuote(code="close", market_cap=1.1e10)]
        picked = _select_closest_peers(quotes, target_mcap=1e10, max_peers=1)
        assert picked[0].code == "close"


# ============================================================
# 主路径：同板块成分
# ============================================================

class TestIndustryBoardPath:
    def test_happy_path_stats(self, monkeypatch):
        _install_get(monkeypatch, _board_handler)
        out = compute_relative_valuation("002669")
        assert out.peer_source == "industry_board"
        assert out.industry == "化学制品"
        assert out.data_date == "2026-07-10"
        assert out.universe_size == 181
        assert out.insufficient_peers is False
        # 目标自己被从 peer 集剔除
        assert out.peer_count == 6
        assert all(p.code != "002669" for p in out.peers)
        # 目标原始倍数
        assert out.target_pe_ttm == pytest.approx(32.62)
        assert out.target_pb == pytest.approx(1.48)
        # PE：有效样本 [10,20,30,40]，亏损 1 家剔除但计数
        assert out.pe_sample_size == 4
        assert out.loss_making_count == 1
        assert out.peer_pe_median == pytest.approx(25.0)
        assert out.pe_percentile == pytest.approx(75.0)
        # PB：有效样本 [1,2,3,4,5]
        assert out.pb_sample_size == 5
        assert out.peer_pb_median == pytest.approx(3.0)
        assert out.pb_percentile == pytest.approx(20.0)

    def test_board_query_pinned_to_target_trade_date(self, monkeypatch):
        calls = _install_get(monkeypatch, _board_handler)
        compute_relative_valuation("002669")
        board_calls = [p for _, p in calls if 'BOARD_CODE="016041"' in p.get("filter", "")]
        assert len(board_calls) == 1
        # 实测坑：不带 TRADE_DATE 的板块查询服务端超时，日期必须钉死
        assert "TRADE_DATE='2026-07-10'" in board_calls[0]["filter"]
        assert board_calls[0]["reportName"] == "RPT_VALUEANALYSIS_DET"

    def test_code_suffix_normalized(self, monkeypatch):
        calls = _install_get(monkeypatch, _board_handler)
        out = compute_relative_valuation("002669.SZ")
        assert out.stock_code == "002669"
        assert out.peer_source == "industry_board"
        first_filter = calls[0][1]["filter"]
        assert 'SECURITY_CODE="002669"' in first_filter

    def test_caps_at_max_peers(self, monkeypatch):
        rows = [TARGET_ROW] + [
            _row(f"{100000 + i}", mcap=4.1e9 * (1 + i / 50), pe=10.0 + i, pb=1.0 + i / 10)
            for i in range(15)
        ]
        _install_get(monkeypatch, lambda u, p: _dc([TARGET_ROW])
                     if "SECURITY_CODE" in p.get("filter", "") else _dc(rows))
        out = compute_relative_valuation("002669")
        assert out.peer_count == 10  # DEFAULT_MAX_PEERS


# ============================================================
# 红线 5：薄覆盖 gate
# ============================================================

class TestSampleGates:
    def test_insufficient_when_fewer_than_four_peers(self, monkeypatch):
        rows = [TARGET_ROW,
                _row("000002", mcap=4e9, pe=20.0, pb=2.0),
                _row("000003", mcap=3e9, pe=30.0, pb=3.0),
                _row("000004", mcap=5e9, pe=40.0, pb=4.0)]
        _install_get(monkeypatch, lambda u, p: _dc([TARGET_ROW])
                     if "SECURITY_CODE" in p.get("filter", "") else _dc(rows))
        out = compute_relative_valuation("002669")
        assert out.peer_count == 3
        assert out.pe_sample_size == 3 and out.pb_sample_size == 3
        assert out.insufficient_peers is True
        assert out.peer_pe_median is None and out.pb_percentile is None
        text = "\n".join(out.zh_lines())
        assert "同业样本不足" in text
        assert "禁止引用" in text

    def test_pe_gated_pb_still_usable(self, monkeypatch):
        # 6 家同业，5 家亏损 → PE 有效样本 1 家 <4 gate 掉；PB 样本 6 家可用
        rows = [TARGET_ROW] + [
            _row(f"{200000 + i}", mcap=4e9, pe=-1.0 * (i + 1), pb=1.0 + i)
            for i in range(5)
        ] + [_row("000009", mcap=5e9, pe=15.0, pb=9.9)]
        _install_get(monkeypatch, lambda u, p: _dc([TARGET_ROW])
                     if "SECURITY_CODE" in p.get("filter", "") else _dc(rows))
        out = compute_relative_valuation("002669")
        assert out.insufficient_peers is False
        assert out.peer_pe_median is None and out.pe_percentile is None
        assert out.loss_making_count == 5
        assert out.pb_sample_size == 6 and out.peer_pb_median is not None
        text = "\n".join(out.zh_lines())
        assert "6 家中 5 家亏损" in text
        assert "该指标分位不可用" in text

    def test_negative_pb_excluded_from_pb_sample(self, monkeypatch):
        rows = [TARGET_ROW] + [
            _row(f"{300000 + i}", mcap=4e9, pe=10.0 + i,
                 pb=(-1.0 if i == 0 else 1.0 + i))
            for i in range(5)
        ]
        _install_get(monkeypatch, lambda u, p: _dc([TARGET_ROW])
                     if "SECURITY_CODE" in p.get("filter", "") else _dc(rows))
        out = compute_relative_valuation("002669")
        assert out.pe_sample_size == 5
        assert out.pb_sample_size == 4  # 负 PB 剔除

    def test_loss_making_target_no_pe_percentile(self, monkeypatch):
        loss_target = _row("002669", name="康达新材", mcap=4.1e9, pe=-15.0, pb=1.48)
        rows = [loss_target,
                _row("000002", mcap=4e9, pe=20.0, pb=2.0),
                _row("000003", mcap=3e9, pe=30.0, pb=3.0),
                _row("000004", mcap=5e9, pe=40.0, pb=4.0),
                _row("000005", mcap=6e9, pe=50.0, pb=5.0)]
        _install_get(monkeypatch, lambda u, p: _dc([loss_target])
                     if "SECURITY_CODE" in p.get("filter", "") else _dc(rows))
        out = compute_relative_valuation("002669")
        assert out.target_pe_ttm == pytest.approx(-15.0)  # 原始值如实保留
        assert out.peer_pe_median is not None  # 同业中位数照常展示
        assert out.pe_percentile is None       # 但目标分位不适用
        assert "目标 TTM 亏损" in "\n".join(out.zh_lines())


# ============================================================
# 兜底路径与降级
# ============================================================

class TestFallbacks:
    def test_static_map_when_board_fails(self, monkeypatch):
        target_600519 = _row("600519", name="贵州茅台", mcap=1.5e12, pe=18.2,
                             pb=5.56, board_code="016165", board_name="白酒Ⅱ")
        static_codes = {"000858", "000568", "603369", "002304", "000799"}

        def handler(url, params):
            flt = params.get("filter", "")
            if 'SECURITY_CODE="600519"' in flt:
                return _dc([target_600519])
            if "BOARD_CODE" in flt:
                raise ConnectionError("board endpoint down")
            for code in static_codes:
                if f'SECURITY_CODE="{code}"' in flt:
                    return _dc([_row(code, mcap=1e11, pe=20.0, pb=3.0)])
            return _dc([])

        _install_get(monkeypatch, handler)
        out = compute_relative_valuation("600519")
        assert out.peer_source == "static_map"
        assert out.peer_count == 5
        assert {p.code for p in out.peers} == static_codes
        assert out.insufficient_peers is False
        assert "内置同业映射表" in "\n".join(out.zh_lines())

    def test_static_map_stops_after_consecutive_failures(self, monkeypatch):
        def handler(url, params):
            flt = params.get("filter", "")
            if 'SECURITY_CODE="600519"' in flt:
                return _dc([_row("600519", board_code="", board_name="")])
            raise ConnectionError("peers dead")

        calls = _install_get(monkeypatch, handler)
        out = compute_relative_valuation("600519")
        assert out.insufficient_peers is True
        # 目标 1 次 + 连续失败止损 2 次 = 3 次请求（不是 1+5 次）
        assert len(calls) == 3

    def test_total_failure_never_raises(self, monkeypatch):
        calls = _boom(monkeypatch)
        out = compute_relative_valuation("600519")
        assert isinstance(out, RelativeValuation)
        assert out.insufficient_peers is True
        assert out.peer_source == "none"
        assert out.peer_count == 0
        # 目标 fetch 失败 → host_suspect → 不再逐 peer 空耗超时
        assert len(calls) == 1
        text = "\n".join(out.zh_lines())
        assert "同业数据不可用" in text
        assert "禁止引用" in text

    def test_unknown_code_no_static_entry(self, monkeypatch):
        _install_get(monkeypatch, lambda u, p: _dc([]))
        out = compute_relative_valuation("300999")
        assert out.insufficient_peers is True
        assert out.peer_source == "none"

    def test_unexpected_internal_error_degrades(self, monkeypatch):
        monkeypatch.setattr(rv, "_compute",
                            lambda *a, **k: (_ for _ in ()).throw(ValueError("boom")))
        out = compute_relative_valuation("002669")
        assert out.insufficient_peers is True
        assert out.stock_code == "002669"


# ============================================================
# 输出面：zh_lines / to_dict / sanctioned_numbers
# ============================================================

class TestOutputs:
    def _happy(self, monkeypatch):
        _install_get(monkeypatch, _board_handler)
        return compute_relative_valuation("002669")

    def test_zh_lines_format(self, monkeypatch):
        out = self._happy(monkeypatch)
        text = "\n".join(out.zh_lines())
        assert "相对估值锚" in text
        assert "数据日期 2026-07-10" in text
        assert "东财行业板块「化学制品」" in text
        assert "板块成分共 181 家" in text
        assert "PE(TTM): 目标 32.6 倍" in text
        assert "同业中位数 25.0 倍" in text
        assert "处于同业第 75 分位" in text
        assert "PB: 目标 1.48 倍" in text
        assert "同业中位数 3.00 倍" in text
        assert "处于同业第 20 分位" in text
        assert "6 家中 1 家亏损" in text
        # 中文化铁律：不得混入英文叙述词（PE/PB 等国际缩写除外）
        for banned in ("insufficient", "median", "percentile", "peer "):
            assert banned not in text

    def test_to_dict_roundtrip(self, monkeypatch):
        out = self._happy(monkeypatch)
        d = out.to_dict()
        assert d["stock_code"] == "002669"
        assert d["peer_pe_median"] == pytest.approx(25.0)
        assert d["insufficient_peers"] is False
        assert isinstance(d["peers"], list) and isinstance(d["peers"][0], dict)
        assert d["peers"][0]["code"]

    def test_sanctioned_numbers_match_display(self, monkeypatch):
        # 红线 9：面世数字与展示口径一致，供调用方注册 scrubber 白名单
        out = self._happy(monkeypatch)
        nums = out.sanctioned_numbers()
        assert set(nums) == {32.6, 25.0, 1.48, 3.0, 75.0, 20.0}

    def test_sanctioned_numbers_empty_when_insufficient(self):
        out = RelativeValuation(stock_code="300999")
        assert out.sanctioned_numbers() == []


# ============================================================
# Live network smoke（AEGIS_RUN_NETWORK_TESTS=1 时运行）
# ============================================================

class TestLiveNetwork:
    @pytest.mark.parametrize("code", ["002669", "600519"])
    def test_smoke(self, code):
        require_network()
        out = compute_relative_valuation(code)
        assert isinstance(out, RelativeValuation)
        assert out.stock_code == code
        # 两只票都是主流板块成员，主路径应当可用
        assert out.peer_source == "industry_board"
        assert out.industry
        assert out.data_date
        assert out.peer_count >= MIN_PEER_SAMPLE
        assert out.insufficient_peers is False
        assert out.peer_pe_median is not None or out.peer_pb_median is not None
        lines = out.zh_lines()
        assert lines and "相对估值锚" in lines[0]
        print("\n".join(lines))
