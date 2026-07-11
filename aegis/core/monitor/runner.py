"""In-process ``--update`` 复研触发器 — Aegis 2.0 Phase 3 任务 B2.

扫描器（:mod:`aegis.core.monitor.scanner`）检测到事件 / 监控点触发后，需要真的
跑一次增量复研（``--update``）。本模块把「跑一次复研」封成一个**永不 raise**的
函数 :func:`run_update`：给 ticker + 触发原因，new 一个
:class:`~aegis.core.orchestrator.auto_research.AutoResearchOrchestrator` 实例
跑增量流程，回报 run_id / entity_id / 本次成本（USD）/ 新论点版本号。

设计取舍：

- **延迟 import orchestrator**（重依赖，避免 import 本模块就把 3500 行主流程
  连同它的 LLM / 数据栈全拉进来；也让单元测试能只测容错分支不触发重依赖）。
- **每次 new 一个 orchestrator 实例**：orchestrator 的成本累计是「实例级」，
  每票独立实例 ⇒ ``last_run_cost_usd()`` 恰好等于「该票本次复研成本」，直接
  喂给每日预算熔断（:class:`~aegis.core.monitor.budget.DailyBudget`）。
- **向后兼容 update_trigger 字段**：主线 ResearchConfig 已有 ``update_trigger``，
  但为防止在旧分支上构造报 TypeError，用 try/except 兜底去掉该字段再构造。
- **永不 raise**：orchestrator.run 抛任何异常都吞掉，降级为
  ``ok=False`` + ``error`` 文本，绝不把异常冒泡到扫描器（否则一票崩全轮崩）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from aegis.core.thesis.persistence import load_latest

logger = logging.getLogger(__name__)

__all__ = ["UpdateRunResult", "run_update"]


@dataclass
class UpdateRunResult:
    """一次 ``--update`` 复研的结果（供扫描器计费 / 落 delta / 记扫描报告）。"""

    ticker: str
    ok: bool
    run_id: str = ""
    entity_id: str = ""
    cost_usd: float = 0.0
    thesis_version: int | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# 内部工具（全部容错）
# ---------------------------------------------------------------------------

def _build_config(research_config_cls: Any, ticker: str, *,
                  trigger_zh: str | None, smoke: bool, use_llm: bool) -> Any:
    """构造 ResearchConfig；``update_trigger`` 字段缺失（旧分支）时兜底去掉。"""
    base: dict[str, Any] = dict(
        ticker=ticker.upper(),
        period="latest",
        update_mode=True,
        use_llm=use_llm,
        smoke_mode=smoke,
        generate_html=True,
    )
    try:
        return research_config_cls(update_trigger=(trigger_zh or ""), **base)
    except TypeError:
        # 向后兼容：主线尚未补 update_trigger 字段时，去掉再构造。
        return research_config_cls(**base)


def _extract_cost(orchestrator: Any) -> float:
    """读本次复研成本（USD）：优先 ``last_run_cost_usd()``，否则 walk 缓存 client。

    永不 raise：任何一步失败都降级，最坏返回 0.0。
    """
    getter = getattr(orchestrator, "last_run_cost_usd", None)
    if callable(getter):
        try:
            return float(getter())
        except Exception as e:  # noqa: BLE001 — 成本读取失败不阻断
            logger.debug("runner: last_run_cost_usd() 失败，走兜底: %s", e)

    total = 0.0
    seen_ids: set[int] = set()
    for attr in ("_cached_llm_client", "_cached_fast_llm_client"):
        client = getattr(orchestrator, attr, None)
        if client is None or id(client) in seen_ids:
            continue
        seen_ids.add(id(client))
        tracker = getattr(client, "cost_tracker", None)
        if tracker is None:
            continue
        try:
            total += float(getattr(tracker, "total_cost_usd"))
        except Exception:  # noqa: BLE001 — 单个 tracker 坏值跳过
            continue
    return total


def _read_thesis_version(entity_id: str, thesis_dir: str | None) -> int | None:
    """读论点链最新版本号；无链 / 异常 → None。永不 raise。"""
    try:
        record = load_latest(entity_id, dir=thesis_dir)
    except Exception as e:  # noqa: BLE001 — 读链失败降级
        logger.debug("runner: load_latest(%s) 失败: %s", entity_id, e)
        return None
    if not isinstance(record, dict):
        return None
    version = record.get("version")
    return version if isinstance(version, int) else None


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------

def run_update(
    ticker: str,
    *,
    trigger_zh: str | None = None,
    smoke: bool = False,
    use_llm: bool = True,
    thesis_dir: str | None = None,
) -> UpdateRunResult:
    """跑一次增量复研（``--update``）并回报结果。永不 raise。

    Args:
        ticker: 标的代码（A 股 6 位数字 / 美股 symbol）。
        trigger_zh: 本次复研的中文触发原因（哪个监控点 / 事件触发的），透传给
            ResearchConfig.update_trigger，最终落进论点链的 version_change_trigger。
        smoke: 冒烟模式（HTML / 缓存重定向到 smoke 目录，不覆盖生产报告）。
        use_llm: 是否启用 LLM（关掉走 mock 模板，用于离线联调）。
        thesis_dir: 论点链目录（缺省 :data:`~aegis.core.thesis.persistence.DEFAULT_THESIS_DIR`）；
            读新版本号时透传。

    Returns:
        :class:`UpdateRunResult`：``ok`` 标识是否跑通；失败时 ``error`` 带原因。
    """
    ticker = str(ticker or "").strip()
    if not ticker:
        return UpdateRunResult(ticker="", ok=False, error="空 ticker")

    # 延迟 import：本模块被 import 时不牵动 3500 行主流程及其重依赖。
    try:
        from aegis.core.orchestrator.auto_research import (
            AutoResearchOrchestrator,
            ResearchConfig,
        )
    except Exception as e:  # noqa: BLE001 — 主流程 import 失败降级
        logger.warning("runner: 无法 import orchestrator: %s", e)
        return UpdateRunResult(
            ticker=ticker, ok=False, error=f"orchestrator import 失败: {e}")

    try:
        orchestrator = AutoResearchOrchestrator()
        config = _build_config(
            ResearchConfig, ticker,
            trigger_zh=trigger_zh, smoke=smoke, use_llm=use_llm,
        )
    except Exception as e:  # noqa: BLE001 — 构造失败降级
        logger.warning("runner: 构造 orchestrator/config 失败: %s", e)
        return UpdateRunResult(
            ticker=ticker, ok=False, error=f"构造失败: {e}")

    try:
        result = orchestrator.run(config)
    except Exception as e:  # noqa: BLE001 — 复研主流程抛异常一律吞掉
        logger.warning("runner: %s 复研失败: %s", ticker, e)
        cost = _extract_cost(orchestrator)
        entity_id = ""
        return UpdateRunResult(
            ticker=ticker, ok=False, entity_id=entity_id,
            cost_usd=cost, error=str(e),
        )

    entity_id = str(getattr(result, "entity_id", "") or "")
    run_id = str(getattr(result, "run_id", "") or "")
    cost = _extract_cost(orchestrator)
    version = _read_thesis_version(entity_id or ticker, thesis_dir)

    return UpdateRunResult(
        ticker=ticker,
        ok=True,
        run_id=run_id,
        entity_id=entity_id,
        cost_usd=cost,
        thesis_version=version,
    )
