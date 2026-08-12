"""L1 Wave 3（2026-08-01）：A 股大股东股权质押摄取（东财股权质押专题）。

与 restricted_release.py 同批接入的第二类 A 股特有数据缺口：**股权质押
（治理/平仓风险）**——agents 此前只能把"大股东质押比例"写进
open_questions。数据源两路互补，2026-08-01 均经真实网络调用实测：

1. 东财 datacenter ``RPT_CSDC_LIST`` 按 ``SECURITY_CODE`` 过滤（中登周五
   快照主路径）：一次小页拿该股历史快照行，用全市场最新 ``TRADE_DATE``
   判定当前周是否收录。``PLEDGE_RATIO`` 为**百分数**（7.09 = 7.09%），
   ``REPURCHASE_BALANCE`` 单位**万股**。个股不在当前快照 = 全股质押
   比例视为 ≈0（负面证据；按码过滤会带回陈年行，不能把历史最新行
   当成现口径）。失败回退 akshare ``stock_gpzy_pledge_ratio_em(date)``
   全表周五试探（最多 6 周，快照滞后防御）。
2. akshare ``stock_gpzy_individual_pledge_ratio_detail_em(symbol)``
   （东财-重要股东股权质押明细，RPTA_APP_ACCUMDETAILS 按代码过滤）：
   逐笔质押记录，``占所持股份比例`` / ``占总股本比例`` 为**百分数**，
   ``状态`` ∈ {未解押, 已解押}。未解押记录按股东聚合；跨年多笔占比
   相加是近似口径（持股数可能已变动），中文行以"约"标注。

产出三份消费物（两路全部失败返回 None，单路失败干净降级，永不 raise）：
- 质押事实：``pledge_ratio_total`` / ``pledge_count`` /
  ``major_holders``（未解押按股东聚合）/ ``high_pledge_flag`` /
  ``holder_strain_flag``；
- ``lines_zh``：prompt 注入用中文行（agents + synthesizer）；
- ``sanctioned_pcts``：质押比例 % 进清洗白名单（设计红线 9 同则）。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from aegis.core.acquisition.connectors.akshare_connector import _no_proxy

# 高质押关注阈值（百分数）。依据：沪深交易所 2018-03《股票质押式回购
# 交易及登记结算业务办法（2018 年修订）》将单只 A 股整体质押比例上限
# 定为 50%；卖方/风控惯例把 30% 以上视为高质押关注区（监管红线的
# 六折预警位）。阈值本身不是披露数字，不进白名单、不在 lines_zh 里
# 以字面量出现（同 customer_concentration 对 "50%" 的处理）。
_HIGH_PLEDGE_THRESHOLD_PCT = 30.0

# 大股东"满仓质押"张力阈值（百分数）：所持股份 80% 以上已质押 →
# 补仓空间枯竭，平仓/控制权移转风险的市场惯例判定线。
_HOLDER_STRAIN_THRESHOLD_PCT = 80.0

# 中登快照按周五发布；akshare 全表回退最多试探的周数
_CSDC_LOOKBACK_WEEKS = 6

# 东财 datacenter 直连（与 margin_trading._fetch_ggmx 同主机同三态）
_EM_DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_EM_EMPTY_CODE = 9201  # success=false「返回数据为空」
_CSDC_DC_PAGE_SIZE = 8  # 按码过滤倒序，够对齐当前快照日

# 东财明细 vs 中登口径的对账容差（百分点）。中登是质押登记的权威口径；
# 东财明细的"未解押"状态更新滞后是已知数据缺陷——2026-08-01 实测
# 300502：中登质押登记 0 笔，东财明细仍挂 2018/2021 年"未解押" 13 笔。
# 明细聚合占总股本比例超出中登比例 1pp 以上即判定明细陈旧：中文行降级
# 为口径不符提示，股东占比不进白名单（防 agents 引用过期"事实"，
# 铁律"不要轻信数字"同则）。
_DETAIL_RECONCILE_TOLERANCE_PP = 1.0

# lines_zh 里逐股东列出的上限（噪声控制，同 segment_zygc）
_MAX_HOLDER_LINES = 3


def _f(v: Any) -> float | None:
    """宽容 float 转换，NaN → None。"""
    try:
        fv = float(v)
    except (TypeError, ValueError):
        return None
    return fv if fv == fv else None


def _recent_csdc_dates(today: date, weeks: int) -> list[date]:
    """今天起最近的 N 个周五（含当天若恰为周五），新 → 旧。"""
    friday = today - timedelta(days=(today.weekday() - 4) % 7)
    return [friday - timedelta(weeks=i) for i in range(weeks)]


def fetch_equity_pledge(
    stock_code: str, today: date | None = None,
) -> dict[str, Any] | None:
    """两路拉取并装配质押块。两路全败返回 None，永不 raise。"""
    code = str(stock_code).strip()[:6]
    ratio_part = _fetch_csdc_ratio(code, today or date.today())
    holders_part = _fetch_holder_detail(code)
    if ratio_part is None and holders_part is None:
        return None
    try:
        return _assemble(ratio_part, holders_part, code)
    except Exception:
        return None


def _fetch_csdc_ratio(code: str, today: date) -> dict[str, Any] | None:
    """中登周五快照编排：datacenter 按码优先，失败回退 akshare 全表。

    失败返回 None，永不 raise。
    """
    out = _fetch_csdc_ratio_datacenter(code, today)
    if out is not None:
        return out
    return _fetch_csdc_ratio_akshare(code, today)


def _iso_date(v: Any) -> str | None:
    """``2026-08-07 00:00:00`` / ``2026-08-07`` → ``YYYY-MM-DD``。"""
    s = str(v or "").strip()
    return s[:10] if len(s) >= 10 and s[4] == "-" and s[7] == "-" else None


def _csdc_absent(trade_date: str) -> dict[str, Any]:
    """当前快照未收录该股 = 中登无质押登记（负面证据）。"""
    return {
        "trade_date": trade_date,
        "listed": False,
        "pledge_ratio_total": 0.0,
        "pledge_count": 0,
        "pledged_shares": 0.0,
    }


def _csdc_from_em_row(row: dict[str, Any], trade_date: str) -> dict[str, Any]:
    """RPT_CSDC_LIST 原始行 → 与 ``_csdc_row`` 同形状。

    字段名来自 akshare 源码列映射 + 2026-08-13 真网核对：
    ``PLEDGE_RATIO`` 百分数原样；``REPURCHASE_BALANCE`` 万股 → 股。
    """
    ratio = _f(row.get("PLEDGE_RATIO"))
    count = _f(row.get("PLEDGE_DEAL_NUM"))
    shares_wan = _f(row.get("REPURCHASE_BALANCE"))
    return {
        "trade_date": trade_date,
        "listed": True,
        "pledge_ratio_total": round(ratio, 2) if ratio is not None else None,
        "pledge_count": int(count) if count is not None else None,
        "pledged_shares": shares_wan * 1e4 if shares_wan is not None else None,
    }


def _em_csdc_get(
    *, filt: str | None, page_size: int,
) -> list[dict[str, Any]] | None:
    """RPT_CSDC_LIST 三态：有行 / 空列表（明确空） / None（失败）。永不 raise。"""
    try:
        with _no_proxy():
            import requests
            params: dict[str, str] = {
                "reportName": "RPT_CSDC_LIST",
                "columns": "ALL",
                "sortColumns": "TRADE_DATE",
                "sortTypes": "-1",
                "pageNumber": "1",
                "pageSize": str(page_size),
                "source": "WEB",
                "client": "WEB",
            }
            if filt:
                params["filter"] = filt
            resp = requests.get(
                _EM_DATACENTER_URL,
                params=params,
                timeout=30,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            payload = resp.json()
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    result = payload.get("result")
    if payload.get("success") and isinstance(result, dict):
        data = result.get("data")
        return [r for r in data if isinstance(r, dict)] \
            if isinstance(data, list) else None
    if payload.get("code") == _EM_EMPTY_CODE:
        return []
    return None


def _fetch_latest_csdc_trade_date() -> str | None:
    """全市场最新快照日（pageSize=1、不带码过滤、TRADE_DATE 倒序）。"""
    rows = _em_csdc_get(filt=None, page_size=1)
    if not rows:
        return None
    return _iso_date(rows[0].get("TRADE_DATE"))


def _fetch_csdc_ratio_datacenter(
    code: str, today: date,
) -> dict[str, Any] | None:
    """东财 RPT_CSDC_LIST 按码过滤取当前中登快照。失败返回 None。

    三态（照 ``_fetch_ggmx``）：有当前快照行 = 解析；success 且空 /
    仅有陈年行 = 负面证据；网络/结构失败 = None。

    按码过滤返回该股**全部历史周五**（2026-08-13 实测 300502 最新行
    停在 2023-10-27，当前周表内缺席）。必须用全市场最新 TRADE_DATE
    判定「当前快照是否收录」，否则会把陈年质押行当成现口径。
    """
    try:
        rows = _em_csdc_get(
            filt=f'(SECURITY_CODE="{code}")',
            page_size=_CSDC_DC_PAGE_SIZE,
        )
        if rows is None:
            return None

        # 日历最近周五与该股最新行对齐则可省掉第二次请求
        latest_friday = _recent_csdc_dates(today, 1)[0].isoformat()
        if rows:
            row_td = _iso_date(rows[0].get("TRADE_DATE"))
            if row_td == latest_friday:
                return _csdc_from_em_row(rows[0], row_td)

        # 空结果、或最新行不是日历最近周五 → 锚定全市场最新快照日
        market_td = _fetch_latest_csdc_trade_date()
        if market_td is None:
            return None
        if rows:
            for r in rows:
                if _iso_date(r.get("TRADE_DATE")) == market_td:
                    return _csdc_from_em_row(r, market_td)
        return _csdc_absent(market_td)
    except Exception:
        return None


def _fetch_csdc_ratio_akshare(code: str, today: date) -> dict[str, Any] | None:
    """akshare 全表周五试探（datacenter 失败时的回退）。永不 raise。"""
    try:
        with _no_proxy():
            import akshare as ak
            for d in _recent_csdc_dates(today, _CSDC_LOOKBACK_WEEKS):
                try:
                    df = ak.stock_gpzy_pledge_ratio_em(date=d.strftime("%Y%m%d"))
                except Exception:
                    continue
                if df is None or df.empty:
                    continue
                return _csdc_row(df, code, d)
    except Exception:
        return None
    return None


def _csdc_row(df: Any, code: str, d: date) -> dict[str, Any] | None:
    if not {"股票代码", "质押比例"}.issubset(set(df.columns)):
        return None
    sub = df[df["股票代码"].astype(str).str.strip() == code]
    if sub.empty:
        # 表拉取成功但个股缺席 = 中登无质押登记（负面证据同样有价值）
        return {
            "trade_date": d.isoformat(),
            "listed": False,
            "pledge_ratio_total": 0.0,
            "pledge_count": 0,
            "pledged_shares": 0.0,
        }
    r = sub.iloc[0]
    ratio = _f(r.get("质押比例"))
    count = _f(r.get("质押笔数"))
    shares_wan = _f(r.get("质押股数"))
    return {
        "trade_date": d.isoformat(),
        "listed": True,
        "pledge_ratio_total": round(ratio, 2) if ratio is not None else None,
        "pledge_count": int(count) if count is not None else None,
        # 中登口径质押股数单位为万股 → 换算成股
        "pledged_shares": shares_wan * 1e4 if shares_wan is not None else None,
    }


def _fetch_holder_detail(code: str) -> dict[str, Any] | None:
    """东财重要股东质押明细 → 未解押按股东聚合。失败返回 None。"""
    try:
        with _no_proxy():
            import akshare as ak
            df = ak.stock_gpzy_individual_pledge_ratio_detail_em(symbol=code)
    except Exception:
        return None
    if df is None:
        return None
    try:
        return _holder_agg(df)
    except Exception:
        return None


def _holder_agg(df: Any) -> dict[str, Any] | None:
    if df.empty:
        return {"total_records": 0, "active": []}
    need = {"股东名称", "状态", "占总股本比例", "占所持股份比例"}
    if not need.issubset(set(df.columns)):
        return None
    act = df[df["状态"].astype(str).str.strip() == "未解押"]
    holders: dict[str, dict[str, Any]] = {}
    for _, r in act.iterrows():
        name = str(r.get("股东名称") or "").strip()
        if not name:
            continue
        h = holders.setdefault(name, {
            "name": name, "pct_of_total": 0.0, "pct_of_holding": 0.0,
            "records": 0, "latest_notice_date": "",
        })
        h["pct_of_total"] += _f(r.get("占总股本比例")) or 0.0
        h["pct_of_holding"] += _f(r.get("占所持股份比例")) or 0.0
        h["records"] += 1
        nd = str(r.get("公告日期") or "")[:10]
        if nd > h["latest_notice_date"]:
            h["latest_notice_date"] = nd
    active = sorted(holders.values(),
                    key=lambda h: h["pct_of_total"], reverse=True)
    for h in active:
        h["pct_of_total"] = round(h["pct_of_total"], 2)
        h["pct_of_holding"] = round(h["pct_of_holding"], 2)
    return {"total_records": int(len(df)), "active": active}


def _assemble(
    ratio_part: dict[str, Any] | None,
    holders_part: dict[str, Any] | None,
    code: str,
) -> dict[str, Any] | None:
    ratio = ratio_part.get("pledge_ratio_total") if ratio_part else None
    high = ratio > _HIGH_PLEDGE_THRESHOLD_PCT if ratio is not None else None
    active = (holders_part or {}).get("active") or []
    mh_total = (round(sum(h["pct_of_total"] for h in active), 2)
                if holders_part is not None else None)
    # 对账：明细聚合超出中登口径 → 明细陈旧（见常量注释），张力标记
    # 一并降级为未知——建立在陈旧记录上的平仓风险主张比缺失更糟。
    detail_stale = bool(
        ratio is not None and mh_total is not None
        and mh_total > ratio + _DETAIL_RECONCILE_TOLERANCE_PP
    )
    strain = (any(h["pct_of_holding"] > _HOLDER_STRAIN_THRESHOLD_PCT
                  for h in active)
              if holders_part is not None and not detail_stale else None)

    lines: list[str] = []
    pcts: list[float] = []

    if ratio_part:
        td = ratio_part.get("trade_date", "")
        if ratio_part.get("listed"):
            bit = (f"[股权质押] 中登口径全股质押比例 {ratio:.2f}%"
                   f"（截至 {td}，{ratio_part.get('pledge_count') or 0} 笔）")
            if ratio is not None:
                pcts.append(round(ratio, 2))
            if high:
                # 措辞避开阈值字面量——它不是披露数字（红线 9 同则）
                bit += "，整体质押比例处于高位（高质押关注区间）"
            lines.append(bit)
        else:
            lines.append(f"[股权质押] 截至 {td} 中登质押登记未见该股，"
                         "整体质押比例可视为接近零")

    if holders_part is not None:
        if active and detail_stale:
            lines.append("[股权质押] 东财重要股东质押明细与中登口径不符"
                         "（含疑似未更新的历史「未解押」记录），"
                         "以上述中登质押比例为准")
        elif active:
            n = sum(h["records"] for h in active)
            frags = []
            for h in active[:_MAX_HOLDER_LINES]:
                nd_bit = (f"，最近公告 {h['latest_notice_date']}"
                          if h["latest_notice_date"] else "")
                frags.append(
                    f"{h['name']} 占总股本 {h['pct_of_total']:.2f}%"
                    f"（约占其持股 {h['pct_of_holding']:.2f}%{nd_bit}）"
                )
                pcts.extend((h["pct_of_total"], h["pct_of_holding"]))
            omitted = len(active) - min(len(active), _MAX_HOLDER_LINES)
            tail = f"；（另 {omitted} 名股东从略）" if omitted > 0 else ""
            lines.append(f"[股权质押] 东财重要股东未解押质押 {n} 笔："
                         + "；".join(frags) + tail)
            if strain:
                lines.append("[股权质押] 存在将所持股份大比例质押的重要股东"
                             "（补仓空间有限，注意平仓/控制权风险）")
        else:
            lines.append("[股权质押] 东财重要股东质押明细无未解押记录")

    if not lines:
        return None

    return {
        "source": "eastmoney_gpzy",
        "trade_date": (ratio_part or {}).get("trade_date", ""),
        "pledge_ratio_total": ratio,
        "pledge_count": (ratio_part or {}).get("pledge_count"),
        "pledged_shares": (ratio_part or {}).get("pledged_shares"),
        "high_pledge_flag": high,
        "major_holders": active,
        "major_holder_pledged_pct_of_total": mh_total,
        "detail_stale": detail_stale,
        "holder_strain_flag": strain,
        "lines_zh": lines,
        # 设计红线 9：真实数据派生的 % 注册进清洗白名单（去重保序）
        "sanctioned_pcts": list(dict.fromkeys(round(p, 2) for p in pcts)),
        "source_note": (f"东财股权质押专题（{code}：中登周频质押比例"
                        "+重要股东质押明细）"),
    }


def pledge_sanctioned_pcts(blk: Any) -> list[float]:
    """从 __equity_pledge 块提取白名单 %（缺省容错）。

    红线 8 说明：本模块自身不读写共享事实字典——盖章由 orchestrator
    （棘轮白名单内）完成，这里只接收已提取的块作显式参数。
    """
    if isinstance(blk, dict):
        vals = blk.get("sanctioned_pcts")
        if isinstance(vals, list):
            return [float(v) for v in vals if isinstance(v, (int, float))]
    return []
