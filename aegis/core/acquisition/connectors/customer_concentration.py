"""L1 Wave 2（2026-07-31）：A 股客户集中度摄取（巨潮年报 PDF）。

七轮 Grok 审计反复扣分的第二个数据缺口："前五大客户集中度不在研究
上下文里，agents 只能把大客户依赖写进 open_questions"（第一个缺口
分部收入已由 Wave 1 解决，见 segment_zygc.py——本模块接线模式同构）。

东财无此数据（2026-07-31 探查确认：PC_HSF10 BusinessAnalysis 只有
zyfw/zygcfx/jyps 三块，datacenter-web RPT_* 亦无）；唯一可靠来源是
年报 PDF「经营情况讨论与分析」节的标准披露，两种模板：

- 深交所（表格）："前五名客户合计销售金额占年度销售总额比例
  38.96%" + 前 5 大客户逐行占比（top1 可得）；
- 上交所（句子）："前五名客户销售额 1,704,578.78 万元，占年度销售
  总额 10.10%"（无逐客户明细，top1 缺省 None）。

链路：akshare ``stock_zh_a_disclosure_report_cninfo``（巨潮公告检索，
``_no_proxy`` 复用——.cn 域名须绕开代理）定位最新年报 →
``static.cninfo.com.cn/finalpage/{date}/{id}.PDF`` 直链下载（缓存
``.cache/annual_reports/``，网络失败时回退最近缓存）→ PyMuPDF（缺则
pypdf）逐页抽取 → 正则解析。同一披露表顺带解析前五名供应商采购占比。

产出三份消费物（任何环节失败返回 None，永不 raise）：
- ``top5_customer_share`` / ``top1_customer_share`` / ``customer_shares``
  / ``single_customer_over_50pct`` / ``top5_supplier_share``：集中度事实；
- ``lines_zh``：prompt 注入用中文行（agents + synthesizer），模式同
  segment_zygc；
- ``sanctioned_pcts``：占比 % 进清洗白名单（设计红线 9 同则——引用
  真数据的 % 不许被 strict 清洗误杀）。
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

from aegis.core.acquisition.connectors.akshare_connector import _no_proxy

logger = logging.getLogger(__name__)

# 年报一般在报告期次年 3-4 月披露；回看 500 天必含最新一份
_LOOKBACK_DAYS = 500

# 缓存目录（测试可用环境变量改道）
_CACHE_DIR_ENV = "AEGIS_ANNUAL_REPORT_CACHE_DIR"
_DEFAULT_CACHE_DIR = ".cache/annual_reports"

# 公告标题过滤：正文优先（"更新后"版发布更晚，按时间倒序自然胜出）
_EXCLUDE_TITLE = re.compile(r"摘要|英文|已取消")

_NUM = r"([0-9]+(?:\.[0-9]+)?)"
# 深交所表格模板："合计销售金额占"锚点天然排除关联方行（关联方行是
# "销售额中关联方销售额占"，不含"合计销售金额占"）
_TOP5_TABLE = re.compile(
    r"前五名客户合计销售金额[^0-9%％]{0,30}占(?:年度)?销售总额的?比例"
    r"[^0-9%％]{0,10}" + _NUM + r"[%％]"
)
# 上交所句子模板；关联方分句以"销售额中关联方"续接，首个匹配即合计行
_TOP5_SENT = re.compile(
    r"前五名客户销售额[0-9,，.]+万?元[^0-9%％]{0,20}占(?:年度)?销售总额"
    r"的?比?例?约?为?" + _NUM + r"[%％]"
)
_SUP5_TABLE = re.compile(
    r"前五名供应商合计采购金额[^0-9%％]{0,30}占(?:年度)?采购总额的?比例"
    r"[^0-9%％]{0,10}" + _NUM + r"[%％]"
)
_SUP5_SENT = re.compile(
    r"前五名供应商采购额[0-9,，.]+万?元[^0-9%％]{0,20}占(?:年度)?采购总额"
    r"的?比?例?约?为?" + _NUM + r"[%％]"
)
# 逐行占比：占比在自己的行上（"13.73%"），金额行无 % 不会误中
_ROW_PCT = re.compile(r"^([0-9]+(?:\.[0-9]+)?)[%％]$", re.MULTILINE)


def _cache_dir() -> Path:
    return Path(os.environ.get(_CACHE_DIR_ENV, _DEFAULT_CACHE_DIR))


def _squash(text: str) -> str:
    """去除全部空白（PDF 版面空格/换行是排版噪声）——标签+数值匹配用。"""
    return re.sub(r"\s+", "", text)


def _strip_lines(text: str) -> str:
    """行内去空格、保留换行——逐行占比提取用（全 squash 会把金额与
    占比连成一串产生歧义，如 "…620.38" + "12.72%" → "620.3812.72%"）。"""
    return "\n".join(re.sub(r"[ \t　]+", "", ln) for ln in text.splitlines())


def _shares_consistent(shares: list[float], top5: float) -> bool:
    """逐行占比自洽校验：降序、每项 ≤ 合计、加总 ≈ 合计（±1.5pp 容差
    覆盖披露四舍五入）。不自洽宁缺毋滥——top1 缺省好过引错。"""
    if not shares or len(shares) > 6:
        return False
    if any(s <= 0 or s > top5 + 0.01 for s in shares):
        return False
    if any(shares[i] < shares[i + 1] - 0.01 for i in range(len(shares) - 1)):
        return False
    return abs(sum(shares) - top5) <= 1.5


def _customer_row_shares(lines_text: str) -> list[float]:
    """深交所模板「前 5 大客户资料」表 → 逐客户占比列表（降序）。"""
    m = re.search(r"客户资料", lines_text)
    if not m:
        return []
    tail = lines_text[m.end():]
    end = tail.find("合计")
    if end > 0:
        tail = tail[:end]
    return [float(x) for x in _ROW_PCT.findall(tail)]


def _parse_concentration(text: str) -> dict[str, Any] | None:
    """从一页（或跨页拼接）文本解析集中度事实。top5 缺失即整体放弃。"""
    sq = _squash(text)
    m = _TOP5_TABLE.search(sq) or _TOP5_SENT.search(sq)
    if not m:
        return None
    top5 = float(m.group(1))
    if not 0.0 < top5 <= 100.0:
        return None

    shares = _customer_row_shares(_strip_lines(text))
    if shares and not _shares_consistent(shares, top5):
        shares = []
    top1 = shares[0] if shares else None

    ms = _SUP5_TABLE.search(sq) or _SUP5_SENT.search(sq)
    sup5 = float(ms.group(1)) if ms else None
    if sup5 is not None and not 0.0 < sup5 <= 100.0:
        sup5 = None

    # 单一客户过半判定：top1 已知看 top1；top5 合计都不过半则必然无
    if top1 is not None:
        over50: bool | None = top1 > 50.0
    elif top5 <= 50.0:
        over50 = False
    else:
        over50 = None

    return {
        "top5_customer_share": top5,
        "top1_customer_share": top1,
        "customer_shares": shares,
        "single_customer_over_50pct": over50,
        "top5_supplier_share": sup5,
    }


def _iter_page_texts(pdf_path: Path):
    """逐页产出文本。PyMuPDF 优先（CJK 抽取更稳），缺则 pypdf。"""
    try:
        import fitz
    except ImportError:
        fitz = None
    if fitz is not None:
        doc = fitz.open(str(pdf_path))
        try:
            for page in doc:
                yield page.get_text()
        finally:
            doc.close()
        return
    from pypdf import PdfReader
    for page in PdfReader(str(pdf_path)).pages:
        yield page.extract_text() or ""


def _parse_pdf(pdf_path: Path) -> dict[str, Any] | None:
    """按阅读顺序扫页：首个含关键词且解析成功的页胜出（即「经营情况
    讨论与分析」的主披露表；财务附注里的次生提法排在其后）。命中页
    解析失败时与下一页拼接重试一次（披露表跨页断行）。"""
    pending: str | None = None
    for text in _iter_page_texts(pdf_path):
        if pending is not None:
            out = _parse_concentration(pending + "\n" + text)
            pending = None
            if out:
                return out
        sq = _squash(text)
        if "前五名客户" in sq or "前五大客户" in sq:
            out = _parse_concentration(text)
            if out:
                return out
            pending = text
    return None


def _locate_and_download(stock_code: str) -> tuple[Path | None, str, str]:
    """巨潮公告检索定位最新年报正文并下载（带缓存）。

    返回 (pdf_path, 公告标题, 披露日期)；定位失败 (None, "", "")。
    """
    code = str(stock_code).strip()[:6]
    with _no_proxy():
        import akshare as ak
        import requests
        from datetime import datetime, timedelta

        end = datetime.now()
        start = end - timedelta(days=_LOOKBACK_DAYS)
        df = ak.stock_zh_a_disclosure_report_cninfo(
            symbol=code, market="沪深京", category="年报",
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
        )
        if df is None or df.empty:
            return None, "", ""
        titles = df["公告标题"].astype(str)
        df = df[titles.str.contains("年度报告")
                & ~titles.str.contains(_EXCLUDE_TITLE.pattern, regex=True)]
        if df.empty:
            return None, "", ""

        for _, row in df.sort_values("公告时间", ascending=False).iterrows():
            link = str(row["公告链接"])
            m_id = re.search(r"announcementId=(\d+)", link)
            m_t = re.search(r"announcementTime=([\d\-]+)", link)
            if not (m_id and m_t):
                continue
            title = str(row["公告标题"]).strip()
            year_m = re.search(r"(20\d{2})", title)
            year = year_m.group(1) if year_m else "0000"
            cache = _cache_dir() / f"{code}_{year}_{m_id.group(1)}.pdf"
            if cache.exists() and cache.stat().st_size > 10_000:
                return cache, title, m_t.group(1)
            url = (f"http://static.cninfo.com.cn/finalpage/"
                   f"{m_t.group(1)}/{m_id.group(1)}.PDF")
            resp = requests.get(url, timeout=120,
                                headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code != 200 or not resp.content.startswith(b"%PDF"):
                logger.debug("annual report download miss: %s -> %s",
                             url, resp.status_code)
                continue
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(resp.content)
            return cache, title, m_t.group(1)
    return None, "", ""


def _cached_fallback(stock_code: str) -> tuple[Path | None, str, str]:
    """网络定位失败时回退最近一份本地缓存年报（文件名携带年份）。"""
    code = str(stock_code).strip()[:6]
    cands = sorted(_cache_dir().glob(f"{code}_*.pdf"),
                   key=lambda p: p.name, reverse=True)
    if not cands:
        return None, "", ""
    parts = cands[0].stem.split("_")
    year = parts[1] if len(parts) >= 3 and parts[1].isdigit() else ""
    title = f"{year}年年度报告" if year and year != "0000" else "年度报告"
    return cands[0], title, ""


def _assemble(parsed: dict[str, Any], title: str, disclosed: str,
              code: str) -> dict[str, Any]:
    year_m = re.search(r"(20\d{2})", title or "")
    fiscal_period = f"{year_m.group(1)}-12-31" if year_m else ""
    label = f"{year_m.group(1)}年报" if year_m else "最新年报"

    top5 = parsed["top5_customer_share"]
    top1 = parsed.get("top1_customer_share")
    shares = parsed.get("customer_shares") or []
    over50 = parsed.get("single_customer_over_50pct")
    sup5 = parsed.get("top5_supplier_share")

    pcts: list[float] = [round(top5, 2)]
    bits = [f"前五名客户合计销售占比 {top5:.2f}%"]
    if top1 is not None:
        bits.append(f"第一大客户占比 {top1:.2f}%")
        pcts.append(round(top1, 2))
    if len(shares) > 1:
        bits.append("前五分别为 " + "/".join(f"{s:.2f}%" for s in shares))
        pcts.extend(round(s, 2) for s in shares)
    # 措辞避开 "50%" 字面量——它不是披露数字，不进白名单也不该被引用
    if over50 is True:
        bits.append("存在销售占比过半的单一大客户（重大客户依赖）")
    elif over50 is False:
        bits.append("不存在销售占比过半的单一客户")

    lines = [f"[{label}] " + "，".join(bits)]
    if sup5 is not None:
        lines.append(f"[{label}] 前五名供应商合计采购占比 {sup5:.2f}%")
        pcts.append(round(sup5, 2))

    disclosed_bit = f"，{disclosed} 披露" if disclosed else "，本地缓存"
    return {
        "source": "cninfo_annual_pdf",
        "fiscal_period": fiscal_period,
        "report_title": title or "",
        "top5_customer_share": top5,
        "top1_customer_share": top1,
        "customer_shares": shares,
        "single_customer_over_50pct": over50,
        "top5_supplier_share": sup5,
        "lines_zh": lines,
        # 设计红线 9：真实披露 % 注册进清洗白名单（去重保序）
        "sanctioned_pcts": list(dict.fromkeys(pcts)),
        "source_note": f"巨潮资讯年报 PDF《{title}》（{code}{disclosed_bit}）",
    }


def fetch_customer_concentration(stock_code: str) -> dict[str, Any] | None:
    """定位最新年报 → 下载缓存 → 抽取解析客户集中度。

    任何环节失败（网络 / 无年报 / PDF 损坏 / 模板不识别）返回 None，
    永不 raise——集中度是增益数据，不阻断主流程。
    """
    try:
        pdf_path, title, disclosed = _locate_and_download(stock_code)
    except Exception as e:
        logger.debug("annual report locate failed for %s: %s", stock_code, e)
        pdf_path, title, disclosed = None, "", ""
    if pdf_path is None:
        try:
            pdf_path, title, disclosed = _cached_fallback(stock_code)
        except Exception:
            return None
    if pdf_path is None:
        return None
    try:
        parsed = _parse_pdf(pdf_path)
        if not parsed:
            return None
        return _assemble(parsed, title, disclosed,
                         str(stock_code).strip()[:6])
    except Exception as e:
        logger.debug("annual report parse failed for %s: %s", stock_code, e)
        return None


def customer_sanctioned_pcts(blk: Any) -> list[float]:
    """从 __customer_concentration 块提取白名单 %（缺省容错）。

    红线 8 说明：本模块自身不读写共享事实字典——盖章由 orchestrator
    （棘轮白名单内）完成，这里只接收已提取的块作显式参数。
    """
    if isinstance(blk, dict):
        vals = blk.get("sanctioned_pcts")
        if isinstance(vals, list):
            return [float(v) for v in vals if isinstance(v, (int, float))]
    return []
