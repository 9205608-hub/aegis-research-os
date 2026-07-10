"""A 股相对估值锚 — 同业 PE(TTM)/PB 中位数与分位（Phase 1, DESIGN_2.0 §三.A / §五 Phase 1）.

背景（DESIGN_2.0 评审意见）：体制感知降权 DCF 之后，题材/转型股在估值上
"系统无话可说"。本模块补上最小可用的**相对估值锚**：目标股的 PE(TTM)/PB
在同业中处于什么位置。它回答的不是"值多少钱"，而是"**相对同业，市场给
这只股票的倍数是贵还是便宜**"——与预期前沿（隐含增速）、DCF 差值并列，
构成第三个估值视角。

数据源（2026-07-10 实测，Clash 代理环境，datacenter.eastmoney.com 可达）：

1. **个股估值日频** —— eastmoney datacenter ``RPT_VALUEANALYSIS_DET``，
   filter ``(SECURITY_CODE="002669")`` + TRADE_DATE 倒序取最新一行。
   实测字段：``PE_TTM`` / ``PB_MRQ`` / ``TOTAL_MARKET_CAP`` /
   ``TRADE_DATE`` / ``BOARD_CODE`` / ``BOARD_NAME``（东财行业板块，如
   "化学制品" / "白酒Ⅱ"）。单次 ~0.2s。
2. **同业发现** —— 同一张表按 ``(BOARD_CODE="016041")(TRADE_DATE='2026-07-10')``
   过滤即得全部行业成分**连同各自估值**（化学制品 181 行 / 白酒Ⅱ 19 行，
   ~0.3s）——peer 发现与 peer 估值一次请求完成，不需要独立的板块成分接口。
   **工程要点（实测坑）**：不带 TRADE_DATE 的板块全历史排序查询服务端
   直接超时（>10s），日期必须钉在目标股自己最新行的 TRADE_DATE 上；
   非交易日查询返回 success=False（解析为空行，安全）。
3. **兜底** —— 东财路径失败时退回内置 A 股同业映射表（搬自
   ``openbb_connector._PEER_MAP`` 的 A 股条目，去掉 yfinance 后缀），
   每个 peer 单独拉最新估值行。再失败则 ``insufficient_peers=True``。

工程契约（Phase 1 任务 C）：
- 所有 HTTP 走 :func:`_no_proxy`（CN 域名绕过 Clash 代理），超时 ≤10s；
- 公共入口 :func:`compute_relative_valuation` **永不 raise**，全链失败
  返回 ``insufficient_peers=True`` 的空结果，调用方显示「同业样本不足」；
- 板块查询单页 500 行按市值降序——若板块成分 >500 且目标股市值排在
  500 名开外，取到的是市值最大的 500 家（已知局限，东财二级板块目前
  均 <500 家，实测化学制品 181 家）。

设计红线对照：
- **红线 5（薄覆盖必须 gate）**：PE / PB 各自要求有效同业样本
  ≥ ``MIN_PEER_SAMPLE`` (4) 家，不足该指标输出 None 并在 zh_lines 里
  明示"样本不足、禁止引用"；两个指标全部无效时 ``insufficient_peers=True``。
- **红线 9（新数字必须可注册白名单）**：:meth:`RelativeValuation.sanctioned_numbers`
  返回本结果所有面世数字的展示口径幅值，调用方把它们注入
  scrubber/critic 的 sanctioned 白名单后才允许进 LLM 叙述。
- PE 为负（TTM 亏损）的 peer 从 PE 分位样本剔除但计数披露
  （「N 家中 M 家亏损」）；PB ≤0（净资产为负）同样剔除。
- 不引新依赖（红线 10）：requests + statistics + dataclasses。
"""

from __future__ import annotations

import logging
import math
import statistics
from dataclasses import asdict, dataclass, field
from typing import Any

from aegis.core.acquisition.connectors.akshare_connector import (
    _no_proxy,
    _safe_float,
)
from aegis.core.acquisition.connectors.em_events_connector import (
    _clean_code,
    _date_only,
)

logger = logging.getLogger(__name__)

_DATACENTER_URL = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
_TIMEOUT = 10  # hard cap per task spec

_VALUATION_REPORT = "RPT_VALUEANALYSIS_DET"
_VALUATION_COLUMNS = (
    "SECURITY_CODE,SECURITY_NAME_ABBR,TOTAL_MARKET_CAP,"
    "PE_TTM,PB_MRQ,TRADE_DATE,BOARD_CODE,BOARD_NAME"
)

# 红线 5：任一指标的有效同业样本低于该值 → 该指标 gate 掉。
MIN_PEER_SAMPLE = 4
# 取市值最接近的同业家数上限（规格：5-10 家）。
DEFAULT_MAX_PEERS = 10

# 兜底同业映射 —— 搬自 openbb_connector.get_sector_peers._PEER_MAP 的
# A 股条目（该表被审计判为半死代码，这里是其 A 股部分的正式去处）。
# 统一为裸 6 位代码；估值查询用东财 datacenter，不再走 yfinance。
_STATIC_PEER_MAP: dict[str, list[str]] = {
    "600519": ["000858", "000568", "603369", "002304", "000799"],  # 白酒
    "000858": ["600519", "000568", "603369", "002304", "000799"],
    "601318": ["601628", "601336", "600030", "601688", "601211"],  # 保险/金融
    "000333": ["000651", "600690", "002032", "600060", "002508"],  # 家电
    "600036": ["601166", "000001", "601288", "601398", "601818"],  # 银行
    "000651": ["000333", "600690", "002032", "600060", "002508"],
    "300750": ["002594", "600438", "002812", "300014", "688005"],  # 新能源
    "002594": ["300750", "601238", "600104", "000625", "601127"],  # 汽车
    "601888": ["600138", "000069", "002007", "300144", "600258"],  # 旅游免税
    "600276": ["000538", "600196", "300122", "002422", "300015"],  # 医药
    "688981": ["002371", "600584", "603501", "688012", "300223"],  # 半导体
}

_SOURCE_ZH = {
    "industry_board": "东财行业板块",
    "static_map": "内置同业映射表（东财板块口径不可用）",
    "none": "无",
}


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class PeerQuote:
    """一个同业公司的最新估值快照（东财日频估值表一行）。"""
    code: str
    name: str = ""
    market_cap: float | None = None   # 总市值, CNY
    pe_ttm: float | None = None       # 负值 = TTM 亏损
    pb: float | None = None           # PB_MRQ；负值 = 净资产为负
    trade_date: str = ""              # "2026-07-10"


@dataclass
class RelativeValuation:
    """相对估值锚结果。**永不 raise 的降级合同**：

    - ``insufficient_peers=True`` 表示锚不可用（PE 与 PB 两个指标的有效
      样本都不足 :data:`MIN_PEER_SAMPLE` 家），调用方必须显示
      「同业样本不足」而不是引用残缺数字（红线 5）。
    - 单指标 gate：``peer_pe_median`` / ``pe_percentile`` 为 None 即该
      指标样本不足，zh_lines 会逐指标披露。
    - ``loss_making_count``：同业中 TTM 亏损（PE 为负）家数——已从 PE
      分位样本剔除，但必须计数披露。
    """
    stock_code: str
    industry: str = ""                    # 东财板块名（BOARD_NAME），兜底路径为 ""
    data_date: str = ""                   # 估值数据交易日 "2026-07-10"
    peer_source: str = "none"             # industry_board | static_map | none
    target_pe_ttm: float | None = None    # 原始值，负数如实保留（TTM 亏损）
    target_pb: float | None = None
    peer_pe_median: float | None = None   # 有效样本 (PE>0) 中位数；样本不足为 None
    peer_pb_median: float | None = None   # 有效样本 (PB>0) 中位数；样本不足为 None
    pe_percentile: float | None = None    # 目标在有效同业 PE 样本中的百分位 0-100
    pb_percentile: float | None = None
    peer_count: int = 0                   # 选中的同业家数（含亏损/缺数据的）
    pe_sample_size: int = 0               # PE>0 的有效同业家数
    pb_sample_size: int = 0               # PB>0 的有效同业家数
    loss_making_count: int = 0            # 同业中 PE≤0（亏损）家数
    universe_size: int = 0                # 板块成分总家数（兜底路径为 0）
    insufficient_peers: bool = True
    peers: list[PeerQuote] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def sanctioned_numbers(self) -> list[float]:
        """本结果所有面世数字的**展示口径**幅值（红线 9）。

        新数字面世必须同步注册 scrubber/critic 白名单——调用方在把
        zh_lines 注入 prompt/报告前，须把这里的数字并入 sanctioned
        集合（参照 thesis_synthesizer.frontier_sanctioned_growth_pcts
        的接线方式）。返回值与 zh_lines 的四舍五入口径一致。
        """
        out: list[float] = []
        for v in (self.target_pe_ttm, self.peer_pe_median):
            if v is not None:
                out.append(round(abs(float(v)), 1))
        for v in (self.target_pb, self.peer_pb_median):
            if v is not None:
                out.append(round(abs(float(v)), 2))
        for v in (self.pe_percentile, self.pb_percentile):
            if v is not None:
                out.append(float(round(v)))
        return out

    # -- 中文摘要（供 prompt / 报告渲染；A 股中文化铁律） --------------

    def zh_lines(self) -> list[str]:
        date_str = self.data_date or "未知"
        header = f"■ 相对估值锚（同业 PE/PB 分位，东方财富，数据日期 {date_str}）"
        if self.insufficient_peers:
            if self.peer_source == "none":
                reason = "同业数据不可用（数据源获取失败）"
            else:
                reason = (f"同业样本不足（有效同业 PE 样本 {self.pe_sample_size} 家、"
                          f"PB 样本 {self.pb_sample_size} 家，均低于 "
                          f"{MIN_PEER_SAMPLE} 家门槛）")
            return [header,
                    f"- {reason}，相对估值锚不可用，禁止引用任何同业倍数或分位数字。"]

        lines = [header]
        if self.peer_source == "industry_board" and self.industry:
            src = f"东财行业板块「{self.industry}」"
        else:
            src = _SOURCE_ZH.get(self.peer_source, self.peer_source)
        scope = f"- 同业口径: {src}，取市值最接近的 {self.peer_count} 家"
        if self.universe_size:
            scope += f"（板块成分共 {self.universe_size} 家）"
        lines.append(scope)

        lines.append(self._metric_line_zh(
            label="PE(TTM)", target=self.target_pe_ttm,
            median=self.peer_pe_median, percentile=self.pe_percentile,
            sample=self.pe_sample_size, decimals=1,
            negative_note="目标 TTM 亏损（PE 为负），PE 分位不适用",
        ))
        if self.loss_making_count:
            lines.append(
                f"- 同业 {self.peer_count} 家中 {self.loss_making_count} 家亏损"
                "（PE 为负），已从 PE 分位样本剔除"
            )
        lines.append(self._metric_line_zh(
            label="PB", target=self.target_pb,
            median=self.peer_pb_median, percentile=self.pb_percentile,
            sample=self.pb_sample_size, decimals=2,
            negative_note="目标净资产为负，PB 分位不适用",
        ))
        return lines

    @staticmethod
    def _metric_line_zh(label: str, target: float | None, median: float | None,
                        percentile: float | None, sample: int, decimals: int,
                        negative_note: str) -> str:
        """单个倍数指标的中文摘要行（含红线 5 的逐指标 gate 披露）。"""
        if median is None:
            return (f"- {label}: 有效同业样本 {sample} 家（低于 "
                    f"{MIN_PEER_SAMPLE} 家门槛），该指标分位不可用")
        parts = [f"- {label}:"]
        if target is None:
            parts.append("目标值缺失")
        elif target <= 0:
            parts.append(negative_note)
        else:
            parts.append(f"目标 {target:.{decimals}f} 倍")
        parts.append(f"| 同业中位数 {median:.{decimals}f} 倍")
        if percentile is not None:
            parts.append(f"| 处于同业第 {percentile:.0f} 分位")
        parts.append(f"（有效样本 {sample} 家）")
        return " ".join(parts)


# ---------------------------------------------------------------------------
# HTTP layer（每个调用点自己 try/except——单源失败静默降级）
# ---------------------------------------------------------------------------

def _datacenter_result(params: dict[str, str]) -> dict:
    """One GET against the eastmoney datacenter → ``result`` dict ({} on 空)。

    Raises on network failure — callers catch and degrade.
    """
    import requests
    with _no_proxy():
        resp = requests.get(_DATACENTER_URL, params=params,
                            headers=_UA, timeout=_TIMEOUT)
    return ((resp.json() or {}).get("result")) or {}


def _row_to_quote(row: dict) -> PeerQuote:
    return PeerQuote(
        code=str(row.get("SECURITY_CODE") or "").strip(),
        name=str(row.get("SECURITY_NAME_ABBR") or "").strip(),
        market_cap=_safe_float(row.get("TOTAL_MARKET_CAP")),
        pe_ttm=_safe_float(row.get("PE_TTM")),
        pb=_safe_float(row.get("PB_MRQ")),
        trade_date=_date_only(row.get("TRADE_DATE")),
    )


def _fetch_stock_row(clean: str) -> dict | None:
    """最新一行个股估值（TRADE_DATE 倒序 pageSize=1）；无数据返回 None。"""
    result = _datacenter_result({
        "reportName": _VALUATION_REPORT,
        "columns": _VALUATION_COLUMNS,
        "filter": f'(SECURITY_CODE="{clean}")',
        "sortColumns": "TRADE_DATE",
        "sortTypes": "-1",
        "pageSize": "1",
        "pageNumber": "1",
    })
    rows = result.get("data") or []
    return rows[0] if rows else None


def _fetch_board_rows(board_code: str, trade_date: str) -> tuple[list[dict], int]:
    """某行业板块在指定交易日的全部成分估值行 + 成分总数。

    TRADE_DATE 必须钉死（实测：不带日期的板块查询服务端超时）。
    按市值降序单页 500 行——东财二级板块实测均在此之内。
    """
    result = _datacenter_result({
        "reportName": _VALUATION_REPORT,
        "columns": _VALUATION_COLUMNS,
        "filter": f'(BOARD_CODE="{board_code}")(TRADE_DATE=\'{trade_date}\')',
        "sortColumns": "TOTAL_MARKET_CAP",
        "sortTypes": "-1",
        "pageSize": "500",
        "pageNumber": "1",
    })
    rows = result.get("data") or []
    count = result.get("count")
    try:
        universe = int(count) if count else len(rows)
    except (TypeError, ValueError):
        universe = len(rows)
    return rows, universe


# ---------------------------------------------------------------------------
# 纯计算层（无网络，单测直接打）
# ---------------------------------------------------------------------------

def _select_closest_peers(quotes: list[PeerQuote], target_mcap: float | None,
                          max_peers: int) -> list[PeerQuote]:
    """取市值最接近目标的 ≤max_peers 家（log 距离；目标市值未知时取最大的）。"""
    if target_mcap and target_mcap > 0:
        def key(q: PeerQuote):
            if q.market_cap and q.market_cap > 0:
                return (0, abs(math.log(q.market_cap) - math.log(target_mcap)))
            return (1, 0.0)  # 缺市值的排最后，只在不足时补位
    else:
        def key(q: PeerQuote):
            return (0, -(q.market_cap or 0.0))
    return sorted(quotes, key=key)[:max_peers]


def _percentile_rank(sample: list[float], target: float) -> float:
    """target 在 sample 中的百分位（midrank：低于者 + 一半平手），0-100。"""
    below = sum(1 for v in sample if v < target)
    ties = sum(1 for v in sample if v == target)
    return 100.0 * (below + 0.5 * ties) / len(sample)


def _assemble(stock_code: str, target: PeerQuote | None, peers: list[PeerQuote],
              peer_source: str, industry: str, universe_size: int) -> RelativeValuation:
    """从目标 + 已选同业组装最终结果（含红线 5 逐指标 gate）。"""
    pe_sample = [p.pe_ttm for p in peers if p.pe_ttm is not None and p.pe_ttm > 0]
    pb_sample = [p.pb for p in peers if p.pb is not None and p.pb > 0]
    loss_making = sum(1 for p in peers if p.pe_ttm is not None and p.pe_ttm <= 0)

    target_pe = target.pe_ttm if target else None
    target_pb = target.pb if target else None

    pe_median = pe_percentile = None
    if len(pe_sample) >= MIN_PEER_SAMPLE:
        pe_median = statistics.median(pe_sample)
        if target_pe is not None and target_pe > 0:
            pe_percentile = _percentile_rank(pe_sample, target_pe)

    pb_median = pb_percentile = None
    if len(pb_sample) >= MIN_PEER_SAMPLE:
        pb_median = statistics.median(pb_sample)
        if target_pb is not None and target_pb > 0:
            pb_percentile = _percentile_rank(pb_sample, target_pb)

    data_date = target.trade_date if target and target.trade_date else ""
    if not data_date and peers:
        data_date = max((p.trade_date for p in peers if p.trade_date), default="")

    insufficient = pe_median is None and pb_median is None
    return RelativeValuation(
        stock_code=stock_code,
        industry=industry,
        data_date=data_date,
        peer_source=peer_source if peers else "none",
        target_pe_ttm=target_pe,
        target_pb=target_pb,
        peer_pe_median=pe_median,
        peer_pb_median=pb_median,
        pe_percentile=pe_percentile,
        pb_percentile=pb_percentile,
        peer_count=len(peers),
        pe_sample_size=len(pe_sample),
        pb_sample_size=len(pb_sample),
        loss_making_count=loss_making,
        universe_size=universe_size,
        insufficient_peers=insufficient,
        peers=peers,
    )


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def compute_relative_valuation(stock_code: str,
                               max_peers: int = DEFAULT_MAX_PEERS) -> RelativeValuation:
    """计算 A 股相对估值锚。**永不 raise** —— 全链失败返回
    ``insufficient_peers=True`` 的空结果（调用方显示「同业样本不足」）。

    流程：
    1. 目标股最新估值行（PE_TTM/PB_MRQ/市值/板块代码/交易日）；
    2. 同板块 + 同交易日的成分估值 → 市值最接近的 ≤max_peers 家；
    3. 板块路径失败 → 内置映射表逐 peer 拉估值兜底；
    4. 组装中位数/分位/亏损计数（红线 5 gate 见 :func:`_assemble`）。
    """
    try:
        return _compute(stock_code, max_peers)
    except Exception as e:  # 兜底护栏：任何未预期错误也不许穿透
        logger.warning(f"relative_valuation: unexpected failure for "
                       f"{stock_code}: {type(e).__name__}: {e}")
        return RelativeValuation(stock_code=_clean_code(stock_code))


def _compute(stock_code: str, max_peers: int) -> RelativeValuation:
    clean = _clean_code(stock_code)

    # 1) 目标股最新估值行。失败标记 host_suspect —— 若东财整体不可达，
    #    跳过兜底路径的逐 peer 请求（避免 5×10s 的无谓超时串）。
    target: PeerQuote | None = None
    board_code = ""
    industry = ""
    host_suspect = False
    try:
        row = _fetch_stock_row(clean)
    except Exception as e:
        logger.warning(f"relative_valuation: target valuation fetch failed for "
                       f"{clean}: {type(e).__name__}: {e}")
        row, host_suspect = None, True
    if row:
        target = _row_to_quote(row)
        board_code = str(row.get("BOARD_CODE") or "").strip()
        industry = str(row.get("BOARD_NAME") or "").strip()

    # 2) 主路径：同板块成分（peer 发现与估值同一次请求）。
    peers: list[PeerQuote] = []
    peer_source = "none"
    universe_size = 0
    if board_code and target and target.trade_date:
        try:
            rows, universe_size = _fetch_board_rows(board_code, target.trade_date)
        except Exception as e:
            logger.warning(f"relative_valuation: board constituents fetch failed "
                           f"for {clean} (board {board_code}): "
                           f"{type(e).__name__}: {e}")
            rows = []
        quotes = [_row_to_quote(r) for r in rows]
        quotes = [q for q in quotes if q.code and q.code != clean]
        if quotes:
            peers = _select_closest_peers(
                quotes, target.market_cap if target else None, max_peers)
            peer_source = "industry_board"

    # 3) 兜底路径：内置映射表，逐 peer 拉最新估值行。
    if not peers and not host_suspect:
        codes = _STATIC_PEER_MAP.get(clean, [])
        consecutive_failures = 0
        for code in codes:
            if consecutive_failures >= 2:  # 连续失败视为源已死，止损
                break
            try:
                r = _fetch_stock_row(code)
            except Exception as e:
                consecutive_failures += 1
                logger.debug(f"relative_valuation: static peer {code} fetch "
                             f"failed: {type(e).__name__}: {e}")
                continue
            consecutive_failures = 0
            if r:
                q = _row_to_quote(r)
                if q.code:
                    peers.append(q)
        if peers:
            peer_source = "static_map"
            universe_size = 0

    return _assemble(clean, target, peers, peer_source, industry, universe_size)
