"""L1 Wave 1（2026-07-31）：A 股分部收入摄取回归测试。

锁定的行为：

① _normalize：真实 zygc 形态（三轴多期）→ segment_detail 契约
   {axis: {name: {"revenue": ...}}}，年报期选轴、最新中报期补 lines；
② detail 与 BUG-46 _dedupe_segment_detail 兼容（revenue 键、正数过滤）；
③ lines_zh 含占比/毛利率、超量分部截断标注"从略"；
④ sanctioned_pcts：占比与毛利率 % 进清洗白名单（红线 9），
   segment_sanctioned_pcts 容错提取；
⑤ _em_symbol 交易所前缀映射；坏输入（缺列/空表/NaN）永不 raise。
"""

from __future__ import annotations

import pandas as pd
import pytest

from aegis.core.acquisition.connectors.segment_zygc import (
    _em_symbol,
    _normalize,
    segment_sanctioned_pcts,
)


def _fixture_df() -> pd.DataFrame:
    """300750 真实形态缩样：年报期三轴 + 中报期产品/地区轴。"""
    rows = [
        # 2025-12-31 年报期
        ("2025-12-31", "按行业分类", "电气机械及器材制造业", 4.177e11, 0.9859, 3.07e11, 1.106e11, 0.2649),
        ("2025-12-31", "按产品分类", "动力电池系统", 3.165e11, 0.7470, 2.41e11, 7.54e10, 0.2384),
        ("2025-12-31", "按产品分类", "储能电池系统", 6.244e10, 0.1474, 4.58e10, 1.67e10, 0.2671),
        ("2025-12-31", "按产品分类", "电池材料及回收", 2.186e10, 0.0516, 1.59e10, 5.96e09, 0.2727),
        ("2025-12-31", "按地区分类", "境内", 2.9e11, 0.6850, 2.3e11, 6.0e10, 0.2069),
        ("2025-12-31", "按地区分类", "境外", 1.28e11, 0.3150, 9.5e10, 3.3e10, 0.2578),
        # 2026-06-30 中报期
        ("2026-06-30", "按产品分类", "动力电池系统", 1.921e11, 0.6938, 1.52e11, 3.96e10, 0.2063),
        ("2026-06-30", "按产品分类", "储能电池系统", 5.326e10, 0.1923, 4.05e10, 1.28e10, 0.2396),
        ("2026-06-30", "按地区分类", "境内", 1.898e11, 0.6854, 1.50e11, 4.02e10, 0.2116),
        ("2026-06-30", "按地区分类", "境外", 8.713e10, 0.3146, 6.10e10, 2.61e10, 0.2997),
        # NaN 收入行：应被静默丢弃
        ("2025-12-31", "按产品分类", "其他(补充)", float("nan"), 0.01, 1e9, 1e8, 0.1),
    ]
    return pd.DataFrame(rows, columns=[
        "报告日期", "分类类型", "主营构成", "主营收入",
        "收入比例", "主营成本", "主营利润", "毛利率",
    ])


class TestNormalize:

    def test_detail_contract_and_annual_axis(self):
        out = _normalize(_fixture_df())
        assert out["fiscal_period"] == "2025-12-31"
        assert out["latest_period"] == "2026-06-30"
        d = out["detail"]
        assert set(d.keys()) == {"product", "region", "industry"}
        # detail 用年报期；revenue 键为 BUG-46 去重契约
        assert d["product"]["动力电池系统"]["revenue"] == pytest.approx(3.165e11)
        assert d["product"]["动力电池系统"]["gross_margin"] == pytest.approx(0.2384)
        # NaN 收入行被丢弃
        assert "其他(补充)" not in d["product"]

    def test_lines_include_interim_and_percentages(self):
        out = _normalize(_fixture_df())
        blob = "\n".join(out["lines_zh"])
        assert "[2025-12-31 分产品]" in blob
        assert "[2026-06-30 分产品]" in blob  # 最新中报期补充
        assert "占74.7%" in blob and "毛利率23.8%" in blob
        assert "动力电池系统" in blob

    def test_sanctioned_pcts_cover_shares_and_margins(self):
        out = _normalize(_fixture_df())
        pcts = out["sanctioned_pcts"]
        assert 74.7 in pcts      # 收入占比
        assert 23.8 in pcts      # 毛利率
        assert segment_sanctioned_pcts(out) == pcts
        assert segment_sanctioned_pcts(None) == []
        assert segment_sanctioned_pcts({"sanctioned_pcts": "junk"}) == []

    def test_segment_cap_marks_omission(self):
        rows = [
            ("2025-12-31", "按产品分类", f"产品{i}", 1e9 * (20 - i), 0.05, 5e8, 1e8, 0.2)
            for i in range(12)
        ]
        df = pd.DataFrame(rows, columns=[
            "报告日期", "分类类型", "主营构成", "主营收入",
            "收入比例", "主营成本", "主营利润", "毛利率",
        ])
        out = _normalize(df)
        assert "另 4 项从略" in out["lines_zh"][0]

    def test_bad_inputs_never_raise(self):
        assert _normalize(pd.DataFrame()) is None
        assert _normalize(pd.DataFrame({"别的列": [1]})) is None
        df = _fixture_df()
        df["主营收入"] = float("nan")
        assert _normalize(df) is None  # 全 NaN → 无有效分部


class TestDedupCompat:

    def test_detail_feeds_bug46_dedup(self):
        from aegis.core.orchestrator.auto_research import (
            AutoResearchOrchestrator,
        )
        out = _normalize(_fixture_df())
        detail = out["detail"]
        company_rev = 4.237e11  # 年报营收（略高于行业轴合计）
        cleaned = AutoResearchOrchestrator._dedupe_segment_detail(
            detail, company_rev,
        )
        assert isinstance(cleaned, dict) and "product" in cleaned
        # 产品轴合计低于营收 85% 阈值时补 synthetic 缺口项或保持原样——
        # 只要不 raise 且 revenue 键仍在即兼容
        for segs in cleaned.values():
            for e in segs.values():
                assert "revenue" in e


class TestEmSymbol:

    @pytest.mark.parametrize("code,expected", [
        ("300750", "SZ300750"), ("002594", "SZ002594"),
        ("600519", "SH600519"), ("688256", "SH688256"),
        ("830799", "BJ830799"),
    ])
    def test_prefix(self, code, expected):
        assert _em_symbol(code) == expected
