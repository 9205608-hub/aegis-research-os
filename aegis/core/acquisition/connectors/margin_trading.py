"""L1 Wave 4（2026-08-01）：A 股融资融券余额摄取（东财个股两融明细）。

与 holder_count.py 同批接入的第五类 A 股特有数据缺口：**两融余额
（杠杆资金情绪）**——agents 此前只能把"融资盘规模/杠杆资金动向"写进
open_questions。

数据源：东财数据中心 ``RPTA_WEB_RZRQ_GGMX``（datacenter-web.eastmoney
.com，与 Wave 3 质押专题同主机，代理 bypass 可达）。当前 akshare
(1.18.55) 无个股两融序列函数（``stock_margin_detail_szse`` 走 szse.cn，
本环境 SSL 握手失败；``stock_margin_em`` 仅有全市场聚合），故按 Wave 2
customer_concentration 先例直接 requests 调 API。2026-08-01 实测
（300502 / 301358 / 688981）确认字段结构：

- ``DATE``：倒序日历（最新在前），逐交易日一行；
- ``RZYE`` 融资余额 / ``RQYE`` 融券余额 / ``RZRQYE`` 两融余额：**元**；
- ``RZYEZB`` 融资余额占流通市值比：**百分数**（4.49 = 4.49%，与
  ``RZYE``/``SZ`` 交叉验证一致——注意与解禁占比"小数"口径相反）；
- ``SZ``：流通市值（元）；
- 非两融标的（或代码不存在）→ ``success: false`` + ``code: 9201``
  （"返回数据为空"）：该票不是两融标的本身就是信息（杠杆资金无法
  场内参与），与网络失败（→ None）区别对待。

产出三份消费物（网络失败返回 None，非标的返回 is_margin_eligible=False
的干净降级块，永不 raise）：
- 两融事实：``margin_balance`` / ``margin_balance_pct_of_float`` /
  ``balance_chg_pct``（近 20 交易日）/ ``is_margin_eligible``；
- ``lines_zh``：prompt 注入用中文行（agents + synthesizer）；
- ``sanctioned_pcts``：占比/变化 % 进清洗白名单（设计红线 9 同则）。
"""

from __future__ import annotations

from typing import Any

from aegis.core.acquisition.connectors.akshare_connector import _no_proxy

_EM_DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"

# 余额变化回看窗口（交易日）：契约口径"近 20 交易日"
_LOOKBACK_TRADING_DAYS = 20

# 拉取行数 = 回看窗口 + 余量（21 行即够算 20 日变化，多拉防缺行）
_PAGE_SIZE = 30

# 东财 datacenter 空结果码（实测 999999 与非标的股均返回此码）
_EM_EMPTY_CODE = 9201

# 余额变化的方向性判定阈值（百分数）。依据：两融余额单日环比常见
# ±1-2%（实测 300502 单日 -9.3% 已属剧烈），20 交易日累计 ±5% 以上
# 才值得定性为"加码/退潮"，以内视为规模平稳。阈值本身不是披露数字，
# 不进白名单、不在 lines_zh 里以字面量出现（同 equity_pledge 阈值处理）。
_CHG_NOTABLE_PCT = 5.0

# 占流通市值比的合理上限（百分数）：防御口径突变（若源头改发小数，
# 4.49% 会变成 0.0449 → 经 ≤1.0 判定改走 SZ 重算路径；若值离谱 >100
# 则丢弃）。单股融资余额占流通市值超 100% 物理不可能。
_PCT_OF_FLOAT_MAX = 100.0


def _f(v: Any) -> float | None:
    """宽容 float 转换，NaN → None。"""
    try:
        fv = float(v)
    except (TypeError, ValueError):
        return None
    return fv if fv == fv else None


def _fmt_yuan(v: float) -> str:
    """金额（元）→ 亿/万元显示（A 股惯例）。"""
    if abs(v) >= 1e8:
        return f"{v / 1e8:.2f} 亿元"
    if abs(v) >= 1e4:
        return f"{v / 1e4:.0f} 万元"
    return f"{v:.0f} 元"


def fetch_margin_trading(stock_code: str) -> dict[str, Any] | None:
    """拉取个股两融余额序列并装配契约块。

    网络失败返回 None（未知）；接口可达但个股无记录返回
    ``is_margin_eligible=False`` 的干净降级块（非标的即信息）；
    永不 raise。
    """
    code = str(stock_code).strip()[:6]
    rows = _fetch_ggmx(code)
    if rows is None:
        return None
    try:
        if not rows:
            return _not_eligible_block(code)
        return _assemble(rows, code)
    except Exception:
        return None


def _fetch_ggmx(code: str) -> list[dict[str, Any]] | None:
    """东财 RPTA_WEB_RZRQ_GGMX 个股两融明细（倒序日历）。

    返回 ``None`` = 网络/结构失败（未知）；``[]`` = 接口明确空结果
    （非两融标的的负面证据）。永不 raise。
    """
    try:
        with _no_proxy():
            import requests
            resp = requests.get(
                _EM_DATACENTER_URL,
                params={
                    "reportName": "RPTA_WEB_RZRQ_GGMX",
                    "columns": "ALL",
                    "filter": f'(scode="{code}")',
                    "sortColumns": "date",
                    "sortTypes": "-1",
                    "pageNumber": "1",
                    "pageSize": str(_PAGE_SIZE),
                    "source": "WEB",
                },
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
    # success=false：仅"空结果"码视为非标的负面证据，其余按失败处理
    if payload.get("code") == _EM_EMPTY_CODE:
        return []
    return None


def _parse_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """原始行 → {date, rzye, rqye, rzyezb, sz}，按日期倒序（防御排序）。"""
    parsed: list[dict[str, Any]] = []
    for r in rows:
        d = str(r.get("DATE") or "")[:10]
        rzye = _f(r.get("RZYE"))
        if len(d) != 10 or rzye is None or rzye < 0:
            continue
        parsed.append({
            "date": d,
            "rzye": rzye,
            "rqye": _f(r.get("RQYE")),
            "rzyezb": _f(r.get("RZYEZB")),
            "sz": _f(r.get("SZ")),
        })
    parsed.sort(key=lambda x: x["date"], reverse=True)
    return parsed


def _pct_of_float(latest: dict[str, Any]) -> float | None:
    """融资余额占流通市值 %（RZYEZB 已是百分数；防御口径突变）。"""
    v = latest.get("rzyezb")
    # ≤1.0 视为源头改发小数或数据异常 → 改用 SZ 重算（见常量注释）
    if v is not None and 1.0 < v <= _PCT_OF_FLOAT_MAX:
        return round(v, 2)
    sz = latest.get("sz")
    if sz and sz > 0:
        calc = latest["rzye"] / sz * 100.0
        if calc <= _PCT_OF_FLOAT_MAX:
            return round(calc, 2)
    if v is not None and 0 <= v <= 1.0:
        # SZ 不可得时退回原值（小数口径异常但仍在物理范围内）
        return round(v, 2)
    return None


def _assemble(rows: list[dict[str, Any]], code: str) -> dict[str, Any] | None:
    parsed = _parse_rows(rows)
    if not parsed:
        return None
    latest = parsed[0]
    pct_float = _pct_of_float(latest)

    chg_pct: float | None = None
    chg_window = 0
    if len(parsed) >= 2:
        idx = min(_LOOKBACK_TRADING_DAYS, len(parsed) - 1)
        base = parsed[idx]
        if base["rzye"] > 0:
            chg_pct = round((latest["rzye"] / base["rzye"] - 1) * 100.0, 2)
            chg_window = idx

    # ── lines_zh + 白名单 % ──
    lines: list[str] = []
    pcts: list[float] = []

    bit = f"[两融] 融资余额 {_fmt_yuan(latest['rzye'])}（截至 {latest['date']}）"
    if pct_float is not None:
        bit += f"，约占流通市值 {pct_float:.2f}%"
        pcts.append(pct_float)
    if latest.get("rqye"):
        bit += f"，融券余额 {_fmt_yuan(latest['rqye'])}"
    lines.append(bit)

    if chg_pct is not None:
        pcts.append(abs(chg_pct))
        if chg_pct <= -_CHG_NOTABLE_PCT:
            lines.append(
                f"[两融] 近 {chg_window} 个交易日融资余额下降 "
                f"{abs(chg_pct):.2f}%（杠杆资金退潮，融资盘对股价的"
                "边际支撑减弱）"
            )
        elif chg_pct >= _CHG_NOTABLE_PCT:
            lines.append(
                f"[两融] 近 {chg_window} 个交易日融资余额上升 "
                f"{chg_pct:.2f}%（杠杆资金加码，情绪升温的同时"
                "放大回撤时的踩踏风险）"
            )
        else:
            lines.append(
                f"[两融] 近 {chg_window} 个交易日融资余额变动 "
                f"{chg_pct:+.2f}%（杠杆资金规模大体平稳）"
            )

    return {
        "source": "eastmoney_rzrq",
        "is_margin_eligible": True,
        "latest_date": latest["date"],
        "margin_balance": latest["rzye"],
        "short_balance": latest.get("rqye"),
        "margin_balance_pct_of_float": pct_float,
        "balance_chg_pct": chg_pct,
        "chg_window_days": chg_window or None,
        "float_market_cap": latest.get("sz"),
        "lines_zh": lines,
        # 设计红线 9：真实数据派生的 % 注册进清洗白名单（去重保序）
        "sanctioned_pcts": list(dict.fromkeys(round(p, 2) for p in pcts)),
        "source_note": (f"东财融资融券个股明细（{code}，"
                        f"截至 {latest['date']}）"),
    }


def _not_eligible_block(code: str) -> dict[str, Any]:
    """接口可达但个股无两融记录 → 非标的干净降级块（缺席即信息）。"""
    return {
        "source": "eastmoney_rzrq",
        "is_margin_eligible": False,
        "latest_date": None,
        "margin_balance": None,
        "short_balance": None,
        "margin_balance_pct_of_float": None,
        "balance_chg_pct": None,
        "chg_window_days": None,
        "float_market_cap": None,
        "lines_zh": [
            "[两融] 该股不在融资融券标的范围内（东财两融明细无记录）"
            "——杠杆资金无法通过场内两融参与，不存在融资盘平仓踩踏"
            "风险，但同时也缺少这一路增量资金"
        ],
        "sanctioned_pcts": [],
        "source_note": f"东财融资融券个股明细（{code}：非两融标的）",
    }


def margin_sanctioned_pcts(blk: Any) -> list[float]:
    """从 __margin_trading 块提取白名单 %（缺省容错）。

    红线 8 说明：本模块自身不读写共享事实字典——盖章由 orchestrator
    （棘轮白名单内）完成，这里只接收已提取的块作显式参数。
    """
    if isinstance(blk, dict):
        vals = blk.get("sanctioned_pcts")
        if isinstance(vals, list):
            return [float(v) for v in vals if isinstance(v, (int, float))]
    return []
