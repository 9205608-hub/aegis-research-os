"""L1 Wave 2（2026-07-31）：A 股客户集中度摄取回归测试。

锁定的行为（fixture 全部取自真实年报 PDF 的 PyMuPDF 抽取形态）：

① _parse_concentration 深交所表格模板：top5 合计、逐客户 top1/shares、
   关联方行不误中、同表供应商占比；
② 上交所句子模板：top5 可得、top1 干净降级 None、over50 由 top5 推断；
③ 逐行占比自洽校验（降序/加总≈合计）——不自洽宁缺毋滥；
④ 跨页断表 pending 拼接重试；
⑤ _assemble 契约：lines_zh 中文行、sanctioned_pcts 白名单（红线 9）、
   fiscal_period 由标题年份派生；
⑥ customer_sanctioned_pcts 容错提取；缓存回退按年份取最新；
⑦ 坏输入（空文本/乱码/超界 %）永不 raise。
"""

from __future__ import annotations

import pytest

from aegis.core.acquisition.connectors.customer_concentration import (
    _assemble,
    _cached_fallback,
    _parse_concentration,
    _parse_pdf,
    _shares_consistent,
    customer_sanctioned_pcts,
    fetch_customer_concentration,
)

# 300750《2025年年度报告》第 28 页缩样（PyMuPDF 抽取形态：标签与数值
# 各占一行、金额行无 %、含关联方干扰行与供应商同表）
SZSE_TABLE = """（8） 主要销售客户和主要供应商情况
公司主要销售客户情况
前五名客户合计销售金额（千元）
                          165,061,533
前五名客户合计销售金额占年度销售总额比例
38.96%
前五名客户销售额中关联方销售额占年度销售总额比例
0.00%
公司前5 大客户资料
序号
客户名称
销售额（千元）
占年度销售总额比例
1
第一名
                            58,159,202
13.73%
2
第二名
                            47,127,609
11.12%
3
第三名
                            30,201,701
7.13%
4
第四名
                            15,419,319
3.64%
5
第五名
                            14,153,702
3.34%
合计
--
                          165,061,533
38.96%
主要客户其他情况说明
□适用 不适用
公司主要供应商情况
前五名供应商合计采购金额（千元）
                            59,938,203
前五名供应商合计采购金额占年度采购总额比例
10.38%
前五名供应商采购额中关联方采购额占年度采购总额比例
0.00%
"""

# 600519《2025年年度报告》第 11 页缩样（上交所句子模板，含换行断句）
SSE_SENTENCE = """(7). 主要销售客户及主要供应商情况
A.公司主要销售客户及主要供应商情况
√适用□不适用
前五名客户销售额1,704,578.78万元，占年度销售总额10.10%；其中前五名客户销售额中关联方
销售额635,829.28万元，占年度销售总额3.77%。
前五名供应商采购额348,825.84万元，占年度采购总额38.12%；其中前五名供应商采购额中关联
方采购额139,283.25万元，占年度采购总额15.22%。
"""


class TestParseSzseTable:

    def test_top5_top1_shares_supplier(self):
        out = _parse_concentration(SZSE_TABLE)
        assert out["top5_customer_share"] == pytest.approx(38.96)
        assert out["top1_customer_share"] == pytest.approx(13.73)
        assert out["customer_shares"] == pytest.approx(
            [13.73, 11.12, 7.13, 3.64, 3.34])
        assert out["single_customer_over_50pct"] is False
        assert out["top5_supplier_share"] == pytest.approx(10.38)

    def test_related_party_row_not_mistaken(self):
        # 关联方行 0.00% 紧跟合计行之后——合计锚点不得吃到它
        out = _parse_concentration(SZSE_TABLE)
        assert out["top5_customer_share"] != pytest.approx(0.0)

    def test_inconsistent_rows_drop_top1_keep_top5(self):
        # 逐行占比与合计对不上（伪造 40%+40%）→ shares 整体放弃
        text = SZSE_TABLE.replace("13.73%", "40.00%").replace("11.12%", "40.00%")
        out = _parse_concentration(text)
        assert out["top5_customer_share"] == pytest.approx(38.96)
        assert out["top1_customer_share"] is None
        assert out["customer_shares"] == []

    def test_over50_true_from_top1(self):
        text = """前五名客户合计销售金额占年度销售总额比例
72.00%
公司前5大客户资料
序号
客户名称
销售额（元）
占年度销售总额比例
1
第一名
1,000
60.00%
2
第二名
500
12.00%
合计
--
1,500
72.00%
"""
        out = _parse_concentration(text)
        assert out["top1_customer_share"] == pytest.approx(60.0)
        assert out["single_customer_over_50pct"] is True


class TestParseSseSentence:

    def test_sentence_template(self):
        out = _parse_concentration(SSE_SENTENCE)
        assert out["top5_customer_share"] == pytest.approx(10.10)
        assert out["top1_customer_share"] is None
        assert out["customer_shares"] == []
        # top5 合计不过半 → 必然不存在单一客户过半
        assert out["single_customer_over_50pct"] is False
        assert out["top5_supplier_share"] == pytest.approx(38.12)

    def test_over50_unknown_when_no_top1_and_top5_high(self):
        text = "前五名客户销售额900,000万元，占年度销售总额80.00%。"
        out = _parse_concentration(text)
        assert out["top5_customer_share"] == pytest.approx(80.0)
        assert out["single_customer_over_50pct"] is None


class TestBadInputsNeverRaise:

    @pytest.mark.parametrize("text", [
        "",
        "本报告不含相关披露",
        "前五名客户合计销售金额占年度销售总额比例\n180.00%",  # 超界 %
        "前五名客户……占比未披露",
    ])
    def test_returns_none(self, text):
        assert _parse_concentration(text) is None

    def test_shares_consistent_guards(self):
        assert not _shares_consistent([], 38.96)
        assert not _shares_consistent([10, 20], 38.96)          # 升序
        assert not _shares_consistent([50.0], 38.96)            # 超合计
        assert not _shares_consistent([5.0] * 7, 35.0)          # 行数超界
        assert _shares_consistent([13.73, 11.12, 7.13, 3.64, 3.34], 38.96)


class TestCrossPagePending:

    def test_table_split_across_pages(self, monkeypatch, tmp_path):
        # 标签在上页末、数值在下页首——pending 拼接后应解析成功
        page1 = "（8） 主要销售客户情况\n前五名客户合计销售金额占年度销售总额比例"
        page2 = "38.96%\n后续内容"
        import aegis.core.acquisition.connectors.customer_concentration as cc
        monkeypatch.setattr(cc, "_iter_page_texts",
                            lambda _p: iter([page1, page2]))
        out = _parse_pdf(tmp_path / "fake.pdf")
        assert out["top5_customer_share"] == pytest.approx(38.96)


class TestAssembleContract:

    def test_lines_pcts_fiscal_period(self):
        parsed = _parse_concentration(SZSE_TABLE)
        out = _assemble(parsed, "2025年年度报告", "2026-03-10", "300750")
        assert out["fiscal_period"] == "2025-12-31"
        blob = "\n".join(out["lines_zh"])
        assert "[2025年报]" in blob
        assert "前五名客户合计销售占比 38.96%" in blob
        assert "第一大客户占比 13.73%" in blob
        assert "前五名供应商合计采购占比 10.38%" in blob
        assert "不存在销售占比过半的单一客户" in blob
        # 红线 9：lines 里出现的 % 全部注册进白名单
        pcts = out["sanctioned_pcts"]
        for v in (38.96, 13.73, 11.12, 7.13, 3.64, 3.34, 10.38):
            assert v in pcts
        assert "2026-03-10 披露" in out["source_note"]

    def test_offline_fallback_note_and_no_top1(self):
        parsed = _parse_concentration(SSE_SENTENCE)
        out = _assemble(parsed, "2025年年度报告", "", "600519")
        assert "本地缓存" in out["source_note"]
        assert "第一大客户" not in "\n".join(out["lines_zh"])
        assert out["top1_customer_share"] is None


class TestSanctionedPctsExtraction:

    def test_tolerant_extraction(self):
        parsed = _parse_concentration(SZSE_TABLE)
        out = _assemble(parsed, "2025年年度报告", "2026-03-10", "300750")
        assert customer_sanctioned_pcts(out) == out["sanctioned_pcts"]
        assert customer_sanctioned_pcts(None) == []
        assert customer_sanctioned_pcts({"sanctioned_pcts": "junk"}) == []
        assert customer_sanctioned_pcts("not-a-dict") == []


class TestCacheFallback:

    def test_latest_year_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AEGIS_ANNUAL_REPORT_CACHE_DIR", str(tmp_path))
        (tmp_path / "300750_2024_111.pdf").write_bytes(b"%PDF-old")
        (tmp_path / "300750_2025_222.pdf").write_bytes(b"%PDF-new")
        path, title, disclosed = _cached_fallback("300750")
        assert path.name == "300750_2025_222.pdf"
        assert title == "2025年年度报告"
        assert disclosed == ""

    def test_empty_cache(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AEGIS_ANNUAL_REPORT_CACHE_DIR", str(tmp_path))
        assert _cached_fallback("300750") == (None, "", "")


class TestFetchNeverRaises:

    def test_locate_raises_and_no_cache(self, monkeypatch, tmp_path):
        import aegis.core.acquisition.connectors.customer_concentration as cc
        monkeypatch.setenv("AEGIS_ANNUAL_REPORT_CACHE_DIR", str(tmp_path))
        monkeypatch.setattr(cc, "_locate_and_download",
                            lambda _c: (_ for _ in ()).throw(RuntimeError("net")))
        assert fetch_customer_concentration("300750") is None

    def test_corrupt_cached_pdf_degrades(self, monkeypatch, tmp_path):
        import aegis.core.acquisition.connectors.customer_concentration as cc
        monkeypatch.setenv("AEGIS_ANNUAL_REPORT_CACHE_DIR", str(tmp_path))
        (tmp_path / "300750_2025_1.pdf").write_bytes(b"not a pdf at all")
        monkeypatch.setattr(cc, "_locate_and_download",
                            lambda _c: (None, "", ""))
        assert fetch_customer_concentration("300750") is None
