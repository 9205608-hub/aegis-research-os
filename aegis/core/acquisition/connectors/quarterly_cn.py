"""A 股季报连接器 — 东财 datacenter F10 三大报表 per-stock 拉取 → 直写 PIT store.

DESIGN_2.0 Phase 1「季报 + TTM 引擎」的摄取端。对单 ticker 拉最近 ~6 个报告期的
利润表 + 现金流量表（**年初累计口径**）与资产负债表（**时点口径**），每个事实
直接写入 :class:`aegis.pit.PITStore`（红线：新数据从第一天走 PIT，不进 meta_facts）。

数据源选型（2026-07-10 实测，Clash 代理环境 ``_no_proxy`` 生效）：

- **选用** eastmoney datacenter F10 报表族，``SECUCODE`` 单票过滤、
  ``REPORT_DATE`` 倒序、单请求 ~0.2s，每行自带 ``NOTICE_DATE``（披露日 =
  ``announce_date`` 来源）与 ``OPINION_TYPE``（审计意见，仅年报非空）：

  - ``RPT_F10_FINANCE_GINCOME``   利润表（含 ``DEDUCT_PARENT_NETPROFIT`` 扣非归母，
    实测 002669 FY2025 扣非 1672 万 = 已知基准值，故 **无需** 另拉业绩报表）
  - ``RPT_F10_FINANCE_GCASHFLOW`` 现金流量表
  - ``RPT_F10_FINANCE_GBALANCE``  资产负债表

- **弃用备选**：akshare ``stock_lrb_em/stock_xjll_em/stock_zcfzb_em``（按报告期
  全市场拉取，5215 行 × 6 期，无单票过滤）；``stock_yjbb_em``（同为全市场拉取，
  其扣非与最新公告日期两个字段 GINCOME 均已覆盖）。

工程铁律：所有请求走 ``_no_proxy``、超时 ≤10s、单表失败静默降级为空
（一张表挂掉不影响另外两张表入库，也不打断主流程）。

红线 #3（PIT 双时间戳）：``as_of`` = 摄取时刻（store 默认 now）；
``announce_date`` = 东财 ``NOTICE_DATE`` 披露日；披露日早于摄取日超过
``BACKFILL_THRESHOLD_DAYS`` 的行自动标 ``backfilled=True``（历史回填必须显式标注，
只有 announce_date 回填后的数据才谈得上回测级 PIT）。

红线 #8（concept 词表）：registry 外的概念由 :func:`register_quarterly_concepts`
在摄取端显式声明（词表进程内有效，每次启动重注册——可审计性设计）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from .akshare_connector import _no_proxy, _safe_float

logger = logging.getLogger(__name__)

_DATACENTER_URL = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
_TIMEOUT = 10  # 铁律硬上限

#: 默认拉取的报告期数。6 期 = 当前期 + 上年 FY + 上年同期 皆在窗内（TTM 最小需求）。
DEFAULT_N_PERIODS = 6

#: 披露日早于摄取日超过该天数 → 自动标 backfilled=True（红线 #3）。
BACKFILL_THRESHOLD_DAYS = 30

#: A 股披露节点：报告期末的月-日 → fiscal_period（与 PITStore.FISCAL_PERIODS 对齐）。
_FISCAL_BY_MMDD = {"03-31": "Q1", "06-30": "H1", "09-30": "Q3", "12-31": "FY"}

# ---------------------------------------------------------------------------
# concept 映射（东财列名 → registry / 注册概念）
# ---------------------------------------------------------------------------

#: 利润表（流量科目，年初累计值）。net_income 取归母口径（与 1.x 口径一致，
#: 见 HANDOFF「A 股口径三连：净利润切归母」）；扣非归母另立概念双轨并存。
INCOME_CONCEPTS: dict[str, str] = {
    "revenue": "TOTAL_OPERATE_INCOME",
    "net_income": "PARENT_NETPROFIT",
    "net_income_deducted": "DEDUCT_PARENT_NETPROFIT",
    "operating_income": "OPERATE_PROFIT",
}

#: 现金流量表（流量科目，年初累计值）。capex 在摄取时翻负号（SIGN_FLIP_CAPEX：
#: 东财存正的现金流出额，全管线统一 yfinance/EDGAR 的带符号口径）。
CASHFLOW_CONCEPTS: dict[str, str] = {
    "cfo": "NETCASH_OPERATE",
    "capex_ppe": "CONSTRUCT_LONG_ASSET",
}

#: 资产负债表（时点科目，禁做 TTM——红线 #4）。
#: accounts_receivable / inventory / total_liabilities 供验证点核验器
#: （verification.py 封闭目录：应收/存货增速 vs 营收、资产负债率趋势）
#: 做同比比较——列名 2026-07-10 对 002669 实测确认。
BALANCE_CONCEPTS: dict[str, str] = {
    "cash_and_equivalents": "MONETARYFUNDS",
    "total_assets": "TOTAL_ASSETS",
    "total_equity_attributable": "TOTAL_PARENT_EQUITY",
    "accounts_receivable": "ACCOUNTS_RECE",
    "inventory": "INVENTORY",
    "total_liabilities": "TOTAL_LIABILITIES",
}

#: registry 词表外、需 register_concept 显式声明的概念（红线 #8 逃生口）。
EXTRA_CONCEPTS: tuple[str, ...] = (
    "net_income_deducted",       # 扣非归母净利润（A 股双轨口径）
    "total_assets",              # 资产总计（registry 只有 avg_total_assets）
    "total_equity_attributable",  # 归母所有者权益合计
    "accounts_receivable",       # 应收账款（验证点：应收增速 vs 营收增速）
    "inventory",                 # 存货（验证点：存货增速 vs 营收增速）
    "total_liabilities",         # 负债合计（验证点：资产负债率趋势）
)

#: 摄取时需要翻负号的概念（东财现金流出额为正 → 带符号口径为负）。
_SIGN_FLIP_CONCEPTS = frozenset({"capex_ppe"})

#: (报表名, source 标签, concept 映射) 三元组——单表失败互不影响。
_STATEMENTS: tuple[tuple[str, str, dict[str, str]], ...] = (
    ("RPT_F10_FINANCE_GINCOME", "em_f10_income", INCOME_CONCEPTS),
    ("RPT_F10_FINANCE_GCASHFLOW", "em_f10_cashflow", CASHFLOW_CONCEPTS),
    ("RPT_F10_FINANCE_GBALANCE", "em_f10_balance", BALANCE_CONCEPTS),
)


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------

@dataclass
class ParsedFact:
    """一条已解析、待写入 PIT 的季报事实（纯数据，方便离线测试）。"""

    concept: str
    period: str                  # "2026-03-31"
    fiscal_period: str           # Q1 / H1 / Q3 / FY
    value: float
    announce_date: str | None    # "2026-04-28"，无则 None
    unaudited: bool              # 无审计意见（季报/中报常态）= True
    source: str
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class QuarterlyIngestResult:
    """一次单票季报摄取的结果摘要。"""

    stock_code: str
    facts_written: int = 0
    periods: list[str] = field(default_factory=list)     # 入库的报告期（去重、倒序）
    sources_ok: list[str] = field(default_factory=list)  # 成功入库的报表 source
    errors: list[str] = field(default_factory=list)      # 静默降级的失败记录


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _clean_code(stock_code: str) -> str:
    """``002669.SZ`` / ``SZ002669`` / ``002669`` → ``002669``。"""
    c = stock_code.strip().upper()
    c = c.replace(".SZ", "").replace(".SS", "").replace(".SH", "")
    for p in ("SZ", "SH"):
        if c.startswith(p):
            c = c[2:]
    return c


def _secucode(clean: str) -> str:
    return f"{clean}.{'SH' if clean.startswith('6') else 'SZ'}"


def _date_only(raw: object) -> str:
    """``"2026-04-28 00:00:00"`` → ``"2026-04-28"``。永不 raise。"""
    return str(raw or "").strip()[:10]


def register_quarterly_concepts(store: Any) -> None:
    """在摄取端显式声明本连接器引入的 registry 外概念（红线 #8）。

    词表注册进程内有效、不落库——每次启动摄取前调用一次。
    :func:`ingest_quarterly` 内部已自动调用，单独暴露供只读端复用词表声明。
    """
    for concept in EXTRA_CONCEPTS:
        store.register_concept(concept)


# ---------------------------------------------------------------------------
# 网络层
# ---------------------------------------------------------------------------

def _fetch_statement_rows(
    report_name: str, secucode: str, n_periods: int,
) -> list[dict]:
    """拉一张报表最近 n 期的原始行（REPORT_DATE 倒序）。网络失败向上抛。"""
    import requests

    with _no_proxy():
        resp = requests.get(_DATACENTER_URL, params={
            "reportName": report_name,
            "columns": "ALL",
            "filter": f'(SECUCODE="{secucode}")',
            "sortColumns": "REPORT_DATE",
            "sortTypes": "-1",
            "pageSize": str(n_periods),
            "pageNumber": "1",
        }, headers=_UA, timeout=_TIMEOUT)
    return ((resp.json() or {}).get("result") or {}).get("data") or []


# ---------------------------------------------------------------------------
# 解析层（纯函数，离线可测）
# ---------------------------------------------------------------------------

def parse_statement_facts(
    rows: list[dict],
    concept_map: dict[str, str],
    source: str,
) -> list[ParsedFact]:
    """把一张报表的原始行解析为待入库事实列表。

    - 报告期末月-日不在 A 股披露节点（Q1/H1/Q3/FY）的行整行跳过；
    - 值缺失 / 非有限数的字段跳过（不写 NULL 占位）；
    - ``unaudited`` 判定：``OPINION_TYPE`` 为空 = 未经审计（A 股仅年报出具
      审计意见，季报/中报/快报天然 True）；
    - ``_SIGN_FLIP_CONCEPTS`` 里的概念翻负号（东财流出额为正 → 带符号口径）。
    """
    out: list[ParsedFact] = []
    for row in rows:
        period = _date_only(row.get("REPORT_DATE"))
        if len(period) != 10:
            continue
        fiscal = _FISCAL_BY_MMDD.get(period[5:])
        if fiscal is None:
            continue
        announce = _date_only(row.get("NOTICE_DATE")) or None
        opinion = str(row.get("OPINION_TYPE") or "").strip()
        unaudited = not opinion
        report_type = str(row.get("REPORT_TYPE") or "").strip()
        for concept, column in concept_map.items():
            value = _safe_float(row.get(column))
            if value is None:
                continue
            if concept in _SIGN_FLIP_CONCEPTS and value > 0:
                value = -value
            meta: dict[str, Any] = {"em_column": column}
            if report_type:
                meta["report_type"] = report_type
            if opinion:
                meta["opinion_type"] = opinion
            out.append(ParsedFact(
                concept=concept,
                period=period,
                fiscal_period=fiscal,
                value=value,
                announce_date=announce,
                unaudited=unaudited,
                source=source,
                meta=meta,
            ))
    return out


def _is_backfill(
    announce_date: str | None,
    ingest_date: date,
    threshold_days: int,
) -> bool:
    """披露日早于摄取日超过阈值 → 历史回填（红线 #3 必须显式标注）。

    披露日缺失时保守地不标（无从判定；as_of 仍诚实记录摄取时刻）。
    """
    if not announce_date:
        return False
    try:
        announced = date.fromisoformat(announce_date)
    except ValueError:
        return False
    return (ingest_date - announced).days > threshold_days


# ---------------------------------------------------------------------------
# 摄取入口
# ---------------------------------------------------------------------------

def ingest_quarterly(
    store: Any,
    stock_code: str,
    *,
    n_periods: int = DEFAULT_N_PERIODS,
    as_of: str | datetime | None = None,
    backfill_threshold_days: int = BACKFILL_THRESHOLD_DAYS,
) -> QuarterlyIngestResult:
    """对单 ticker 拉最近 ``n_periods`` 期三大报表并直写 PIT store。

    永不 raise：单张报表失败记入 ``result.errors`` 并继续下一张
    （静默降级铁律）。幂等：同值重录由 store 去重，重述值自动成新版本链。

    Parameters
    ----------
    store: PITStore（duck-typed，便于测试注入）。
    as_of: 摄取时刻覆盖（默认 now）；backfilled 判定与之同基准。
    """
    clean = _clean_code(stock_code)
    secucode = _secucode(clean)
    result = QuarterlyIngestResult(stock_code=clean)

    register_quarterly_concepts(store)

    if as_of is None:
        ingest_date = datetime.now(timezone.utc).date()
    elif isinstance(as_of, datetime):
        ingest_date = as_of.date()
    else:
        ingest_date = date.fromisoformat(str(as_of)[:10])

    periods: set[str] = set()
    for report_name, source, concept_map in _STATEMENTS:
        try:
            rows = _fetch_statement_rows(report_name, secucode, n_periods)
        except Exception as e:  # noqa: BLE001 — 静默降级铁律
            msg = f"{source}: {type(e).__name__}: {e}"
            logger.warning(f"quarterly_cn: fetch failed for {clean} — {msg}")
            result.errors.append(msg)
            continue
        facts = parse_statement_facts(rows, concept_map, source)
        if not facts:
            result.errors.append(f"{source}: 0 usable rows")
            continue
        written = 0
        for f in facts:
            try:
                store.record_fact(
                    entity_id=clean,
                    concept=f.concept,
                    period=f.period,
                    fiscal_period=f.fiscal_period,
                    value=f.value,
                    announce_date=f.announce_date,
                    source=f.source,
                    unaudited=f.unaudited,
                    backfilled=_is_backfill(
                        f.announce_date, ingest_date, backfill_threshold_days),
                    as_of=as_of,
                    meta=f.meta,
                )
                written += 1
                periods.add(f.period)
            except Exception as e:  # noqa: BLE001 — 单条坏数据不打断整表
                result.errors.append(
                    f"{source}/{f.concept}@{f.period}: {type(e).__name__}: {e}")
        if written:
            result.facts_written += written
            result.sources_ok.append(source)

    result.periods = sorted(periods, reverse=True)
    logger.info(
        f"quarterly_cn: {clean} ingested {result.facts_written} facts "
        f"across {len(result.periods)} periods "
        f"(ok={result.sources_ok}, errors={len(result.errors)})"
    )
    return result
