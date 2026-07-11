"""事件循环监控包 — Aegis 2.0 Phase 3.

Phase 3 在「一次性出报告」之外补上**持续监控回路**：按 watchlist 票池
定期拉取公告 / 业绩预告 / 行情，与已落盘的论点（thesis 版本链）比对，
生成增量简报（delta）、扫描报告与复盘（postmortem）。

设计红线 10：不建正式状态机、不引入 async / 新第三方依赖，存储一律用
JSON 文件（与 thesis JSONL 风格一致）。

分层（import 方向自上而下，无环）::

    watchlist / watermark / delta / budget   —— 叶子（纯逻辑 + JSON 存储）
    triggers                                 —— 事件/监控点触发判定
    runner                                   —— in-process 触发 --update（延迟 import 主流程）
    scanner                                  —— 一轮扫描编排（心跳）
    postmortem                               —— 90 天回看

.. note::
   ``runner`` 只在 :func:`run_update` 函数体内延迟 import 3500 行主流程，
   故 ``import aegis.core.monitor`` 本身是轻量的（不会拉起 orchestrator）。
"""

from __future__ import annotations

from aegis.core.monitor.budget import DailyBudget, SpendRecord
from aegis.core.monitor.delta import (
    DeltaBriefing,
    FieldChange,
    diff_theses,
    summarize_change,
)
from aegis.core.monitor.postmortem import (
    POSTMORTEM_DIR,
    build_postmortem,
    due_records,
    run_postmortems,
)
from aegis.core.monitor.runner import UpdateRunResult, run_update
from aegis.core.monitor.scanner import (
    DELTA_DIR,
    SCAN_REPORT_DIR,
    ScanReport,
    TickerScanOutcome,
    scan_once,
)
from aegis.core.monitor.triggers import (
    Trigger,
    active_model_ids,
    evaluate_triggers,
)
from aegis.core.monitor.watchlist import (
    DEFAULT_WATCHLIST_PATH,
    MonitorSwitches,
    Watchlist,
    WatchlistEntry,
    load_watchlist,
)
from aegis.core.monitor.watermark import (
    TickerWatermark,
    WatermarkStore,
    announcement_key,
    diff_new,
    forecast_key,
    merge_seen,
)

__all__ = [
    # watchlist
    "MonitorSwitches", "WatchlistEntry", "Watchlist", "load_watchlist",
    "DEFAULT_WATCHLIST_PATH",
    # watermark
    "TickerWatermark", "WatermarkStore", "announcement_key", "forecast_key",
    "diff_new", "merge_seen",
    # delta
    "FieldChange", "DeltaBriefing", "diff_theses", "summarize_change",
    # budget
    "SpendRecord", "DailyBudget",
    # triggers
    "Trigger", "active_model_ids", "evaluate_triggers",
    # runner
    "UpdateRunResult", "run_update",
    # scanner
    "ScanReport", "TickerScanOutcome", "scan_once", "DELTA_DIR",
    "SCAN_REPORT_DIR",
    # postmortem
    "build_postmortem", "due_records", "run_postmortems", "POSTMORTEM_DIR",
]
