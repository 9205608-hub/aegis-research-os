"""L1 Wave 4（2026-08-01）：A 股龙虎榜活跃度摄取（东财个股上榜统计）。

与 holder_count.py / margin_trading.py 同批接入的第六类 A 股特有数据
缺口：**龙虎榜（游资/机构席位异动）**。数据源：akshare
``stock_lhb_stock_statistic_em(symbol="近三月")``（东财数据中心个股
上榜统计，全市场一张表按代码过滤）。2026-08-01 实测（300502 命中 /
301358 缺席）确认字段结构：

- ``上榜次数``：int；``最近上榜日``：str "YYYY-MM-DD"；
- ``龙虎榜净买额`` / ``买入额`` / ``卖出额`` / ``总成交额``：**元**
  （1.93e9 = 19.3 亿元）；
- ``买方机构次数`` / ``卖方机构次数``：int；``机构买入净额``：元；
- 近三月表约 1800 行；个股缺席 = 近三个月未上榜。多数票不上榜，
  **查无记录返回 None**（缺席即无事实可注入，不盖章不注入——与
  segment 缺失同待遇，负面证据价值低于两融"非标的"）。

产出（失败 / 未上榜返回 None，永不 raise）：
- 龙虎榜事实：``times_on_list`` / ``latest_list_date`` / ``net_buy`` /
  ``inst_net_buy``；
- ``lines_zh``：prompt 注入用中文行（agents + synthesizer）。

红线 9 说明：本块只有金额与次数、无百分数，无须接清洗白名单——
``sanctioned_pcts`` 恒为空列表，仅为契约形态与其他 L1 块对齐。
"""

from __future__ import annotations

from typing import Any

from aegis.core.acquisition.connectors.akshare_connector import _no_proxy

# 统计窗口：东财"近三月"≈ 任务口径"近 90 天"
_WINDOW_SYMBOL = "近三月"
_WINDOW_ZH = "近三个月"


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
        return f"{abs(v) / 1e8:.2f} 亿元"
    if abs(v) >= 1e4:
        return f"{abs(v) / 1e4:.0f} 万元"
    return f"{abs(v):.0f} 元"


def fetch_lhb_activity(stock_code: str) -> dict[str, Any] | None:
    """拉取个股近三个月龙虎榜统计。失败 / 未上榜返回 None，永不 raise。"""
    try:
        with _no_proxy():
            import akshare as ak
            df = ak.stock_lhb_stock_statistic_em(symbol=_WINDOW_SYMBOL)
    except Exception:
        return None
    if df is None or df.empty:
        return None
    try:
        return _extract(df, str(stock_code).strip()[:6])
    except Exception:
        return None


def _extract(df: Any, code: str) -> dict[str, Any] | None:
    need = {"代码", "上榜次数"}
    if not need.issubset(set(df.columns)):
        return None
    sub = df[df["代码"].astype(str).str.strip() == code]
    if sub.empty:
        # 近三个月未上榜——多数票的常态，缺席即无事实可注入
        return None
    r = sub.iloc[0]
    times = _f(r.get("上榜次数"))
    if times is None or times <= 0:
        return None
    times = int(times)
    latest_date = str(r.get("最近上榜日") or "")[:10] or None
    net_buy = _f(r.get("龙虎榜净买额"))
    turnover = _f(r.get("龙虎榜总成交额"))
    inst_buy_n = int(_f(r.get("买方机构次数")) or 0)
    inst_sell_n = int(_f(r.get("卖方机构次数")) or 0)
    inst_net = _f(r.get("机构买入净额"))

    lines: list[str] = []
    bit = f"[龙虎榜] {_WINDOW_ZH}上榜 {times} 次"
    if latest_date:
        bit += f"（最近 {latest_date}）"
    if net_buy is not None:
        direction = "净买入" if net_buy >= 0 else "净卖出"
        bit += f"，龙虎榜席位合计{direction} {_fmt_yuan(net_buy)}"
    if turnover:
        bit += f"，榜上总成交 {_fmt_yuan(turnover)}"
    lines.append(bit)

    if inst_buy_n or inst_sell_n:
        inst_bit = (f"[龙虎榜] 机构专用席位买方 {inst_buy_n} 次 / "
                    f"卖方 {inst_sell_n} 次")
        if inst_net is not None:
            direction = "净买入" if inst_net >= 0 else "净卖出"
            inst_bit += f"，机构{direction} {_fmt_yuan(inst_net)}"
        inst_bit += "——机构席位动向可作为主力资金参与度的旁证"
        lines.append(inst_bit)

    return {
        "source": "eastmoney_lhb",
        "window_zh": _WINDOW_ZH,
        "times_on_list": times,
        "latest_list_date": latest_date,
        "net_buy": net_buy,
        "total_turnover": turnover,
        "inst_buy_times": inst_buy_n,
        "inst_sell_times": inst_sell_n,
        "inst_net_buy": inst_net,
        "lines_zh": lines,
        # 本块无百分数（见模块 docstring），空列表仅为契约形态对齐
        "sanctioned_pcts": [],
        "source_note": f"东财龙虎榜个股上榜统计（{_WINDOW_ZH}）",
    }
