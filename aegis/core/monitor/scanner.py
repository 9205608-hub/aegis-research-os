"""一轮监控扫描编排 — Aegis 2.0 Phase 3 任务 B2（本波最重）.

事件循环的「一次心跳」：读 watchlist 票池，对每只 enabled 票拉近事件
（公告 / 业绩预告）、读已落盘论点、算触发（:mod:`aegis.core.monitor.triggers`），
命中就跑一次增量复研（:func:`aegis.core.monitor.runner.run_update`），并落
delta 简报 + 扫描报告；每票都把 fresh 事件并进水位线（幂等：再扫无新增）。

流程（每 enabled 票）::

    events = events_fn(ticker)          # 近事件切片
    thesis = load_latest(entity)        # 已落盘论点（可能为 None）
    wm     = store.get(ticker)          # 事件水位线
    price  = quote_fn(ticker)           # 仅 price_deviation 相关时取
    triggers = evaluate_triggers(...)   # 事件/监控点触发判定（B1）
      ├─ 无触发           → no_change（仍并水位线）
      └─ 有触发
           ├─ dry_run     → 只报「会触发」，零副作用
           ├─ 无 thesis   → no_thesis（仍并水位线，避免下次重复触发）
           ├─ 预算耗尽    → budget_exhausted（不跑复研）
           └─ 否则        → run_update → 计费 + 落 delta + 重锚水位线

设计红线 10：不建状态机、不引入 async / 新第三方依赖，存储一律 JSON 文件。
容错：**单票异常降级为 outcome error，绝不打断整轮**；扫描报告落盘失败仅告警。

可注入依赖（测试用，全部有生产默认，绝不真连网络）：
``events_fn`` / ``quote_fn`` / ``update_fn`` / ``triggers_fn``。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

from aegis.core.monitor.budget import DailyBudget
from aegis.core.monitor.delta import DeltaBriefing, summarize_change
from aegis.core.monitor.watchlist import WatchlistEntry, load_watchlist
from aegis.core.monitor.watermark import (
    TickerWatermark,
    WatermarkStore,
    merge_seen,
)
from aegis.core.thesis.persistence import (
    history,
    load_latest,
    normalize_entity_id,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DELTA_DIR",
    "SCAN_REPORT_DIR",
    "TickerScanOutcome",
    "ScanReport",
    "scan_once",
]

#: delta 简报默认落盘目录（每票每版一份 .md + .json）。
DELTA_DIR = Path(".cache/deltas")
#: 扫描报告默认落盘目录（每轮一份 .md）。
SCAN_REPORT_DIR = Path(".cache/monitor/scans")

#: skipped_reason → 人读中文（扫描报告用）。
_REASON_ZH: dict[str | None, str] = {
    "no_change": "无触发",
    "budget_exhausted": "当日预算耗尽，跳过复研",
    "no_thesis": "尚无底稿论点，仅更新水位线",
    "error": "扫描出错",
}


@contextlib.contextmanager
def _scan_lock(monitor_root: Path) -> Iterator[bool]:
    """**跨进程**互斥锁：yield True=独占本轮，False=已有扫描在跑（应跳过）。

    launchd 定时任务与 dashboard 兜底线程是**不同进程**，in-process
    ``threading.Lock`` 挡不住（审查发现 #6）——用文件锁 ``fcntl.flock`` 把两者
    串到同一把锁。非阻塞（``LOCK_NB``）：抢不到就跳过本轮（务实兜底，不排队），
    避免两轮并发读改写共享水位线 / 预算台账丢更新。

    平台无 fcntl（非 Unix）或加锁异常时**降级为不加锁**（yield True）——本项目
    部署在 Mac，降级仅为健壮性兜底。
    """
    try:
        import fcntl
    except Exception:  # noqa: BLE001 — 非 Unix：降级为不加锁
        yield True
        return
    lock_path = monitor_root / "scan.lock"
    fd = None
    try:
        monitor_root.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    except OSError as e:
        logger.debug("scanner: 打开锁文件失败，降级不加锁: %s", e)
        if fd is not None:
            os.close(fd)
        yield True
        return
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            yield False
            return
        try:
            yield True
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# 结果数据结构
# ---------------------------------------------------------------------------

@dataclass
class TickerScanOutcome:
    """单只票一轮扫描的结果。"""

    ticker: str
    triggered: bool
    updated: bool
    triggers: list                       # list[Trigger]（B1），可为空
    skipped_reason: str | None           # "no_change"|"budget_exhausted"|"no_thesis"|"error"|None
    delta: object | None                 # DeltaBriefing | None
    cost_usd: float = 0.0
    error: str | None = None


@dataclass
class ScanReport:
    """一轮扫描的汇总报告（可 to_markdown / to_dict）。"""

    started_at: str
    tickers_scanned: int
    tickers_triggered: int
    tickers_updated: int
    total_cost_usd: float
    outcomes: list = field(default_factory=list)   # list[TickerScanOutcome]

    # -- 序列化 -----------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """转成可 JSON 落盘的 dict。"""
        return {
            "started_at": self.started_at,
            "tickers_scanned": self.tickers_scanned,
            "tickers_triggered": self.tickers_triggered,
            "tickers_updated": self.tickers_updated,
            "total_cost_usd": round(self.total_cost_usd, 6),
            "outcomes": [_outcome_to_dict(o) for o in self.outcomes],
        }

    def to_markdown(self) -> str:
        """人读的中文扫描报告：汇总 + 各标的明细表。"""
        lines: list[str] = [
            f"# 监控扫描报告 · {self.started_at}",
            "",
            f"- 扫描标的数：{self.tickers_scanned}",
            f"- 触发复核数：{self.tickers_triggered}",
            f"- 实际复研数：{self.tickers_updated}",
            f"- 本轮 LLM 成本（USD）：${self.total_cost_usd:.4f}",
            "",
            "## 各标的明细",
            "",
            "| 标的 | 触发 | 复研 | 说明 | 成本(USD) |",
            "| --- | --- | --- | --- | --- |",
        ]
        if not self.outcomes:
            lines.append("| （本轮无 enabled 标的） | — | — | — | — |")
        for o in self.outcomes:
            lines.append(
                f"| {o.ticker} | {'是' if o.triggered else '否'} "
                f"| {'是' if o.updated else '否'} | {_status_zh(o)} "
                f"| ${o.cost_usd:.4f} |"
            )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 序列化小工具（容错）
# ---------------------------------------------------------------------------

def _trigger_to_dict(t: Any) -> dict[str, Any]:
    """Trigger 对象 / dict → 统一 dict（字段缺失容错）。"""
    def _f(key: str) -> Any:
        if isinstance(t, dict):
            return t.get(key)
        return getattr(t, key, None)
    return {
        "kind": _f("kind"),
        "model_id": _f("model_id"),
        "reason_zh": _f("reason_zh"),
        "detail": _f("detail"),
    }


def _outcome_to_dict(o: TickerScanOutcome) -> dict[str, Any]:
    delta = o.delta
    delta_dict = None
    if delta is not None and hasattr(delta, "to_dict"):
        try:
            delta_dict = delta.to_dict()
        except Exception as e:  # noqa: BLE001 — 序列化失败降级
            logger.debug("scanner: delta.to_dict 失败: %s", e)
    return {
        "ticker": o.ticker,
        "triggered": o.triggered,
        "updated": o.updated,
        "triggers": [_trigger_to_dict(t) for t in (o.triggers or [])],
        "skipped_reason": o.skipped_reason,
        "delta": delta_dict,
        "cost_usd": round(o.cost_usd, 6),
        "error": o.error,
    }


def _reason_zh(t: Any) -> str:
    """从 Trigger 取 reason_zh（dict / 对象双形态）。"""
    if isinstance(t, dict):
        return str(t.get("reason_zh") or "").strip()
    return str(getattr(t, "reason_zh", "") or "").strip()


def _status_zh(o: TickerScanOutcome) -> str:
    """一只票的扫描结论中文短语（扫描报告表格用）。"""
    if o.error:
        return f"出错：{o.error}"
    if o.updated:
        reasons = "；".join(r for r in (_reason_zh(t) for t in (o.triggers or [])) if r)
        return f"已复研（{reasons}）" if reasons else "已复研"
    if o.skipped_reason in _REASON_ZH:
        base = _REASON_ZH[o.skipped_reason]
        if o.triggered and o.skipped_reason not in ("no_change",):
            reasons = "；".join(
                r for r in (_reason_zh(t) for t in (o.triggers or [])) if r)
            if reasons:
                return f"{base}（触发：{reasons}）"
        return base
    if o.triggered:
        reasons = "；".join(
            r for r in (_reason_zh(t) for t in (o.triggers or [])) if r)
        return f"已触发未复研（{reasons}）" if reasons else "已触发未复研"
    return "无触发"


# ---------------------------------------------------------------------------
# 依赖解析（生产默认；测试注入假函数覆盖，绝不真连网络）
# ---------------------------------------------------------------------------

def _make_default_events_fn(lookback_days: int) -> Callable[[str], Any]:
    """生产默认事件源：em_events.fetch_recent_events（永不 raise，失败 → None）。"""
    def _fn(ticker: str) -> Any:
        try:
            from aegis.core.acquisition.connectors.em_events_connector import (
                fetch_recent_events,
            )
            return fetch_recent_events(ticker, days=lookback_days)
        except Exception as e:  # noqa: BLE001 — 事件源失败降级
            logger.warning("scanner: 默认拉事件失败 %s: %s", ticker, e)
            return None
    return _fn


def _default_quote_fn(ticker: str) -> float | None:
    """生产默认取价：tencent/sina 实时价，失败 / 不可达 → None。"""
    try:
        from aegis.core.acquisition.connectors.tencent_sina_quote import (
            fetch_cn_quote,
        )
        q = fetch_cn_quote(ticker)
    except Exception as e:  # noqa: BLE001 — 取价失败降级
        logger.debug("scanner: 默认取价失败 %s: %s", ticker, e)
        return None
    return _coerce_price(q)


def _default_update_fn(
    ticker: str, trigger_zh: str | None, smoke: bool,
    use_llm: bool, thesis_dir: str | None,
) -> Any:
    """生产默认复研：runner.run_update（延迟 import，永不 raise）。"""
    from aegis.core.monitor.runner import run_update
    return run_update(
        ticker, trigger_zh=trigger_zh, smoke=smoke,
        use_llm=use_llm, thesis_dir=thesis_dir,
    )


def _resolve_triggers_fn(triggers_fn: Callable | None) -> Callable:
    """注入的 triggers_fn 优先；否则延迟 import B1 的 evaluate_triggers。

    triggers 模块（B1，同波）缺失 / import 失败时降级为「永不触发」，保证扫描
    不崩（生产落地时 B1 已合并，走真实判定）。
    """
    if triggers_fn is not None:
        return triggers_fn
    try:
        from aegis.core.monitor.triggers import evaluate_triggers
        return evaluate_triggers
    except Exception as e:  # noqa: BLE001 — B1 尚未落地时降级
        logger.warning("scanner: 无法 import evaluate_triggers，本轮按无触发处理: %s", e)

        def _noop(**_kwargs: Any) -> list:
            return []
        return _noop


def _active_models(thesis: Any) -> set[str] | None:
    """thesis 的活跃监控型号集合；triggers 模块缺失 / 异常 → None（不作门控）。"""
    if thesis is None:
        return None
    try:
        from aegis.core.monitor.triggers import active_model_ids
        return set(active_model_ids(thesis))
    except Exception:  # noqa: BLE001 — 缺 B1 或异常时不门控
        return None


def _coerce_price(v: Any) -> float | None:
    """把 quote_fn 返回（float / CNQuote / None）归一为 float | None。"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    cp = getattr(v, "current_price", None)
    if isinstance(cp, (int, float)) and not isinstance(cp, bool):
        return float(cp)
    return None


def _safe_quote(quote_fn: Callable[[str], Any], ticker: str) -> float | None:
    """调 quote_fn 取价并归一；抛异常 → None（永不 raise 到扫描主流程）。"""
    try:
        return _coerce_price(quote_fn(ticker))
    except Exception as e:  # noqa: BLE001 — 取价失败降级
        logger.debug("scanner: 取价失败 %s: %s", ticker, e)
        return None


# ---------------------------------------------------------------------------
# 水位线并入（幂等的关键：每轮把 fresh 事件 key 并进 seen）
# ---------------------------------------------------------------------------

def _persist_watermark(
    store: WatermarkStore, wm: TickerWatermark,
    announcements: list, forecasts: list, now_iso: str,
) -> None:
    """把本轮 fresh 事件并入水位线 + 记 last_scan_at + 落盘。"""
    merged = merge_seen(wm, announcements, forecasts)
    merged.last_scan_at = now_iso
    store.put(merged)


def _touch_scan_at(
    store: WatermarkStore, wm: TickerWatermark, now_iso: str,
) -> None:
    """只记 last_scan_at 落盘，**不并入 fresh 事件**（复研瞬时失败时用）。

    区别于 :func:`_persist_watermark`：失败分支绝不把触发的公告/预告 key 并进
    seen，否则一次 LLM 超时 / 抓数失败会把催化剂永久消费、次轮不再重试
    （审查发现 #1，高危）。仅记扫描时间，保留 fresh 事件的「未见」状态。
    """
    wm.last_scan_at = now_iso
    store.put(wm)


# ---------------------------------------------------------------------------
# delta 简报落盘
# ---------------------------------------------------------------------------

def _write_delta(
    ticker: str, entity_id: str, thesis_dir: str | None,
    delta_dir: Path, trigger_zh: str | None, result: Any,
) -> DeltaBriefing | None:
    """读论点链前后两版 → summarize_change → 落 {entity}_v{N}.md + .json。

    链为空（复研未写入新版）时返回 None（不落文件）。永不 raise。
    """
    try:
        records = history(ticker, dir=thesis_dir)
    except Exception as e:  # noqa: BLE001 — 读链失败降级
        logger.warning("scanner: %s 读论点链失败: %s", ticker, e)
        return None
    if not records:
        return None

    new_record = records[-1]
    prev_record = records[-2] if len(records) >= 2 else None
    brief = summarize_change(prev_record, new_record, trigger_zh=trigger_zh)

    version = new_record.get("version")
    if not isinstance(version, int):
        version = getattr(result, "thesis_version", None)
    if not isinstance(version, int):
        version = 0

    try:
        delta_dir.mkdir(parents=True, exist_ok=True)
        (delta_dir / f"{entity_id}_v{version}.md").write_text(
            brief.to_markdown(), encoding="utf-8")
        (delta_dir / f"{entity_id}_v{version}.json").write_text(
            json.dumps(brief.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8")
    except OSError as e:
        logger.warning("scanner: %s delta 落盘失败: %s", ticker, e)
    return brief


# ---------------------------------------------------------------------------
# 单票扫描
# ---------------------------------------------------------------------------

def _scan_one(
    entry: WatchlistEntry,
    *,
    store: WatermarkStore,
    thesis_dir: str | None,
    delta_dir: Path,
    budget: DailyBudget,
    dry_run: bool,
    smoke: bool,
    use_llm: bool,
    events_fn: Callable[[str], Any],
    quote_fn: Callable[[str], Any],
    update_fn: Callable,
    triggers_fn: Callable,
    now_iso: str,
) -> TickerScanOutcome:
    """扫一只票并回报 outcome。内部各步容错；最外层 caller 再兜一层。"""
    ticker = entry.ticker
    entity_id = normalize_entity_id(ticker)

    # 1) 近事件切片（事件源抛异常 → error outcome，不并水位线）。
    try:
        events = events_fn(ticker)
    except Exception as e:  # noqa: BLE001 — 事件源失败该票降级
        logger.warning("scanner: %s 拉事件失败: %s", ticker, e)
        return TickerScanOutcome(
            ticker=ticker, triggered=False, updated=False, triggers=[],
            skipped_reason="error", delta=None, error=str(e),
        )
    announcements = list(getattr(events, "announcements", []) or [])
    forecasts = list(getattr(events, "forecasts", []) or [])

    # 2) 已落盘论点 + 水位线。
    try:
        thesis = load_latest(ticker, dir=thesis_dir)
    except Exception as e:  # noqa: BLE001 — 读论点失败当作无底稿
        logger.warning("scanner: %s 读论点失败: %s", ticker, e)
        thesis = None
    wm = store.get(ticker)

    # 3) 行情（仅 price_deviation 开关打开且论点存在且该型号活跃时取）。
    price: float | None = None
    if entry.monitors.price_deviation and thesis is not None:
        active = _active_models(thesis)
        if active is None or "price_deviation" in active:
            price = _safe_quote(quote_fn, ticker)
            # 审查发现 #2：首次观测即以当轮现价播种锚定价，打破「价格触发需
            # anchor / anchor 需触发驱动的成功复研」死锁——否则 price_deviation-only
            # 票恒不触发。此后现价相对该基线偏离超阈值即可触发（本轮偏离 0 不触发）。
            # wm 是 store.get 的拷贝，就地改，经末尾 _persist_watermark(merge_seen
            # 透传 anchor_price) 落盘。
            if price is not None and wm.anchor_price is None:
                wm.anchor_price = price

    # 4) 触发判定（B1；异常降级为无触发）。
    try:
        triggers = list(triggers_fn(
            entry=entry, thesis_record=thesis, events=events,
            watermark=wm, current_price=price,
        ) or [])
    except Exception as e:  # noqa: BLE001 — 触发判定失败当作无触发
        logger.warning("scanner: %s 触发判定失败: %s", ticker, e)
        triggers = []

    triggered = bool(triggers)
    trigger_zh = "；".join(
        r for r in (_reason_zh(t) for t in triggers) if r) or None

    # 5a) dry_run：只报「会不会触发」，**零副作用**——必须在任何
    #     _persist_watermark 之前早退。否则无触发票的水位线（及 #2 播种的
    #     anchor）会被落盘，违反 dry_run「不改水位线」契约（审查复核发现）。
    if dry_run:
        return TickerScanOutcome(
            ticker=ticker, triggered=triggered, updated=False,
            triggers=triggers,
            skipped_reason=(None if triggered else "no_change"),
            delta=None,
        )

    # 5) 无触发：仅并水位线（marks scanned events seen → 幂等）。
    if not triggered:
        _persist_watermark(store, wm, announcements, forecasts, now_iso)
        return TickerScanOutcome(
            ticker=ticker, triggered=False, updated=False, triggers=[],
            skipped_reason="no_change", delta=None,
        )

    # 5b) 无 thesis（首次没底稿）：跳过复研，但仍并水位线避免下次重复触发。
    if thesis is None:
        _persist_watermark(store, wm, announcements, forecasts, now_iso)
        return TickerScanOutcome(
            ticker=ticker, triggered=True, updated=False, triggers=triggers,
            skipped_reason="no_thesis", delta=None,
        )

    # 5c) 预算耗尽：不跑复研，记录后仍并水位线。
    if not budget.can_afford():
        _persist_watermark(store, wm, announcements, forecasts, now_iso)
        return TickerScanOutcome(
            ticker=ticker, triggered=True, updated=False, triggers=triggers,
            skipped_reason="budget_exhausted", delta=None,
        )

    # 5d) 跑增量复研。
    result = update_fn(ticker, trigger_zh, smoke, use_llm, thesis_dir)
    cost = 0.0
    if result is not None:
        try:
            cost = float(getattr(result, "cost_usd", 0.0) or 0.0)
        except (TypeError, ValueError):
            cost = 0.0
    # 复研实际发生的成本一律计入当日预算（无论成败）。
    if cost > 0:
        budget.charge(ticker, cost)

    ok = bool(getattr(result, "ok", False)) if result is not None else False
    if not ok:
        err = getattr(result, "error", None) if result is not None else "复研返回 None"
        # 审查发现 #1（高危）：复研**瞬时失败**（LLM 超时 / 抓数中断）时绝不把
        # 触发的公告/预告并入 seen，否则催化剂被永久消费、故障恢复后次轮不再重试。
        # 只记 last_scan_at，保留 fresh 事件「未见」→ 下一轮重新触发重试。
        _touch_scan_at(store, wm, now_iso)
        return TickerScanOutcome(
            ticker=ticker, triggered=True, updated=False, triggers=triggers,
            skipped_reason="error", delta=None, cost_usd=cost,
            error=str(err) if err else "复研未成功",
        )

    # 复研成功：落 delta + 重锚水位线（anchor_price=price, version=新版）。
    brief = _write_delta(
        ticker, entity_id, thesis_dir, delta_dir, trigger_zh, result)
    new_version = brief.to_version if brief is not None else None
    if not isinstance(new_version, int):
        v = getattr(result, "thesis_version", None)
        new_version = v if isinstance(v, int) else None

    if price is not None:
        wm.anchor_price = price
    if isinstance(new_version, int):
        wm.anchor_thesis_version = new_version
    _persist_watermark(store, wm, announcements, forecasts, now_iso)

    return TickerScanOutcome(
        ticker=ticker, triggered=True, updated=True, triggers=triggers,
        skipped_reason=None, delta=brief, cost_usd=cost,
    )


# ---------------------------------------------------------------------------
# 报告落盘
# ---------------------------------------------------------------------------

def _write_scan_report(report: ScanReport, scans_dir: Path, now_iso: str) -> None:
    """落 {scans_dir}/{ts}.md；失败仅告警，不 raise。"""
    stamp = re.sub(r"[^0-9]", "", now_iso)[:14] or datetime.now().strftime(
        "%Y%m%d%H%M%S")
    try:
        scans_dir.mkdir(parents=True, exist_ok=True)
        (scans_dir / f"{stamp}.md").write_text(
            report.to_markdown(), encoding="utf-8")
    except OSError as e:
        logger.warning("scanner: 扫描报告落盘失败: %s", e)


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------

def scan_once(
    *,
    watchlist_path: Path | str | None = None,
    watermark_path: Path | str | None = None,
    thesis_dir: str | None = None,
    delta_dir: Path | str | None = None,
    dry_run: bool = False,
    smoke: bool = False,
    use_llm: bool = True,
    events_fn: Callable[[str], Any] | None = None,
    quote_fn: Callable[[str], Any] | None = None,
    update_fn: Callable | None = None,
    triggers_fn: Callable | None = None,
    now_iso: str | None = None,
) -> ScanReport:
    """跑一轮监控扫描并返回 :class:`ScanReport`。永不 raise。

    Args:
        watchlist_path: 票池 YAML；缺省内置默认票池。
        watermark_path: 水位线库路径；缺省 ``.cache/monitor/watermarks.json``。
            当日预算台账（``spend/``）与扫描报告（``scans/``）落在其父目录下——
            测试把 ``watermark_path`` 指向 tmp 即可一并重定向，无需额外参数。
        thesis_dir: 论点链目录（读 load_latest / history）。
        delta_dir: delta 简报目录；缺省 :data:`DELTA_DIR`。
        dry_run: 只报「会触发」，零副作用（不复研、不计费、不改水位线）。
        smoke: 冒烟模式（透传给复研，重定向 HTML / 缓存到 smoke 目录）。
        use_llm: 复研是否启用 LLM（透传给复研）。
        events_fn / quote_fn / update_fn / triggers_fn: 可注入依赖（测试用）；
            缺省走生产实现（em 事件 / tencent 行情 / runner 复研 / B1 触发判定）。
        now_iso: 本轮时间戳（缺省 ``datetime.now().isoformat()``），决定当日预算
            台账日期、水位线 last_scan_at、扫描报告文件名。

    Returns:
        :class:`ScanReport`：本轮汇总 + 各标的 :class:`TickerScanOutcome`。
    """
    now_iso = (now_iso or datetime.now().isoformat()).strip()

    watchlist = load_watchlist(watchlist_path)

    # spend / scans 目录挂在水位线库父目录下（默认 .cache/monitor/）。
    monitor_root = (
        Path(watermark_path).parent if watermark_path is not None
        else SCAN_REPORT_DIR.parent  # == .cache/monitor
    )
    spend_dir = monitor_root / "spend"
    scans_dir = monitor_root / "scans"
    d_dir = Path(delta_dir) if delta_dir is not None else DELTA_DIR

    ev_fn = events_fn or _make_default_events_fn(
        watchlist.announcement_lookback_days)
    q_fn = quote_fn or _default_quote_fn
    u_fn = update_fn or _default_update_fn
    trig_fn = _resolve_triggers_fn(triggers_fn)

    def _empty_report() -> ScanReport:
        return ScanReport(
            started_at=now_iso, tickers_scanned=0, tickers_triggered=0,
            tickers_updated=0, total_cost_usd=0.0, outcomes=[])

    # 审查发现 #6：非 dry_run 会读改写共享水位线 / 预算台账，须**跨进程**互斥
    # （launchd 进程 vs dashboard 兜底线程）。dry_run 只读，无需锁。抢不到锁 →
    # 跳过本轮（务实兜底，不排队）。store / budget 在锁内构造，保证「读—改—写」
    # 全程持锁，不读到别的扫描写了一半的状态。
    lock_cm = (contextlib.nullcontext(True) if dry_run
               else _scan_lock(monitor_root))
    with lock_cm as _locked:
        if not _locked:
            logger.info("scanner: 已有扫描在跑，跳过本轮（跨进程锁）")
            return _empty_report()

        store = WatermarkStore(watermark_path)
        budget = DailyBudget(
            watchlist.daily_llm_budget_usd, dir=spend_dir, today=now_iso[:10])

        outcomes: list[TickerScanOutcome] = []
        for entry in watchlist.enabled_entries():
            try:
                outcome = _scan_one(
                    entry,
                    store=store, thesis_dir=thesis_dir, delta_dir=d_dir,
                    budget=budget, dry_run=dry_run, smoke=smoke, use_llm=use_llm,
                    events_fn=ev_fn, quote_fn=q_fn, update_fn=u_fn,
                    triggers_fn=trig_fn, now_iso=now_iso,
                )
            except Exception as e:  # noqa: BLE001 — 单票任何未捕获异常都不打断整轮
                logger.warning("scanner: %s 扫描异常: %s", entry.ticker, e)
                outcome = TickerScanOutcome(
                    ticker=entry.ticker, triggered=False, updated=False,
                    triggers=[], skipped_reason="error", delta=None, error=str(e),
                )
            outcomes.append(outcome)

        report = ScanReport(
            started_at=now_iso,
            tickers_scanned=len(outcomes),
            tickers_triggered=sum(1 for o in outcomes if o.triggered),
            tickers_updated=sum(1 for o in outcomes if o.updated),
            total_cost_usd=sum(o.cost_usd for o in outcomes),
            outcomes=outcomes,
        )
        _write_scan_report(report, scans_dir, now_iso)
        return report
