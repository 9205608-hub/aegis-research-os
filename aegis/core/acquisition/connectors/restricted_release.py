"""L1 Wave 3（2026-08-01）：A 股限售解禁日历摄取（东财个股解禁批次）。

七轮审计验证过的唯一有效质量杠杆是 L1 数据摄取（Wave 1 分部收入 /
Wave 2 客户集中度均实证消灭对应 open_questions）。本模块延续同一模式，
补第三类 A 股特有数据缺口：**限售解禁供给侧抛压**——agents 此前只能把
"解禁减持时点"写进 open_questions，拿不到任何日历事实。

数据源：akshare ``stock_restricted_release_queue_em``（东财数据中心
个股限售解禁-解禁批次，RPT_LIFT_STAGE）。2026-08-01 实测（300502 全
历史批次 / 301358 含未来批次）确认字段结构：

- ``解禁时间``：datetime.date，按 FREE_DATE 倒序；
- ``解禁数量`` / ``实际解禁数量`` / ``未解禁数量``：股（akshare 已 ×1e4）；
- ``占总市值比例`` / ``占流通市值比例``：**小数**（0.0974 = 9.74%，
  akshare 不做 ×100，本模块负责换算并防御已是百分数的异常值）；
- ``限售股类型`` / ``解禁股东数``：批次属性。

产出三份消费物（任何环节失败返回 None，永不 raise）：
- 解禁日历事实：``next_release_date`` / ``next_release_pct`` /
  ``upcoming_12m``（未来 12 个月批次列表）/ ``total_pending_pct`` /
  ``recent_3m_released_pct``；
- ``lines_zh``：prompt 注入用中文行（agents + synthesizer），模式同
  segment_zygc / customer_concentration；
- ``sanctioned_pcts``：解禁占比 % 进清洗白名单（设计红线 9 同则——
  引用真数据的 % 不许被 strict 清洗误杀）。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from aegis.core.acquisition.connectors.akshare_connector import _no_proxy

# 前瞻 / 回看窗口（天）：契约口径"未来 12 个月 / 近 3 个月"
_FORWARD_DAYS = 365
_BACKWARD_DAYS = 90

# lines_zh 里逐批列出的未来批次上限——透传要全，清单无限长是审计眼里
# 的噪声（R2-3 触发器"垃圾场"教训同则，同 segment_zygc）。
_MAX_UPCOMING_LINES = 6


def _f(v: Any) -> float | None:
    """宽容 float 转换，NaN → None。"""
    try:
        fv = float(v)
    except (TypeError, ValueError):
        return None
    return fv if fv == fv else None


def _to_date(v: Any) -> date | None:
    """解禁时间列 → date（实测为 datetime.date，防御 str / NaT）。"""
    try:
        if isinstance(v, datetime):
            v = v.date()
        if isinstance(v, date):
            return v
        return date.fromisoformat(str(v)[:10])
    except (ValueError, TypeError):
        return None


def _ratio_to_pct(v: Any) -> float | None:
    """东财 FREE_RATIO/TOTAL_RATIO 小数 → 百分数。

    单批解禁占总/流通市值不可能超过 100%，值 >1.0 视为源头已是百分数
    （防御 akshare 未来改口径），直接采用。
    """
    fv = _f(v)
    if fv is None or fv < 0:
        return None
    return round(fv * 100.0, 2) if fv <= 1.0 else round(fv, 2)


def _fmt_shares(n: float) -> str:
    """股数 → 亿/万股显示（A 股惯例）。"""
    if n >= 1e8:
        return f"{n / 1e8:.2f}亿股"
    if n >= 1e4:
        return f"{n / 1e4:.0f}万股"
    return f"{n:.0f}股"


def fetch_restricted_release(
    stock_code: str, today: date | None = None,
) -> dict[str, Any] | None:
    """拉取并归一个股解禁批次。网络失败 / 无数据返回 None，永不 raise。"""
    try:
        with _no_proxy():
            import akshare as ak
            df = ak.stock_restricted_release_queue_em(
                symbol=str(stock_code).strip()[:6]
            )
    except Exception:
        return None
    if df is None or df.empty:
        return None
    try:
        return _normalize(df, today or date.today())
    except Exception:
        return None


def _batch_bits(b: dict[str, Any]) -> str:
    bits: list[str] = []
    if b.get("shares"):
        bits.append(_fmt_shares(b["shares"]))
    if b.get("share_type"):
        bits.append(str(b["share_type"]))
    if b.get("pct_of_total") is not None:
        bits.append(f"占总市值 {b['pct_of_total']:.2f}%")
    if b.get("pct_of_float") is not None:
        bits.append(f"占流通市值 {b['pct_of_float']:.2f}%")
    return "，".join(bits) if bits else "明细未披露"


def _normalize(df: Any, today: date) -> dict[str, Any] | None:
    need = {"解禁时间", "解禁数量", "占总市值比例"}
    if not need.issubset(set(df.columns)):
        return None

    batches: list[tuple[date, dict[str, Any]]] = []
    for _, r in df.iterrows():
        d = _to_date(r.get("解禁时间"))
        if d is None:
            continue
        holder_num = _f(r.get("解禁股东数"))
        entry: dict[str, Any] = {
            "date": d.isoformat(),
            "shares": _f(r.get("解禁数量")),
            "pct_of_total": _ratio_to_pct(r.get("占总市值比例")),
            "pct_of_float": _ratio_to_pct(r.get("占流通市值比例")),
            "share_type": str(r.get("限售股类型") or "").strip(),
            "holder_num": int(holder_num) if holder_num is not None else None,
        }
        batches.append((d, entry))
    if not batches:
        return None
    batches.sort(key=lambda t: t[0])

    horizon = today + timedelta(days=_FORWARD_DAYS)
    lookback = today - timedelta(days=_BACKWARD_DAYS)
    future = [(d, e) for d, e in batches if d > today]
    upcoming_12m = [e for d, e in future if d <= horizon]
    recent_3m = [e for d, e in batches if lookback <= d <= today]

    def _pct_sum(entries: list[dict[str, Any]]) -> float:
        return round(sum(e["pct_of_total"] or 0.0 for e in entries), 2)

    upcoming_12m_pct = _pct_sum(upcoming_12m)
    total_pending_pct = _pct_sum([e for _, e in future])
    recent_3m_released_pct = _pct_sum(recent_3m)

    # ── lines_zh + 白名单 % ──
    lines: list[str] = []
    pcts: list[float] = []

    def _collect(e: dict[str, Any]) -> None:
        for k in ("pct_of_total", "pct_of_float"):
            if e.get(k) is not None:
                pcts.append(e[k])

    if future:
        nxt = future[0][1]
        _collect(nxt)
        lines.append(f"[解禁日历] 下一批解禁 {nxt['date']}：{_batch_bits(nxt)}")
        if len(upcoming_12m) > 1:
            shown = upcoming_12m[:_MAX_UPCOMING_LINES]
            frag_parts = []
            for e in shown:
                _collect(e)
                p = (f"{e['pct_of_total']:.2f}%"
                     if e.get("pct_of_total") is not None else "占比未披露")
                frag_parts.append(f"{e['date']} {p}"
                                  + (f"（{e['share_type']}）" if e.get("share_type") else ""))
            omitted = len(upcoming_12m) - len(shown)
            tail = f"；（另 {omitted} 批从略）" if omitted > 0 else ""
            lines.append(
                f"[解禁日历] 未来 12 个月共 {len(upcoming_12m)} 批待解禁，"
                f"合计占总市值约 {upcoming_12m_pct:.2f}%："
                + "；".join(frag_parts) + tail
            )
            pcts.append(upcoming_12m_pct)
        elif not upcoming_12m:
            lines.append(
                f"[解禁日历] 未来 12 个月内无解禁批次；此后已公告待解禁"
                f"合计占总市值约 {total_pending_pct:.2f}%"
            )
            pcts.append(total_pending_pct)
    else:
        last = batches[-1][1]
        last_bit = ""
        if last.get("pct_of_total") is not None:
            last_bit = (f"（最近一批 {last['date']} 已解禁，"
                        f"占总市值 {last['pct_of_total']:.2f}%）")
            pcts.append(last["pct_of_total"])
        lines.append("[解禁日历] 已公告限售批次均已解禁，"
                     "未来 12 个月无已公告解禁批次" + last_bit)
    if recent_3m:
        lines.append(
            f"[解禁日历] 近 3 个月已解禁 {len(recent_3m)} 批，"
            f"合计占总市值约 {recent_3m_released_pct:.2f}%"
        )
        pcts.append(recent_3m_released_pct)

    return {
        "source": "eastmoney_restricted_release",
        "as_of": today.isoformat(),
        "next_release_date": future[0][1]["date"] if future else None,
        "next_release_pct": future[0][1]["pct_of_total"] if future else None,
        "upcoming_12m": upcoming_12m,
        "upcoming_12m_pct": upcoming_12m_pct,
        "total_pending_pct": total_pending_pct,
        "recent_3m_released_pct": recent_3m_released_pct,
        "recent_3m_batches": len(recent_3m),
        "lines_zh": lines,
        # 设计红线 9：真实数据派生的 % 注册进清洗白名单（去重保序）
        "sanctioned_pcts": list(dict.fromkeys(pcts)),
        "source_note": f"东财个股限售解禁批次（截至 {today.isoformat()}）",
    }


def restricted_sanctioned_pcts(blk: Any) -> list[float]:
    """从 __restricted_release 块提取白名单 %（缺省容错）。

    红线 8 说明：本模块自身不读写共享事实字典——盖章由 orchestrator
    （棘轮白名单内）完成，这里只接收已提取的块作显式参数。
    """
    if isinstance(blk, dict):
        vals = blk.get("sanctioned_pcts")
        if isinstance(vals, list):
            return [float(v) for v in vals if isinstance(v, (int, float))]
    return []
