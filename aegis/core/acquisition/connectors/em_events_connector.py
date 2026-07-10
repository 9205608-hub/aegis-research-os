"""A-share recent-events slice connector — 公告 + 业绩预告 + 一致预期.

DESIGN_2.0 Phase 0 §五.3: the report must quote *known catalysts* from a
verifiable fact source, otherwise the LLM agents can only hallucinate
"并购故事". This connector pulls three thin event slices for one stock:

1. **业绩预告** (earnings forecast) — eastmoney datacenter
   ``RPT_PUBLIC_OP_NEWPREDICT`` filtered by ``SECURITY_CODE``. Chosen over
   ``akshare.stock_yjyg_em`` because the akshare path downloads the whole
   market for a *guessed* report period (631 rows, paginated) while the
   filtered datacenter call returns exactly this stock's rows in one
   request regardless of period (verified live 2026-07-10 for 002669).
2. **公告标题流** (announcement titles, last N days) — primary:
   ``np-anotice-stock.eastmoney.com`` per-stock announcement API
   (verified live: 30 rows, date-desc, category tags). Fallback: cninfo
   ``hisAnnouncement/query`` POST (anti-crawler prone → degrade silently).
3. **一致预期** (analyst consensus) — three datacenter-family calls:
   ``RPT_RES_PROFITPREDICT`` (per-year EPS / net profit / revenue),
   ``RPT_WEB_RESPREDICT`` (``RATING_ORG_NUM`` = covering orgs, 6 months),
   ``reportapi.eastmoney.com/report/list`` (latest research report date).

   **设计红线 5** — consensus is 旁证 not 主口径: it is only *usable* when
   coverage ≥ ``MIN_CONSENSUS_ORGS`` orgs **and** the latest report is
   ≤ ``MAX_CONSENSUS_STALENESS_DAYS`` days old. Below that threshold
   ``insufficient_coverage=True`` and :meth:`RecentEvents.to_prompt_block`
   renders 「无有效一致预期」 instead of garbage numbers (small-cap zero
   coverage is the norm: 002669 has 1 org / 0 reports in 12 months).

Engineering contract (Phase 0 task B):
- every HTTP call goes through :func:`_no_proxy` (CN hosts must bypass the
  Clash proxy — semantics reused from ``akshare_connector``);
- timeouts ≤ 10s;
- any single-source failure degrades silently to an empty section + log —
  one dead endpoint never breaks the other sources or the main pipeline.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .akshare_connector import _no_proxy, _safe_float

logger = logging.getLogger(__name__)

_DATACENTER_URL = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
_ANNOUNCE_URL = "https://np-anotice-stock.eastmoney.com/api/security/ann"
_REPORTAPI_URL = "https://reportapi.eastmoney.com/report/list"
_CNINFO_URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"

_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

_TIMEOUT = 10  # hard cap per task spec

# 设计红线 5: consensus usability gate.
MIN_CONSENSUS_ORGS = 3
MAX_CONSENSUS_STALENESS_DAYS = 90

# Default lookback window for the announcement stream.
DEFAULT_ANNOUNCEMENT_DAYS = 90
MAX_ANNOUNCEMENTS = 30


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class Announcement:
    """One disclosure title from the exchange announcement stream."""
    title: str
    date: str                 # "2026-07-02"
    category: str = ""        # eastmoney column tags, "/" joined; "" for cninfo
    source: str = "eastmoney"  # "eastmoney" | "cninfo"


@dataclass
class EarningsForecast:
    """One 业绩预告 row (latest report period, per predicted indicator)."""
    report_period: str            # "2025-12-31" — forecasted period end
    forecast_type: str            # 预增 / 扭亏 / 预减 / 首亏 ...
    indicator: str                # e.g. "扣除非经常性损益后的净利润"
    value_low: float | None       # CNY absolute
    value_high: float | None      # CNY absolute
    change_pct_low: float | None  # YoY % lower bound
    change_pct_high: float | None
    notice_date: str              # "2026-01-21" — disclosure date
    prev_year_value: float | None = None


@dataclass
class ConsensusYear:
    """One forecast year of the eastmoney aggregated consensus."""
    year: int
    eps: float | None
    net_profit: float | None      # 归母净利润, CNY
    revenue: float | None         # 营业总收入, CNY


@dataclass
class ConsensusForecast:
    """Aggregated analyst consensus **with mandatory coverage metadata**.

    设计红线 5: consumers must check ``insufficient_coverage`` before using
    ``predictions`` as evidence. ``to_prompt_block`` enforces this for the
    LLM-facing surface; the raw numbers stay available for debugging only.
    """
    org_count: int                      # covering orgs, past 6 months (0 = unknown/none)
    latest_report_date: str | None      # "2026-05-25" or None (no report found)
    insufficient_coverage: bool = True
    predictions: list[ConsensusYear] = field(default_factory=list)


@dataclass
class RecentEvents:
    """Unified recent-events slice for one A-share stock."""
    stock_code: str
    as_of: str                          # ingestion date "2026-07-10"
    announcements: list[Announcement] = field(default_factory=list)
    forecasts: list[EarningsForecast] = field(default_factory=list)
    consensus: ConsensusForecast | None = None

    def to_prompt_block(self) -> str:
        """Render the slice as a Chinese fact block for LLM prompts.

        The first line is a fixed disclaimer instructing agents to treat
        this block as the *only* sanctioned catalyst source.
        """
        lines: list[str] = [
            f"以下为公开披露事实（截至 {self.as_of}），分析必须以此为准，"
            "禁止引用未在此列出的催化剂或传闻。",
            "",
            "■ 业绩预告（东方财富）",
        ]
        if self.forecasts:
            for f in self.forecasts:
                rng = _format_range(f.value_low, f.value_high)
                pct = _format_pct_range(f.change_pct_low, f.change_pct_high)
                line = (f"- 报告期 {f.report_period} | 类型: {f.forecast_type or '未知'}"
                        f" | 指标: {f.indicator or '净利润'} | 预告区间: {rng}")
                if pct:
                    line += f" | 同比: {pct}"
                line += f" | 公告日期: {f.notice_date}"
                lines.append(line)
        else:
            lines.append("- 暂无业绩预告（该股未披露或数据源不可用）")

        lines.append("")
        lines.append("■ 一致预期（东方财富，旁证口径）")
        c = self.consensus
        if c is None:
            lines.append("- 一致预期数据不可用（数据源获取失败）")
        elif c.insufficient_coverage:
            latest = c.latest_report_date or "无"
            lines.append(
                f"- 无有效一致预期：近6个月覆盖机构 {c.org_count} 家，"
                f"最近研报日期 {latest}，未达使用门槛"
                f"（≥{MIN_CONSENSUS_ORGS} 家且 ≤{MAX_CONSENSUS_STALENESS_DAYS} 天）。"
                "禁止在分析中引用任何一致预期数字。"
            )
        else:
            lines.append(
                f"- 覆盖机构 {c.org_count} 家（近6个月），"
                f"最近研报日期 {c.latest_report_date}："
            )
            for p in c.predictions:
                eps = f"{p.eps:.2f} 元" if p.eps is not None else "—"
                np_ = _format_cny(p.net_profit) if p.net_profit is not None else "—"
                rev = _format_cny(p.revenue) if p.revenue is not None else "—"
                lines.append(
                    f"  · {p.year}E: EPS {eps} | 归母净利润 {np_} | 营业总收入 {rev}"
                )

        lines.append("")
        n = len(self.announcements)
        lines.append(f"■ 近{DEFAULT_ANNOUNCEMENT_DAYS}天公告标题（共 {n} 条，按日期倒序）")
        if self.announcements:
            for a in self.announcements:
                tag = f" [{a.category}]" if a.category else ""
                lines.append(f"- {a.date} {a.title}{tag}")
        else:
            lines.append("- 暂无公告（该期间无披露或数据源不可用）")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _format_cny(value: float | None) -> str:
    """CNY absolute → human Chinese units (亿元 / 万元)."""
    if value is None:
        return "未披露"
    a = abs(value)
    if a >= 1e8:
        return f"{value / 1e8:.2f}亿元"
    if a >= 1e4:
        return f"{value / 1e4:.0f}万元"
    if a >= 100:
        return f"{value:.0f}元"
    # Per-share magnitudes (e.g. 每股收益 预告 0.06 元) need decimals,
    # otherwise they collapse to a misleading "0元" (seen live for 002669).
    return f"{value:.2f}元"


def _format_range(low: float | None, high: float | None) -> str:
    if low is None and high is None:
        return "未披露"
    if low is not None and high is not None:
        if low == high:
            return _format_cny(low)
        return f"{_format_cny(low)} ~ {_format_cny(high)}"
    return _format_cny(low if low is not None else high)


def _format_pct_range(low: float | None, high: float | None) -> str:
    if low is None and high is None:
        return ""
    if low is not None and high is not None:
        if low == high:
            return f"{low:+.2f}%"
        return f"{low:+.2f}% ~ {high:+.2f}%"
    v = low if low is not None else high
    return f"{v:+.2f}%"


def _clean_code(stock_code: str) -> str:
    """``002669.SZ`` / ``SZ002669`` / ``002669`` → ``002669``."""
    c = stock_code.strip().upper()
    c = c.replace(".SZ", "").replace(".SS", "").replace(".SH", "")
    for p in ("SZ", "SH"):
        if c.startswith(p):
            c = c[2:]
    return c


def _secucode(clean: str) -> str:
    return f"{clean}.{'SH' if clean.startswith('6') else 'SZ'}"


def _date_only(raw: object) -> str:
    """``"2026-01-21 00:00:00"`` → ``"2026-01-21"``. Never raises."""
    s = str(raw or "").strip()
    return s[:10]


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _datacenter_rows(params: dict[str, str]) -> list[dict]:
    """One GET against the eastmoney datacenter; [] on any failure."""
    import requests
    with _no_proxy():
        resp = requests.get(_DATACENTER_URL, params=params,
                            headers=_UA, timeout=_TIMEOUT)
    return ((resp.json() or {}).get("result") or {}).get("data") or []


# ---------------------------------------------------------------------------
# Source 1: 业绩预告
# ---------------------------------------------------------------------------

def fetch_earnings_forecasts(stock_code: str) -> list[EarningsForecast]:
    """Latest-period 业绩预告 rows for one stock; [] on failure.

    Keeps only rows of the most recent REPORT_DATE (one 预告 may carry
    multiple indicator rows, e.g. 归母净利润 + 扣非净利润), preferring
    rows flagged ``IS_LATEST="T"`` when the flag is present.
    """
    clean = _clean_code(stock_code)
    try:
        rows = _datacenter_rows({
            "reportName": "RPT_PUBLIC_OP_NEWPREDICT",
            "columns": "ALL",
            "filter": f'(SECURITY_CODE="{clean}")',
            "sortColumns": "NOTICE_DATE",
            "sortTypes": "-1",
            "pageSize": "20",
            "pageNumber": "1",
        })
    except Exception as e:
        logger.warning(f"em_events: 业绩预告 fetch failed for {clean}: "
                       f"{type(e).__name__}: {e}")
        return []

    if not rows:
        return []
    # Prefer rows the API marks as latest revision.
    latest_rows = [r for r in rows if str(r.get("IS_LATEST", "T")) == "T"] or rows
    # Restrict to the newest report period.
    periods = [_date_only(r.get("REPORT_DATE")) for r in latest_rows]
    newest = max((p for p in periods if p), default="")
    out: list[EarningsForecast] = []
    for r in latest_rows:
        if _date_only(r.get("REPORT_DATE")) != newest:
            continue
        out.append(EarningsForecast(
            report_period=newest,
            forecast_type=str(r.get("PREDICT_TYPE") or "").strip(),
            indicator=str(r.get("PREDICT_FINANCE") or "").strip(),
            value_low=_safe_float(r.get("PREDICT_AMT_LOWER")),
            value_high=_safe_float(r.get("PREDICT_AMT_UPPER")),
            change_pct_low=_safe_float(r.get("ADD_AMP_LOWER")),
            change_pct_high=_safe_float(r.get("ADD_AMP_UPPER")),
            notice_date=_date_only(r.get("NOTICE_DATE")),
            prev_year_value=_safe_float(r.get("PREYEAR_SAME_PERIOD")),
        ))
    return out


# ---------------------------------------------------------------------------
# Source 2: 公告标题流
# ---------------------------------------------------------------------------

def _fetch_announcements_em(clean: str, since: date) -> list[Announcement]:
    """Primary: eastmoney per-stock announcement API."""
    import requests
    with _no_proxy():
        resp = requests.get(_ANNOUNCE_URL, params={
            "sr": "-1",
            "page_size": str(MAX_ANNOUNCEMENTS),
            "page_index": "1",
            "ann_type": "A",
            "stock_list": clean,
            "f_node": "0",
            "s_node": "0",
        }, headers=_UA, timeout=_TIMEOUT)
    rows = (((resp.json() or {}).get("data")) or {}).get("list") or []
    out: list[Announcement] = []
    for r in rows:
        d = _date_only(r.get("notice_date"))
        dt = _parse_date(d)
        if dt is None or dt < since:
            continue
        cats = "/".join(
            str(c.get("column_name") or "").strip()
            for c in (r.get("columns") or []) if c.get("column_name")
        )
        title = str(r.get("title") or "").strip()
        if not title:
            continue
        out.append(Announcement(title=title, date=d, category=cats,
                                source="eastmoney"))
    return out


def _fetch_announcements_cninfo(clean: str, since: date) -> list[Announcement]:
    """Fallback: cninfo hisAnnouncement POST (anti-crawler prone)."""
    import requests
    column = "sse" if clean.startswith("6") else "szse"
    payload = {
        "stock": f"{clean},",
        "pageNum": 1,
        "pageSize": MAX_ANNOUNCEMENTS,
        "column": column,
        "tabName": "fulltext",
        "sortName": "time",
        "sortType": "desc",
        "seDate": f"{since.isoformat()}~{date.today().isoformat()}",
    }
    with _no_proxy():
        resp = requests.post(_CNINFO_URL, data=payload, headers=_UA,
                             timeout=_TIMEOUT)
    rows = (resp.json() or {}).get("announcements") or []
    out: list[Announcement] = []
    for r in rows:
        title = re.sub(r"</?em>", "", str(r.get("announcementTitle") or "")).strip()
        ts = r.get("announcementTime")
        try:
            d = datetime.fromtimestamp(float(ts) / 1000.0).date()
        except (TypeError, ValueError, OSError):
            continue
        if not title or d < since:
            continue
        out.append(Announcement(title=title, date=d.isoformat(),
                                category="", source="cninfo"))
    return out


def fetch_announcements(
    stock_code: str,
    days: int = DEFAULT_ANNOUNCEMENT_DAYS,
    as_of: date | None = None,
) -> list[Announcement]:
    """Recent announcement titles, date-desc, capped at MAX_ANNOUNCEMENTS.

    eastmoney first; cninfo fallback; [] when both fail.
    """
    clean = _clean_code(stock_code)
    since = (as_of or date.today()) - timedelta(days=days)
    for name, fetcher in (("eastmoney", _fetch_announcements_em),
                          ("cninfo", _fetch_announcements_cninfo)):
        try:
            rows = fetcher(clean, since)
        except Exception as e:
            logger.warning(f"em_events: {name} announcements failed for "
                           f"{clean}: {type(e).__name__}: {e}")
            continue
        if rows:
            rows.sort(key=lambda a: a.date, reverse=True)
            return rows[:MAX_ANNOUNCEMENTS]
    return []


# ---------------------------------------------------------------------------
# Source 3: 一致预期
# ---------------------------------------------------------------------------

def _fetch_consensus_predictions(clean: str) -> list[ConsensusYear]:
    rows = _datacenter_rows({
        "reportName": "RPT_RES_PROFITPREDICT",
        "columns": "ALL",
        "filter": f'(SECUCODE="{_secucode(clean)}")',
        "pageSize": "20",
        "pageNumber": "1",
    })
    out: list[ConsensusYear] = []
    for r in rows:
        try:
            year = int(r.get("PREDICT_YEAR"))
        except (TypeError, ValueError):
            continue
        out.append(ConsensusYear(
            year=year,
            eps=_safe_float(r.get("EPS")),
            net_profit=_safe_float(r.get("PARENT_NETPROFIT")),
            revenue=_safe_float(r.get("TOTAL_OPERATE_INCOME")),
        ))
    out.sort(key=lambda p: p.year)
    return out


def _fetch_consensus_org_count(clean: str) -> int:
    """RATING_ORG_NUM (covering orgs, past 6 months) from RPT_WEB_RESPREDICT."""
    import requests
    with _no_proxy():
        resp = requests.get(
            "https://datacenter-web.eastmoney.com/api/data/v1/get",
            params={
                "reportName": "RPT_WEB_RESPREDICT",
                "columns": "WEB_RESPREDICT",
                "filter": f'(SECURITY_CODE="{clean}")',
                "pageSize": "1",
                "pageNumber": "1",
            }, headers=_UA, timeout=_TIMEOUT)
    rows = ((resp.json() or {}).get("result") or {}).get("data") or []
    if not rows:
        return 0
    n = _safe_float(rows[0].get("RATING_ORG_NUM"))
    return int(n) if n and n > 0 else 0


def _fetch_latest_report_date(clean: str, as_of: date) -> str | None:
    """Latest research-report publish date from reportapi (12-month window)."""
    import requests
    with _no_proxy():
        resp = requests.get(_REPORTAPI_URL, params={
            "pageSize": "1",
            "pageNo": "1",
            "qType": "0",
            "code": clean,
            "beginTime": (as_of - timedelta(days=365)).isoformat(),
            "endTime": as_of.isoformat(),
        }, headers=_UA, timeout=_TIMEOUT)
    rows = (resp.json() or {}).get("data") or []
    if not rows:
        return None
    d = _date_only(rows[0].get("publishDate"))
    return d or None


def fetch_consensus(stock_code: str,
                    as_of: date | None = None) -> ConsensusForecast | None:
    """Analyst consensus with the 设计红线 5 coverage gate applied.

    Returns ``None`` only when *every* sub-call fails (data unavailable);
    otherwise a :class:`ConsensusForecast` whose ``insufficient_coverage``
    reflects the ``≥3 orgs and ≤90 days`` usability threshold.
    """
    clean = _clean_code(stock_code)
    ref = as_of or date.today()

    predictions: list[ConsensusYear] = []
    org_count = 0
    latest: str | None = None
    failures = 0

    try:
        predictions = _fetch_consensus_predictions(clean)
    except Exception as e:
        failures += 1
        logger.warning(f"em_events: consensus predictions failed for {clean}: "
                       f"{type(e).__name__}: {e}")
    try:
        org_count = _fetch_consensus_org_count(clean)
    except Exception as e:
        failures += 1
        logger.warning(f"em_events: consensus org count failed for {clean}: "
                       f"{type(e).__name__}: {e}")
    try:
        latest = _fetch_latest_report_date(clean, ref)
    except Exception as e:
        failures += 1
        logger.warning(f"em_events: latest report date failed for {clean}: "
                       f"{type(e).__name__}: {e}")

    if failures == 3:
        return None

    latest_dt = _parse_date(latest)
    fresh = (latest_dt is not None
             and (ref - latest_dt).days <= MAX_CONSENSUS_STALENESS_DAYS)
    usable = bool(predictions) and org_count >= MIN_CONSENSUS_ORGS and fresh
    return ConsensusForecast(
        org_count=org_count,
        latest_report_date=latest,
        insufficient_coverage=not usable,
        predictions=predictions,
    )


# ---------------------------------------------------------------------------
# Facade
# ---------------------------------------------------------------------------

def fetch_recent_events(
    stock_code: str,
    days: int = DEFAULT_ANNOUNCEMENT_DAYS,
    as_of: date | None = None,
) -> RecentEvents:
    """Fetch all three event slices. Never raises — dead sources yield
    empty sections and the block still renders (with 暂无/不可用 markers)."""
    ref = as_of or date.today()
    return RecentEvents(
        stock_code=_clean_code(stock_code),
        as_of=ref.isoformat(),
        announcements=fetch_announcements(stock_code, days=days, as_of=ref),
        forecasts=fetch_earnings_forecasts(stock_code),
        consensus=fetch_consensus(stock_code, as_of=ref),
    )
