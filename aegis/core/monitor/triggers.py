"""复研触发判定 — Aegis 2.0 Phase 3 任务 B1.

事件循环每轮扫描到一只票，会把这只票的**最新论点版本**（persistence 链上
record）、fresh 事件切片（em_events 近 90 天的公告 / 业绩预告）、该票的
**水位线**（已见事件 key + 锚定价）与**现价**一起喂给本模块，判定是否需要
触发一次 ``--update`` 复研，并给出**触发列表**——每条说清「哪类触发、命中哪个
封闭目录型号、中文原因、结构化细节」。

设计取舍（红线 10 / 红线 6）：

- **纯逻辑函数**：事件 / 水位线 / 现价全部由调用方（扫描器）传入，本模块不碰
  文件系统、不连网络、不建状态机。落盘 / 读链 / 更新水位线都在扫描器一侧。
- **触发源分两类**：
    1. **全 watchlist 触发源**——公告增量、预告增量。只要 :func:`diff_new`
       判出新事件（且对应开关开），就触发；**不要求** thesis 里挂了对应型号。
       事件是客观新信息，任何在监控的票出了新公告都值得看一眼。
    2. **封闭目录型号**（红线 6）——公告关键词命中〔并购/减值/订单〕、股价偏离、
       预告 vs 一致预期。这些能反解到 :data:`CATALOG` 型号，``model_id`` 填目录
       型号；:func:`active_model_ids` 反解 thesis 已挂哪些型号，用来给带型号的
       触发标注「已武装」（armed）并排序（论点显式承诺过的型号优先级更高）。
- **中文化铁律**：``reason_zh`` 全简体中文，只保留国际缩写。
- **永不 raise**：坏输入 / 缺字段一律容错降级，最坏返回 ``[]``。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from aegis.core.thesis.monitorables import (
    ANNOUNCEMENT_KEYWORDS,
    PRICE_DEVIATION_MAX,
    monitorable_model_id,
)
from aegis.core.monitor.watermark import diff_new

logger = logging.getLogger(__name__)

__all__ = [
    "Trigger",
    "active_model_ids",
    "evaluate_triggers",
]

#: 触发类别的展示 / 排序优先级（越小越靠前）。关键词命中优先于普通公告增量。
_KIND_PRIORITY: dict[str, int] = {
    "keyword_announcement": 0,
    "price_deviation": 1,
    "new_forecast": 2,
    "new_announcement": 3,
}


@dataclass
class Trigger:
    """一条复研触发原因。"""

    kind: str            # "new_announcement"|"keyword_announcement"|"new_forecast"|"price_deviation"
    model_id: str | None  # 命中的 CATALOG 型号；纯公告增量等无目录型号时为 None
    reason_zh: str       # 中文原因，如「新增业绩预告：扭亏（2026-01-21 披露）」
    detail: dict = field(default_factory=dict)  # 结构化细节（标题列表 / 现价 vs 锚定价…）


# ---------------------------------------------------------------------------
# 双形态取值 + 容错原语（dict 与 dataclass/对象通吃，永不 raise）
# ---------------------------------------------------------------------------

def _get(obj: Any, key: str, default: Any = None) -> Any:
    """dict 与对象双形态取值。"""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _events_list(events: Any, name: str) -> list[Any]:
    """从 RecentEvents（或其 dict）取一个事件列表；缺失 / 非列表 → []。"""
    raw = _get(events, name)
    if isinstance(raw, (list, tuple)):
        return list(raw)
    return []


def _switch(entry: Any, name: str, default: bool) -> bool:
    """读一只票的监控器开关位；缺 monitors / 非布尔一律取型号默认。"""
    monitors = _get(entry, "monitors")
    if monitors is None:
        return default
    val = _get(monitors, name, default)
    return val if isinstance(val, bool) else default


def _as_float(v: Any) -> float | None:
    """取数值（bool 不当数字）；非数值 → None。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


def _thesis_of(record: Any) -> dict[str, Any] | None:
    """从链上 record 取 thesis payload dict；None / 非 dict → None。

    主路径为 persistence 的 record 形态 ``{"version":…, "thesis": {…}}``；
    兼容直接传 thesis dict（顶层带 must_monitor）的情况。
    """
    if not isinstance(record, dict):
        return None
    thesis = record.get("thesis")
    if isinstance(thesis, dict):
        return thesis
    if "must_monitor" in record:  # 已经是 thesis payload 本身
        return record
    return None


# ---------------------------------------------------------------------------
# thesis 已武装型号反解
# ---------------------------------------------------------------------------

def active_model_ids(thesis_record: dict | None) -> set[str]:
    """反解 ``thesis.must_monitor`` 里所有可执行监控点型号（封闭目录 model_id）。

    ``thesis_record`` 为 ``None`` / 无 thesis / must_monitor 非列表时返回空集。
    watch_only（非目录）条目经 :func:`monitorable_model_id` 反解为 None，
    自动被排除。永不 raise。
    """
    thesis = _thesis_of(thesis_record)
    if thesis is None:
        return set()
    monitors = thesis.get("must_monitor")
    if not isinstance(monitors, (list, tuple)):
        return set()
    out: set[str] = set()
    for m in monitors:
        try:
            mid = monitorable_model_id(m)
        except Exception as e:  # noqa: BLE001 — 反解失败跳过该条，不崩
            logger.debug("triggers: 监控点型号反解失败，跳过: %s", e)
            mid = None
        if mid:
            out.add(mid)
    return out


# ---------------------------------------------------------------------------
# 各触发源判定（纯函数，返回该源的触发列表，永不 raise）
# ---------------------------------------------------------------------------

def _announcement_triggers(
    new_anns: list[Any], armed: set[str]
) -> list[Trigger]:
    """公告增量 → 普通公告触发 + （命中关键词时）关键词触发。"""
    out: list[Trigger] = []
    if not new_anns:
        return out

    titles = [str(_get(a, "title") or "").strip() for a in new_anns]
    dates = [str(_get(a, "date") or "").strip() for a in new_anns]

    # 1) 普通公告增量（全 watchlist 触发源，不要求 thesis 挂型号）。
    newest = max(new_anns, key=lambda a: str(_get(a, "date") or ""))
    nt = str(_get(newest, "title") or "").strip() or "（无标题）"
    nd = str(_get(newest, "date") or "").strip() or "日期未知"
    n = len(new_anns)
    if n == 1:
        reason = f"新增公告：{nt}（{nd}）"
    else:
        reason = f"新增 {n} 条公告（最新：{nt}，{nd}）"
    out.append(Trigger(
        kind="new_announcement",
        model_id=None,
        reason_zh=reason,
        detail={"count": n, "titles": titles, "dates": dates},
    ))

    # 2) 关键词命中〔并购/减值/订单〕→ 额外一条更高优先级的目录型号触发。
    matched: list[dict[str, str]] = []
    for a in new_anns:
        title = str(_get(a, "title") or "")
        for kw in ANNOUNCEMENT_KEYWORDS:
            if kw in title:
                matched.append({
                    "keyword": kw,
                    "title": title.strip(),
                    "date": str(_get(a, "date") or "").strip(),
                })
                break  # 一条公告只记首个命中关键词
    if matched:
        hit_kws = list(dict.fromkeys(m["keyword"] for m in matched))
        first = matched[0]
        if len(matched) == 1:
            reason = (
                f"公告命中关键词〔{first['keyword']}〕："
                f"{first['title'] or '（无标题）'}（{first['date'] or '日期未知'}）"
            )
        else:
            reason = (
                f"{len(matched)} 条公告命中关键词〔{'/'.join(hit_kws)}〕"
                f"（首条：{first['title'] or '（无标题）'}，"
                f"{first['date'] or '日期未知'}）"
            )
        out.append(Trigger(
            kind="keyword_announcement",
            model_id="announcement_keyword",
            reason_zh=reason,
            detail={
                "keywords": hit_kws,
                "matched": matched,
                "armed": "announcement_keyword" in armed,
            },
        ))
    return out


def _forecast_trigger(
    new_fcs: list[Any], armed: set[str]
) -> Trigger | None:
    """预告增量 → 一条 new_forecast 触发（thesis 挂了预告型号则填 model_id）。"""
    if not new_fcs:
        return None
    n = len(new_fcs)
    newest = max(new_fcs, key=lambda f: str(_get(f, "notice_date") or ""))
    ftype = str(_get(newest, "forecast_type") or "").strip() or "业绩预告更新"
    ndate = str(_get(newest, "notice_date") or "").strip() or "日期未知"
    if n == 1:
        reason = f"新增业绩预告：{ftype}（{ndate} 披露）"
    else:
        reason = f"新增 {n} 条业绩预告（最新：{ftype}，{ndate} 披露）"

    model_id = "forecast_vs_consensus" if "forecast_vs_consensus" in armed else None
    forecasts = [
        {
            "report_period": str(_get(f, "report_period") or "").strip(),
            "forecast_type": str(_get(f, "forecast_type") or "").strip(),
            "notice_date": str(_get(f, "notice_date") or "").strip(),
        }
        for f in new_fcs
    ]
    return Trigger(
        kind="new_forecast",
        model_id=model_id,
        reason_zh=reason,
        detail={"count": n, "forecasts": forecasts, "armed": model_id is not None},
    )


def _price_trigger(
    watermark: Any, current_price: float | None, armed: set[str]
) -> Trigger | None:
    """股价偏离锚定价超阈值 → price_deviation 触发；缺锚定价 / 现价则不触发。"""
    anchor = _as_float(_get(watermark, "anchor_price"))
    cur = _as_float(current_price)
    if anchor is None or cur is None or anchor == 0:
        return None
    deviation = (cur - anchor) / anchor
    if abs(deviation) <= PRICE_DEVIATION_MAX:
        return None
    direction = "上涨" if cur >= anchor else "下跌"
    reason = (
        f"股价偏离锚定价超阈值：现价 ¥{cur:.2f} 相对锚定价 ¥{anchor:.2f} "
        f"{direction} {abs(deviation) * 100:.1f}%"
        f"（阈值 ±{PRICE_DEVIATION_MAX * 100:.0f}%）"
    )
    return Trigger(
        kind="price_deviation",
        model_id="price_deviation",
        reason_zh=reason,
        detail={
            "anchor_price": anchor,
            "current_price": cur,
            "deviation": deviation,
            "deviation_pct": round(abs(deviation) * 100, 2),
            "direction": direction,
            "armed": "price_deviation" in armed,
        },
    )


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def _sort_key(t: Trigger) -> tuple[int, int]:
    """排序键：类别优先级为主，已武装（armed）为次（同类里 armed 靠前）。"""
    armed = bool(t.detail.get("armed")) if isinstance(t.detail, dict) else False
    return (_KIND_PRIORITY.get(t.kind, 99), 0 if armed else 1)


def evaluate_triggers(
    *,
    entry: Any,
    thesis_record: dict | None,
    events: Any,
    watermark: Any,
    current_price: float | None = None,
) -> list[Trigger]:
    """判定一只票是否需要复研，返回触发列表（无触发 → ``[]``，永不 raise）。

    Parameters
    ----------
    entry:
        :class:`~aegis.core.monitor.watchlist.WatchlistEntry`——提供监控器开关位
        （announcements / forecasts / price_deviation）。
    thesis_record:
        该票**最新论点版本**的链上 record（``{"version":…, "thesis": {…}}``），
        用 :func:`active_model_ids` 反解已挂型号，给带 model_id 的触发标「已武装」。
        ``None`` 时视作无已武装型号（公告 / 预告增量仍会触发）。
    events:
        :class:`~aegis.core.acquisition.connectors.em_events_connector.RecentEvents`
        ——fresh 事件切片（公告 + 业绩预告）。
    watermark:
        :class:`~aegis.core.monitor.watermark.TickerWatermark`——已见事件 key +
        锚定价，用来 :func:`diff_new` 求增量、判股价偏离。
    current_price:
        A 股现价（可选）；缺失或 anchor_price 缺失时股价偏离一律不触发。

    规则
    ----
    1. **新公告**（``monitors.announcements`` 开）：``diff_new`` 得到的新增公告 →
       一条 ``new_announcement``（detail 带标题列表）。
    2. **关键词公告**：新增公告标题命中 :data:`ANNOUNCEMENT_KEYWORDS`
       〔并购/减值/订单〕→ 额外一条 ``keyword_announcement``，
       ``model_id="announcement_keyword"``（更高优先级）。
    3. **新预告**（``monitors.forecasts`` 开）：新增业绩预告 → 一条
       ``new_forecast``；thesis 挂了 ``forecast_vs_consensus`` 则填该 model_id，
       否则 ``model_id=None``。
    4. **股价偏离**（``monitors.price_deviation`` 开）：有锚定价、有现价、
       ``|现价-锚定价|/锚定价 > PRICE_DEVIATION_MAX`` → 一条 ``price_deviation``，
       ``model_id="price_deviation"``（reason 含偏离百分比）。
    """
    try:
        armed = active_model_ids(thesis_record)

        anns = _events_list(events, "announcements")
        fcs = _events_list(events, "forecasts")

        ann_on = _switch(entry, "announcements", True)
        fc_on = _switch(entry, "forecasts", True)
        price_on = _switch(entry, "price_deviation", False)

        # 一次 diff 拿到公告 / 预告增量，再按开关取用。
        try:
            new_anns, new_fcs = diff_new(watermark, anns, fcs)
        except Exception as e:  # noqa: BLE001 — diff 异常降级为无增量
            logger.warning("triggers: diff_new 失败，视作无增量: %s", e)
            new_anns, new_fcs = [], []

        triggers: list[Trigger] = []

        if ann_on:
            triggers.extend(_announcement_triggers(new_anns, armed))
        if fc_on:
            fc_trigger = _forecast_trigger(new_fcs, armed)
            if fc_trigger is not None:
                triggers.append(fc_trigger)
        if price_on:
            price_trigger = _price_trigger(watermark, current_price, armed)
            if price_trigger is not None:
                triggers.append(price_trigger)

        triggers.sort(key=_sort_key)
        return triggers
    except Exception as e:  # noqa: BLE001 — 兜底：任何意外都不 raise 到扫描器
        logger.warning("triggers: evaluate_triggers 意外失败，返回空: %s", e)
        return []
