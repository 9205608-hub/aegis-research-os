"""L1 Wave 4（2026-08-01）：A 股股东户数摄取（东财股东户数明细）。

七轮审计验证过的唯一有效质量杠杆是 L1 数据摄取（Wave 1 分部收入 /
Wave 2 客户集中度 / Wave 3 解禁与质押均实证消灭对应 open_questions）。
本模块延续同一模式，补第四类 A 股特有数据缺口：**股东户数（筹码结构）**
——户数连续下降通常意味着筹码向少数账户集中（A 股经典信号），agents
此前只能把"筹码结构/股东户数变化"写进 open_questions。

数据源：akshare ``stock_zh_a_gdhs_detail_em(symbol)``（东财数据中心
股东户数明细）。2026-08-01 实测（301358 / 300502）确认字段结构：

- ``股东户数统计截止日`` / ``股东户数公告日期``：datetime.date（dtype
  object，防御 str）；行序按截止日**升序**（最新在表尾）；
- ``股东户数-本次`` / ``-上次`` / ``-增减``：户（int）；
- ``股东户数-增减比例``：**百分数**（-27.25 = 下降 27.25%，与 Wave 3
  解禁占比的"小数"口径相反——两个东财报表口径不同，均以实测为准）；
- ``户均持股市值`` / ``总市值``：元；
- 上市前静态登记行陷阱：IPO 前各期户数恒为发起人户数（301358 实测
  恒为 30 户），混入趋势推断会制造假"集中"信号，须先过滤。

产出三份消费物（任何环节失败返回 None，永不 raise）：
- 户数事实：``latest_holder_count`` / ``latest_change_pct`` /
  ``periods``（最近 4-8 期序列）/ ``holder_count_trend``；
- ``lines_zh``：prompt 注入用中文行（agents + synthesizer）；
- ``sanctioned_pcts``：户数变化 % 进清洗白名单（设计红线 9 同则）。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from aegis.core.acquisition.connectors.akshare_connector import _no_proxy

# 上市公司股东户数有效下限（户）。依据：《证券法》以 200 人为公开发行
# 界线，上市公司股东户数必然远高于 200；东财明细表内低于该值的早期行
# 是 IPO 前发起人静态登记数据（301358 实测上市前恒为 30 户），计入
# 趋势会制造假"筹码集中"信号，一律过滤。
_MIN_LISTED_HOLDER_COUNT = 200

# 契约保留的最近期数（任务口径"最近 4-8 期"取上限）
_MAX_PERIODS = 8

# lines_zh 序列行里逐期列出的上限——噪声控制，同 segment_zygc /
# restricted_release 的 _MAX_UPCOMING_LINES 手法。
_MAX_SERIES_LINE_PERIODS = 5

# 趋势判定的"平稳"死区（百分数）。依据：户数季度环比 ±1% 以内多为
# 送转除权、账户合并注销等登记口径的技术性抖动，不足以判定筹码迁移
# 方向；连续两期同向且幅度超过该死区才定向为"集中"/"分散"。阈值
# 本身不是披露数字，不进白名单、不在 lines_zh 里以字面量出现
# （同 equity_pledge 对 30%/80% 阈值的处理）。
_TREND_FLAT_EPS_PCT = 1.0

# 趋势判定所需的最少期数：连续两期变化方向 = 3 个数据点
_TREND_MIN_PERIODS = 3


def _f(v: Any) -> float | None:
    """宽容 float 转换，NaN → None。"""
    try:
        fv = float(v)
    except (TypeError, ValueError):
        return None
    return fv if fv == fv else None


def _to_date(v: Any) -> date | None:
    """日期列 → date（实测为 datetime.date，防御 str / NaT）。"""
    try:
        if isinstance(v, datetime):
            v = v.date()
        if isinstance(v, date):
            return v
        return date.fromisoformat(str(v)[:10])
    except (ValueError, TypeError):
        return None


def fetch_holder_count(stock_code: str) -> dict[str, Any] | None:
    """拉取并归一个股股东户数序列。失败 / 无数据返回 None，永不 raise。"""
    try:
        with _no_proxy():
            import akshare as ak
            df = ak.stock_zh_a_gdhs_detail_em(
                symbol=str(stock_code).strip()[:6]
            )
    except Exception:
        return None
    if df is None or df.empty:
        return None
    try:
        return _normalize(df)
    except Exception:
        return None


def _trend(changes: list[float | None]) -> str | None:
    """按最近连续两期变化方向推断趋势（见 _TREND_FLAT_EPS_PCT 注释）。

    ``changes`` 为按时间升序的环比 %；不足两期有效变化返回 None。
    """
    valid = [c for c in changes if c is not None]
    if len(valid) < 2:
        return None
    last_two = valid[-2:]
    if all(c < -_TREND_FLAT_EPS_PCT for c in last_two):
        return "集中"
    if all(c > _TREND_FLAT_EPS_PCT for c in last_two):
        return "分散"
    return "平稳"


def _normalize(df: Any) -> dict[str, Any] | None:
    need = {"股东户数统计截止日", "股东户数-本次"}
    if not need.issubset(set(df.columns)):
        return None

    rows: list[dict[str, Any]] = []
    for _, r in df.iterrows():
        d = _to_date(r.get("股东户数统计截止日"))
        cnt = _f(r.get("股东户数-本次"))
        if d is None or cnt is None:
            continue
        # 上市前发起人静态登记行过滤（见 _MIN_LISTED_HOLDER_COUNT 注释）
        if cnt < _MIN_LISTED_HOLDER_COUNT:
            continue
        ann = _to_date(r.get("股东户数公告日期"))
        rows.append({
            "period": d.isoformat(),
            "_date": d,
            "holder_count": int(cnt),
            "announce_date": ann.isoformat() if ann else None,
            "avg_holding_value": _f(r.get("户均持股市值")),
        })
    if not rows:
        return None
    rows.sort(key=lambda x: x["_date"])
    rows = rows[-_MAX_PERIODS:]

    # 环比 % 自行重算（首期无前值 = None）。源表 ``股东户数-增减比例``
    # 已是百分数，但其"上次"锚点跨过被过滤的上市前行会失真，重算更稳。
    periods: list[dict[str, Any]] = []
    for i, row in enumerate(rows):
        chg = None
        if i > 0 and rows[i - 1]["holder_count"] > 0:
            chg = round(
                (row["holder_count"] / rows[i - 1]["holder_count"] - 1) * 100.0,
                2,
            )
        periods.append({
            "period": row["period"],
            "holder_count": row["holder_count"],
            "change_pct": chg,
        })

    latest_row = rows[-1]
    latest = periods[-1]
    trend = _trend([p["change_pct"] for p in periods])

    # ── lines_zh + 白名单 % ──
    lines: list[str] = []
    pcts: list[float] = []

    chg_bit = ""
    if latest["change_pct"] is not None:
        chg_bit = f"，较上期变动 {latest['change_pct']:+.2f}%"
        pcts.append(abs(latest["change_pct"]))
    ahv = latest_row.get("avg_holding_value")
    ahv_bit = f"，户均持股市值约 {ahv / 1e4:.1f} 万元" if ahv else ""
    lines.append(
        f"[股东户数] 最新披露股东户数 {latest['holder_count']:,} 户"
        f"（截至 {latest['period']}"
        + (f"，公告 {latest_row['announce_date']}"
           if latest_row.get("announce_date") else "")
        + f"）{chg_bit}{ahv_bit}"
    )

    if len(periods) >= 2:
        shown = periods[-_MAX_SERIES_LINE_PERIODS:]
        frags = []
        for p in shown:
            bit = f"{p['period']} {p['holder_count']:,} 户"
            if p["change_pct"] is not None:
                bit += f"（{p['change_pct']:+.2f}%）"
                pcts.append(abs(p["change_pct"]))
            frags.append(bit)
        lines.append(f"[股东户数] 近 {len(shown)} 期户数序列：" + " → ".join(frags))

    if trend == "集中":
        lines.append(
            "[股东户数] 户数连续两期下降（筹码趋于集中）——户数连续下降"
            "通常意味着筹码向少数账户集中，是主力吸筹或长线资金增持的"
            "典型痕迹，但需结合解禁与减持公告交叉验证"
        )
    elif trend == "分散":
        lines.append(
            "[股东户数] 户数连续两期上升（筹码趋于分散）——通常意味着"
            "筹码向更多账户扩散，常见于高位派发或市场热度扩散阶段"
        )
    elif trend == "平稳":
        lines.append(
            "[股东户数] 近两期户数变动幅度较小或方向互现"
            "（筹码结构大体平稳）"
        )

    return {
        "source": "eastmoney_gdhs",
        "latest_holder_count": latest["holder_count"],
        "latest_period": latest["period"],
        "latest_announce_date": latest_row.get("announce_date"),
        "latest_change_pct": latest["change_pct"],
        "avg_holding_value": latest_row.get("avg_holding_value"),
        "periods": periods,
        "holder_count_trend": trend,
        "lines_zh": lines,
        # 设计红线 9：真实数据派生的 % 注册进清洗白名单（去重保序）
        "sanctioned_pcts": list(dict.fromkeys(round(p, 2) for p in pcts)),
        "source_note": f"东财股东户数明细（截至 {latest['period']}）",
    }


def holder_count_sanctioned_pcts(blk: Any) -> list[float]:
    """从 __holder_count 块提取白名单 %（缺省容错）。

    红线 8 说明：本模块自身不读写共享事实字典——盖章由 orchestrator
    （棘轮白名单内）完成，这里只接收已提取的块作显式参数。
    """
    if isinstance(blk, dict):
        vals = blk.get("sanctioned_pcts")
        if isinstance(vals, list):
            return [float(v) for v in vals if isinstance(v, (int, float))]
    return []
