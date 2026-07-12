"""Auto Research Orchestrator — ticker to full report, fully automated.

Pipeline:
  1. SECEntityRegistry.get_cik(ticker) → CIK
  2. SECEDGARConnector.fetch() → RawDataPacket (XBRL facts)
  3. USMarketAdapter.adapt_filing_data() → canonical concepts
  4. FactNormalizationBridge.normalize() → meta_facts
  5. FormulaEngine.compute() → computed_metrics
  6. DCFEngine.compute_dcf() → valuation (bear/base/bull)
  7. SensitivityAnalyzer → sensitivity rankings + 2-way table
  8. ReverseDCFSolver → implied growth
  9. MacroContextLayer + MarketExpectationsLayer → macro context
  10. 7 Agents → judgments
  11. 7 Critics → reviews
  12. PublishGate → publish decision
  13. DecisionEngine → thesis
  14. PortfolioIntegration → signal
  15. ReportSerializer + HTMLReport → output
"""

from __future__ import annotations

import re as _re

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# AUDIT-D1/D4: caps and timeouts are backend-tiered now — the orchestrator
# resolves the backend kind once per run and asks config for the tier.
from aegis.core.config import (
    agent_batch_timeout_for,
    agent_max_parallel_for,
    agent_watchdog_timeout_for,
)


# Risk-warning prefixes used by Chinese A-share exchanges. Listed in
# longest-first order so "*ST中珠" matches "*ST" before "ST". Lowercase
# variants are accepted for safety; the canonical form is uppercase.
_A_SHARE_RISK_PREFIXES = ("*ST", "ST", "*st", "st")


def normalize_entity_display(name: str) -> tuple[str, str]:
    """Split an entity display name into (clean_name, risk_warning_prefix).

    A-share companies under regulatory risk warning carry a prepended ST
    / *ST tag (e.g. "ST中珠", "*ST华仪"). The tag is exchange metadata
    rather than part of the company name, and conflating them broke
    multiple downstream consumers (badge rendering picked up "S" as the
    ticker initial; fuzzy name matching mismatched against canonical
    registries; sector inference saw "ST..." as a unique entity). Surface
    the prefix as its own first-class field so each consumer can decide
    whether to display it as a badge, ignore it, or use it.

    Returns a 2-tuple `(entity_name_clean, risk_warning_prefix)`. For
    non-A-share or unwarned names the prefix is empty string.
    """
    if not name:
        return ("", "")
    for pfx in _A_SHARE_RISK_PREFIXES:
        if name.startswith(pfx):
            return (name[len(pfx):].lstrip(), pfx.upper())
    return (name, "")


# ─────────────────────────────────────────────────────────────────────
# Aegis 2.0 Phase 2 (任务 C1/C2): 最小 stage 化 checkpoint + 输入摘要 digest
#
# DESIGN_2.0 §五 Phase 2 第 1 项：沿 replay_from_cache 已验证的缓存缝，把
# 管线切成 data → valuation → agents → report 四个可断点续跑 checkpoint。
# **不搬代码块、不拆函数**——run() 里只在四个缝上插桩落盘；真正的单体
# 拆解是 Phase 4 的事（红线 7 挂闸）。
#
# digest 语义（--update 增量复用的判据）：对 stage 的输入做 JSON 规范化
# 序列化后 sha256。agents stage 的输入 = meta_facts 基本面子集 + 事件块
# （__recent_events，含在 meta_facts 里）+ sector pack；**显式排除实时
# 价格、时间戳、行情市值与一切价格衍生键**——盘中价格抖动不应作废昨天
# 的深度分析（价格照旧进当日报告：valuation/report 永远重算/重渲染）。
# ─────────────────────────────────────────────────────────────────────

STAGE_NAMES = ("data", "valuation", "agents", "report")

#: agents/valuation digest 排除的 meta_facts 顶层键（价格衍生 / 渲染时点
#: 重算 / 纯 prompt 文本内嵌了摄取日期的重复体）。基本面子集 = 其余全部。
AGENTS_DIGEST_EXCLUDE_KEYS: frozenset[str] = frozenset({
    "__expectations_frontier",     # 反解自现价（Step 7d）
    "__pricing_regime",            # 现价 vs 估值缺口驱动（Step 7d）
    "__relative_valuation",        # 同业 PE/PB 分位——行情驱动
    "__data_freshness",            # 「数据截至」时效天数按渲染时点漂移
    "__implied_growth_unreliable",  # reverse-DCF vs 现价的派生标志
    "__implied_growth_boundary_hit",
    "__recent_events_prompt",      # 与 __recent_events 同源，文本内嵌 as_of 日期
})

#: 递归剔除的易变键名模式（任意层级，大小写不敏感）：实时价格、行情市值、
#: 摄取时间戳。注意不匹配 announce_date 之类的事件日期——事件是基本面。
_VOLATILE_KEY_PAT = _re.compile(
    r"price|market_cap|as_of|asof|timestamp|fetched|retrieved|quote",
    _re.IGNORECASE,
)


def _strip_volatile_keys(obj: Any) -> Any:
    """递归剔除易变键（dict 键名命中 _VOLATILE_KEY_PAT 即整枝丢弃）。"""
    if isinstance(obj, dict):
        return {
            str(k): _strip_volatile_keys(v)
            for k, v in obj.items()
            if not _VOLATILE_KEY_PAT.search(str(k))
        }
    if isinstance(obj, (list, tuple)):
        return [_strip_volatile_keys(v) for v in obj]
    return obj


def compute_stage_digest(payload: Any) -> str:
    """任意 JSON-able payload → 规范化 JSON → sha256 hex。永不 raise。"""
    import hashlib
    import json as _json
    try:
        text = _json.dumps(
            payload, sort_keys=True, ensure_ascii=False, default=str,
        )
    except Exception:
        text = str(payload)
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def agents_digest_payload(
    meta_facts: dict[str, Any] | None,
    sector_pack: Any = None,
    period: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """agents stage 的输入摘要 payload（digest 前的规范化视图）。

    = meta_facts 基本面子集（排除价格衍生键 + 递归剔除时间戳/行情键；
    事件块 __recent_events 含在其中）+ sector pack + 会计期。
    """
    mf = {
        k: v for k, v in (meta_facts or {}).items()
        if k not in AGENTS_DIGEST_EXCLUDE_KEYS
    }
    payload: dict[str, Any] = {
        "period": period,
        "sector_pack": _strip_volatile_keys(sector_pack)
        if isinstance(sector_pack, (dict, list)) else str(sector_pack),
        "meta_facts": _strip_volatile_keys(mf),
    }
    if extra:
        payload["extra"] = _strip_volatile_keys(dict(extra))
    return payload


def compute_agents_digest(
    meta_facts: dict[str, Any] | None,
    sector_pack: Any = None,
    period: str | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    """agents stage 输入 digest（C2 复用判据）。"""
    return compute_stage_digest(
        agents_digest_payload(meta_facts, sector_pack, period, extra=extra)
    )


def stage_checkpoint_dir(ticker: str, *, smoke_mode: bool = False) -> Path:
    """checkpoint 目录：.cache/stages/{ticker}/（smoke 隔离到 .cache/smoke/，
    测试可用 AEGIS_STAGE_DIR 整体重定向）。"""
    import os
    root = os.environ.get("AEGIS_STAGE_DIR", "").strip()
    if root:
        base = Path(root)
    elif smoke_mode:
        base = Path(".cache/smoke/stages")
    else:
        base = Path(".cache/stages")
    return base / str(ticker).lower()


def dump_stage_checkpoint(
    ticker: str,
    stage: str,
    payload: dict[str, Any],
    *,
    digest: str,
    run_id: str = "",
    smoke_mode: bool = False,
    log: Any = None,
) -> Path | None:
    """落一个 stage checkpoint（pkl）。永不 raise；失败返回 None。

    防 RLock 净化（replay cache 的教训）：先整体 pickle 探测，失败则逐键
    兜底——不可序列化的键置 None，其余照存；runtime 句柄键直接剔除。
    """
    import pickle
    try:
        cleaned: dict[str, Any] = {
            k: v for k, v in dict(payload).items()
            if k not in ("shared_llm_client",)  # 已知 runtime 句柄
        }
        try:
            pickle.dumps(cleaned)
        except Exception:
            dropped = []
            for k in list(cleaned.keys()):
                try:
                    pickle.dumps(cleaned[k])
                except Exception:
                    dropped.append(k)
                    cleaned[k] = None
            if log is not None and dropped:
                log(f"  ⚠ Stage checkpoint [{stage}]: dropped unpicklable keys {dropped}")
        record = {
            "stage": str(stage),
            "digest": str(digest),
            "run_id": str(run_id or ""),
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "payload": cleaned,
        }
        out_dir = stage_checkpoint_dir(ticker, smoke_mode=smoke_mode)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{stage}.pkl"
        with out_path.open("wb") as f:
            f.write(pickle.dumps(record))
        if log is not None:
            log(f"💾 Stage checkpoint [{stage}] saved: {out_path} "
                f"(digest={record['digest'][:12]}…)")
        return out_path
    except Exception as e:  # noqa: BLE001 — 插桩永不阻断主流程
        if log is not None:
            log(f"  ⚠ Stage checkpoint [{stage}] dump failed: {e}")
        return None


def load_stage_checkpoint(
    ticker: str,
    stage: str,
    *,
    smoke_mode: bool = False,
    expected_digest: str | None = None,
    log: Any = None,
) -> dict[str, Any] | None:
    """读一个 stage checkpoint record。缺文件 / 反序列化失败 / digest 不匹配
    → None（调用方按「正常重跑该 stage」处理）。永不 raise。"""
    import pickle
    try:
        path = stage_checkpoint_dir(ticker, smoke_mode=smoke_mode) / f"{stage}.pkl"
        if not path.exists():
            return None
        with path.open("rb") as f:
            record = pickle.load(f)
        if not isinstance(record, dict) or "payload" not in record:
            return None
        if expected_digest is not None and record.get("digest") != expected_digest:
            if log is not None:
                log(f"  Stage checkpoint [{stage}]: digest 不一致"
                    f"（{str(record.get('digest'))[:12]}… ≠ {expected_digest[:12]}…），"
                    f"该 stage 将正常重跑")
            return None
        return record
    except Exception as e:  # noqa: BLE001
        if log is not None:
            log(f"  ⚠ Stage checkpoint [{stage}] load failed: {e}")
        return None


def _currency_symbol_for_logs(meta_facts: dict[str, Any] | None) -> str:
    """Resolve currency symbol for console-only log lines.

    Refactor 2 unified rendering layers via `meta_facts["__display"]`, but
    a few orchestrator log statements still hardcoded `$`. For A-share runs
    (CNY) this prints `$0.05` for what is really `¥0.05/股` and confuses
    diagnostics. Centralise the symbol lookup here so future log additions
    don't reintroduce the same drift.
    """
    if not meta_facts:
        return "$"
    disp = meta_facts.get("__display") or {}
    sym = disp.get("symbol")
    if sym:
        return sym
    cur = meta_facts.get("__currency") or "USD"
    return {"CNY": "¥", "JPY": "¥", "EUR": "€", "GBP": "£"}.get(cur, "$")


def _fmt_duration(delta: timedelta) -> str:
    """Format a timedelta: '45s' / '24m 37s' / '2h 15m'."""
    total = int(delta.total_seconds())
    if total < 0:
        total = 0
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


# AUDIT-A4 (2026-07): shared cumulative-growth cap, extracted from the
# inline BUG-Y23 block in `_build_dcf_input` so the bear/bull driver_deltas
# path (`_driver_adjusted_growth`) can apply the exact same bound. Before
# this, only the base path was capped at 30× — a hyper-growth bear tree
# would compound past base, trip the inversion guard, and get silently
# rewritten to a mechanical 0.5× base.
MAX_TERMINAL_RATIO = 30.0


def max_cumulative_growth_ratio(base_revenue: float, is_cny: bool) -> float:
    """AUDIT 2026-07-12 R3-1：规模感知的累计增长封顶（Grok Round-2 复审）。

    统一 30× 封顶对小基数超成长股（寒武纪 ¥65亿）是合理的防爆栓，但对
    巨头是幻想通行证：宁德 ¥4237亿 营收按 30× 封顶合法地滚到 ¥12.7万亿
    （≈中国 GDP 的 1/10），DCF base ¥4064 vs 市价 ¥349（11.65×），审计
    判词"估值体系实质性破产、用坏模型反解市场预期=循环论证"。

    十年营收倍数的历史基率强烈依赖起始规模——按规模分档取近似历史
    top-decile 上限：
      mega（≥¥2000亿 / $30B）  → 3×（≈11.6% 十年 CAGR，巨头顶配）
      large（≥¥500亿 / $7B）   → 6×（≈19.6%）
      mid（≥¥100亿 / $1.4B）   → 12×（≈28.2%）
      small（其余，如寒武纪 ¥65亿）→ 30×（原防爆栓，超成长可达）
    """
    if not isinstance(base_revenue, (int, float)) or base_revenue <= 0:
        return MAX_TERMINAL_RATIO
    t_mega, t_large, t_mid = (
        (2000e8, 500e8, 100e8) if is_cny else (30e9, 7e9, 1.4e9)
    )
    if base_revenue >= t_mega:
        return 3.0
    if base_revenue >= t_large:
        return 6.0
    if base_revenue >= t_mid:
        return 12.0
    return MAX_TERMINAL_RATIO


def cap_cumulative_growth_path(
    growth_path: list[float],
    terminal_growth: float,
    max_ratio: float = MAX_TERMINAL_RATIO,
) -> tuple[list[float], int]:
    """Cap a revenue growth path so cumulative scaling stays plausible.

    Walks the path compounding (1+g); the first year whose cumulative scale
    exceeds ``max_ratio`` and every year after it are replaced with
    ``terminal_growth + 0.005`` (same convention as the original BUG-Y23
    fix). Returns ``(capped_path, capped_year)`` where ``capped_year`` is
    the 0-indexed year the cap first triggered, or ``-1`` if untouched.
    Never mutates the input list.
    """
    cum_scale = 1.0
    capped_year = -1
    for yr_idx, g in enumerate(growth_path):
        cum_scale *= (1 + g)
        if cum_scale > max_ratio:
            capped_year = yr_idx
            break
    if capped_year < 0:
        return list(growth_path), -1
    capped = list(growth_path)
    for yr_idx in range(capped_year, len(capped)):
        capped[yr_idx] = round(terminal_growth + 0.005, 4)
    return capped, capped_year


def renormalize_scenario_probabilities(
    probs: dict[str, float],
    log: Callable[[str], None] | None = None,
) -> dict[str, float]:
    """AUDIT-A5 (orchestrator side): force bear/base/bull weights to sum to 1.

    The ScenarioArchitect only normalizes the cases the LLM actually
    returned; a dropped/renamed case keeps its orchestrator default
    (0.25/0.50/0.25) so the three weights can sum to 1.25-1.5 and silently
    inflate the probability-weighted target price. Warn when the drift
    exceeds 1% and divide through by the total. Degenerate totals (≤ 0)
    fall back to the mechanical default split.
    """
    total = sum(probs.values())
    if total <= 0:
        if log:
            log(f"  ⚠ Scenario probabilities sum to {total:.3f} — resetting to default 0.25/0.50/0.25")
        return {"bear": 0.25, "base": 0.50, "bull": 0.25}
    if abs(total - 1.0) > 0.01 and log:
        log(f"  ⚠ Scenario probabilities sum to {total:.3f} ≠ 1.0 — renormalizing before pw_value")
    return {k: v / total for k, v in probs.items()}


# ═══ Aegis 2.0 Phase 0 — 预期前沿 / 定价体制接线辅助 ═══════════════════
#
# DESIGN_2.0 §三.A：报告主轴从「DCF 说值 X，市场错了 Y%」转为「当前价格
# 隐含了什么预期？该预期与可验证事实是否相容？」。下面的纯函数负责把
# 现有管线数据映射成 solve_expectations_frontier / assess_pricing_regime
# 的输入——保持纯函数便于单测（test_phase0_wiring.py）。

# 行业档缺省利润率：sector pack 没有典型利润率字段时的兜底（任务规格）。
DEFAULT_SECTOR_TYPICAL_MARGIN = 0.08

# pw_value ≤ 0 时 DCF 缺口无法定义为比值——现价为正而概率加权价值非正，
# 属于「现价与已实现现金流彻底脱钩」的极端溢价形态，直接喂给体制感知
# 其 clip 上限（assess_pricing_regime 内部 clip 到 10.0）。
_EXTREME_GAP_SENTINEL = 10.0


def _sector_typical_margin(sector_pack: dict | None) -> float:
    """从 sector pack yaml 里读「行业典型营业利润率」（取区间中点）。

    优先 valuation_framework.typical_operating_margin_range，其次
    benchmarks.typical_margins.operating_margin；两者都没有则返回
    行业档缺省 8%（DEFAULT_SECTOR_TYPICAL_MARGIN）。
    """
    pack = sector_pack or {}
    for path in (
        ("valuation_framework", "typical_operating_margin_range"),
        ("benchmarks", "typical_margins", "operating_margin"),
    ):
        node: Any = pack
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
            if node is None:
                break
        if isinstance(node, (list, tuple)) and len(node) >= 2:
            try:
                lo, hi = float(node[0]), float(node[1])
            except (TypeError, ValueError):
                continue
            return (lo + hi) / 2.0
        if isinstance(node, (int, float)):
            return float(node)
    return DEFAULT_SECTOR_TYPICAL_MARGIN


def _sector_margin_has_real_value(sector_pack: dict | None) -> bool:
    """sector pack 里是否存在**真实**的行业典型营业利润率。

    区别于 :func:`_sector_typical_margin` 的缺省 8%（General pack 假锚）。
    只有 pack 明确给出 valuation_framework / benchmarks 的利润率时才 True。
    """
    pack = sector_pack or {}
    for path in (
        ("valuation_framework", "typical_operating_margin_range"),
        ("benchmarks", "typical_margins", "operating_margin"),
    ):
        node: Any = pack
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
            if node is None:
                break
        if isinstance(node, (list, tuple)) and len(node) >= 2:
            try:
                float(node[0]); float(node[1])
                return True
            except (TypeError, ValueError):
                continue
        if isinstance(node, (int, float)):
            return True
    return False


def build_margin_scenarios(
    current_margin: float,
    sector_pack: dict | None,
    zh: bool,
) -> list[tuple[str, float]]:
    """构造预期前沿的 2-3 档终年利润率情景（Phase 0 接线规格）。

    档位：维持现状（当前利润率）/ 行业中位（sector pack 典型利润率）/ 两者中点。
    当前与行业档几乎重合（<0.5pp）时去重，只留两档。标签 zh/en 双语由调用方
    按市场选择（中文化铁律）。

    **假锚防护（Grok 评审 2026-07-11 §3.4 + 校准闭环实证）**：落 General pack
    （无真实行业利润率数据）时**只出「维持现状」一档**——不把缺省 8% 当「行业中位」
    展示（否则「行业中位 8%→需 +X% 增速」看着像 cross-check 实为假锚，比单点隐含
    增速更危险）。有真实 pack 的名字（茅台白酒/寒武纪半导体等）行为不变。
    """
    cur = float(current_margin)
    labels = (
        ("维持现状", "行业中位", "两者中点") if zh
        else ("Hold current margin", "Sector median", "Midpoint")
    )
    # General / 无真实行业利润率 → 只出维持现状档，不出 8% 假锚。
    if not _sector_margin_has_real_value(sector_pack):
        return [(labels[0], round(cur, 4))]
    sector_m = _sector_typical_margin(sector_pack)
    # AUDIT 2026-07-12 R2（Grok round-1 复审 002371/002594）：假锚防护扩展。
    # sector pack 按行业大类挂载，中位数可能来自完全不同的商业模式（半导体
    # pack 的 50% 是 fabless 口径，套到设备商 14.7% OPM 头上就成了"行业中位
    # 50%→市场只给零改善定价"的幻想上行锚）。判据用**绝对差 >25pp**：修复
    # 股（康达 2.9%→行业 20%）的合理修复情景保留；跨商业模式的幻想锚
    # （+35pp 到 fabless 利润率）剔除。向下的行业档（均值回归情景）不受限。
    if sector_m - cur > 0.25:
        return [(labels[0], round(cur, 4))]
    if abs(cur - sector_m) < 0.005:
        return [(labels[0], round(cur, 4))]
    mid = (cur + sector_m) / 2.0
    return [
        (labels[0], round(cur, 4)),
        (labels[1], round(sector_m, 4)),
        (labels[2], round(mid, 4)),
    ]


def compute_pricing_regime_inputs(
    meta_facts: dict[str, Any],
    computed_metrics: dict[str, Any],
    market_price: float,
    pw_value: float,
    terminal_value_gate_triggered: bool,
) -> dict[str, Any]:
    """把管线已算好的量映射成 assess_pricing_regime 的关键字参数。

    特征口径（与 pricing_regime docstring 对齐）：
    - dcf_gap: (price − pw_value) / pw_value；pw_value ≤ 0 时给极端溢价
      哨兵值（clip 上限），不猜。
    - fcf_positive: 优先 meta_facts.free_cash_flow，退 computed
      fcf_simple，再退 CFO；全缺按 False（保守）。
    - cfo_to_ni: 净利润 ≤ 0 时口径失效传 None。
    - growth_regime_break: 最新一年 YoY 与历史 CAGR 的背离幅度；CAGR
      不可靠（__revenue_cagr_unreliable）或数据缺失时传 False。
    - net_debt_to_ebitda: EBITDA ≤ 0 传 None。
    """
    price = float(market_price or 0.0)
    pw = float(pw_value or 0.0)
    if pw > 0 and price > 0:
        dcf_gap = price / pw - 1.0
    elif price > 0:
        dcf_gap = _EXTREME_GAP_SENTINEL
    else:
        dcf_gap = 0.0

    fcf = meta_facts.get("free_cash_flow")
    if not isinstance(fcf, (int, float)):
        fcf = computed_metrics.get("fcf_simple")
    if not isinstance(fcf, (int, float)):
        fcf = meta_facts.get("cfo") or meta_facts.get("operating_cash_flow")
    fcf_positive = bool(isinstance(fcf, (int, float)) and fcf > 0)

    accruals = computed_metrics.get("accruals_ratio")
    cfo_to_ni = computed_metrics.get("cfo_to_net_income")
    ni = meta_facts.get("net_income")
    if isinstance(ni, (int, float)) and ni <= 0:
        cfo_to_ni = None  # 净利润非正时 CFO/NI 口径失效（不猜）

    growth_break: float | bool = False
    hist = meta_facts.get("__historical_growth") or {}
    cagr = meta_facts.get("__revenue_cagr")
    if (
        isinstance(hist, dict) and hist
        and isinstance(cagr, (int, float))
        and not meta_facts.get("__revenue_cagr_unreliable")
    ):
        try:
            latest_yoy = float(hist[max(hist.keys())])
            growth_break = abs(latest_yoy - float(cagr))
        except (TypeError, ValueError):
            growth_break = False

    net_debt = computed_metrics.get("net_debt")
    if not isinstance(net_debt, (int, float)):
        net_debt = meta_facts.get("net_debt")
    ebitda = meta_facts.get("ebitda")
    nd_to_ebitda = None
    if (
        isinstance(net_debt, (int, float))
        and isinstance(ebitda, (int, float)) and ebitda > 0
    ):
        nd_to_ebitda = float(net_debt) / float(ebitda)

    return {
        "dcf_gap": dcf_gap,
        "fcf_positive": fcf_positive,
        "accruals_ratio": accruals if isinstance(accruals, (int, float)) else None,
        "cfo_to_ni": cfo_to_ni if isinstance(cfo_to_ni, (int, float)) else None,
        "growth_regime_break": growth_break,
        "net_debt_to_ebitda": nd_to_ebitda,
        "terminal_value_gate_triggered": bool(terminal_value_gate_triggered),
    }


# ═══ Aegis 2.0 Phase 1 — A 股季报 PIT / TTM / 数据时效接线辅助 ══════════
#
# DESIGN_2.0 §三.B（信息架构）：新数据从第一天写 PIT 层（sqlite3），
# 不进 meta_facts 自由键。唯一例外（红线 8 的显式豁免，见任务 D1）：
# 衍生 TTM 汇总值走 3 个固定键 ttm_revenue / ttm_net_income /
# ttm_net_income_deducted，外加 __data_freshness 时效标注——除此之外
# 本步骤不得再新增任何 meta_facts 键。
# 全部函数永不 raise（静默降级铁律：无季报 → 年报口径照旧）。

#: 红线 8 豁免清单——Phase 1 允许新增的 meta_facts 固定键（衍生 TTM 汇总值）。
TTM_META_KEYS = ("ttm_revenue", "ttm_net_income", "ttm_net_income_deducted")


def build_data_freshness(
    latest_period: str, *, basis: str, announce_date: str | None = None,
) -> dict[str, Any]:
    """构造 ``meta_facts["__data_freshness"]``（最新报告期 + 时效天数）。

    basis: ``"quarterly"``（季报口径）| ``"annual"``（降级：年报口径）。

    时效口径（红线 #3 的披露日语义）：``announce_date``（披露日）可得时
    days_since 按披露日计——「时效」回答的是「我们手里最新的公开信息
    面世多久了」。季报期末 +90 天到下一次披露之间，期末口径必然 >90 天，
    但只要最新披露仍是市场上最新的可得信息，数据并不陈旧；反之披露口径
    一旦 >90 天说明真错过了披露节点，才是该报警的情形。披露日缺失退回
    报告期末口径。解析失败 days_since=None（渲染层显示「暂无」不烂值）。
    """
    from datetime import date as _date
    period_s = str(latest_period)[:10]
    announce_s = str(announce_date)[:10] if announce_date else None
    days: int | None = None
    for anchor in (announce_s, period_s):
        if not anchor:
            continue
        try:
            days = (_date.today() - _date.fromisoformat(anchor)).days
            break
        except ValueError:
            continue
    out: dict[str, Any] = {
        "latest_period": period_s, "basis": basis, "days_since": days,
    }
    if announce_s:
        out["announce_date"] = announce_s
    return out


def _latest_announce_date(store: Any, entity_id: str, period: str) -> str | None:
    """PIT 库中该报告期事实的最新披露日（无库/无值/失败 → None）。"""
    if store is None:
        return None
    try:
        dates = [
            f.announce_date for f in store.get_facts(entity_id)
            if f.period == str(period)[:10] and f.announce_date
        ]
        return max(dates) if dates else None
    except Exception:  # noqa: BLE001 — 只读辅助，失败静默降级
        return None


def _coerce_ttm_snapshot(snap: Any) -> dict[str, Any]:
    """把 TTM 引擎返回值（dataclass / dict / None）容错归一成 plain dict。"""
    if snap is None:
        return {}
    if isinstance(snap, dict):
        return snap
    if hasattr(snap, "to_dict"):
        try:
            d = snap.to_dict()
            if isinstance(d, dict):
                return d
        except Exception:
            pass
    out: dict[str, Any] = {}
    for key in (*TTM_META_KEYS, "latest_period", "as_of_period"):
        val = getattr(snap, key, None)
        if val is not None:
            out[key] = val
    return out


def _default_ttm_snapshot(store: Any, entity_id: str) -> Any:
    """TTM 引擎适配器（Wave 3 seam）。

    期望接口：``aegis.core.truth.ttm_engine`` 暴露
    ``ttm_snapshot(store, entity_id)``（或 compute_/build_ 前缀变体），
    返回含 ttm_revenue / ttm_net_income / ttm_net_income_deducted /
    latest_period 的 dataclass 或 dict（红线 4：只对流量科目、累计差分、
    归母/扣非双轨——口径由引擎负责，这里只做读出）。
    引擎尚未落地（ImportError）→ 返回 None，管线降级为年报口径。
    """
    try:
        from aegis.core.truth import ttm_engine  # type: ignore[attr-defined]
    except ImportError:
        return None
    for name in ("ttm_snapshot", "compute_ttm_snapshot", "build_ttm_snapshot"):
        fn = getattr(ttm_engine, name, None)
        if callable(fn):
            return fn(store, entity_id)
    return None


def run_quarterly_pit_step(
    ticker: str,
    *,
    smoke_mode: bool = False,
    store: Any | None = None,
    ingest_fn: Callable[..., Any] | None = None,
    ttm_fn: Callable[[Any, str], Any] | None = None,
    log: Callable[[str], None] = lambda _m: None,
) -> tuple[Any | None, dict[str, Any]]:
    """A 股季报连接器拉数写 PIT → TTM 快照读出（任务 D1 的可测缝）。

    Returns ``(pit_store, meta_updates)``。**永不 raise**：
    - PIT store 建库失败 → ``(None, {})``；
    - 摄取失败 → store 照常返回（供验证点核验读历史库），无 TTM 键；
    - TTM 引擎缺席/失败 → 只缺 ttm_* 键，__data_freshness 仍可由
      摄取到的最新报告期派生。
    meta_updates 只含 :data:`TTM_META_KEYS` + ``__data_freshness``（红线 8）。

    Parameters
    ----------
    store / ingest_fn / ttm_fn: 测试注入缝；生产默认分别为
        PITStore(.cache/pit.db)、quarterly_cn.ingest_quarterly、
        :func:`_default_ttm_snapshot`。
    """
    import math as _math

    updates: dict[str, Any] = {}
    clean = ticker.strip().upper()
    for suffix in (".SZ", ".SS", ".SH"):
        clean = clean.replace(suffix, "")

    if store is None:
        try:
            from aegis.pit.store import DEFAULT_DB_PATH, PITStore
            db_path = (
                Path(".cache") / "smoke" / "pit.db" if smoke_mode
                else DEFAULT_DB_PATH
            )
            store = PITStore(db_path)
        except Exception as e:  # noqa: BLE001 — 静默降级铁律
            log(f"  ⚠ PIT store unavailable ({type(e).__name__}: {e}) — "
                f"quarterly step skipped, annual-report basis retained")
            return None, updates

    periods: list[str] = []
    try:
        if ingest_fn is None:
            from aegis.core.acquisition.connectors.quarterly_cn import (
                ingest_quarterly as ingest_fn,
            )
        res = ingest_fn(store, clean)
        periods = [str(p) for p in (getattr(res, "periods", None) or [])]
        errors = getattr(res, "errors", None) or []
        log(f"Quarterly PIT: {getattr(res, 'facts_written', 0)} facts across "
            f"{len(periods)} periods (errors={len(errors)})")
    except Exception as e:  # noqa: BLE001 — 静默降级铁律
        log(f"  ⚠ Quarterly ingest failed ({type(e).__name__}: {e}) — "
            f"annual-report basis retained")

    snap_dict: dict[str, Any] = {}
    try:
        snap = (ttm_fn or _default_ttm_snapshot)(store, clean)
        snap_dict = _coerce_ttm_snapshot(snap)
        if not snap_dict:
            log("  ⚠ TTM engine unavailable — ttm_* keys skipped "
                "(Wave 3 seam, degrades to annual basis)")
    except Exception as e:  # noqa: BLE001 — 静默降级铁律
        log(f"  ⚠ TTM snapshot failed ({type(e).__name__}: {e})")

    for key in TTM_META_KEYS:
        val = snap_dict.get(key)
        if isinstance(val, (int, float)) and _math.isfinite(val):
            updates[key] = float(val)

    latest_period = snap_dict.get("latest_period") or snap_dict.get("as_of_period")
    if not latest_period and periods:
        latest_period = max(periods)
    if latest_period:
        updates["__data_freshness"] = build_data_freshness(
            str(latest_period), basis="quarterly",
            announce_date=_latest_announce_date(store, clean, str(latest_period)),
        )
    return store, updates


@dataclass
class ResearchConfig:
    """Configuration for an automated research run."""

    ticker: str
    period: str = "latest"  # "latest" auto-detects most recent 10-K; or "FY2025" etc.
    filing_type: str = "10-K"

    # Market data (must be provided externally — no market data connector yet)
    current_price: float | None = None
    market_cap: float | None = None

    # Macro snapshot overrides (optional)
    cycle_phase: str = "late_expansion"
    fed_funds_rate: float = 0.0425
    us_10y_yield: float = 0.043

    # Sector pack ID (auto-detected if None)
    sector_pack_id: str | None = None

    # DCF assumption overrides (if None, use defaults)
    revenue_growth_path: list[float] | None = None
    operating_margin_path: list[float] | None = None
    capex_to_revenue_path: list[float] | None = None
    wacc: float = 0.095
    terminal_growth_rate: float = 0.03
    effective_tax_rate: float = 0.21

    # LLM mode
    use_llm: bool = False
    llm_model: str = "sonnet"  # "sonnet", "opus", "haiku"
    llm_backend: str = "auto"  # "auto", "deepseek", "grok", "sdk", "subprocess"
    deepseek_api_key: str | None = None
    deepseek_model: str = "deepseek-v4-pro"  # flagship; "deepseek-v4-flash" for cheaper/faster
    grok_api_key: str | None = None  # falls back to GROK_API_KEY / XAI_API_KEY env
    grok_model: str | None = None  # None → GROK_MODEL env, else "grok-4"
    fast_agents: bool = False  # Use cheaper model for specialist agents
    fast_agent_model: str = "deepseek-v4-flash"  # Cheaper/faster model for agents
    # TODO-X3: --fast pipeline shortcut. When True, Research Director's per-
    # agent DEEP designations are overridden to "standard" (no
    # narrative_supplement, smaller schema, ~50-70% faster per call). Used
    # for iteration / debugging where the rich narrative memos aren't worth
    # the 25-40 min wall-clock cost. Independent of fast_agents (model swap).
    fast_pipeline: bool = False
    # TODO-6: split each agent's single 25K-token call into two shorter
    # calls — first observations only, then inferences/counterarguments/
    # triggers conditioned on those observations. Each shorter call has
    # smaller reasoning overhead so wall-clock often drops 8-15 min → 5-8
    # min per agent. Off by default since 2x round-trips cost more on
    # cheap-but-fast prompts; turn on for A-share DEEP runs where each
    # call is currently bottlenecked on output thinking.
    split_prompts: bool = False
    # TODO-5 (2026-04-24): when True, agents that exhaust LLM retries raise
    # instead of silently falling back to mock templates. Use for production
    # runs where mock-mixed reports are worse than no report.
    strict_llm: bool = False
    # TODO-1 (2026-04-24): smoke mode — redirects HTML and replay cache to
    # `demos/smoke/` and `.cache/smoke/` so plumbing tests don't clobber
    # production reports. Toggled by demos/auto_research_demo.py --smoke.
    smoke_mode: bool = False

    # Output
    output_dir: str | None = None
    generate_html: bool = True

    # OpenBB data enrichment
    fmp_api_key: str | None = None
    fred_api_key: str | None = None
    enable_openbb: bool = True       # Auto-fetch consensus, macro, peers

    # SEC API
    sec_user_agent: str | None = None

    # News sentiment
    enable_news_sentiment: bool = True

    # Aegis 2.0 Phase 0: A 股近事件切片（公告标题 + 业绩预告 + 一致预期）。
    # 事实源注入 agent prompt，防止 LLM 幻觉「并购故事」。离线测试可关。
    enable_recent_events: bool = True

    # Aegis 2.0 Phase 1: A 股季报 PIT + TTM + 相对估值锚 + 验证点核验。
    # 失败静默降级为年报口径（报告标注数据截至年报）。离线测试可关。
    enable_quarterly: bool = True

    # Aegis 2.0 Phase 2 (任务 C2): --update 增量模式。data stage 永远重跑
    # （行情/事件/季报必须新鲜）、valuation 永远重算、report 永远重渲染；
    # agents stage 仅当输入 digest（基本面子集+事件块+sector pack，显式
    # 排除实时价格）与上次 checkpoint 一致时直接复用缓存判断与合成产物
    # （零 LLM 调用），否则正常重跑。
    update_mode: bool = False

    # Aegis 2.0 Phase 3 (事件循环): 本次复研的触发原因（中文），由扫描器
    # (aegis.core.monitor.scanner) 在事件/监控点触发 --update 时透传。落进
    # thesis 版本链的 version_change_trigger 字段，供 delta 简报说明「哪个
    # 监控点触发的」。人工/定期全量运行留空。
    update_trigger: str = ""


@dataclass
class ResearchResult:
    """Complete result of an automated research run."""

    ticker: str
    entity_id: str
    run_id: str
    entity_name: str
    meta_facts: dict[str, Any]
    computed_metrics: dict[str, float]
    dcf_per_share: float
    scenarios: dict[str, float]
    implied_growth: float
    decision: Any  # ThesisDecision
    signal: Any  # PortfolioSignal
    segment_detail: dict[str, Any] = field(default_factory=dict)
    html_path: str | None = None
    bridge_warnings: list[str] = field(default_factory=list)
    # OpenBB enrichment data
    consensus_estimates: list[Any] = field(default_factory=list)
    earnings_history: list[Any] = field(default_factory=list)
    peer_fundamentals: list[Any] = field(default_factory=list)
    price_target_consensus: dict[str, Any] = field(default_factory=dict)
    pipeline_log: list[str] = field(default_factory=list)
    # Display-time decomposition: A-share risk warnings (ST / *ST) are
    # exchange metadata, not part of the company name. Surfaced separately
    # so badges, titles, and fuzzy lookups consume the right form.
    entity_name_clean: str = ""
    risk_warning_prefix: str = ""


class AutoResearchOrchestrator:
    """Fully automated research pipeline: ticker → report.

    Usage:
        orchestrator = AutoResearchOrchestrator()
        result = orchestrator.run(ResearchConfig(
            ticker="META",
            current_price=585.0,
            market_cap=1_510_000_000_000,
        ))
    """

    def __init__(
        self,
        *,
        market_data_connector_factory: Callable[[], Any] | None = None,
        openbb_connector_factory: Callable[..., Any] | None = None,
        catalyst_calendar_factory: Callable[[], Any] | None = None,
        form4_connector_factory: Callable[[], Any] | None = None,
        news_connector_factory: Callable[[], Any] | None = None,
        news_sentiment_analyzer_factory: Callable[..., Any] | None = None,
        earnings_call_analyzer_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._market_data_connector_factory = market_data_connector_factory
        self._openbb_connector_factory = openbb_connector_factory
        self._catalyst_calendar_factory = catalyst_calendar_factory
        self._form4_connector_factory = form4_connector_factory
        self._news_connector_factory = news_connector_factory
        self._news_sentiment_analyzer_factory = news_sentiment_analyzer_factory
        self._earnings_call_analyzer_factory = earnings_call_analyzer_factory
        self._cached_llm_client = None
        self._cached_fast_llm_client = None

    def last_run_cost_usd(self) -> float:
        """本实例累计 LLM 成本（美元），只读。

        Aegis 2.0 Phase 3：扫描器为每票 new 一个 orchestrator 实例跑 --update，
        因此这个"实例累计"就等于"该票本次复研成本"（用于每日预算熔断）。逻辑与
        Step 17 的成本汇总同源——walk 缓存的主/快 LLM client 的 cost_tracker
        求和；无 client / 无 tracker 时返回 0.0。永不 raise。
        """
        total = 0.0
        clients: list[Any] = []
        if getattr(self, "_cached_llm_client", None) is not None:
            clients.append(self._cached_llm_client)
        fast = getattr(self, "_cached_fast_llm_client", None)
        if fast is not None and fast is not self._cached_llm_client:
            clients.append(fast)
        for c in clients:
            ct = getattr(c, "cost_tracker", None)
            if ct is None:
                continue
            try:
                total += float(ct.total_cost_usd)
            except Exception:  # noqa: BLE001 — 成本读取失败不阻断
                continue
        return total

    def _make_market_data_connector(self) -> Any:
        if self._market_data_connector_factory is not None:
            return self._market_data_connector_factory()
        from aegis.core.acquisition.connectors.market_data_connector import MarketDataConnector

        return MarketDataConnector()

    def _make_openbb_connector(
        self,
        *,
        fmp_api_key: str | None = None,
        fred_api_key: str | None = None,
    ) -> Any:
        if self._openbb_connector_factory is not None:
            return self._openbb_connector_factory(
                fmp_api_key=fmp_api_key,
                fred_api_key=fred_api_key,
            )
        from aegis.core.acquisition.connectors.openbb_connector import OpenBBConnector

        return OpenBBConnector(fmp_api_key=fmp_api_key, fred_api_key=fred_api_key)

    def _make_catalyst_calendar(self) -> Any:
        if self._catalyst_calendar_factory is not None:
            return self._catalyst_calendar_factory()
        from aegis.core.catalyst_calendar import CatalystCalendar

        return CatalystCalendar()

    def _make_form4_connector(self) -> Any:
        if self._form4_connector_factory is not None:
            return self._form4_connector_factory()
        from aegis.core.acquisition.connectors.sec_form4_connector import SECForm4Connector

        return SECForm4Connector()

    def _make_news_connector(self) -> Any:
        if self._news_connector_factory is not None:
            return self._news_connector_factory()
        from aegis.core.acquisition.connectors.news_connector import NewsConnector

        return NewsConnector()

    def _make_news_sentiment_analyzer(self, *, llm_client: Any = None) -> Any:
        if self._news_sentiment_analyzer_factory is not None:
            return self._news_sentiment_analyzer_factory(llm_client=llm_client)
        from aegis.core.chief_analyst.news_sentiment_analyzer import NewsSentimentAnalyzer

        return NewsSentimentAnalyzer(llm_client=llm_client)

    def _make_earnings_call_analyzer(self, *, llm_client: Any) -> Any:
        if self._earnings_call_analyzer_factory is not None:
            return self._earnings_call_analyzer_factory(llm_client=llm_client)
        from aegis.core.chief_analyst.earnings_call_analyzer import EarningsCallAnalyzer

        return EarningsCallAnalyzer(llm_client=llm_client)

    def run(self, config: ResearchConfig) -> ResearchResult:
        """Execute the full research pipeline."""
        log: list[str] = []

        def _log(msg: str) -> None:
            entry = f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}"
            log.append(entry)
            print(f"  {entry}", flush=True)

        # BUG-52: reset the cached LLM client at the start of every run.
        # Previously the same orchestrator instance across multiple run()
        # calls would carry the client (and its cumulative CostTracker)
        # forward, causing the cost summary at the end to show the sum of
        # all runs ever executed in this process instead of just this one.
        self._cached_llm_client = None
        self._cached_fast_llm_client = None

        # BUG-43: wall-time watchdog for LLM agent calls. GOOGL Run #3 died
        # silently at business_analyst → valuation_analyst boundary with no
        # stack trace, no exit log, 50 min of zero output. Most likely cause
        # is an LLM API call hanging indefinitely (no HTTP timeout). Use
        # SIGALRM to force a TimeoutError after N seconds so the agent loop
        # catches it, logs it, and falls through to mock instead of hanging
        # the whole pipeline.
        import signal as _signal
        from contextlib import contextmanager as _cm

        @_cm
        def _agent_watchdog(seconds: int, agent_name: str):
            """Hard per-agent wall-time limit via SIGALRM. Unix-only.
            Raises TimeoutError if the agent doesn't return in `seconds`."""
            def _handler(signum, frame):
                raise TimeoutError(
                    f"{agent_name} exceeded {seconds}s wall-time watchdog — "
                    f"likely a hung LLM call, aborting agent"
                )
            # Save previous handler; restore on exit
            prev = _signal.signal(_signal.SIGALRM, _handler)
            _signal.alarm(seconds)
            try:
                yield
            finally:
                _signal.alarm(0)  # cancel
                _signal.signal(_signal.SIGALRM, prev)

        from aegis.core.governance.run_manifest import generate_run_id

        run_id = generate_run_id()
        _log(f"Starting research run {run_id} for {config.ticker}")
        _pipeline_start = datetime.now(timezone.utc)

        # ── Step 0: LLM backend health check (BUG-22) ────────────────
        # Fail fast if the chosen LLM backend is unreachable/unauthorized.
        # Without this, pipelines ran 15+ min before every agent fell back
        # to mock (e.g. a revoked API key returning 401, or CLI timeouts).
        if config.use_llm:
            health_err = self._check_llm_backend_health(config)
            if health_err:
                _log(f"❌ LLM backend health check FAILED: {health_err}")
                raise RuntimeError(
                    f"LLM backend unhealthy before pipeline start: {health_err}. "
                    f"Fix the backend or rerun with --no-llm to use rule-based mode."
                )
            _log("LLM backend health check: OK")

        # ── Step 1: Detect market & resolve entity ───────────────────
        is_a_share = self._is_a_share_ticker(config.ticker)

        # For A-share path, 'latest' needs to be resolved upfront since the
        # CNINFO connector doesn't have a true probe API. We use an *optimistic*
        # placeholder (current_year - 1), which is what the filing window
        # requires (A-share annual reports due by April 30 of Y+1). The actual
        # fiscal year is then DETECTED from the yfinance column header after
        # fetch (see detected_fy below at Step 2), and config.period is
        # OVERWRITTEN with the ground truth before the report is labeled.
        #
        # BUG-FIX (2026-04-15): previously hard-coded `current_year - 2` when
        # month < 5, guaranteeing stale data (FY2024 report on 2026-04-15 even
        # if FY2025 was available). User rule: A-share analysis MUST be
        # timely — we always ATTEMPT the latest year, let detection correct.
        if is_a_share and config.period.upper() in ("LATEST", "AUTO"):
            from datetime import datetime as _dt
            current_year = _dt.now().year
            # Always attempt the most recent completed fiscal year first.
            optimistic_year = current_year - 1
            config.period = f"FY{optimistic_year}"
            _log(f"A-share 'latest' → attempting {config.period} (will auto-correct to actual data year)")

        if is_a_share:
            # A-share pipeline: CNINFO + yfinance + CN adapter
            from aegis.core.acquisition.connectors.cninfo_connector import (
                CninfoConnector, COMMON_A_SHARES,
            )
            from aegis.core.acquisition.models import DataQuery
            from aegis.core.market_adapter.cn_adapter import CNMarketAdapter

            stock_code = config.ticker.replace(".SS", "").replace(".SZ", "")
            company_info = COMMON_A_SHARES.get(stock_code, {})
            entity_id = stock_code
            # A-share: prefer Chinese name for display; fall back to English, then code
            entity_name = company_info.get("name") or company_info.get("name_en") or stock_code
            # BUG-32 (2026-04-23): when the ticker is not in COMMON_A_SHARES,
            # entity_name fell back to the bare 6-digit code, so the report
            # title rendered "Aegis 投研 — 600089 (600089)". Fetch the canonical
            # Chinese name from the Tencent quote endpoint (already used as the
            # price-fallback connector — same source, no extra dependency) so
            # the registry doesn't need to enumerate every ticker.
            if entity_name == stock_code:
                try:
                    from aegis.core.acquisition.connectors.tencent_sina_quote import fetch_cn_quote
                    _q = fetch_cn_quote(stock_code)
                    if _q and _q.name:
                        entity_name = _q.name
                except Exception as _name_err:
                    # AUDIT bonus (TODO-Y8): this used to be a silent pass.
                    # When the name lookup fails, entity_name stays the bare
                    # 6-digit code and the BUG-Y18 name→industry sector
                    # fallback goes dead (→ General pack → large DCF bias),
                    # so at minimum leave a trace for operators.
                    _log(f"  ⚠ Tencent name lookup failed for {stock_code} "
                         f"({type(_name_err).__name__}: {_name_err}) — "
                         f"entity_name stays '{stock_code}', BUG-Y18 industry fallback may degrade")
            _log(f"A-share detected: {stock_code} ({entity_name})")

            cn_connector = CninfoConnector()
            query = DataQuery(
                entity_id=stock_code,
                market_id="cn",
                data_type="filing",
                period=config.period,
                filing_type="annual",
                extra_params={"fiscal_year": int(config.period[2:]), "fiscal_period": "annual"},
            )
            packet = cn_connector.fetch(query)
            if packet.raw_content is None or not packet.raw_content.get("facts"):
                raise RuntimeError(
                    f"Failed to fetch A-share data for {stock_code}. "
                    f"Ensure yfinance is installed and network is available."
                )

            facts_raw = packet.raw_content.get("facts", {})
            segment_facts_raw = packet.raw_content.get("segment_facts", {})
            segment_detail = packet.raw_content.get("segment_detail", {})
            _fetch_source = packet.response_metadata.get("data_source", "unknown") if hasattr(packet, "response_metadata") else "unknown"
            _log(f"Fetched {len(facts_raw)} financial facts via {_fetch_source}")

            # TIMELINESS: correct config.period to the ACTUAL fiscal year in
            # the fetched data (yfinance may be behind the filing calendar).
            # The cninfo connector detects the real year from the yfinance
            # column header and reports it back — use it as source of truth.
            detected_fy = packet.raw_content.get("fiscal_year")
            if detected_fy and isinstance(detected_fy, int):
                actual_period = f"FY{detected_fy}"
                if actual_period != config.period:
                    _log(f"  ⚠ Requested {config.period} but data source latest = {actual_period}; using {actual_period}")
                    config.period = actual_period
                # Warn if data is stale vs current calendar
                from datetime import datetime as _dt2
                now = _dt2.now()
                stale_months = (now.year - detected_fy - 1) * 12 + now.month
                if stale_months > 15:
                    _log(f"  ⚠⚠ TIMELINESS WARNING: FY{detected_fy} ended ~{stale_months} months ago. "
                         f"Data source (yfinance) is behind the current filing calendar. "
                         f"Consider using a fresher source (akshare/eastmoney/CNINFO direct).")

            # CN adapter: CAS concept mapping
            adapter = CNMarketAdapter()
            adapted_dict, adapted_meta = adapter.adapt_filing_data(facts_raw)
            _log(f"Adapted {len(adapted_dict)} CAS concepts ({len(adapted_meta.adaptation_notes)} notes)")

            # Extract embedded market data from CNINFO response
            cn_market = packet.raw_content.get("market_data", {})

        else:
            # US pipeline: SEC → EDGAR → XBRL → US adapter (original flow)
            from aegis.core.acquisition.connectors.sec_entity_registry import SECEntityRegistry

            registry = SECEntityRegistry()
            cik = registry.get_cik(config.ticker)
            if cik is None:
                raise ValueError(f"Unknown ticker: {config.ticker}. Not in SEC Entity Registry.")
            entity_id = config.ticker.lower() + "_platforms" if config.ticker == "META" else config.ticker.lower()
            entity_name = registry.get_name_by_ticker(config.ticker) or config.ticker
            _log(f"Resolved {config.ticker} ({entity_name}) → CIK {cik}")

            # ── Step 2: Fetch XBRL facts ────────────────────────────────
            from aegis.core.acquisition.connectors.edgar_connector import SECEDGARConnector
            from aegis.core.acquisition.connectors.sec_api_client import SECAPIClient
            from aegis.core.acquisition.connectors.xbrl_parser import XBRLParser
            from aegis.core.acquisition.models import DataQuery

            connector = SECEDGARConnector(user_agent=config.sec_user_agent)
            self._parser = XBRLParser()

            # ── Auto-detect latest fiscal year if requested ──
            # Pass period="latest" or "auto" to fetch the most recent 10-K.
            # This avoids stale data from hardcoded FY defaults.
            resolved_period = config.period
            if config.period.upper() in ("LATEST", "AUTO"):
                probe_query = DataQuery(
                    entity_id=cik,
                    market_id="us",
                    data_type="filing",
                    period="probe",
                    filing_type=config.filing_type,
                    # No fiscal_year → connector returns available_periods
                )
                probe_packet = connector.fetch(probe_query)
                available = (probe_packet.raw_content or {}).get("available_periods", [])
                fy_periods = [p for p in available if p.get("fiscal_period") == "FY"]
                if not fy_periods:
                    raise RuntimeError(
                        f"No FY periods found for {config.ticker} in EDGAR. "
                        f"Available: {available[:5]}"
                    )
                # Already sorted descending (latest first) by parser
                latest_fy = fy_periods[0]["fiscal_year"]
                resolved_period = f"FY{latest_fy}"
                _log(f"Auto-resolved period: {config.period} → {resolved_period} "
                     f"(available: {[p['fiscal_year'] for p in fy_periods[:5]]})")
                # Mutate config so downstream code uses the resolved period
                config.period = resolved_period

            query = DataQuery(
                entity_id=cik,
                market_id="us",
                data_type="filing",
                period=resolved_period,
                filing_type=config.filing_type,
                extra_params={"fiscal_year": int(resolved_period[2:]), "fiscal_period": "FY"},
            )
            packet = connector.fetch(query)

            if packet.raw_content is None:
                raise RuntimeError(f"Failed to fetch EDGAR data for {config.ticker} ({cik})")

            facts_raw = packet.raw_content.get("facts", {})
            segment_facts_raw = packet.raw_content.get("segment_facts", {})
            segment_detail = packet.raw_content.get("segment_detail", {})
            _log(f"Fetched {len(facts_raw)} XBRL facts, {len(segment_facts_raw)} segments")

            # ── Step 3: Adapt XBRL → canonical concepts ─────────────────
            from aegis.core.market_adapter.us_adapter import USMarketAdapter

            adapter = USMarketAdapter()
            adapted_dict, adapted_meta = adapter.adapt_filing_data(facts_raw)
            _log(f"Adapted {len(adapted_dict)} concepts ({len(adapted_meta.adaptation_notes)} unmapped)")
            cn_market = None

        # ── Step 4: Bridge → meta_facts ──────────────────────────────
        from aegis.core.acquisition.fact_bridge import FactNormalizationBridge

        bridge = FactNormalizationBridge()
        bridge_result = bridge.normalize(
            adapted_data=adapted_dict,
            segment_facts=segment_facts_raw,
            filing_context={
                "entity_id": entity_id,
                "fiscal_year": int(config.period[2:]),
                "fiscal_period": "FY",
            },
            market_id="cn" if is_a_share else "us",
            currency="CNY" if is_a_share else "USD",
        )
        meta_facts = bridge_result.meta_facts
        _log(f"Normalized to {len(meta_facts)} meta_facts, derived: {bridge_result.derived_fields}")
        if bridge_result.warnings:
            _log(f"Bridge warnings: {bridge_result.warnings}")
        # Surface DQ alerts (severity-prefixed) so they're visible in run logs.
        _dq_issues = meta_facts.get("__data_quality_issues") or []
        for _iss in _dq_issues:
            _log(f"  DQ[{_iss['severity']}] {_iss['code']}: {_iss['message']}")

        # BUG-46: clean segment_detail so every category has non-overlapping
        # members that sum to company revenue. Fixes:
        #  - Products Breakdown double-counting parent+child roll-ups
        #  - Business Segment missing unallocated revenue (adds synthetic gap)
        #  - % of Total denominators becoming meaningful company-wide
        if segment_detail:
            _company_rev_for_dedup = meta_facts.get("revenue", 0)
            if _company_rev_for_dedup > 0:
                _before = {k: len(v) if isinstance(v, dict) else 0
                           for k, v in segment_detail.items()}
                segment_detail = self._dedupe_segment_detail(
                    segment_detail, _company_rev_for_dedup,
                )
                _after = {k: len(v) if isinstance(v, dict) else 0
                          for k, v in segment_detail.items()}
                _changed = {k: (_before.get(k, 0), _after.get(k, 0))
                            for k in _after if _before.get(k, 0) != _after.get(k, 0)}
                if _changed:
                    _log(f"  Segment dedup (BUG-46): {_changed}")

        # BUG-46: broader EBITDA fallback for tickers missing the
        # DepreciationDepletionAndAmortization XBRL tag (e.g. GOOGL, TSLA).
        # If bridge failed to derive ebitda, try CFO reconciliation DA or
        # a proxy from capex and PP&E.
        if "ebitda" not in meta_facts or not meta_facts.get("ebitda"):
            opinc = meta_facts.get("operating_income") or meta_facts.get("ebit") or 0
            da = (
                meta_facts.get("depreciation_amortization")
                or meta_facts.get("depreciation_and_amortization")
                or meta_facts.get("depreciation")
                or 0
            )
            if opinc and not da:
                # Last-resort proxy: DA ≈ 50% of capex (industry rule of thumb
                # for mature large caps). Tag as proxy so downstream can warn.
                capex = meta_facts.get("capex") or meta_facts.get("capex_ppe") or 0
                if capex > 0:
                    da = capex * 0.5
                    meta_facts["depreciation_amortization"] = da
                    meta_facts["_ebitda_proxy"] = True
                    # BUG-Y19: log uses __display when available so A-share
                    # entities log ¥X亿 instead of $X.XB.
                    _disp = meta_facts.get("__display") or {}
                    if _disp.get("symbol") == "¥":
                        _log(f"  EBITDA proxy: DA≈capex×0.5=¥{da/1e8:.1f}亿 "
                             f"(no DepreciationDepletionAndAmortization)")
                    else:
                        _log(f"  EBITDA proxy: DA≈capex×0.5=${da/1e9:.1f}B "
                             f"(no DepreciationDepletionAndAmortization in XBRL)")
            if opinc and da:
                meta_facts["ebitda"] = opinc + da

        # ── Step 4b: Extract historical annual data (3-5 years) ────────
        historical_data: dict[int, dict[str, float]] = {}
        rev_series: list[tuple[int, float]] = []

        if is_a_share:
            # A-share: prefer historical from akshare packet (fetched in Step 2);
            # fall back to yfinance if packet didn't include it.
            packet_historical = packet.raw_content.get("historical_by_year") if hasattr(packet, "raw_content") else None
            if packet_historical:
                # akshare gives up to 7 years of history, but early years may
                # pre-date the company's current form (e.g., pre-IPO small-cap
                # years with <¥2B revenue distort the growth CAGR). Cap at
                # the most recent 5 years AND filter out years before the
                # listing date (if known from akshare company_info).
                company_info = packet.raw_content.get("company_info", {}) or {}
                listing_date = str(company_info.get("listing_date", ""))
                listing_year = None
                if listing_date and len(listing_date) >= 4 and listing_date[:4].isdigit():
                    listing_year = int(listing_date[:4])
                    # Use the prior fiscal year as the earliest allowed — the
                    # first full post-IPO fiscal year. (e.g. listing 2023 →
                    # start from FY2022 which is the earliest fully-comparable
                    # pre-listing audited year.)
                    earliest_allowed = listing_year - 1
                else:
                    earliest_allowed = 0

                all_years = sorted({int(y) for y in packet_historical.keys()}, reverse=True)
                # Take last 5 years, but drop pre-listing anomaly years
                kept_years = []
                for yr in all_years[:5]:
                    if yr >= earliest_allowed:
                        kept_years.append(yr)
                historical_data = {yr: dict(packet_historical[yr]) for yr in kept_years}
                rev_series = []
                for yr in sorted(historical_data.keys()):
                    rev = historical_data[yr].get("营业收入") or historical_data[yr].get("Total Revenue")
                    if rev and rev > 0:
                        rev_series.append((yr, float(rev)))
                _log(f"A-share historical (akshare): {len(historical_data)} years "
                     f"(listing_year={listing_year}, kept≥{earliest_allowed}), "
                     f"revenue series: {len(rev_series)} points")
            else:
                try:
                    historical_data, rev_series = self._extract_a_share_historical(
                        config.ticker, entity_id, _log,
                    )
                except Exception as e:
                    _log(f"A-share historical extraction failed: {e}")
        else:
            # US: extract from SEC EDGAR XBRL
            historical_concepts = [
                "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
                "us-gaap:Revenues",
                "us-gaap:NetIncomeLoss",
                "us-gaap:OperatingIncomeLoss",
                "us-gaap:GrossProfit",
                "us-gaap:NetCashProvidedByUsedInOperatingActivities",
                "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment",
                "us-gaap:PaymentsForRepurchaseOfCommonStock",
                "us-gaap:DepreciationDepletionAndAmortization",
                "us-gaap:ResearchAndDevelopmentExpense",
                "us-gaap:AllocatedShareBasedCompensationExpense",
                "us-gaap:ShareBasedCompensation",
                "us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding",
            ]
            try:
                raw_facts_json = self._client_cache if hasattr(self, '_client_cache') else None
                if raw_facts_json is None:
                    client = SECAPIClient(user_agent=config.sec_user_agent)
                    raw_facts_json = client.get_company_facts(cik)
                if raw_facts_json:
                    historical_data = self._parser.extract_historical_annual(
                        raw_facts_json, historical_concepts, num_years=5,
                    )
                    _log(f"Extracted {len(historical_data)} years of historical data")
            except Exception as e:
                _log(f"Historical data extraction failed: {e}")

            # Build revenue series from US GAAP concepts
            if historical_data:
                years = sorted(historical_data.keys())
                rev_key = "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"
                rev_alt = "us-gaap:Revenues"
                for yr in years:
                    r = historical_data[yr].get(rev_key) or historical_data[yr].get(rev_alt)
                    if r:
                        rev_series.append((yr, r))

        # Compute historical growth rates and add to meta_facts (SHARED — both
        # A-share and US paths; pre-2026-04-15 this was mis-indented inside the
        # US else block so A-share never got CAGR populated).
        if len(rev_series) >= 2:
            growth_rates = []
            for i in range(1, len(rev_series)):
                prev_rev = rev_series[i - 1][1]
                curr_rev = rev_series[i][1]
                if prev_rev > 0:
                    growth_rates.append((rev_series[i][0], (curr_rev - prev_rev) / prev_rev))

            meta_facts["__historical_revenue"] = {yr: rev for yr, rev in rev_series}
            meta_facts["__historical_growth"] = {yr: g for yr, g in growth_rates}

            first_rev = rev_series[0][1]
            last_rev = rev_series[-1][1]
            n_years = rev_series[-1][0] - rev_series[0][0]
            if first_rev > 0 and n_years > 0:
                cagr = (last_rev / first_rev) ** (1.0 / n_years) - 1
                meta_facts["__revenue_cagr"] = round(cagr, 4)
                meta_facts["__revenue_cagr_years"] = n_years

                # ── CAGR sanity check (BUG-fix 2026-04-15) ──
                # A naive CAGR over a multi-year window is dangerous when:
                #   1. Base year is tiny (early-stage hyper-growth that won't
                #      repeat — 301358 went from ¥0.96亿 (2018) to ¥226亿 (2024),
                #      CAGR=127% which DCF then projects forward as if normal)
                #   2. The growth is mathematically extreme (>50% sustained
                #      is implausible for any company past startup phase)
                #   3. Window is too short (<3y) for the geometric mean to
                #      smooth out cyclical noise
                #   4. The most recent year diverges sharply from the trend
                #      (e.g. 301358 FY2024 was -45% YoY — CAGR average hides
                #      the actual cyclical state)
                # When any of these fire, mark the CAGR as unreliable so the
                # DCF builder falls through to size-bucket defaults instead of
                # extrapolating the noise.
                cagr_warnings: list[str] = []
                if cagr > 0.60:
                    # BUG-Y30 (2026-05-06): "early-stage scaling or one-off
                    # boom" was too narrow — NVDA's 68% 4y CAGR is real
                    # product-cycle demand (AI training/inference), not
                    # "early-stage". Reword to enumerate the actual
                    # candidate causes so downstream agents can match the
                    # right narrative to the company.
                    cagr_warnings.append(
                        f"CAGR {cagr:.0%} >60% — exceeds the cap most companies "
                        f"sustain. Possible drivers: early-stage scaling, "
                        f"one-off boom (cyclical / policy / single-customer), "
                        f"or genuine product-cycle demand shock. Naive "
                        f"extrapolation is unsafe regardless of cause"
                    )
                if first_rev < last_rev * 0.10:
                    # BUG-Y30: scale unit by currency. `/1e8` (亿) is right
                    # for A-share but US/EUR/etc.) need /1e9 (B). Use the
                    # __display block on meta_facts when present.
                    _disp = (meta_facts.get("__display") or {})
                    if _disp.get("symbol") == "¥":
                        _scale = _disp.get("scale", 1e8)
                        _unit = _disp.get("unit", "亿")
                        _sym = "¥"
                    else:
                        _scale = 1e9
                        _unit = "B"
                        _sym = "$"
                    cagr_warnings.append(
                        f"Base year revenue is <10% of latest "
                        f"({_sym}{first_rev/_scale:.1f}{_unit} → {_sym}{last_rev/_scale:.1f}{_unit}) — "
                        f"window may span early-stage scaling"
                    )
                if n_years < 3:
                    cagr_warnings.append(
                        f"Only {n_years}y window — too short for stable CAGR"
                    )
                # Recent-year regime check: if the most recent YoY is opposite
                # sign, far from CAGR, or itself a >30% swing, the company is
                # in a regime change (cyclical peak/trough, demand shock) and
                # the smoothed CAGR hides the actual cyclical state.
                if len(rev_series) >= 2:
                    last_yoy = (last_rev - rev_series[-2][1]) / rev_series[-2][1] if rev_series[-2][1] > 0 else 0
                    # BUG-28: persist for downstream DCF growth-path
                    # construction so distressed companies fall back to
                    # last-year YoY rather than size-bucket 20% defaults.
                    meta_facts["__revenue_last_yoy"] = round(last_yoy, 4)
                    if (cagr > 0 and last_yoy < 0) or (cagr < 0 and last_yoy > 0):
                        cagr_warnings.append(
                            f"Most recent YoY ({last_yoy:+.0%}) opposite sign "
                            f"to CAGR ({cagr:+.0%}) — regime change"
                        )
                    elif abs(last_yoy - cagr) > 0.40:
                        cagr_warnings.append(
                            f"Most recent YoY ({last_yoy:+.0%}) diverges "
                            f">40 pts from CAGR ({cagr:+.0%}) — non-stationary"
                        )
                    elif abs(last_yoy) > 0.30:
                        # Big swing in the most recent year, even if same
                        # sign as CAGR, signals cyclical or demand shock.
                        cagr_warnings.append(
                            f"Most recent YoY swing ({last_yoy:+.0%}) >30 pts — "
                            f"regime not stationary"
                        )

                if cagr_warnings:
                    meta_facts["__revenue_cagr_unreliable"] = True
                    meta_facts["__revenue_cagr_warnings"] = cagr_warnings
                    _log(f"Revenue CAGR ({rev_series[0][0]}-{rev_series[-1][0]}): "
                         f"{cagr:.1%} ⚠ UNRELIABLE — {'; '.join(cagr_warnings)}")
                else:
                    _log(f"Revenue CAGR ({rev_series[0][0]}-{rev_series[-1][0]}): {cagr:.1%}")

        if historical_data:
            meta_facts["__historical_data"] = historical_data

        # ── Step 4c (Aegis 2.0 Phase 1): A 股季报 PIT + TTM + 相对估值 ──
        # DESIGN_2.0 §三.B / §五 Phase 1：季报连接器直写 PIT（sqlite3）→
        # TTM 快照读出（红线 8 豁免的固定键）→ 数据时效标注 → 相对估值锚。
        # 全部失败静默降级：无季报 → 年报口径照旧，报告标注数据截至年报。
        _pit_store: Any = None
        relative_valuation: Any = None  # RelativeValuation | None
        if is_a_share and getattr(config, "enable_quarterly", True):
            _pit_store, _ttm_updates = run_quarterly_pit_step(
                config.ticker,
                smoke_mode=getattr(config, "smoke_mode", False),
                log=_log,
            )
            meta_facts.update(_ttm_updates)
            if "ttm_revenue" in meta_facts or "ttm_net_income" in meta_facts:
                _sym = _currency_symbol_for_logs(meta_facts)
                _log(
                    "TTM (rolling 4Q): "
                    + ", ".join(
                        f"{k}={_sym}{meta_facts[k] / 1e8:.2f}亿"
                        for k in TTM_META_KEYS if k in meta_facts
                    )
                )
            # 相对估值锚（东财同业 PE/PB 分位；compute_relative_valuation
            # 契约上永不 raise，import 失败也不许打断主流程）。
            try:
                from aegis.core.truth.relative_valuation import (
                    compute_relative_valuation,
                )
                relative_valuation = compute_relative_valuation(config.ticker)
                meta_facts["__relative_valuation"] = relative_valuation.to_dict()
                if relative_valuation.insufficient_peers:
                    _log("Relative valuation: 同业样本不足（锚不可用，红线 5 gate）")
                else:
                    _log(
                        f"Relative valuation: 「{relative_valuation.industry}」"
                        f"PE(TTM) {relative_valuation.target_pe_ttm} vs 中位 "
                        f"{relative_valuation.peer_pe_median} / PB "
                        f"{relative_valuation.target_pb} vs 中位 "
                        f"{relative_valuation.peer_pb_median} "
                        f"(peers={relative_valuation.peer_count})"
                    )
            except Exception as e:
                _log(f"  ⚠ Relative valuation skipped: {e}")
        if is_a_share and "__data_freshness" not in meta_facts:
            # 降级路径（任务 D1）：无季报 → 数据截至年报（A 股 FY 固定
            # 12-31 期末），渲染层「数据截至」行据此标注年报口径。
            try:
                meta_facts["__data_freshness"] = build_data_freshness(
                    f"{int(config.period[2:])}-12-31", basis="annual")
            except (TypeError, ValueError):
                pass

        # ── Stage checkpoint: data（Aegis 2.0 Phase 2 C1）────────────
        # 数据获取 + 归一化 + PIT/TTM 完成缝（A 股事件切片在 Step 8b 落
        # meta_facts，属 agents digest 的输入；本缝只锁「取数」产物）。
        # data stage 输入 = ticker/period/filing_type（--update 下永远重跑）。
        dump_stage_checkpoint(
            config.ticker, "data",
            {
                "entity_id": entity_id,
                "entity_name": entity_name,
                "period": config.period,
                "meta_facts": meta_facts,
                "bridge_warnings": list(bridge_result.warnings or []),
            },
            digest=compute_stage_digest({
                "ticker": config.ticker,
                "period": config.period,
                "filing_type": config.filing_type,
            }),
            run_id=run_id,
            smoke_mode=getattr(config, "smoke_mode", False),
            log=_log,
        )

        # ── Step 5: Compute metrics ──────────────────────────────────
        from aegis.core.truth.registry.metric_registry import MetricRegistry
        from aegis.core.truth.registry.seed_metrics import seed_core_metrics
        from aegis.core.truth.formulas.formula_engine import FormulaEngine

        metric_registry = MetricRegistry()
        seed_core_metrics(metric_registry)
        engine = FormulaEngine()

        computed_metrics = self._compute_metrics(engine, meta_facts, entity_id, config.period)
        _log(f"Computed {len(computed_metrics)} metrics")

        # ── Step 6: Market data ──────────────────────────────────────
        # BUG-fix (2026-04-15): preserve "missing" vs "real value" distinction.
        # The previous default of 1 caused the split detector below to compute
        # ratio = live_shares / 1 = ~843M for any A-share whose CAS report
        # lacked the shares_outstanding field, producing absurd log lines like
        # "Stock split detected: ratio=843340214:1". Use None as the missing
        # sentinel and check for it explicitly before doing ratio math.
        _xbrl_diluted = meta_facts.get("diluted_shares")
        _xbrl_basic = meta_facts.get("shares_outstanding")
        xbrl_shares = _xbrl_diluted or _xbrl_basic or None
        # Treat zero or near-zero (< 1M shares — implausibly small for any
        # listed company) as missing rather than real.
        if xbrl_shares is not None and xbrl_shares < 1e6:
            xbrl_shares = None
        # Default `shares` to xbrl when present, else 1 (sentinel; will be
        # overridden by live snapshot if available, otherwise downstream
        # error path catches it).
        shares = xbrl_shares if xbrl_shares is not None else 1

        # Always try to get live shares_outstanding from yfinance to handle stock splits
        # (XBRL shares may be pre-split if the filing predates a split event)
        _live_snapshot = None
        try:
            _mkt_conn = self._make_market_data_connector()
            yf_ticker = config.ticker
            if is_a_share:
                from aegis.core.acquisition.connectors.cninfo_connector import CninfoConnector
                yf_ticker = CninfoConnector._to_yfinance_ticker(
                    config.ticker.replace(".SS", "").replace(".SZ", ""),
                ) or config.ticker
            _live_snapshot = _mkt_conn.get_snapshot(yf_ticker)
            if _live_snapshot.shares_outstanding > 0:
                live_shares = _live_snapshot.shares_outstanding
                # BUG-39: dual-class correction. For tickers like GOOGL (Class A
                # only, ~5.8B) yfinance only returns the single-class count, but
                # market_cap correctly reflects ALL classes (~12.1B × price).
                # Back out the true share count from market_cap / price when the
                # implied count is significantly higher — this fixes Alphabet,
                # News Corp, Discovery, and other dual-class structures.
                if (_live_snapshot.market_cap
                        and _live_snapshot.current_price
                        and _live_snapshot.current_price > 0):
                    implied = _live_snapshot.market_cap / _live_snapshot.current_price
                    if implied > live_shares * 1.15:  # ≥15% more than single-class
                        _log(f"Dual-class detected: single-class shares="
                             f"{live_shares/1e9:.2f}B, market_cap/price implies "
                             f"{implied/1e9:.2f}B — using implied count")
                        live_shares = implied
                # Detect stock split: only meaningful when BOTH XBRL and live
                # are real positive values that disagree by >2x. If XBRL was
                # missing/zero (common for A-share CAS reports), this is just a
                # data-availability event, not a split — silently use live.
                if xbrl_shares is None:
                    _log(f"XBRL shares field missing; using live shares="
                         f"{live_shares/1e9:.2f}B from market data snapshot")
                else:
                    ratio = live_shares / xbrl_shares
                    if ratio > 2 or ratio < 0.5:
                        _log(f"Stock split detected: XBRL shares={xbrl_shares/1e9:.2f}B, "
                             f"live shares={live_shares/1e9:.2f}B (ratio={ratio:.1f}:1)")
                shares = live_shares
                # Also update meta_facts so DCF engine uses correct share count
                meta_facts["diluted_shares"] = live_shares
                meta_facts["shares_outstanding"] = live_shares
        except Exception as e:
            if xbrl_shares is None:
                _log(f"Live shares fetch failed ({e}); XBRL shares also missing — "
                     f"will try A-share connector market_data fallback below.")
            else:
                _log(f"Live shares fetch failed ({e}), using XBRL shares={xbrl_shares/1e9:.2f}B")

        # BUG-fix (2026-04-15): A-share fallback. yfinance routinely returns
        # shares_outstanding=0 for .SZ/.SS tickers, which causes the live
        # branch above to silently skip without raising — leaving meta_facts
        # diluted_shares unset and the DCF builder to blow up. The akshare
        # connector already fetches the real share count from eastmoney
        # ('总股本') and stuffs it in cn_market["shares_outstanding"]. Use it
        # here when the live snapshot didn't provide a usable value.
        if (is_a_share
                and (shares is None or shares <= 1)
                and cn_market
                and cn_market.get("shares_outstanding", 0) > 0):
            cn_shares = float(cn_market["shares_outstanding"])
            _log(f"A-share shares fallback: using akshare/eastmoney "
                 f"shares_outstanding={cn_shares/1e8:.2f}亿 ({cn_shares/1e9:.2f}B)")
            shares = cn_shares
            meta_facts["diluted_shares"] = cn_shares
            meta_facts["shares_outstanding"] = cn_shares

        # Auto-fetch from Yahoo Finance if price not provided
        if config.current_price is None:
            if is_a_share and cn_market and cn_market.get("current_price", 0) > 0:
                # Use market data already fetched from CNINFO yfinance call
                config = ResearchConfig(**{
                    **config.__dict__,
                    "current_price": cn_market["current_price"],
                    "market_cap": cn_market.get("market_cap") or cn_market["current_price"] * shares,
                })
                _log(f"A-share market data: ¥{cn_market['current_price']:.2f}, "
                     f"cap=¥{cn_market.get('market_cap', 0)/1e8:.0f}亿")
            else:
                try:
                    # Reuse the snapshot already fetched for live shares if available
                    snapshot = _live_snapshot
                    if snapshot is None:
                        mkt_connector = self._make_market_data_connector()
                        yf_ticker = config.ticker
                        if is_a_share:
                            from aegis.core.acquisition.connectors.cninfo_connector import CninfoConnector
                            yf_ticker = CninfoConnector._to_yfinance_ticker(
                                config.ticker.replace(".SS", "").replace(".SZ", ""),
                            ) or config.ticker
                        snapshot = mkt_connector.get_snapshot(yf_ticker)
                    if snapshot.current_price > 0:
                        config = ResearchConfig(**{
                            **config.__dict__,
                            "current_price": snapshot.current_price,
                            "market_cap": snapshot.market_cap or snapshot.current_price * shares,
                        })
                        # TODO-Y5 (2026-05-06): branch the symbol so A-share
                        # tickers that fall through to yfinance fallback log
                        # ¥X亿 instead of $XB. Akshare path above already
                        # uses ¥ — this is the rare yfinance-fallback case.
                        if is_a_share:
                            _log(f"Auto-fetched market data: ¥{snapshot.current_price:.2f}, "
                                 f"cap=¥{snapshot.market_cap/1e8:.0f}亿")
                        else:
                            _log(f"Auto-fetched market data: ${snapshot.current_price:.2f}, "
                                 f"cap=${snapshot.market_cap/1e9:.0f}B")
                except Exception as e:
                    _log(f"Market data auto-fetch failed: {e}")

        market_data = {
            "current_price": config.current_price or 0,
            "market_cap": config.market_cap or (config.current_price or 0) * shares,
            "shares_outstanding": shares,
        }

        # ── Price=0 guard (P0-REVIEW 2026-04-16) ──
        # When price is 0/None, all price-dependent metrics (P/E, EV, market cap)
        # are invalid. Pipeline should NOT silently proceed — abort with clear error.
        if not market_data["current_price"]:
            _is_a = self._is_a_share_ticker(config.ticker)
            _sources = ("yfinance + Tencent (qt.gtimg.cn) + Sina (hq.sinajs.cn)"
                        if _is_a else "yfinance")
            raise ValueError(
                f"current_price is 0 or missing for {config.ticker}. "
                f"All price-dependent metrics (P/E, EV, market_cap) would be invalid. "
                f"All realtime quote sources failed: {_sources}.\n"
                f"  Fix: ./run_research.sh {config.ticker} <PRICE>"
            )

        # Market-dependent metrics.
        # Refactor 3 (2026-05-04): only emit ratios that are *meaningful*.
        # P/E with negative earnings, EV/EBITDA with non-positive EBITDA,
        # and Net-Debt/EBITDA with non-positive EBITDA are mathematically
        # computable but semantically meaningless (a "multiple of a loss"
        # has no comparable interpretation). Previously every renderer had
        # to re-derive a guard ("if val < 0 → n/m"); now we omit the key
        # entirely so the renderer's natural `if val:` skip path handles
        # it. Single source of truth.
        if market_data["current_price"] and meta_facts.get("net_income"):
            eps = meta_facts["net_income"] / shares
            if eps and eps > 0:
                computed_metrics["pe_ratio"] = market_data["current_price"] / eps
        if market_data["market_cap"]:
            nd = computed_metrics.get("net_debt", 0)
            computed_metrics["enterprise_value"] = market_data["market_cap"] + nd
            _ebitda_val = meta_facts.get("ebitda")
            if _ebitda_val and _ebitda_val > 0:
                computed_metrics["ev_to_ebitda"] = computed_metrics["enterprise_value"] / _ebitda_val
            if meta_facts.get("revenue"):
                computed_metrics["ev_to_revenue"] = computed_metrics["enterprise_value"] / meta_facts["revenue"]

        # ── Step 6b: Load Sector Pack (needed for DCF calibration) ──
        # BUG-A14: pull akshare's industry classification (when present) so
        # tickers absent from TICKER_SECTOR_MAP can still hit a domain pack
        # instead of the "General" template. yfinance/SEC paths set this to
        # empty string so US tickers continue to use the manual whitelist.
        _ak_industry = ""
        if is_a_share:
            try:
                _ak_industry = str(
                    (packet.raw_content.get("company_info") or {}).get("industry", "")
                ).strip()
            except (NameError, AttributeError):
                _ak_industry = ""
            # BUG-Y18: when akshare didn't supply an industry (eastmoney
            # push2 endpoint flaky), fall back to inferring from the
            # company name. Without this, sector_pack collapses to General
            # and DCF assumptions become wildly inappropriate.
            if not _ak_industry:
                _name = entity_name or ""
                _ak_industry = self._infer_industry_from_name(_name) or ""
                if _ak_industry:
                    _log(f"  ↳ akshare industry empty; inferred '{_ak_industry}' "
                         f"from name '{_name}' (BUG-Y18 fallback)")
            # AUDIT 2026-07: surface the resolved industry to the report
            # layer (html_report_v2 reads meta_facts sector/industry —
            # without this the report showed "—" even when the industry
            # resolved fine and only the pack fell back to General).
            if _ak_industry and not meta_facts.get("industry"):
                meta_facts["industry"] = _ak_industry
        sector_pack = self._load_sector_pack(
            config.sector_pack_id, config.ticker, _ak_industry,
        )
        if _ak_industry and not config.sector_pack_id:
            _log(f"Sector pack: {sector_pack.get('sector_name', 'Generic')} "
                 f"(akshare industry: {_ak_industry or '—'})")
        else:
            _log(f"Sector pack: {sector_pack.get('sector_name', 'Generic')}")

        # ── Step 6c: OpenBB Data Enrichment ────────────────────────────
        consensus_estimates: list = []
        earnings_history: list = []
        peer_fundamentals: list = []
        price_target_consensus: dict = {}
        openbb_macro = None
        obb_conn = None
        historical_valuation: dict = {}

        if config.enable_openbb:
            # OpenBB enrichment: yfinance features work for both US and A-shares.
            # FRED macro and FMP transcripts are US-only.
            try:
                import os

                fmp_key = config.fmp_api_key or os.environ.get("FMP_API_KEY")
                fred_key = config.fred_api_key or os.environ.get("FRED_API_KEY")
                obb_conn = self._make_openbb_connector(
                    fmp_api_key=fmp_key,
                    fred_api_key=fred_key,
                )

                # yfinance symbol for A-shares (600519 → 600519.SS)
                yf_symbol = self._to_yfinance_symbol(config.ticker) if is_a_share else config.ticker

                # Consensus estimates (yfinance)
                # AUDIT-D5 (BUG-29 follow-up): A-shares must skip yfinance
                # here — same policy as earnings_history / historical_valuation
                # below. The unconditional call went through the proxy for
                # .SS/.SZ symbols and returned junk/timeouts.
                if is_a_share:
                    consensus_estimates = []
                    _log("Consensus: skipped for A-share (yfinance coverage unreliable)")
                else:
                    consensus_estimates = obb_conn.get_consensus_estimates(yf_symbol)
                    _log(f"Consensus: {len(consensus_estimates)} estimates")

                # Earnings history (yfinance — works for US; A-shares typically
                # return bogus US ticker collisions from FMP fallback, so skip)
                if is_a_share:
                    earnings_history = []
                    _log("Earnings history: skipped for A-share (yfinance/FMP coverage unreliable)")
                else:
                    earnings_history = obb_conn.get_earnings_history(yf_symbol, limit=8)
                    _log(f"Earnings history: {len(earnings_history)} records")

                # Peer discovery and fundamentals (built-in map has both US + A-share peers)
                # AUDIT-D5: A-share peers in _PEER_MAP are .SS/.SZ tickers →
                # _fetch_single_peer would hit yfinance serially through the
                # proxy for each one. Skip, same as the other yfinance paths.
                if is_a_share:
                    peer_fundamentals = []
                    _log("Peers: skipped for A-share (yfinance peer fundamentals unreliable)")
                else:
                    peer_tickers = obb_conn.get_sector_peers(config.ticker, limit=6)
                    if peer_tickers:
                        peer_fundamentals = obb_conn.get_peer_fundamentals(peer_tickers[:6])
                        _log(f"Peers: {len(peer_fundamentals)} fundamentals ({', '.join(peer_tickers[:6])})")

                # Historical valuation multiples (US only; A-share yfinance
                # coverage is unreliable and the connector now skips .SZ/.SS
                # tickers at entry to avoid NoneType subscript crashes).
                # BUG-25: pass computed EV/EBITDA as override to protect against
                # stale/wrong yfinance enterpriseToEbitda snapshots.
                _ev_ebitda_override = computed_metrics.get("ev_to_ebitda")
                historical_valuation = obb_conn.get_historical_valuation(
                    yf_symbol, years=5, ev_ebitda_override=_ev_ebitda_override,
                )
                if historical_valuation and historical_valuation.get("dates"):
                    pe_stats = historical_valuation.get("pe_stats", {})
                    _log(f"Historical valuation: {len(historical_valuation['dates'])} months, "
                         f"PE range {pe_stats.get('min', 0):.0f}-{pe_stats.get('max', 0):.0f}x "
                         f"(median {pe_stats.get('median', 0):.0f}x)")

                    # ── P/E unification (BUG-fix 2026-04-15) ──
                    # Two P/E numbers existed in parallel:
                    #   1. computed_metrics["pe_ratio"] = price / (FY_NI/shares)  — STATIC FY
                    #   2. historical_valuation.pe_stats.current = yf trailingPE  — TTM
                    # Peers' P/E in peer_fundamentals come from yf trailingPE (TTM),
                    # so the peer comparison was mixing FY-static (subject) with
                    # TTM (peers) — apples to oranges, premium/discount label wrong.
                    # Fix: always also store TTM under pe_ratio_ttm so downstream
                    # consumers (peer panel, LLM agents, displays) can pick the
                    # correct one. Both remain available; the report shows both
                    # rows clearly labeled.
                    # BUG-27 (2026-04-23): this block was previously indented
                    # inside the `elif is_a_share:` branch, which was a scope
                    # bug — pe_stats is only defined in the `if historical_
                    # valuation` branch. A-share runs (historical_valuation
                    # empty) hit UnboundLocalError, spoiling OpenBB enrichment.
                    pe_ttm = pe_stats.get("current") if isinstance(pe_stats, dict) else None
                    if pe_ttm and pe_ttm > 0:
                        computed_metrics["pe_ratio_ttm"] = pe_ttm
                        pe_fy = computed_metrics.get("pe_ratio", 0)
                        if pe_fy and abs(pe_ttm - pe_fy) / max(pe_ttm, pe_fy) > 0.10:
                            _log(f"  P/E reconciliation: FY-static={pe_fy:.1f}x vs "
                                 f"TTM={pe_ttm:.1f}x (>10% spread — peer comparison "
                                 f"will use TTM for apples-to-apples).")
                elif is_a_share:
                    _log("Historical valuation: skipped for A-share (yfinance coverage unreliable)")

                # US-only features: price target, FRED macro
                if not is_a_share:
                    price_target_consensus = obb_conn.get_price_target_consensus(config.ticker)
                    if price_target_consensus:
                        _log(f"Price target consensus: ${price_target_consensus.get('target_consensus', 0):.0f} "
                             f"({price_target_consensus.get('analyst_count', 0)} analysts)")

                    if fred_key:
                        openbb_macro = obb_conn.get_macro_snapshot()
                        _log(f"OpenBB: macro snapshot (fed={openbb_macro.fed_funds_rate:.3f}, "
                             f"10y={openbb_macro.us_10y_yield:.3f}, cpi={openbb_macro.cpi_yoy:.3f})")

            except Exception as e:
                _log(f"OpenBB enrichment failed (non-fatal): {e}")

        # ── Step 6d: Earnings Call Transcript ──────────────────────────
        earnings_call_insights = None
        transcript_raw: dict = {}

        if config.enable_openbb and config.use_llm and not is_a_share:
            try:
                import os
                fmp_key = config.fmp_api_key or os.environ.get("FMP_API_KEY")
                if fmp_key:
                    if not obb_conn:
                        obb_conn = self._make_openbb_connector(fmp_api_key=fmp_key)
                    transcript_raw = obb_conn.get_earnings_transcript(config.ticker)
                    if transcript_raw and transcript_raw.get("content"):
                        _log(f"Transcript: Q{transcript_raw['quarter']} {transcript_raw['year']} "
                             f"({transcript_raw['word_count']} words)")

                        ec_client = self._resolve_llm_client(config, _log, quiet=True)
                        analyzer = self._make_earnings_call_analyzer(llm_client=ec_client)
                        earnings_call_insights = analyzer.analyze(
                            transcript_text=transcript_raw["content"],
                            symbol=config.ticker,
                            quarter=transcript_raw.get("quarter", 0),
                            year=transcript_raw.get("year", 0),
                            entity_name=entity_name,
                        )
                        _log(f"Earnings Call: tone={earnings_call_insights.overall_tone}, "
                             f"materiality={earnings_call_insights.materiality}, "
                             f"{len(earnings_call_insights.guidance_items)} guidance items")
                    else:
                        _log("No transcript available for latest quarter")
            except Exception as e:
                _log(f"Earnings call analysis failed (non-fatal): {e}")

        # ── Step 6e: Catalyst Calendar ────────────────────────────────
        catalyst_timeline = None
        try:
            cat_cal = self._make_catalyst_calendar()
            catalyst_timeline = cat_cal.build(
                ticker=config.ticker,
                entity_id=entity_id,
                earnings_history=earnings_history,
                sector_pack=sector_pack,
                earnings_call_insights=earnings_call_insights,
                market_data=market_data,
            )
            upcoming = catalyst_timeline.upcoming
            _log(f"Catalyst calendar: {len(catalyst_timeline.events)} events "
                 f"({len(upcoming)} upcoming)")
            if catalyst_timeline.next_earnings:
                ne = catalyst_timeline.next_earnings
                _log(f"  Next earnings: {ne.expected_date} ({ne.days_until}d)")
        except Exception as e:
            _log(f"Catalyst calendar failed (non-fatal): {e}")

        # ── Step 6f: Insider Trading (SEC Form 4) ──────────────────────
        insider_summary = None
        if not is_a_share:
            try:
                form4 = self._make_form4_connector()
                insider_summary = form4.get_insider_transactions(
                    config.ticker, months=12
                )
                if insider_summary:
                    _log(
                        f"Form 4: {len(insider_summary.transactions)} transactions, "
                        f"sentiment={insider_summary.sentiment}"
                    )
            except Exception as e:
                _log(f"Form 4 fetch failed (non-fatal): {e}")

        # ── Step 6g: News Sentiment ────────────────────────────────────
        news_sentiment_insights = None
        if config.enable_news_sentiment:
            try:
                news_conn = self._make_news_connector()
                articles = news_conn.get_recent_news(config.ticker, limit=20)
                if articles:
                    ns_client = None
                    if config.use_llm:
                        ns_client = self._resolve_llm_client(config, _log, quiet=True)
                    analyzer = self._make_news_sentiment_analyzer(llm_client=ns_client)
                    news_sentiment_insights = analyzer.analyze(
                        articles=articles,
                        symbol=config.ticker,
                        entity_name=entity_name,
                    )
                    _log(
                        f"News sentiment: {news_sentiment_insights.overall_sentiment} "
                        f"(score={news_sentiment_insights.sentiment_score:.2f}, "
                        f"{news_sentiment_insights.article_count} articles)"
                    )
            except Exception as e:
                _log(f"News sentiment failed (non-fatal): {e}")

        # Inject consensus data into market expectations layer input
        consensus_for_agents: dict[str, Any] = {}
        if consensus_estimates:
            for est in consensus_estimates:
                key = f"{est.metric}_{est.period}"
                consensus_for_agents[key] = {
                    "metric": est.metric,
                    "period": est.period,
                    "mean": est.consensus_mean,
                    "high": est.consensus_high,
                    "low": est.consensus_low,
                    "analyst_count": est.analyst_count,
                }

        # ── Step 7: DCF Valuation ────────────────────────────────────
        from aegis.core.truth.scenario_engine.dcf_engine import (
            DCFEngine, DCFInput, RevenueDriverTree, resolve_driver_revenue, apply_driver_deltas,
        )
        from aegis.core.truth.scenario_engine.reverse_dcf_solver import ReverseDCFSolver
        from aegis.core.truth.scenario_engine.sensitivity_analyzer import SensitivityAnalyzer

        dcf = DCFEngine()
        segment_projections_data: dict[str, list[dict]] | None = None

        # ── Step 7a: Build driver tree if sector pack has revenue_drivers ──
        driver_tree: RevenueDriverTree | None = None
        driver_projections: list[dict[str, float]] | None = None
        driver_tree = self._build_driver_tree(
            sector_pack, meta_facts, computed_metrics, entity_id,
            consensus_for_agents, config.terminal_growth_rate,
        )
        if driver_tree:
            _log(f"Driver tree: {driver_tree.decomposition_formula} "
                 f"({len(driver_tree.drivers)} drivers)")

        # Try segment DCF if we have product-level segment revenue
        product_segs = segment_detail.get("product", {}) if segment_detail else {}
        # Need at least 2 segments with material revenue for Sum-of-Parts to be useful
        material_segs = {k: v for k, v in product_segs.items()
                         if v.get("revenue", 0) > meta_facts.get("revenue", 1) * 0.05}

        # De-duplicate overlapping segments (parent + children both reported).
        # If sum of segment revenues > 1.1x company revenue, we have hierarchy
        # pollution. Strategy: greedy subset-sum — find the subset of segments
        # whose revenue sum is closest to (but not exceeding) company revenue,
        # and discard the rest.
        company_rev = meta_facts.get("revenue", 0)
        if company_rev > 0 and material_segs:
            total_seg_rev = sum(v.get("revenue", 0) for v in material_segs.values())
            if total_seg_rev > company_rev * 1.10:
                # Try all 2^N subsets (small N, typically <10 segments)
                seg_list = list(material_segs.items())
                n = len(seg_list)
                if n <= 12:  # Skip if too many segments (exponential)
                    best_subset = None
                    best_diff = float("inf")
                    for mask in range(1, 1 << n):
                        subset_rev = sum(
                            seg_list[i][1].get("revenue", 0)
                            for i in range(n) if mask & (1 << i)
                        )
                        # Prefer subsets whose revenue is within 10% of company rev
                        if 0.85 * company_rev <= subset_rev <= 1.10 * company_rev:
                            diff = abs(subset_rev - company_rev)
                            if diff < best_diff:
                                best_diff = diff
                                best_subset = mask
                    if best_subset is not None:
                        kept = {}
                        dropped = []
                        for i, (sid, sdata) in enumerate(seg_list):
                            if best_subset & (1 << i):
                                kept[sid] = sdata
                            else:
                                dropped.append(sid)
                        material_segs = kept
                        _log(f"  Segment dedup: dropped {dropped} "
                             f"(kept {list(kept.keys())} — sum ≈ company rev)")

        if len(material_segs) >= 2:
            dcf_input, dcf_output, segment_projections_data = self._build_segment_dcf(
                config, meta_facts, computed_metrics, market_data, sector_pack,
                material_segs, consensus_for_agents,
            )
            _log(f"Segment DCF: {len(material_segs)} segments")
        else:
            dcf_input = self._build_dcf_input(
                config, meta_facts, computed_metrics, market_data,
                sector_pack, consensus_for_agents, driver_tree=driver_tree,
                log=_log,
            )
            dcf_output = dcf.compute_dcf(dcf_input)

        # Build flat DCFInput for reverse DCF, sensitivity, and bear/bull
        dcf_input_flat = self._build_dcf_input(
            config, meta_facts, computed_metrics, market_data,
            sector_pack, consensus_for_agents, driver_tree=driver_tree,
            log=_log,
        )

        # Capture driver projections for reports
        if driver_tree:
            _, driver_projections = resolve_driver_revenue(
                meta_facts.get("revenue", 0), driver_tree,
            )

        # ── Step 7b: Scenario Architecture (LLM narrative or mechanical fallback) ──
        from aegis.core.chief_analyst.scenario_architect import ScenarioArchitect

        base_val = dcf_output.per_share_value
        scenario_blueprint = None

        # Default mechanical deltas (fallback)
        bear_growth_delta = [-0.04] * 10
        bear_margin_delta = [-0.03] * 10
        bull_growth_delta = [0.03] * 10
        bull_margin_delta = [0.02] * 10
        scenario_narratives = {"bear": "", "base": "", "bull": ""}
        scenario_probabilities = {"bear": 0.25, "base": 0.50, "bull": 0.25}
        primary_swing_factor = ""

        # ── Aegis 2.0 Phase 2 (C2): valuation stage 输入 digest ──────
        # 基本面子集 + DCF 假设（不含实时价格——「valuation 永远重算」指
        # 毫秒级的 DCF 数学；ScenarioArchitect 的 LLM 叙事是基本面驱动，
        # 基本面未变时 --update 直接复用上次的情景蓝图，不发 LLM 调用）。
        # 注意：此时 meta_facts 尚无 __recent_events（Step 8b 才注入），
        # 每次 run 都在同一缝上取 digest，比较口径一致。
        _valuation_digest = compute_agents_digest(
            meta_facts, sector_pack, config.period,
            extra={
                "wacc": dcf_input_flat.wacc,
                "terminal_growth_rate": dcf_input_flat.terminal_growth_rate,
                "revenue_growth_path": list(dcf_input_flat.revenue_growth_path),
                "operating_margin_path": list(dcf_input_flat.operating_margin_path),
            },
        )
        _scenario_reuse = None
        if getattr(config, "update_mode", False):
            _scenario_reuse = load_stage_checkpoint(
                config.ticker, "valuation",
                smoke_mode=getattr(config, "smoke_mode", False),
                expected_digest=_valuation_digest,
                log=_log,
            )

        if _scenario_reuse is not None:
            # 增量复用：情景蓝图（LLM 叙事产物）引用自上次 run；DCF 数学
            # 仍按本次新数据在 Step 7c 重算。
            _sp = _scenario_reuse.get("payload") or {}
            scenario_blueprint = _sp.get("scenario_blueprint")
            bear_growth_delta = _sp.get("bear_growth_delta") or bear_growth_delta
            bear_margin_delta = _sp.get("bear_margin_delta") or bear_margin_delta
            bull_growth_delta = _sp.get("bull_growth_delta") or bull_growth_delta
            bull_margin_delta = _sp.get("bull_margin_delta") or bull_margin_delta
            scenario_narratives = _sp.get("scenario_narratives") or scenario_narratives
            scenario_probabilities = (
                _sp.get("scenario_probabilities") or scenario_probabilities
            )
            primary_swing_factor = _sp.get("primary_swing_factor") or ""
            _log(
                f"增量复用: 情景蓝图引用自缓存 run "
                f"{_scenario_reuse.get('run_id') or '?'}（基本面输入未变，零 LLM 调用）"
            )
        elif config.use_llm:
            # BUG-7 fix: retry-once on exception. Before this, any transient
            # LLM error (empty tool_call args, 502, content_filter) would
            # cause a silent fallback to mechanical [-4%/-3%] deltas with
            # empty narratives. The retry absorbs flaky failures; the mock
            # fallback is still there for hard failures.
            arch_client = self._resolve_llm_client(config, _log, quiet=True)
            architect = ScenarioArchitect(llm_client=arch_client)
            arch_kwargs = dict(
                entity_id=entity_id,
                entity_name=entity_name or config.ticker,
                base_dcf_assumptions={
                    "revenue_growth_path": list(dcf_input_flat.revenue_growth_path),
                    "operating_margin_path": list(dcf_input_flat.operating_margin_path),
                    "wacc": dcf_input_flat.wacc,
                    "terminal_growth_rate": dcf_input_flat.terminal_growth_rate,
                },
                meta_facts=meta_facts,
                computed_metrics=computed_metrics,
                market_data=market_data,
                sector_pack=sector_pack,
                consensus_data=consensus_for_agents,
                segment_detail=segment_detail,
                macro_context={"language": "zh-CN", "market_id": "cn"} if is_a_share else None,
            )
            try:
                try:
                    scenario_blueprint = architect.architect(**arch_kwargs)
                except Exception as e1:
                    _log(f"  ScenarioArchitect attempt 1 failed ({e1}), retrying once...")
                    scenario_blueprint = architect.architect(**arch_kwargs)
                def _force_bear_signs(case_obj):
                    """In-place sign-correct a bear case: all deltas ≤ 0."""
                    if case_obj is None:
                        return
                    case_obj.revenue_growth_delta = [
                        -abs(d) if d > 0 else d
                        for d in case_obj.revenue_growth_delta
                    ]
                    case_obj.margin_delta = [
                        -abs(d) if d > 0 else d
                        for d in case_obj.margin_delta
                    ]
                    # driver_deltas takes priority over revenue_growth_delta
                    # when the driver tree is present — must also flip these.
                    if case_obj.driver_deltas:
                        case_obj.driver_deltas = {
                            k: [-abs(d) if d > 0 else d for d in v]
                            for k, v in case_obj.driver_deltas.items()
                        }

                def _force_bull_signs(case_obj):
                    """In-place sign-correct a bull case: all deltas ≥ 0."""
                    if case_obj is None:
                        return
                    case_obj.revenue_growth_delta = [
                        abs(d) if d < 0 else d
                        for d in case_obj.revenue_growth_delta
                    ]
                    case_obj.margin_delta = [
                        abs(d) if d < 0 else d
                        for d in case_obj.margin_delta
                    ]
                    if case_obj.driver_deltas:
                        case_obj.driver_deltas = {
                            k: [abs(d) if d < 0 else d for d in v]
                            for k, v in case_obj.driver_deltas.items()
                        }

                bear_case = scenario_blueprint.get_case("bear")
                bull_case = scenario_blueprint.get_case("bull")
                # Sign-correct BEFORE reading deltas out — this also mutates
                # the case_obj stored in scenario_blueprint, so the
                # _driver_adjusted_growth path (which reads case_obj.driver_deltas
                # directly) also sees correct signs.
                _force_bear_signs(bear_case)
                _force_bull_signs(bull_case)
                if bear_case:
                    bear_growth_delta = bear_case.revenue_growth_delta
                    bear_margin_delta = bear_case.margin_delta
                    scenario_narratives["bear"] = bear_case.narrative
                    scenario_probabilities["bear"] = bear_case.probability
                if bull_case:
                    bull_growth_delta = bull_case.revenue_growth_delta
                    bull_margin_delta = bull_case.margin_delta
                    scenario_narratives["bull"] = bull_case.narrative
                    scenario_probabilities["bull"] = bull_case.probability
                base_case = scenario_blueprint.get_case("base")
                if base_case:
                    scenario_narratives["base"] = base_case.narrative
                    scenario_probabilities["base"] = base_case.probability
                primary_swing_factor = scenario_blueprint.primary_swing_factor
                _log(f"ScenarioArchitect: narrative scenarios generated (swing: {primary_swing_factor})")
            except Exception as e:
                _log(f"ScenarioArchitect fallback to mechanical: {e}")

        # ── Step 7c: Build Bear/Bull DCF from scenario deltas ──
        # When driver tree + driver_deltas are available, use driver-level adjustments
        bear_case_obj = scenario_blueprint.get_case("bear") if scenario_blueprint else None
        bull_case_obj = scenario_blueprint.get_case("bull") if scenario_blueprint else None

        def _driver_adjusted_growth(case_obj, default_delta, growth_floor, label):
            """Compute revenue growth path from driver_deltas if available.

            growth_floor: per-year floor for the aggregate-delta fallback.
            AUDIT-A3: bear uses -0.05 (the "deep recession" limit the
            comment below always promised) — the old hardcoded 0.01 floor
            pinned every non-driver-tree bear at +1%/yr growth, making the
            -5% clamp at the call site dead code and recession scenarios
            mathematically inexpressible. Bull keeps the 0.01 floor.
            """
            if driver_tree and case_obj and case_obj.driver_deltas:
                adjusted_tree = apply_driver_deltas(driver_tree, case_obj.driver_deltas)
                adjusted_growth, _ = resolve_driver_revenue(
                    meta_facts.get("revenue", 0), adjusted_tree,
                )
                # AUDIT-A4: apply the same cumulative cap as the base path
                # (BUG-Y23). Without it, a hyper-growth bear/bull tree
                # compounds past the capped base and the values shown in the
                # report degrade to the mechanical 0.5×/2× clamp envelope.
                # R3-1: bound is scale-tiered, identical to the base path.
                _bb_ratio = max_cumulative_growth_ratio(
                    meta_facts.get("revenue", 0), str(config.ticker).isdigit(),
                )
                adjusted_growth, _capped_yr = cap_cumulative_growth_path(
                    adjusted_growth, dcf_input_flat.terminal_growth_rate,
                    max_ratio=_bb_ratio,
                )
                if _capped_yr >= 0:
                    _log(f"  ⚠ {label} driver-tree growth path capped at "
                         f"Y{_capped_yr + 1} (cumulative "
                         f"{_bb_ratio:.0f}× scale-tier threshold, same as base)")
                return adjusted_growth
            # Fallback: apply aggregate delta to base growth path
            return [max(g + d, growth_floor) for g, d in
                    zip(dcf_input_flat.revenue_growth_path, default_delta)]

        bear_growth = _driver_adjusted_growth(bear_case_obj, bear_growth_delta,
                                              growth_floor=-0.05, label="bear")
        bull_growth = _driver_adjusted_growth(bull_case_obj, bull_growth_delta,
                                              growth_floor=0.01, label="bull")

        # Clamp growth deltas to prevent extreme scenarios:
        # Bear: growth can drop but not go below -5% (deep recession) for any year
        # Bull: growth can rise but cap at 60% (hyper-growth ceiling)
        bear_growth = [max(g, -0.05) for g in bear_growth]
        bull_growth = [min(g, 0.60) for g in bull_growth]

        # Clamp margin deltas to a wide sanity envelope. The previous +5% bear
        # floor was wrong for loss-making companies — it forced bear to
        # become *more profitable* than the base margin (ST中珠 base path
        # ≈ -21%, bear was clamped to +5%, making bear a "good news" case
        # and inverting bear/bull entirely). Use a -50% floor (extreme
        # distress) and +80% ceiling (extreme dominance) instead so bear
        # actually represents a worse-than-base scenario for any starting
        # point.
        bear_margins = [max(m + d, -0.50) for m, d in
                        zip(dcf_input_flat.operating_margin_path, bear_margin_delta)]
        bull_margins = [min(m + d, 0.80) for m, d in
                        zip(dcf_input_flat.operating_margin_path, bull_margin_delta)]

        bear_input = DCFInput(**{**dcf_input_flat.__dict__,
            "revenue_growth_path": bear_growth,
            "operating_margin_path": bear_margins,
        })
        bull_input = DCFInput(**{**dcf_input_flat.__dict__,
            "revenue_growth_path": bull_growth,
            "operating_margin_path": bull_margins,
        })
        bear_output = dcf.compute_dcf(bear_input)
        bull_output = dcf.compute_dcf(bull_input)

        # If segment DCF was used, scale bear/bull relative to base
        if segment_projections_data is not None:
            flat_base = dcf.compute_dcf(dcf_input_flat).per_share_value
            if flat_base > 0:
                bear_ratio = bear_output.per_share_value / flat_base
                bull_ratio = bull_output.per_share_value / flat_base
                bear_output = type(bear_output)(**{
                    **bear_output.__dict__,
                    "per_share_value": base_val * bear_ratio,
                })
                bull_output = type(bull_output)(**{
                    **bull_output.__dict__,
                    "per_share_value": base_val * bull_ratio,
                })

        # Sanity clamp: prevent absurd scenario values (BUG-53).
        # Bear floor: base * 0.50 — at worst 50% loss from base
        # Bull cap:   base * 2.00 — at most 100% upside from base
        # Prior clamps (0.10 / 5.0) were designed for hyper-growth names but
        # let mature mega-caps (e.g. AAPL) produce absurd 10%-500% ranges
        # (bear $21 / bull $763 on a $210 base), which then tripped the DCF
        # artifact guard and downgraded the run to needs_review. A 0.50x/2x
        # spread is a reasonable DCF envelope for any profile — hyper-growth
        # names still get room because their base DCF is already elevated.
        bear_ps = bear_output.per_share_value
        bull_ps = bull_output.per_share_value
        # AUDIT-A10: record every mechanical rewrite (envelope clamp or
        # inversion-guard correction) so the report layer can disclose that
        # the displayed scenario value is a clamp artifact, not a model
        # output. BUG-Y37 only added a stderr log — readers of the HTML had
        # no way to tell a genuine 0.5×/2× spread from a clamped one.
        scenario_clamp_flags: dict[str, float] = {}
        if base_val > 0:
            bear_floor = max(0, base_val * 0.50)
            bull_cap = base_val * 2.00
            if bear_ps < bear_floor:
                # BUG-Y37 (2026-05-06): silent envelope clamp used to make
                # the displayed bear value look like a natural 0.5× base
                # output (茅台 v6 produced an exact 0.5/1/2 spread purely
                # from clamping). Log so operators know the LLM/scenario
                # engine actually wanted a wider tail than 0.5×.
                _sym = _currency_symbol_for_logs(meta_facts)
                _log(f"  ⚠ DCF: bear case {_sym}{bear_ps:.2f} clamped UP to "
                     f"floor {_sym}{bear_floor:.2f} (=0.5× base). "
                     f"Wider tail than 0.5× was suppressed.")
                scenario_clamp_flags["bear"] = bear_ps  # AUDIT-A10: raw pre-clamp value
                bear_output = type(bear_output)(**{
                    **bear_output.__dict__,
                    "per_share_value": bear_floor,
                })
            if bull_ps > bull_cap:
                _sym = _currency_symbol_for_logs(meta_facts)
                _log(f"  ⚠ DCF: bull case {_sym}{bull_ps:.2f} clamped DOWN to "
                     f"cap {_sym}{bull_cap:.2f} (=2.0× base). "
                     f"Higher upside than 2× was suppressed.")
                scenario_clamp_flags["bull"] = bull_ps  # AUDIT-A10
                bull_output = type(bull_output)(**{
                    **bull_output.__dict__,
                    "per_share_value": bull_cap,
                })

        # Inversion guard: ensure bear ≤ base ≤ bull ordering.
        # If ScenarioArchitect deltas produce inverted scenarios force correct
        # ordering. For positive base (going-concern profitable companies),
        # use mechanical ±50% spread. For non-positive base (distressed /
        # loss-making — DCF is n/m), `base * 1.5 / 0.5` mathematically makes
        # negatives more or less negative in the wrong direction, so we
        # instead just sort the three computed values ascending and reassign
        # bear/bull to min/max so the ordering invariant holds and the
        # report layer can render the n/m banner.
        bear_final = bear_output.per_share_value
        bull_final = bull_output.per_share_value
        if base_val > 0:
            if bull_final <= base_val:
                scenario_clamp_flags["bull"] = scenario_clamp_flags.get("bull", bull_final)  # AUDIT-A10
                bull_output = type(bull_output)(**{
                    **bull_output.__dict__,
                    "per_share_value": base_val * 1.50,
                })
                _sym = _currency_symbol_for_logs(meta_facts)
                _log(f"  ⚠ CONSISTENCY: Bull {_sym}{bull_final:.2f} ≤ Base {_sym}{base_val:.2f} — auto-corrected to {_sym}{base_val*1.5:.2f}")
            if bear_final >= base_val:
                scenario_clamp_flags["bear"] = scenario_clamp_flags.get("bear", bear_final)  # AUDIT-A10
                bear_output = type(bear_output)(**{
                    **bear_output.__dict__,
                    "per_share_value": max(0, base_val * 0.50),
                })
                _sym = _currency_symbol_for_logs(meta_facts)
                _log(f"  ⚠ CONSISTENCY: Bear {_sym}{bear_final:.2f} ≥ Base {_sym}{base_val:.2f} — auto-corrected to {_sym}{base_val*0.5:.2f}")
        else:
            # base ≤ 0 (DCF n/m for loss-making). Re-sort so bear ≤ base ≤ bull.
            triplet = sorted([bear_final, base_val, bull_final])
            new_bear, _, new_bull = triplet
            if new_bear != bear_final or new_bull != bull_final:
                bear_output = type(bear_output)(**{
                    **bear_output.__dict__, "per_share_value": new_bear,
                })
                bull_output = type(bull_output)(**{
                    **bull_output.__dict__, "per_share_value": new_bull,
                })
                _log(f"  ⚠ CONSISTENCY: base≤0 (DCF n/m), reordered scenarios "
                     f"(bear={new_bear:.2f}, base={base_val:.2f}, bull={new_bull:.2f})")

        # Probability-weighted target price
        # AUDIT-A5: renormalize first. When the LLM drops/renames a case,
        # ScenarioArchitect only normalizes what it returned while the
        # orchestrator defaults (0.25/0.50/0.25) survive for missed cases —
        # the raw weights can sum to 1.25-1.5 and silently inflate pw_value.
        # The renormalized dict is rebound so every downstream consumer
        # (scenarios dict, replay cache, portfolio signal weights) sees the
        # same corrected weights.
        scenario_probabilities = renormalize_scenario_probabilities(
            scenario_probabilities, log=_log,
        )
        pw_value = (
            scenario_probabilities["bear"] * bear_output.per_share_value
            + scenario_probabilities["base"] * dcf_output.per_share_value
            + scenario_probabilities["bull"] * bull_output.per_share_value
        )

        scenarios = {
            "bear_value": bear_output.per_share_value,
            "base_value": dcf_output.per_share_value,
            "bull_value": bull_output.per_share_value,
            "matrix_id": f"sm_{config.ticker.lower()}_{run_id}",
            "bear_narrative": scenario_narratives["bear"],
            "base_narrative": scenario_narratives["base"],
            "bull_narrative": scenario_narratives["bull"],
            "bear_probability": scenario_probabilities["bear"],
            "base_probability": scenario_probabilities["base"],
            "bull_probability": scenario_probabilities["bull"],
            "probability_weighted_value": pw_value,
            "primary_swing_factor": primary_swing_factor,
            "currency": "CNY" if is_a_share else "USD",
            # AUDIT-A10: clamp disclosure. When a scenario value was
            # mechanically rewritten (0.5×/2× envelope or inversion guard),
            # expose the flag + the raw model output so the report renders a
            # footnote instead of passing the artifact off as a model value.
            "bear_clamped": "bear" in scenario_clamp_flags,
            "bull_clamped": "bull" in scenario_clamp_flags,
            "bear_raw_value": scenario_clamp_flags.get("bear"),
            "bull_raw_value": scenario_clamp_flags.get("bull"),
            # DCF bridge components for transparent per-share derivation in report
            "dcf_bridge": {
                "pv_fcff_sum": dcf_output.pv_fcff_sum,
                "pv_terminal_value": dcf_output.pv_terminal_value,
                "enterprise_value": dcf_output.enterprise_value,
                "net_debt": dcf_input_flat.net_debt,
                "equity_value": dcf_output.equity_value,
                "future_shares": dcf_output.future_shares,
                "per_share_value": dcf_output.per_share_value,
            },
            "dcf_assumptions": {
                "wacc": dcf_input_flat.wacc,
                "terminal_growth_rate": dcf_input_flat.terminal_growth_rate,
                "capex_to_revenue_path": list(dcf_input_flat.capex_to_revenue_path),
            },
        }

        # Attach driver decomposition metadata if available
        if driver_tree:
            scenarios["driver_decomposition"] = {
                "formula": driver_tree.decomposition_formula,
                "drivers": [
                    {"name": d.name, "base_value": d.base_value, "unit": d.unit,
                     "growth_path": d.growth_path}
                    for d in driver_tree.drivers
                ],
                "driver_projections": driver_projections,
            }
        _sym = _currency_symbol_for_logs(meta_facts)
        _log(f"DCF: bear={_sym}{scenarios['bear_value']:.2f} base={_sym}{scenarios['base_value']:.2f} bull={_sym}{scenarios['bull_value']:.2f} pw={_sym}{pw_value:.2f}")

        # ── Valuation Consistency Gate ──
        # Check that scenario values are internally consistent and plausible.
        # Flag warnings (but don't block) if values seem off.
        base_v = scenarios["base_value"]
        bear_v = scenarios["bear_value"]
        bull_v = scenarios["bull_value"]
        price = market_data.get("current_price", 0) or 0

        if base_v > 0 and price > 0:
            _sym = _currency_symbol_for_logs(meta_facts)
            # Warn if base value is less than 20% of current price — extreme pessimism
            if base_v < price * 0.20:
                _log(f"  ⚠ CONSISTENCY: Base DCF {_sym}{base_v:.2f} is <20% of price {_sym}{price:.2f} — check model assumptions")
            # Warn if bull < bear (inverted)
            if bull_v < bear_v:
                _log(f"  ⚠ CONSISTENCY: Bull {_sym}{bull_v:.2f} < Bear {_sym}{bear_v:.2f} — scenario inversion")
            # Warn if bear is negative
            if bear_v < 0:
                _log(f"  ⚠ CONSISTENCY: Bear case is negative {_sym}{bear_v:.2f} — may indicate model scaling issue")
            # Warn if scenarios are too compressed (all within 30% of each other)
            if bull_v > 0 and bear_v >= 0 and (bull_v - bear_v) / bull_v < 0.15:
                _log(f"  ⚠ CONSISTENCY: Scenario range too narrow ({_sym}{bear_v:.2f}-{_sym}{bull_v:.2f}) — mechanical fallback may be too conservative")

        # AUDIT 2026-07-12: two-sided valuation sanity (Grok 20-audit P0).
        # The 20%-of-price warn above is one-directional — a base at 10× the
        # market price (宁德 ¥4000+ vs ¥349) passed silently and every
        # magnitude conclusion downstream was built on it. Stamp the shared
        # verdict into scenarios so the synthesizer (strict scrub), the
        # publish gate (valuation_sanity_gate) and the report renderer all
        # read one source of truth.
        from aegis.core.truth.valuation_sanity import check_valuation_sanity
        _sanity = check_valuation_sanity(base_v, price)
        if _sanity is not None:
            scenarios["valuation_sanity"] = _sanity
            if _sanity["mismatch"]:
                _sym = _currency_symbol_for_logs(meta_facts)
                _log(
                    f"  ⚠ VALUATION SANITY: base {_sym}{base_v:.2f} vs price {_sym}{price:.2f} "
                    f"(ratio {_sanity['ratio']:.2f}×) outside sanity band — magnitude "
                    f"conclusions withheld (strict scrub + publish gate block)"
                )

        # Reverse DCF (always uses flat/consolidated input)
        solver = ReverseDCFSolver()
        implied_growth = 0.0
        # BUG-Y13: also track whether the solver actually converged so the
        # report doesn't display a fake-clean number like "Implied Growth:
        # 50.00%" (= the bisection upper bound) for cases where the price-
        # vs-growth curve is non-monotonic (loss-making / high-capex cos).
        implied_growth_unreliable = False
        if market_data["current_price"]:
            rdcf = solver.solve_implied_growth(
                current_price=market_data["current_price"],
                base_revenue=dcf_input_flat.base_revenue,
                operating_margin_path=dcf_input_flat.operating_margin_path,
                capex_to_revenue_path=dcf_input_flat.capex_to_revenue_path,
                effective_tax_rate=dcf_input_flat.effective_tax_rate,
                nwc_to_revenue_delta=dcf_input_flat.nwc_to_revenue_delta,
                terminal_growth_rate=dcf_input_flat.terminal_growth_rate,
                wacc=dcf_input_flat.wacc,
                sbc_to_revenue=dcf_input_flat.sbc_to_revenue,
                dilution_rate_annual=dcf_input_flat.dilution_rate_annual,
                shares_outstanding=dcf_input_flat.shares_outstanding,
                net_debt=dcf_input_flat.net_debt,
                horizon_years=dcf_input_flat.horizon_years,
                # AUDIT-A2: pass D&A/buyback through so the reverse solve
                # runs the exact same FCFF model as the forward DCF —
                # omitting base_depreciation made implied growth ~2× too high
                # for high-D&A names.
                base_depreciation=dcf_input_flat.base_depreciation,
                capex_useful_life_years=dcf_input_flat.capex_useful_life_years,
                buyback_yield_annual=dcf_input_flat.buyback_yield_annual,
            )
            implied_growth = rdcf.implied_value
            if rdcf.boundary_hit is not None:
                implied_growth_unreliable = True
                # Stash the flag where downstream HTML / synthesizer can read it
                # without us having to thread it through every dataclass.
                meta_facts["__implied_growth_unreliable"] = True
                meta_facts["__implied_growth_boundary_hit"] = rdcf.boundary_hit
                _log(
                    f"  ⚠ ReverseDCF: bisection hit {rdcf.boundary_hit} bound "
                    f"({implied_growth*100:.1f}%) — likely non-monotonic price-vs-growth "
                    f"curve (loss-making/high-capex). Treating as unreliable."
                )

        # Sensitivity (uses flat input, but scale results to match segment DCF if used)
        sensitivity = SensitivityAnalyzer()
        sensitivity_results = sensitivity.rank_assumptions(dcf_input_flat)

        # If segment DCF was used, compute scaling factor to align sensitivity
        # values with the segment-based base valuation (consistency gate).
        _sens_scale = 1.0
        if segment_projections_data is not None:
            flat_base_ps = dcf.compute_dcf(dcf_input_flat).per_share_value
            if flat_base_ps > 0 and base_val > 0:
                _sens_scale = base_val / flat_base_ps

        sensitivity_rankings = [
            {"assumption": r.assumption, "impact_pct": r.impact_pct,
             "signed_impact_pct": getattr(r, "signed_impact_pct", r.impact_pct),
             "base_per_share": r.base_per_share * _sens_scale,
             "shocked_per_share": r.shocked_per_share * _sens_scale}
            for r in sensitivity_results
        ]
        def _centered_range(center: float, step: float, width: int,
                            floor: float | None = None,
                            ceiling: float | None = None) -> list[float]:
            values = []
            for i in range(-width, width + 1):
                v = center + i * step
                if floor is not None:
                    v = max(v, floor)
                if ceiling is not None:
                    v = min(v, ceiling)
                values.append(round(v, 4))
            return list(dict.fromkeys(values))

        wacc_range = _centered_range(dcf_input_flat.wacc, 0.005, 3, floor=0.03)
        terminal_growth_range = _centered_range(
            dcf_input_flat.terminal_growth_rate, 0.005, 2,
            floor=0.0, ceiling=max(dcf_input_flat.wacc - 0.005, 0.0),
        )
        two_way = sensitivity.two_way_table(
            dcf_input_flat,
            "wacc", wacc_range,
            "terminal_growth_rate", terminal_growth_range,
        )
        # Scale the 2-way matrix values to match segment-based valuation.
        # AUDIT-A5 follow-up (dcf-core handoff): two_way_table now emits
        # None for infeasible cells (wacc − tg too small); keep them None
        # here so the renderer can show "n/m" instead of crashing on
        # `None * float`.
        scaled_matrix = [
            [None if cell is None else cell * _sens_scale for cell in row]
            for row in two_way.matrix
        ]
        sensitivity_table = {
            "variable_1": two_way.variable_1,
            "variable_2": two_way.variable_2,
            "var1_values": two_way.var1_values,
            "var2_values": two_way.var2_values,
            "matrix": scaled_matrix,
        }

        # ── Step 7d (Aegis 2.0 Phase 0): 预期前沿 + 定价体制 ──────────
        # DESIGN_2.0 §三.A：反推「现价隐含了什么预期」（条件化，设计红线
        # 2），并评估市场按哪种逻辑定价（只改叙事框架，设计红线 1——
        # DCF-vs-price 差值照旧展示，任何下游不得据此隐藏差值）。
        expectations_frontier_summary: dict[str, Any] | None = None
        pricing_regime_dict: dict[str, Any] | None = None
        if market_data["current_price"]:
            try:
                from aegis.core.chief_analyst.thesis_synthesizer import (
                    frontier_prompt_lines,
                )
                from aegis.core.truth.scenario_engine.expectations_frontier import (
                    solve_expectations_frontier,
                )
                _margin_scens = build_margin_scenarios(
                    current_margin=dcf_input_flat.operating_margin_path[0],
                    sector_pack=sector_pack,
                    zh=is_a_share,
                )
                _frontier = solve_expectations_frontier(
                    dcf_input_flat,
                    float(market_data["current_price"]),
                    _margin_scens,
                )
                _frontier_dict = _frontier.to_dict()
                # 全量 dataclass 树进 meta_facts（渲染层 + replay 缓存消费；
                # __ 前缀键会被 agent prompt 序列化层剥掉，不会撑爆 prompt）。
                meta_facts["__expectations_frontier"] = _frontier_dict
                # prompt / rule-based agent 消费的精简摘要（按市场语言渲染）。
                expectations_frontier_summary = {
                    "market_price": _frontier_dict["market_price"],
                    "currency": _frontier_dict["currency"],
                    "base_wacc": _frontier_dict["base_wacc"],
                    "lines": frontier_prompt_lines(
                        _frontier_dict, "zh" if is_a_share else "en",
                    ),
                }
                _log(
                    f"Expectations frontier: {len(_margin_scens)} margin "
                    f"scenario(s) × WACC±1% solved against price "
                    f"{market_data['current_price']:.2f}"
                )
            except Exception as e:
                _log(f"  ⚠ Expectations frontier skipped: {e}")

            # 定价体制感知 v1。terminal_value_gate 是种子特征——该 gate 只依
            # 赖 DCF 输入/输出与指标，可在 publish gate（Step 12）之前单独
            # 预评估，不影响 Step 12 的正式评估结果。
            try:
                from aegis.core.publish_gate import PublishGate
                _tv_gate_early = not PublishGate()._terminal_value_gate({
                    "dcf_output": dcf_output,
                    "dcf_input": dcf_input_flat,
                    "computed_metrics": computed_metrics,
                    "meta_facts": meta_facts,
                }).passed
            except Exception:
                _tv_gate_early = False
            try:
                from dataclasses import asdict as _asdict
                from aegis.core.truth.pricing_regime import assess_pricing_regime
                _regime = assess_pricing_regime(**compute_pricing_regime_inputs(
                    meta_facts=meta_facts,
                    computed_metrics=computed_metrics,
                    market_price=market_data["current_price"],
                    pw_value=pw_value,
                    terminal_value_gate_triggered=_tv_gate_early,
                ))
                pricing_regime_dict = _asdict(_regime)
                meta_facts["__pricing_regime"] = pricing_regime_dict
                _log(
                    f"Pricing regime: dominant={_regime.dominant} "
                    f"top_two={_regime.top_two} "
                    f"weights=({', '.join(f'{k}={v:.2f}' for k, v in _regime.weights.items())})"
                )
            except Exception as e:
                _log(f"  ⚠ Pricing regime skipped: {e}")

        # DCF projections for report (both types now expose .projections)
        raw_projections = dcf_output.projections
        dcf_projections = [
            {"year": p.year, "revenue": p.revenue, "operating_income": p.operating_income,
             "nopat": p.nopat, "depreciation": p.depreciation, "capex": p.capex,
             "sbc": p.sbc, "change_in_nwc": p.change_in_nwc,
             "fcff": p.fcff, "pv_fcff": p.pv_fcff,
             "discount_factor": p.discount_factor}
            for p in raw_projections
        ]

        # ── Stage checkpoint: valuation（Aegis 2.0 Phase 2 C1）───────
        # DCF + 预期前沿 + 定价体制 + 敏感性完成缝。payload 额外携带
        # ScenarioArchitect 的情景蓝图产物，供 --update 复用（见 Step 7b）。
        dump_stage_checkpoint(
            config.ticker, "valuation",
            {
                "dcf_output": dcf_output,
                "dcf_input_flat": dcf_input_flat,
                "dcf_projections": dcf_projections,
                "scenarios": scenarios,
                "scenario_probabilities": scenario_probabilities,
                "sensitivity_table": sensitivity_table,
                "sensitivity_rankings": sensitivity_rankings,
                "implied_growth": implied_growth,
                "expectations_frontier": meta_facts.get("__expectations_frontier"),
                "pricing_regime": meta_facts.get("__pricing_regime"),
                # ScenarioArchitect 产物（LLM 叙事）——C2 复用面
                "scenario_blueprint": scenario_blueprint,
                "bear_growth_delta": bear_growth_delta,
                "bear_margin_delta": bear_margin_delta,
                "bull_growth_delta": bull_growth_delta,
                "bull_margin_delta": bull_margin_delta,
                "scenario_narratives": scenario_narratives,
                "primary_swing_factor": primary_swing_factor,
            },
            digest=_valuation_digest,
            run_id=run_id,
            smoke_mode=getattr(config, "smoke_mode", False),
            log=_log,
        )

        # ── Step 8: Macro Context ────────────────────────────────────
        from aegis.core.macro import MacroContextLayer
        from aegis.core.market_expectations import MarketExpectationsLayer
        from aegis.data_contracts.macro_snapshot_schema import MacroSnapshot

        macro = MacroContextLayer()

        # Use FRED data if available, else fall back to config/hardcoded
        _ffr = openbb_macro.fed_funds_rate if openbb_macro and openbb_macro.fed_funds_rate else config.fed_funds_rate
        _10y = openbb_macro.us_10y_yield if openbb_macro and openbb_macro.us_10y_yield else config.us_10y_yield
        _vix = openbb_macro.vix if openbb_macro and openbb_macro.vix else 18.5
        _pmi = openbb_macro.pmi_manufacturing if openbb_macro and openbb_macro.pmi_manufacturing else 52.3
        _cpi = openbb_macro.cpi_yoy if openbb_macro and openbb_macro.cpi_yoy else 0.028
        _ycs = openbb_macro.yield_curve_slope_2s10s if openbb_macro and openbb_macro.yield_curve_slope_2s10s else 20
        _uer = openbb_macro.unemployment_rate if openbb_macro and openbb_macro.unemployment_rate else 0.038
        _dxy = openbb_macro.usd_dxy if openbb_macro and openbb_macro.usd_dxy else 104.5
        _src = ["openbb_fred"] if openbb_macro else ["auto_research"]

        us_snap = MacroSnapshot(
            macro_snapshot_id=f"ms_us_{run_id}", region="US",
            snapshot_timestamp=datetime.now(timezone.utc),
            cycle_phase_estimate=config.cycle_phase,
            fed_funds_rate=_ffr, us_10y_yield=_10y,
            vix=_vix, pmi_manufacturing=_pmi, pmi_services=54.1, cpi_yoy=_cpi,
            yield_curve_slope_2s10s=_ycs, unemployment_rate=_uer, usd_dxy=_dxy,
            source_ids=_src, ingestion_batch_id="batch_auto",
        )
        macro.update_snapshot(us_snap)
        if openbb_macro:
            _log(f"Macro snapshot enriched from FRED (live data)")

        mkt_exp = MarketExpectationsLayer()
        if market_data["current_price"]:
            mkt_exp.set_current_price(entity_id, market_data["current_price"])

        # Populate MarketExpectationsLayer with real consensus data
        if consensus_estimates:
            ingested = mkt_exp.ingest_consensus_estimates(entity_id, consensus_estimates)
            _log(f"MarketExpectations: ingested {ingested} consensus snapshots")

        # Compute real revision signal (replaces hardcoded "neutral")
        revision_signal = mkt_exp.get_aggregate_revision_signal(entity_id)
        revision_momentum = "neutral"
        revision_signal_detail: dict[str, Any] = {}
        if revision_signal:
            revision_momentum = revision_signal.momentum
            revision_signal_detail = {
                "momentum": revision_signal.momentum,
                "breadth": revision_signal.breadth,
                "acceleration": revision_signal.acceleration,
                "revision_1w_pct": revision_signal.revision_1w_pct,
                "revision_1m_pct": revision_signal.revision_1m_pct,
                "revision_3m_pct": revision_signal.revision_3m_pct,
            }
            _log(f"Revision signal: momentum={revision_signal.momentum}, "
                 f"breadth={revision_signal.breadth}, accel={revision_signal.acceleration}")

        priced_in = mkt_exp.build_priced_in_object(
            entity_id,
            implied_growth=implied_growth,
            implied_terminal_growth=config.terminal_growth_rate,
            implied_growth_unreliable=bool(meta_facts.get("__implied_growth_unreliable")),
        )

        # AUDIT-A9 (BUG-Y20 third path): when reverse-DCF hit a bisection
        # boundary, `implied_growth` is a fake-clean edge value (e.g. 0.50).
        # Y20 gated the Director/Synthesizer prompts and
        # build_priced_in_object, but this hand-assembled dict flowed the raw
        # number into all 7 agents' MACRO CONTEXT (and the __-prefixed
        # meta_facts flag gets stripped from prompts, so agents couldn't see
        # the unreliable marker). Null it out and pass an explicit flag.
        # ── Step 8b (Aegis 2.0 Phase 0 第 3 项): A 股近事件切片 ─────────
        # 公告标题流 + 业绩预告 + 一致预期（含红线 5 覆盖度 gate）。注入
        # 全体 agent 的 MACRO CONTEXT，作为唯一 sanctioned 催化剂事实源——
        # 否则 LLM 只能幻觉「并购故事」。任何数据源失败静默降级为空段。
        recent_events_prompt: str | None = None
        if is_a_share and getattr(config, "enable_recent_events", True):
            try:
                from dataclasses import asdict as _asdict
                from aegis.core.acquisition.connectors.em_events_connector import (
                    fetch_recent_events,
                )
                _rev = fetch_recent_events(config.ticker)
                recent_events_prompt = _rev.to_prompt_block()
                # 结构化切片进 meta_facts（渲染层「市场在定价什么」区块 +
                # replay 缓存消费）；prompt 版本给 chief-analyst 各层复用。
                meta_facts["__recent_events"] = _asdict(_rev)
                meta_facts["__recent_events_prompt"] = recent_events_prompt
                _cons_ok = bool(
                    _rev.consensus and not _rev.consensus.insufficient_coverage
                )
                _log(
                    f"Recent events: {len(_rev.announcements)} 公告 / "
                    f"{len(_rev.forecasts)} 预告 / "
                    f"一致预期={'可用' if _cons_ok else '无有效覆盖'}"
                )
            except Exception as e:
                _log(f"  ⚠ Recent events fetch skipped: {e}")

        # ── Step 8c (Aegis 2.0 Phase 1 任务 D2): 验证点核验 ─────────────
        # 封闭目录检查器（LLM 不参与，纯数据规则）：PIT 季报库 + 近事件
        # 切片 → 6 项核验结果。渲染层据此把 pricing_regime 的验证点清单
        # 从「未核验」升级为「已核验·通过/未通过」+依据。
        if is_a_share:
            try:
                from aegis.core.truth.verification import run_verification
                _ver_results = run_verification(
                    store=_pit_store,
                    entity_id=entity_id,
                    recent_events=meta_facts.get("__recent_events"),
                )
                if _ver_results:
                    meta_facts["__verification"] = [
                        v.to_dict() for v in _ver_results
                    ]
                    _log("Verification: " + ", ".join(
                        f"{v.name_zh}={v.status_zh}" for v in _ver_results))
            except Exception as e:
                _log(f"  ⚠ Verification checks skipped: {e}")
            finally:
                # PIT 库读写均已完成，尽早释放 sqlite 连接。
                if _pit_store is not None:
                    try:
                        _pit_store.close()
                    except Exception:
                        pass
                    _pit_store = None

        _ig_unreliable_for_agents = bool(meta_facts.get("__implied_growth_unreliable"))
        agent_macro = {
            "cycle_phase": config.cycle_phase,
            "priced_in": {
                "implied_revenue_growth": None if _ig_unreliable_for_agents else implied_growth,
                "implied_growth_unreliable": _ig_unreliable_for_agents,
                "implied_terminal_growth": config.terminal_growth_rate,
                # Aegis 2.0 Phase 0：条件化预期前沿摘要（设计红线 2——
                # 「若利润率 X 则需增速 Y」句式；旧 implied_growth 单点字段
                # 保留兼容，None 表示前沿不可用）。
                "expectations_frontier": expectations_frontier_summary,
                "revision_momentum": revision_momentum,
                "revision_signal": revision_signal_detail,
                # Prefer TTM (matches peer comparison source); fall back
                # to FY-static if TTM unavailable. Field name kept as
                # `pe_ratio_fwd` for downstream compat.
                "pe_ratio_fwd": computed_metrics.get("pe_ratio_ttm") or computed_metrics.get("pe_ratio", 0),
            },
            "scenarios": scenarios,
            "current_price": market_data["current_price"],
            "sensitivity_rankings": sensitivity_rankings,
            # OpenBB enrichment for agents
            "consensus_estimates": consensus_for_agents,
            "price_target_consensus": price_target_consensus,
            "peer_count": len(peer_fundamentals),
            "market_id": "cn" if is_a_share else "us",
            "language": "zh-CN" if is_a_share else "en",
            # TODO-5: propagated to llm_agent_base; if True, mock fallback
            # raises instead of silently writing rule-based templates.
            "strict_llm": bool(getattr(config, "strict_llm", False)),
            # TODO-6: when True, agents do a 2-step LLM call (observations
            # then inferences-conditioned-on-observations) instead of a
            # single all-in-one prompt. Cuts wall-clock per agent for the
            # giant A-share DEEP prompts that bottleneck on output thinking.
            "split_prompts": bool(getattr(config, "split_prompts", False)),
        }

        # Aegis 2.0 Phase 0：近事件事实块（A 股）——整段中文 prompt 文本，
        # 首行即「禁止引用未在此列出的催化剂」硬约束，全 agent 可见。
        if recent_events_prompt:
            agent_macro["recent_events"] = recent_events_prompt

        # Aegis 2.0 Phase 1（任务 D1/D4）：相对估值锚 zh_lines 注入全 agent
        # prompt（样本不足时的行本身就是「禁止引用同业倍数」硬约束）。
        # 红线 9：面世数字已由 synthesizer/editor 的 scrubber 白名单同步
        # 注册（relative_valuation_sanctioned_pcts，读 __relative_valuation）。
        if relative_valuation is not None:
            try:
                agent_macro["relative_valuation"] = "\n".join(
                    relative_valuation.zh_lines())
            except Exception as e:
                _log(f"  ⚠ Relative valuation prompt lines skipped: {e}")

        # Aegis 2.0 Phase 0：定价体制（设计红线 1——只用于叙事框架与验证
        # 点选择，禁止任何消费方据此隐藏 / 折扣 DCF-vs-price 差值展示）。
        if pricing_regime_dict:
            agent_macro["pricing_regime"] = {
                "dominant": pricing_regime_dict["dominant"],
                "top_two": list(pricing_regime_dict["top_two"]),
                "weights": {
                    k: round(v, 3)
                    for k, v in pricing_regime_dict["weights"].items()
                },
                "narrative_frame": (
                    pricing_regime_dict["narrative_frame_zh"] if is_a_share
                    else pricing_regime_dict["narrative_frame_en"]
                ),
                "verification_focus": pricing_regime_dict["verification_focus"],
            }

        # Inject catalyst timeline for agents (VariantAnalyst uses catalyst timing)
        if catalyst_timeline:
            agent_macro["catalyst_calendar"] = catalyst_timeline.to_dict()

        # Inject driver decomposition for agents (BusinessAnalyst uses this)
        if driver_tree:
            agent_macro["driver_values"] = {
                d.name: d.base_value for d in driver_tree.drivers
            }
            agent_macro["driver_decomposition"] = driver_tree.decomposition_formula

        # Inject earnings call insights if available
        if earnings_call_insights:
            agent_macro["earnings_call"] = {
                "tone": earnings_call_insights.overall_tone,
                "tone_shift": earnings_call_insights.tone_shift_vs_prior,
                "materiality": earnings_call_insights.materiality,
                "call_summary": earnings_call_insights.call_summary,
                "guidance_items": earnings_call_insights.guidance_items,
                "analyst_focus_topics": earnings_call_insights.analyst_focus_topics,
                "hedging_signals": earnings_call_insights.hedging_signals,
                "management_key_numbers": earnings_call_insights.management_key_numbers,
                "notable_language_changes": earnings_call_insights.notable_language_changes,
                "quarter": earnings_call_insights.quarter,
                "year": earnings_call_insights.year,
            }

        # Inject insider trading data for ManagementAnalyst
        if insider_summary and insider_summary.transactions:
            agent_macro["insider_trading"] = {
                "sentiment": insider_summary.sentiment,
                "net_value": insider_summary.net_value,
                "buy_count": insider_summary.buy_count,
                "sell_count": insider_summary.sell_count,
                "total_buy_value": insider_summary.total_buy_value,
                "total_sell_value": insider_summary.total_sell_value,
                "cluster_detected": insider_summary.cluster_detected,
                "notable_transactions": [
                    {
                        "name": t.filer_name,
                        "title": t.filer_title,
                        "type": t.transaction_type,
                        "value": t.total_value,
                        "date": t.transaction_date,
                    }
                    for t in insider_summary.notable_transactions[:5]
                ],
            }

        # Inject news sentiment for all agents (especially RiskAnalyst, VariantAnalyst)
        if news_sentiment_insights and news_sentiment_insights.article_count > 0:
            agent_macro["news_sentiment"] = {
                "overall_sentiment": news_sentiment_insights.overall_sentiment,
                "sentiment_score": news_sentiment_insights.sentiment_score,
                "sentiment_trend": news_sentiment_insights.sentiment_trend,
                "key_themes": news_sentiment_insights.key_themes,
                "bullish_signals": news_sentiment_insights.bullish_signals,
                "bearish_signals": news_sentiment_insights.bearish_signals,
                "materiality": news_sentiment_insights.materiality,
                "article_count": news_sentiment_insights.article_count,
            }

        # ── Aegis 2.0 Phase 2 (C2): agents stage 复用判定 ─────────────
        # 输入 digest = meta_facts 基本面子集（此时已含 Step 8b 事件块
        # __recent_events + Step 8c 核验结果）+ sector pack + 会计期；
        # 显式排除实时价格/时间戳/行情市值（盘中价格抖动不应作废昨天的
        # 深度分析）。--update 且 digest 与 checkpoint 一致 → 直接加载
        # agent 判断与合成产物，Steps 9b/10/11/12b/12c 均不发 LLM 调用。
        _agents_digest = compute_agents_digest(
            meta_facts, sector_pack, config.period,
        )
        _agents_reuse = None
        _agents_reuse_payload: dict[str, Any] = {}
        if getattr(config, "update_mode", False):
            _agents_reuse = load_stage_checkpoint(
                config.ticker, "agents",
                smoke_mode=getattr(config, "smoke_mode", False),
                expected_digest=_agents_digest,
                log=_log,
            )
            if _agents_reuse is not None:
                _p = _agents_reuse.get("payload")
                _agents_reuse_payload = _p if isinstance(_p, dict) else {}
                if not _agents_reuse_payload.get("all_judgments"):
                    _log("  ⚠ agents checkpoint 缺 all_judgments，忽略复用，正常重跑")
                    _agents_reuse = None
                    _agents_reuse_payload = {}

        # ── Step 9a: Inject calibration context (historical accuracy feedback) ──
        try:
            from aegis.core.memory.calibration_loop import CalibrationLoop
            _cal_loop = CalibrationLoop()
            cal_ctx = _cal_loop.get_calibration_context()
            if cal_ctx.get("total_postmortems", 0) > 0:
                agent_macro["calibration"] = cal_ctx
                _log(f"Calibration: {cal_ctx['total_postmortems']} post-mortems, "
                     f"score={cal_ctx['overall_calibration_score']:.2f}")
        except Exception as e:
            _log(f"Calibration context skipped: {e}")

        # ── Step 9b: Chief Analyst — Research Director (LLM pre-agent) ──
        research_directive = None
        if _agents_reuse is not None:
            # C2 增量复用：Director 指令引用自缓存 run，不发 LLM 调用。
            research_directive = _agents_reuse_payload.get("research_directive")
        elif config.use_llm:
            try:
                from aegis.core.chief_analyst import ResearchDirector
                director = ResearchDirector()
                # Director uses the same LLM backend resolved later, but we need
                # to resolve it here first for the director
                director_client = self._resolve_llm_client(config, _log)
                director._llm = director_client
                research_directive = director.direct(
                    entity_id=entity_id,
                    entity_name=entity_name,
                    meta_facts=meta_facts,
                    computed_metrics=computed_metrics,
                    macro_context=agent_macro,
                    sector_pack=sector_pack,
                    segment_detail=segment_detail,
                    market_data=market_data,
                    consensus_estimates=consensus_for_agents,
                    price_target_consensus=price_target_consensus,
                    scenarios=scenarios,
                    implied_growth=implied_growth,
                    sensitivity_rankings=sensitivity_rankings,
                )
                _log(f"Research Director: hypothesis_type={research_directive.hypothesis_type}, "
                     f"confidence={research_directive.initial_confidence}")
                _log(f"  Opening angle: {research_directive.opening_angle[:100]}...")
                _log(f"  Key variables: {', '.join(research_directive.key_variables)}")
            except Exception as e:
                _log(f"  ⚠ Research Director failed ({e}), proceeding without directive")
                research_directive = None

        # ── Step 10: 7 Agents (sector_pack loaded in Step 6b) ────────
        from aegis.core.agents.base import AgentInput
        open_questions: list[dict[str, Any]] = []

        base_inp = AgentInput(
            entity_id=entity_id, run_id=run_id, question_id=f"q_{config.ticker.lower()}_auto",
            facts=meta_facts,
            metric_results=computed_metrics, macro_context=agent_macro,
            evidence_packets=[], sector_pack=sector_pack,
            segment_data=bridge_result.segment_data,
            segment_ids=list(bridge_result.segment_data.keys()),
            segment_detail=segment_detail,
            # Inject peer fundamentals and historical valuation so agents
            # don't need to ask for "peer median PE" / "3-year PE range" as
            # follow-up questions. These were previously fetched but never
            # surfaced to agent prompts.
            peer_fundamentals=peer_fundamentals or [],
            historical_valuation=historical_valuation or {},
        )

        # Inject Research Director's emphasis into agent macro context
        if research_directive and research_directive.agent_emphasis:
            base_inp.macro_context["research_directive"] = {
                "initial_hypothesis": research_directive.initial_hypothesis,
                "key_variables": research_directive.key_variables,
                "key_controversy": research_directive.key_controversy,
                "agent_emphasis": research_directive.agent_emphasis,
            }

        # AUDIT-D1/D4: concurrency cap + timeouts are backend-tiered. The
        # legacy values (cap=2, batch=4800s, watchdog=1800s) were sized for
        # the subprocess/Claude-CLI path; API backends (deepseek/grok/sdk)
        # get cap=4 (batch 1's four agents run in one wave) and much tighter
        # hang detection (1800s/900s). Resolved once here — the locals feed
        # `_run_batch` below plus every `_agent_watchdog` in this method.
        _backend_kind = self._resolved_backend_kind(config)
        _agent_max_parallel = agent_max_parallel_for(_backend_kind)
        _agent_batch_timeout_s = agent_batch_timeout_for(_backend_kind)
        _agent_watchdog_timeout_s = agent_watchdog_timeout_for(_backend_kind)

        agents_results = {}
        if _agents_reuse is not None:
            # ── C2 增量复用：直接加载缓存 run 的 agent 判断，零 LLM 调用 ──
            all_judgments = list(_agents_reuse_payload.get("all_judgments") or [])
            open_questions = list(_agents_reuse_payload.get("open_questions") or [])
            _agents_reuse_at = str(
                _agents_reuse.get("created_at")
                or _agents_reuse.get("run_id") or "上次运行"
            )
            _log(
                f"增量复用: {len(all_judgments)} 个 agent 判断引用自 "
                f"{_agents_reuse_at}（run {_agents_reuse.get('run_id') or '?'}，"
                f"基本面输入未变，零 LLM 调用）"
            )
        elif config.use_llm:
            from aegis.core.agents import (
                LLMAccountingAnalyst, LLMBusinessAnalyst, LLMSectorContextAgent,
                LLMManagementAnalyst, LLMValuationAnalyst, LLMVariantAnalyst, LLMRiskAnalyst,
            )
            from aegis.core.agents import (
                AccountingAnalyst, BusinessAnalyst, ManagementAnalyst,
                ValuationAnalyst, VariantAnalyst, RiskAnalyst, SectorContextAgent,
            )
            from aegis.core.llm.config import LLMConfig, LLMMode

            # Use fast client for specialist agents when --fast-agents enabled
            # TODO-X3 续: pre-resolve BOTH tiers so per-agent routing can pick
            # premium for thesis-driving agents (valuation/variant/accounting)
            # and flash for context/risk/business/management. When fast_agents
            # is False, both refs point at the same premium client and
            # routing is a no-op.
            llm_client = self._resolve_fast_llm_client(config, _log)
            llm_premium_client = self._resolve_llm_client(config, _log, quiet=True)
            llm_config = LLMConfig(mode=LLMMode.LIVE)  # placeholder for agent init

            # Build agent roster: LLM class, rule-based fallback, agent name
            agent_roster = [
                (LLMAccountingAnalyst, AccountingAnalyst, "accounting_analyst"),
                (LLMBusinessAnalyst, BusinessAnalyst, "business_analyst"),
                (LLMManagementAnalyst, ManagementAnalyst, "management_analyst"),
                (LLMValuationAnalyst, ValuationAnalyst, "valuation_analyst"),
                (LLMVariantAnalyst, VariantAnalyst, "variant_analyst"),
                (LLMRiskAnalyst, RiskAnalyst, "risk_analyst"),
            ]

            # Dynamic execution: Research Director controls depth and order
            agent_depth = {}
            execution_order = [name for _, _, name in agent_roster]  # default order

            if research_directive:
                agent_depth = research_directive.agent_depth or {}
                # TODO-X3: --fast pipeline override. The Director may flag
                # several agents as DEEP (which adds a 300-800 word
                # narrative_supplement); under fast mode we drop everyone
                # back to "standard" so the pipeline finishes in ~10 min
                # instead of ~40 min. "skip" markers are preserved.
                if getattr(config, "fast_pipeline", False):
                    overridden = [n for n, d in agent_depth.items() if d == "deep"]
                    if overridden:
                        agent_depth = {
                            n: ("standard" if d == "deep" else d)
                            for n, d in agent_depth.items()
                        }
                        _log(f"  ⚡ --fast: DEEP override disabled for {', '.join(overridden)}")
                # Reorder agents based on research_priority_order
                priority = research_directive.research_priority_order or []
                # Map priority topics to agent names
                topic_to_agent = {
                    "segment_economics": "business_analyst",
                    "business_quality": "business_analyst",
                    "competitive_moat": "business_analyst",
                    "moat": "business_analyst",
                    "accounting_quality": "accounting_analyst",
                    "earnings_quality": "accounting_analyst",
                    "valuation": "valuation_analyst",
                    "valuation_analysis": "valuation_analyst",
                    "management": "management_analyst",
                    "management_assessment": "management_analyst",
                    "capital_allocation": "management_analyst",
                    "governance": "management_analyst",
                    "variant": "variant_analyst",
                    "variant_thesis": "variant_analyst",
                    "market_expectations": "variant_analyst",
                    "risk": "risk_analyst",
                    "risk_assessment": "risk_analyst",
                    "downside": "risk_analyst",
                }
                # Build prioritized order from Director's priority list
                ordered_agents = []
                seen = set()
                for topic in priority:
                    agent_name = topic_to_agent.get(topic.lower().replace(" ", "_"), "")
                    if agent_name and agent_name not in seen:
                        ordered_agents.append(agent_name)
                        seen.add(agent_name)
                # Append remaining agents not mentioned in priority
                for name in execution_order:
                    if name not in seen:
                        ordered_agents.append(name)
                execution_order = ordered_agents

            # Build lookup maps
            llm_class_map = {name: llm_cls for llm_cls, _, name in agent_roster}
            fallback_map = {name: fb_cls for _, fb_cls, name in agent_roster}

            _log(f"Agent execution plan: {' → '.join(execution_order)}")
            skipped = [n for n in execution_order if agent_depth.get(n) == "skip"]
            if skipped:
                _log(f"  Skipped by Research Director: {', '.join(skipped)}")
            deep = [n for n in execution_order if agent_depth.get(n) == "deep"]
            if deep:
                _log(f"  Deep analysis: {', '.join(deep)}")

            # Inter-agent information flow: accumulate key findings
            cumulative_findings: list[dict[str, Any]] = []

            # BUG-32: topological parallelization. Split execution_order into
            # two batches based on dependency on prior findings:
            #   Batch 1 (independent): business, valuation, accounting, management
            #     — each analyzes primary facts, no peer agent dependency
            #   Batch 2 (depends on Batch 1's cumulative_findings for context):
            #     variant, risk  — they explicitly reference other agents' red flags
            # Within each batch, LLM agents run concurrently via threads (LLM
            # calls are IO-bound so GIL is not a problem). Rule-based agents
            # (skip/light) stay sequential since they're already fast.
            #
            # Expected speedup: agent layer 15 min → ~6 min on Run #4-class runs
            # (4 agents concurrent in Batch 1, 2 agents concurrent in Batch 2,
            # each ~3 min, total ~6 min vs 6×3 serial).
            _SECOND_BATCH_AGENTS = {"variant_analyst", "risk_analyst"}
            batch1_names = [n for n in execution_order if n not in _SECOND_BATCH_AGENTS]
            batch2_names = [n for n in execution_order if n in _SECOND_BATCH_AGENTS]

            import copy as _copy
            from concurrent.futures import ThreadPoolExecutor, as_completed

            def _run_one_llm_agent(
                agent_name: str, depth: str, findings_snapshot: list[dict],
            ) -> tuple[Any, list[dict], list[str]]:
                """Execute one LLM agent with follow-up + re-run handling.
                Thread-safe: deepcopies base_inp so concurrent agents don't
                stomp on each other's supplemental_data / macro_context.

                Returns (AgentOutput, open_question_entries, log_lines).
                Log lines are returned (not printed) so the main thread can
                emit them in a sane order after the batch completes.
                """
                local_logs: list[str] = []

                def _llog(msg: str) -> None:
                    local_logs.append(msg)

                llm_cls = llm_class_map.get(agent_name)
                if not llm_cls:
                    return None, [], local_logs

                agent = llm_cls(llm_config=llm_config)
                # TODO-X3 续: per-agent LLM tier. heavy agents (valuation /
                # variant / accounting) keep premium even when fast_agents
                # is on; everyone else gets the flash tier.
                agent._llm = self._resolve_per_agent_llm_client(
                    config, agent_name, llm_premium_client, llm_client,
                )

                # Deep-copy so this thread's mutations don't leak
                local_inp = _copy.deepcopy(base_inp)
                local_inp.previous_agent_findings = findings_snapshot
                local_inp.supplemental_data = {}  # fresh per agent

                if depth == "deep":
                    if local_inp.macro_context is None:
                        local_inp.macro_context = {}
                    local_inp.macro_context.setdefault("research_directive", {})
                    local_inp.macro_context["research_directive"]["_depth"] = "deep"
                else:
                    if local_inp.macro_context and "research_directive" in local_inp.macro_context:
                        local_inp.macro_context["research_directive"].pop("_depth", None)

                q_entries: list[dict] = []
                try:
                    # Note: SIGALRM watchdog from BUG-43 can't be used inside
                    # threads (signals are main-thread-only). Rely on
                    # ThreadPoolExecutor as_completed timeout at caller level
                    # for hang protection.
                    out = agent.run(local_inp)
                    # BUG-31 fix: first-pass quality gate. Previously if
                    # agent returned weak output (2 obs / 1 inf, e.g.
                    # variant_analyst's intermittent LLM flakiness) we just
                    # kept it because there were no follow-up questions to
                    # trigger a re-run. Now: if first pass is below a floor,
                    # retry ONCE (same prompt, fresh LLM call) and keep the
                    # richer result.
                    FIRST_PASS_MIN_OBS = 4
                    FIRST_PASS_MIN_INF = 2
                    first_obs = len(out.judgment.observations)
                    first_inf = len(out.judgment.inferences)
                    if first_obs < FIRST_PASS_MIN_OBS or first_inf < FIRST_PASS_MIN_INF:
                        _llog(f"  ⚠ {agent_name} first pass too thin "
                              f"({first_obs}/{first_inf} < {FIRST_PASS_MIN_OBS}/{FIRST_PASS_MIN_INF}), "
                              f"retrying once...")
                        try:
                            # AUDIT-B2 wiring: the retry must reach the real
                            # LLM, not replay the identical thin response out
                            # of the disk cache. `bypass_cache` can't be
                            # threaded through agent.run() (llm_agent_base
                            # owns the client call), so perturb the prompt:
                            # macro_context keys are dumped verbatim into the
                            # MACRO CONTEXT section, so a nonce key changes
                            # the cache hash → guaranteed fresh inner call.
                            import uuid as _uuid
                            if local_inp.macro_context is None:
                                local_inp.macro_context = {}
                            local_inp.macro_context["_quality_retry_nonce"] = (
                                f"quality-gate-retry-{_uuid.uuid4().hex[:8]}"
                            )
                            retry_out = agent.run(local_inp)
                            local_inp.macro_context.pop("_quality_retry_nonce", None)
                            retry_obs = len(retry_out.judgment.observations)
                            retry_inf = len(retry_out.judgment.inferences)
                            # TODO-X2: Prefer retry when it rescues a 0-inferences
                            # first pass even if total richness is the same. A
                            # 12/0 first pass with a 10/3 retry is strictly more
                            # useful for downstream Synthesizer than 12/0, even
                            # though obs+inf = 12 < 13 either way.
                            #
                            # 2026-05-05 validation run revealed a worse case:
                            # first_pass = 8/0 REAL LLM, retry = 2/1 MOCK
                            # template. Old inf_rescue accepted the mock and
                            # overwrote 8 real observations with 2 templated
                            # ones. Fix: when fallback status differs, prefer
                            # the non-fallback output regardless of inf count
                            # — real partial > mock complete.
                            first_fb = bool(getattr(out, "is_llm_fallback", False))
                            retry_fb = bool(getattr(retry_out, "is_llm_fallback", False))
                            inf_rescue = (first_inf == 0 and retry_inf > 0)
                            richer = (retry_obs + retry_inf) > (first_obs + first_inf)
                            mock_over_real = retry_fb and not first_fb
                            real_over_mock = first_fb and not retry_fb
                            if mock_over_real:
                                _llog(f"    ↳ retry rejected (mock {retry_obs}/{retry_inf} vs real {first_obs}/{first_inf}); keeping first")
                            elif real_over_mock:
                                out = retry_out
                                _llog(f"    ↳ retry accepted (real-over-mock): {retry_obs}/{retry_inf}")
                            elif inf_rescue or richer:
                                out = retry_out
                                tag = "inf-rescue" if inf_rescue else "richer"
                                _llog(f"    ↳ retry accepted ({tag}): {retry_obs}/{retry_inf}")
                            else:
                                _llog(f"    ↳ retry not richer ({retry_obs}/{retry_inf}), keeping first")
                        except Exception as retry_e:
                            # Nonce must not leak into later follow-up calls.
                            if local_inp.macro_context:
                                local_inp.macro_context.pop("_quality_retry_nonce", None)
                            _llog(f"    ↳ retry failed ({retry_e}), keeping first pass")
                    depth_label = " [DEEP]" if depth == "deep" else ""
                    narr_label = f" +narrative({len(out.narrative_supplement)}ch)" if out.narrative_supplement else ""
                    fq_count = len(out.judgment.follow_up_questions)
                    fq_label = f" +{fq_count}q" if fq_count else ""
                    _llog(f"  {agent_name}{depth_label}: {len(out.judgment.observations)} obs, "
                          f"{len(out.judgment.inferences)} inf{narr_label}{fq_label}")

                    # Follow-up: check high-priority questions
                    high_fqs = [fq for fq in out.judgment.follow_up_questions
                                if fq.priority == "high"]
                    real_answer_count = 0  # BUG-38
                    oos_keys: dict[str, str] = {}
                    for fq in high_fqs:
                        answer = self._try_answer_follow_up(
                            fq, segment_detail, computed_metrics,
                            meta_facts, historical_data,
                        )
                        if answer is None:
                            continue
                        if isinstance(answer, str) and answer.startswith("OUT_OF_SCOPE:"):
                            reason = answer.split(":", 1)[1]
                            oos_keys[fq.data_key] = reason
                            local_inp.supplemental_data[fq.data_key] = (
                                f"(out of scope: {reason} — not available from "
                                f"annual XBRL filings; proceed without this)"
                            )
                            continue
                        local_inp.supplemental_data[fq.data_key] = answer
                        real_answer_count += 1
                        _llog(f"    ↳ Answered: {fq.data_key}")
                    if oos_keys:
                        _llog(f"    ↳ Out-of-scope: {len(oos_keys)} question(s)")

                    # Re-run logic (BUG-38 + BUG-36)
                    # BUG-45: AEGIS_SKIP_RERUN env flag for speed debugging
                    import os as _os
                    _skip_all_rerun = _os.environ.get("AEGIS_SKIP_RERUN", "").strip() not in ("", "0", "false", "False")
                    if _skip_all_rerun and real_answer_count > 0:
                        _llog(f"    ↳ {agent_name} [RE-RUN skipped]: AEGIS_SKIP_RERUN=1")
                    if real_answer_count == 0 and oos_keys:
                        _llog(f"    ↳ {agent_name} [RE-RUN skipped]: "
                              f"{len(oos_keys)} OOS, 0 real data fills")
                    elif real_answer_count > 0 and not _skip_all_rerun:
                        first_out = out
                        try:
                            rerun_out = agent.run(local_inp)
                            first_richness = (
                                len(first_out.judgment.observations)
                                + len(first_out.judgment.inferences)
                            )
                            rerun_richness = (
                                len(rerun_out.judgment.observations)
                                + len(rerun_out.judgment.inferences)
                            )
                            first_narr = len(getattr(first_out, "narrative_supplement", "") or "")
                            rerun_narr = len(getattr(rerun_out, "narrative_supplement", "") or "")
                            accept = (
                                rerun_richness >= first_richness
                                and rerun_narr >= first_narr * 0.5
                            )
                            if accept:
                                out = rerun_out
                                _llog(f"    ↳ {agent_name} [RE-RUN with data]: "
                                      f"{len(out.judgment.observations)} obs, "
                                      f"{len(out.judgment.inferences)} inf")
                            else:
                                _llog(f"    ↳ {agent_name} [RE-RUN rejected]: "
                                      f"first={first_richness}({first_narr}ch narr) "
                                      f"rerun={rerun_richness}({rerun_narr}ch narr) "
                                      f"— keeping first pass")
                        except Exception:
                            pass  # keep first-pass

                    # Collect unanswered questions
                    for fq in out.judgment.follow_up_questions:
                        if fq.data_key in local_inp.supplemental_data:
                            continue
                        entry = {
                            "agent": agent_name,
                            "question": fq.question,
                            "data_type": fq.data_type,
                            "data_key": fq.data_key,
                            "priority": fq.priority,
                        }
                        if fq.data_key in oos_keys:
                            entry["out_of_scope"] = oos_keys[fq.data_key]
                        else:
                            oos = self._classify_out_of_scope(fq)
                            if oos:
                                entry["out_of_scope"] = oos
                        q_entries.append(entry)

                except Exception as e:
                    # LLM fallback chain. The llm_agent_base.py
                    # wrapper already handles content_filter retry via
                    # prompt stripping (BUG-30, v2 after GLM removal); if
                    # that still fails we go straight to rule-based.
                    _llog(f"  ⚠ {agent_name} LLM call failed ({e}), falling back to rule-based")
                    fb_cls = fallback_map.get(agent_name)
                    if fb_cls:
                        out = fb_cls().run(local_inp)
                    else:
                        out = None

                return out, q_entries, local_logs

            def _run_batch(batch: list[str], findings_snapshot: list[dict]) -> None:
                """Run a batch of agents. Sequential for skip/light (rule-based),
                parallel for deep/standard (LLM)."""
                nonlocal cumulative_findings

                deep_or_std: list[tuple[str, str]] = []
                for agent_name in batch:
                    depth = agent_depth.get(agent_name, "standard")
                    if depth == "skip":
                        fb_cls = fallback_map.get(agent_name)
                        if fb_cls:
                            base_inp.previous_agent_findings = findings_snapshot
                            out = fb_cls().run(base_inp)
                            agents_results[agent_name] = out
                            _log(f"  {agent_name}: SKIPPED by Director → rule-based fallback")
                            cumulative_findings.append(
                                self._extract_key_finding(agent_name, out)
                            )
                    elif depth == "light":
                        fb_cls = fallback_map.get(agent_name)
                        if fb_cls:
                            base_inp.previous_agent_findings = findings_snapshot
                            out = fb_cls().run(base_inp)
                            agents_results[agent_name] = out
                            _log(f"  {agent_name}: LIGHT → rule-based (Director: not central to thesis)")
                            cumulative_findings.append(
                                self._extract_key_finding(agent_name, out)
                            )
                    else:
                        deep_or_std.append((agent_name, depth))

                if not deep_or_std:
                    return

                # Parallel LLM agent execution. BUG-32.
                # TODO-4 (2026-04-24): cap max_workers to stay under Anthropic
                # Sonnet's 50 req/min limit on the subprocess path (earlier
                # batches of 4 parallel CLI calls routinely tripped 429).
                # AUDIT-D1 (2026-07-09): that cap=2 was applied to every
                # backend; API backends now use their own tier (default 4)
                # so batch 1's four agents run in a single wave.
                _max_workers = min(_agent_max_parallel, len(deep_or_std))
                with ThreadPoolExecutor(max_workers=_max_workers) as ex:
                    future_to_name = {
                        ex.submit(_run_one_llm_agent, aname, adepth, findings_snapshot): aname
                        for aname, adepth in deep_or_std
                    }
                    # Overall batch wall-time. Each agent normally takes ~3 min;
                    # DEEP analysis (2 LLM calls — initial + narrative_supplement)
                    # on A-share Chinese prompts via subprocess/Sonnet runs 12-25
                    # min per agent. Bumped 720 → 900 on 2026-04-15 (TSLA FY2025);
                    # bumped again 900 → 2400 on 2026-04-23 (BUG-24 follow-up:
                    # TBEA 600089 Run v5 lost 2-of-4 DEEP agents at 15 min mark
                    # while they were mid second LLM call). Anything less than
                    # ~40 min risks silent rule-based fallback that degrades the
                    # report without failing.
                    try:
                        for fut in as_completed(future_to_name, timeout=_agent_batch_timeout_s):
                            aname = future_to_name[fut]
                            try:
                                out, q_entries, local_logs = fut.result(timeout=0)
                                # Emit collected logs in order
                                for line in local_logs:
                                    _log(line.lstrip())
                                if out is not None:
                                    agents_results[aname] = out
                                    open_questions.extend(q_entries)
                                    cumulative_findings.append(
                                        self._extract_key_finding(aname, out)
                                    )
                                else:
                                    _log(f"  ⚠ {aname} returned None, skipping")
                            except Exception as e:
                                _log(f"  ⚠ {aname} thread raised ({e}), falling back to rule-based")
                                fb_cls = fallback_map.get(aname)
                                if fb_cls:
                                    out = fb_cls().run(base_inp)
                                    agents_results[aname] = out
                                    cumulative_findings.append(
                                        self._extract_key_finding(aname, out)
                                    )
                    except Exception as batch_e:
                        _log(f"  ⚠ batch timeout / error ({batch_e}) — "
                             f"some agents may still be running in background threads")
                        # Process whatever DID complete before the timeout
                        for fut, aname in future_to_name.items():
                            if aname in agents_results:
                                continue
                            if fut.done():
                                try:
                                    out, q_entries, local_logs = fut.result(timeout=0)
                                    for line in local_logs:
                                        _log(line.lstrip())
                                    if out is not None:
                                        agents_results[aname] = out
                                        open_questions.extend(q_entries)
                                        cumulative_findings.append(
                                            self._extract_key_finding(aname, out)
                                        )
                                except Exception as done_e:
                                    # AUDIT bonus (TODO-Y8): this was a silent
                                    # `pass` — an agent whose thread finished
                                    # WITH an exception right as the batch
                                    # timed out vanished from the report with
                                    # no fallback and no log line. Mirror the
                                    # normal path: log + rule-based fallback.
                                    _log(f"  ⚠ {aname} completed with error at batch "
                                         f"timeout ({done_e}), falling back to rule-based")
                                    fb_cls = fallback_map.get(aname)
                                    if fb_cls:
                                        out = fb_cls().run(base_inp)
                                        agents_results[aname] = out
                                        cumulative_findings.append(
                                            self._extract_key_finding(aname, out)
                                        )
                            else:
                                # Hung — fall back to rule-based
                                _log(f"  ⚠ {aname} still running past batch timeout, falling back")
                                fb_cls = fallback_map.get(aname)
                                if fb_cls:
                                    out = fb_cls().run(base_inp)
                                    agents_results[aname] = out
                                    cumulative_findings.append(
                                        self._extract_key_finding(aname, out)
                                    )

            if batch1_names:
                _log(f"  Batch 1 (parallel, {len(batch1_names)}): {', '.join(batch1_names)}")
                _run_batch(batch1_names, cumulative_findings.copy())
            if batch2_names:
                _log(f"  Batch 2 (parallel, {len(batch2_names)}): {', '.join(batch2_names)}")
                _run_batch(batch2_names, cumulative_findings.copy())

            if cumulative_findings:
                _log(f"  Inter-agent flow: {len(cumulative_findings)} findings passed between agents")
                red_flags = [f["agent"] for f in cumulative_findings if f.get("red_flag")]
                if red_flags:
                    _log(f"  ⚠ Red flags from: {', '.join(red_flags)}")

            # Sector agent (always runs — provides benchmark context)
            sector_agent = LLMSectorContextAgent(llm_config=llm_config)
            # TODO-X3 续: sector_context is light-tier — it's already on flash
            # by default in fast mode (not in _HEAVY_AGENTS).
            sector_agent._llm = self._resolve_per_agent_llm_client(
                config, "sector_context_agent", llm_premium_client, llm_client,
            )
            sector_inp = AgentInput(
                entity_id=entity_id, run_id=run_id,
                question_id=f"q_{config.ticker.lower()}_sector",
                metric_results=computed_metrics, sector_pack=sector_pack,
                facts=meta_facts, segment_detail=segment_detail,
                # BUG-FIX (2026-04-15): sector_context_agent was missing macro_context,
                # so language directive (zh-CN for A-shares) never reached it, causing
                # English output in an otherwise Chinese report.
                macro_context=agent_macro,
            )
            try:
                # 2026-04-23: bumped 360 → 900 for A-share subprocess/Sonnet.
                with _agent_watchdog(_agent_watchdog_timeout_s, "sector_context"):
                    sector_out = sector_agent.run(sector_inp)
            except Exception as e:
                _log(f"  ⚠ sector_context LLM call failed ({e}), falling back to rule-based")
                sector_out = SectorContextAgent().run(sector_inp)
            agents_results[sector_out.judgment.agent_name] = sector_out
            # TODO-7 (2026-04-24): log sector_context completion so operators
            # can confirm it ran (it never appears in the per-batch agent log
            # lines because it runs outside _run_batch).
            _so_obs = len(getattr(sector_out.judgment, "observations", []) or [])
            _so_inf = len(getattr(sector_out.judgment, "inferences", []) or [])
            # TODO-X2: sector_context bypassed the per-batch first-pass quality
            # gate, so 0-inference outputs slipped through. Mirror the gate here.
            if _so_obs >= 4 and _so_inf == 0:
                _log(f"  ⚠ sector_context_agent: {_so_obs} obs / 0 inf — retrying once")
                try:
                    with _agent_watchdog(_agent_watchdog_timeout_s, "sector_context_retry"):
                        retry_sector = sector_agent.run(sector_inp)
                    r_inf = len(getattr(retry_sector.judgment, "inferences", []) or [])
                    if r_inf > 0:
                        sector_out = retry_sector
                        agents_results[sector_out.judgment.agent_name] = sector_out
                        _so_obs = len(getattr(sector_out.judgment, "observations", []) or [])
                        _so_inf = r_inf
                        _log(f"    ↳ retry rescued: {_so_obs}/{_so_inf}")
                    else:
                        _log(f"    ↳ retry still 0 inf, keeping first pass")
                except Exception as retry_e:
                    _log(f"    ↳ retry failed ({retry_e})")
            _log(f"  sector_context_agent: {_so_obs} obs, {_so_inf} inf")
        else:
            from aegis.core.agents import (
                AccountingAnalyst, BusinessAnalyst, SectorContextAgent,
                ManagementAnalyst, ValuationAnalyst, VariantAnalyst, RiskAnalyst,
            )
            for AgentCls in [AccountingAnalyst, BusinessAnalyst, ManagementAnalyst,
                             ValuationAnalyst, VariantAnalyst, RiskAnalyst]:
                out = AgentCls().run(base_inp)
                agents_results[AgentCls.AGENT_NAME] = out

            # Sector agent with limited input
            # 中文化铁律: pass macro_context so the rule-based sector agent
            # sees language="zh-CN" for A-shares (mirrors the LLM path's
            # BUG-FIX 2026-04-15 at the sector_inp above).
            sector_inp = AgentInput(
                entity_id=entity_id, run_id=run_id,
                question_id=f"q_{config.ticker.lower()}_sector",
                metric_results={k: v for k, v in computed_metrics.items()
                               if k in ("arpu", "dau_mau_ratio", "gross_margin", "operating_margin")},
                sector_pack=sector_pack,
                macro_context=agent_macro,
            )
            sector_out = SectorContextAgent().run(sector_inp)
            agents_results[sector_out.judgment.agent_name] = sector_out

        # BUG-Y29 (2026-05-06): the renderer iterates `all_judgments` to
        # build agent cards but JudgmentContract has no `narrative_supplement`
        # field — that lives on the wrapping AgentOutput. Pipeline log says
        # `+narrative(2135ch) +narrative(823ch) ...` meaning ~10K chars of
        # LLM-generated deep-mode prose were getting silently dropped before
        # ever reaching the HTML. Attach the narrative + fallback flags as
        # runtime attributes so the existing `_g(j, "...", default)` helpers
        # in the renderer pick them up without a signature change.
        # AUDIT-C4: extracted into a local helper so the Step 12c iterative
        # re-run can re-attach attrs onto the fresh judgment objects too.
        def _attach_judgment_runtime_attrs() -> None:
            for _out in agents_results.values():
                try:
                    object.__setattr__(_out.judgment, "narrative_supplement", _out.narrative_supplement)
                    object.__setattr__(_out.judgment, "is_llm_fallback", _out.is_llm_fallback)
                    object.__setattr__(_out.judgment, "llm_fallback_reason", _out.llm_fallback_reason)
                except Exception:
                    # Pydantic strict-frozen models reject setattr — fall back to
                    # patching __dict__ directly so the renderer still finds them.
                    try:
                        _out.judgment.__dict__["narrative_supplement"] = _out.narrative_supplement
                        _out.judgment.__dict__["is_llm_fallback"] = _out.is_llm_fallback
                        _out.judgment.__dict__["llm_fallback_reason"] = _out.llm_fallback_reason
                    except Exception:
                        pass

        if _agents_reuse is None:
            all_judgments = [out.judgment for out in agents_results.values()]
            _attach_judgment_runtime_attrs()
            _log(f"Ran {len(all_judgments)} agents" + (" (LLM)" if config.use_llm else " (rule-based)"))
        # AUDIT-C4: set when Step 12c replaces first-pass judgments so the
        # decision engine context can note it operates on post-iteration data.
        _judgments_updated_after_iteration = False

        # ── Step 11: 7 Critics ───────────────────────────────────────
        from aegis.core.critics import (
            LogicCritic, AccountingCritic, EvidenceCritic, SectorCritic,
            CognitiveBiasCritic, MacroConsistencyCritic, MarketCritic,
            NumericConsistencyCritic,
        )

        # Build substantive edge assessment from actual analysis
        price = market_data.get("current_price", 0)
        base_val = scenarios.get("base_value", 0)
        gap_pct = ((base_val - price) / price * 100) if price else 0
        is_overvalued = gap_pct < -10
        is_undervalued = gap_pct > 10

        # Derive edge type from the gap (must match EdgeType enum values)
        if abs(gap_pct) > 20:
            edge_type = "analytical"  # Strong divergence → analytical edge
            edge_conf = "medium"
        elif abs(gap_pct) > 10:
            edge_type = "analytical"
            edge_conf = "medium"
        else:
            edge_type = "informational"
            edge_conf = "low"

        # Build substantive "why market is wrong" from sensitivity data
        top_driver = sensitivity_rankings[0]["assumption"] if sensitivity_rankings else "revenue_growth"
        # Suppress the "vs historical CAGR" comparison when the CAGR is
        # marked unreliable — comparing implied growth to a junk CAGR
        # produces misleading edge-assessment narrative.
        _cagr = meta_facts.get('__revenue_cagr', 0)
        _cagr_unreliable = meta_facts.get('__revenue_cagr_unreliable', False)
        if sensitivity_rankings:
            _sym = _currency_symbol_for_logs(meta_facts)
            _why_base = (
                f"DCF base case ({_sym}{base_val:.2f}) implies {gap_pct:+.0f}% vs current {_sym}{price:.2f}. "
                f"Key driver is {top_driver} ({sensitivity_rankings[0]['impact_pct']:.1%} valuation sensitivity). "
            )
            # BUG-Y20 follow-up: when reverse-DCF didn't converge, drop the
            # "market-implied growth" assertion altogether — it would
            # otherwise be the misleading fake-clean boundary value.
            # Aegis 2.0 Phase 0（设计红线 2）：前沿可用时，隐含增速表述
            # 必须条件化——单点 "market implies Z% growth" 不再进叙事。
            _ig_unreliable = meta_facts.get("__implied_growth_unreliable")
            if expectations_frontier_summary and expectations_frontier_summary.get("lines"):
                why_wrong = _why_base + (
                    "Market-implied growth is margin-conditional (see the "
                    "expectations frontier: at each terminal-margin scenario the "
                    "price requires a different growth rate)."
                )
            elif _ig_unreliable:
                why_wrong = _why_base + (
                    "Market-implied revenue growth is undefined "
                    "(reverse-DCF non-monotonic for loss-making/high-capex profile)."
                )
            elif _cagr_unreliable:
                why_wrong = _why_base + (
                    f"Market-implied revenue growth of {implied_growth:.1%}; "
                    f"historical CAGR is unreliable for forward extrapolation "
                    f"(see CAGR warnings)."
                )
            else:
                _hist_rev = meta_facts.get('__historical_revenue', {})
                _cagr_yrs = sorted(_hist_rev.keys())
                _cagr_n = (_cagr_yrs[-1] - _cagr_yrs[0]) if len(_cagr_yrs) >= 2 else 0
                _cagr_label = f"{_cagr_n}-year historical CAGR" if _cagr_n else "historical CAGR"
                why_wrong = _why_base + (
                    f"Market-implied revenue growth of {implied_growth:.1%} "
                    f"{'exceeds' if implied_growth > 0.10 else 'is below'} "
                    f"{_cagr_label} of {_cagr:.1%}."
                )
        else:
            why_wrong = f"DCF base case implies {gap_pct:+.0f}% vs current price"

        # Consensus comparison if available
        if consensus_for_agents:
            rev_keys = [k for k in consensus_for_agents if k.startswith("revenue_")]
            if rev_keys:
                first_rev = consensus_for_agents[rev_keys[0]]
                # BUG-Y19 (2026-05-06): A-share why_wrong used to leak `$31B`
                # for ¥310亿 consensus. Use __display so the edge narrative
                # passed to LLM agents (and downstream report) is currency-
                # consistent.
                _disp = (meta_facts.get("__display") or {})
                if _disp.get("symbol") == "¥":
                    why_wrong += (f" Consensus revenue estimate: ¥{first_rev['mean']/1e8:.0f}亿 "
                                  f"({first_rev['analyst_count']} analysts).")
                else:
                    why_wrong += (f" Consensus revenue estimate: ${first_rev['mean']/1e9:.0f}B "
                                  f"({first_rev['analyst_count']} analysts).")

        edge_assessment_dict = {
            "edge_assessment_id": f"ea_{config.ticker.lower()}_{run_id}",
            "thesis_id": f"th_{config.ticker.lower()}_{run_id}",
            "primary_edge_type": edge_type,
            "edge_source": f"DCF + reverse DCF analysis with {len(sensitivity_rankings)} sensitivity factors",
            "edge_durability": "medium_term",
            # BUG-Y20 follow-up: phrase the decay trigger in terms of the
            # driver shift when the implied growth is unreliable, since the
            # numeric value is meaningless in that case.
            "edge_decay_trigger": (
                # Aegis 2.0 Phase 0（设计红线 2）：前沿可用时衰减触发器同样
                # 用条件化表述，不引用单点隐含增速。
                f"disclosed facts start supporting the price-implied "
                f"margin/growth combination (expectations frontier), or key "
                f"assumption ({top_driver}) shifts materially"
                if (expectations_frontier_summary
                    and expectations_frontier_summary.get("lines"))
                else
                f"key assumption ({top_driver}) shifts materially "
                f"(market-implied growth currently unreliable due to non-monotonic DCF)"
                if meta_facts.get("__implied_growth_unreliable")
                else f"Market-implied growth converges to {implied_growth:.1%} or key assumption ({top_driver}) shifts materially"
            ),
            "edge_confidence": edge_conf,
            "why_market_is_wrong": why_wrong,
            "what_would_change_my_mind": f"If {top_driver} deviates >2σ from base case, or if 2+ kill criteria trigger simultaneously",
            "edge_uniqueness": "moderate",
        }

        critic_context = {
            "sector_pack": sector_pack,
            "cycle_phase": config.cycle_phase,
            # AUDIT-A9: same gate as agent_macro — critics must not treat the
            # boundary-hit fake value as ground truth either.
            "priced_in": {
                "implied_revenue_growth": (
                    None if meta_facts.get("__implied_growth_unreliable") else implied_growth
                ),
                "implied_growth_unreliable": bool(meta_facts.get("__implied_growth_unreliable")),
            },
            "edge_assessment": edge_assessment_dict,
            "scenarios": scenarios,
            # SBC treatment mode — critics use this to skip double-counting
            # false positives when the engine has an explicit safe mode set.
            "sbc_treatment": dcf_input_flat.sbc_treatment,
            # BUG-47: expose meta_facts + segment_detail so LogicCritic can
            # run the segment-margin consistency check (Σ seg_margin × seg_rev
            # must not exceed 1.05 × total operating income).
            "meta_facts": meta_facts,
            "computed_metrics": computed_metrics,
            "market_data": market_data,
            "segment_detail": segment_detail,
            # BUG-Y40 (2026-05-06): share the orchestrator's LLM client with
            # critics that need one (currently just llm_judge_critic). Without
            # this the critic creates a fresh client whose CostTracker never
            # gets summed by the run-level "LLM cost: ..." log.
            "shared_llm_client": (
                self._cached_llm_client
                if config.use_llm and getattr(self, "_cached_llm_client", None) is not None
                else None
            ),
            # BUG-Y41 (2026-05-06): expose market_id so accounting_critic
            # can run the China-specific government-subsidy check (CAS
            # allows subsidies in operating income; readers should know).
            # Previously critic_context didn't carry this field, so the
            # `_check_government_subsidy` early-returned on EVERY run —
            # dead code. Now it fires for A-share entities.
            "market_id": "cn" if is_a_share else "us",
        }

        # BUG-44: parallelize critics. They are pure Python and some are
        # heavy (sector + accounting do regex + cross-judgment scans).
        # ThreadPoolExecutor is useless for CPU-bound pure Python (GIL),
        # but these critics spend measurable time in re / dict operations
        # where the GIL releases, and the sheer count (7) + instantiation
        # cost makes the parallelism worthwhile. Falls back to serial if
        # any thread raises.
        from concurrent.futures import ThreadPoolExecutor as _CTPE, as_completed as _cac
        from aegis.core.critics.narrative_fact_critic.critic import NarrativeFactCritic
        from aegis.core.critics.llm_judge_critic.critic import LLMJudgeCritic
        _critic_classes = [LogicCritic, AccountingCritic, EvidenceCritic, SectorCritic,
                           CognitiveBiasCritic, MacroConsistencyCritic, MarketCritic,
                           NumericConsistencyCritic, NarrativeFactCritic, LLMJudgeCritic]
        if _agents_reuse is not None:
            # C2 增量复用：critic 结果与 agent 判断同源缓存（LLMJudgeCritic
            # 现跑会自建 LLM client——复用路径必须零 LLM 调用）。
            critic_results = [
                cr for cr in (_agents_reuse_payload.get("critic_results") or [])
                if cr is not None
            ]
            _log(f"增量复用: {len(critic_results)} 份 critic 结果引用自缓存 run"
                 f"（零 LLM 调用）")
        else:
            critic_results = [None] * len(_critic_classes)

            def _run_one_critic(idx: int, cls):
                return idx, cls().review(all_judgments, context=critic_context)

            try:
                with _CTPE(max_workers=len(_critic_classes)) as _cex:
                    _futs = {_cex.submit(_run_one_critic, i, c): i
                             for i, c in enumerate(_critic_classes)}
                    for _f in _cac(_futs, timeout=180):
                        idx, res = _f.result()
                        critic_results[idx] = res
            except Exception as _ce:
                _log(f"  ⚠ critic parallelization failed ({_ce}), falling back to serial")
                critic_results = [
                    c().review(all_judgments, context=critic_context)
                    for c in _critic_classes
                ]
            _log(f"Ran {len(critic_results)} critics")

        # ── Step 12: Publish Gate ────────────────────────────────────
        from aegis.core.publish_gate import PublishGate

        gate = PublishGate()
        gate_result = gate.evaluate(
            all_judgments, critic_results,
            context={
                "run_manifest_id": run_id,
                "__data_quality_issues": meta_facts.get("__data_quality_issues", []),
                "meta_facts": meta_facts,
                "computed_metrics": computed_metrics,
                "market_data": market_data,
                "segment_detail": segment_detail,
                "segment_projections": segment_projections_data,
                "scenarios": scenarios,
                "dcf_input": dcf_input_flat,
                "dcf_output": dcf_output,
                "sensitivity_table": sensitivity_table,
            },
        )
        if gate_result.publishable:
            _log("Publish Gate: PASSED")
        else:
            _log(f"Publish Gate: BLOCKED by {gate_result.blocked_by}")
            # Log warn counts for debugging
            total_warns = sum(sum(1 for i in cr.issues if i.severity == "warn") for cr in critic_results)
            total_blocks = sum(sum(1 for i in cr.issues if i.severity == "block") for cr in critic_results)
            _log(f"  Critics: {total_blocks} blocks, {total_warns} warns")

        # AUDIT 2026-07-12 (B4): gates that skipped for missing data are not
        # passes — a thesis published while integrity gates couldn't even run
        # must not carry high confidence (新易盛/沈飞 published+high with
        # missing DCF artifacts). Collect skip names for the decision engine.
        _gate_skipped_names = [
            c.gate_name for c in gate_result.checks
            if c.passed and c.severity == "warn" and "skipped" in c.message
        ]
        if _gate_skipped_names:
            _log(f"  Gate skips (missing inputs): {_gate_skipped_names}")

        # ── Step 12b: Chief Analyst — Thesis Synthesis (LLM post-agent) ─
        synthesized_thesis = None
        if _agents_reuse is not None:
            # C2 增量复用：论点合成产物引用自缓存 run（Steps 12b/12c 整体
            # 跳过，不发 LLM 调用）。
            synthesized_thesis = _agents_reuse_payload.get("synthesized_thesis")
            _log("增量复用: 论点合成引用自缓存 run（零 LLM 调用）")
        elif config.use_llm:
            try:
                from aegis.core.chief_analyst import ThesisSynthesizer
                synthesizer = ThesisSynthesizer()
                synth_client = self._resolve_llm_client(config, _log, quiet=True)
                synthesizer._llm = synth_client
                # Collect narrative supplements from deep-mode agents
                narr_supps = {}
                for aname, aout in agents_results.items():
                    narr = getattr(aout, "narrative_supplement", "")
                    if narr:
                        narr_supps[aname] = narr
                if narr_supps:
                    _log(f"  Narrative supplements from {len(narr_supps)} agents: {', '.join(narr_supps.keys())}")

                # BUG-43: watchdog synthesizer too (LLM call can hang)
                # 2026-04-23: bumped 480 → 900 for subprocess/Sonnet A-share.
                with _agent_watchdog(_agent_watchdog_timeout_s, "thesis_synthesizer"):
                    synthesized_thesis = synthesizer.synthesize(
                        entity_id=entity_id,
                        entity_name=entity_name,
                        directive=research_directive,
                        judgments=all_judgments,
                        computed_metrics=computed_metrics,
                        market_data=market_data,
                        scenarios=scenarios,
                        implied_growth=implied_growth,
                        sensitivity_rankings=sensitivity_rankings,
                        meta_facts=meta_facts,
                        narrative_supplements=narr_supps,
                        open_questions=open_questions,
                    )
                _log(f"Thesis Synthesizer: core_thesis length={len(synthesized_thesis.core_thesis)}")
                _log(f"  Variant: {synthesized_thesis.my_variant[:100]}...")
                _log(f"  Edge durability: {synthesized_thesis.edge_durability}")
                if synthesized_thesis.unresolved_tensions:
                    _log(f"  Unresolved tensions: {len(synthesized_thesis.unresolved_tensions)}")
                # Hypothesis validation
                validated = "CONFIRMED" if synthesized_thesis.hypothesis_validated else "REFUTED/REVISED"
                _log(f"  Hypothesis: {validated}")
                if synthesized_thesis.hypothesis_evolution:
                    _log(f"  Evolution: {synthesized_thesis.hypothesis_evolution[:120]}...")
                if synthesized_thesis.biggest_surprise:
                    _log(f"  Biggest surprise: {synthesized_thesis.biggest_surprise[:120]}...")
                if synthesized_thesis.agents_that_challenged:
                    _log(f"  Challenged by: {', '.join(synthesized_thesis.agents_that_challenged)}")

                # ── Step 12c: Iterative Re-Analysis ──────────────────────
                # If hypothesis was refuted AND specific agents challenged it,
                # re-run those agents in DEEP mode for a second pass.
                # BUG-45: parallelized (was serial for-loop, ~3min × N challengers).
                # BUG-45: AEGIS_SKIP_ITERATIVE=1 fully disables this stage.
                import os as _os_iter
                _skip_iterative = _os_iter.environ.get("AEGIS_SKIP_ITERATIVE", "").strip() not in ("", "0", "false", "False")
                if _skip_iterative:
                    _log(f"  ── ITERATIVE RE-ANALYSIS: skipped (AEGIS_SKIP_ITERATIVE=1) ──")
                if (not _skip_iterative
                        and not synthesized_thesis.hypothesis_validated
                        and synthesized_thesis.agents_that_challenged
                        and config.use_llm):
                    challengers = synthesized_thesis.agents_that_challenged
                    _log(f"  ── ITERATIVE RE-ANALYSIS: hypothesis refuted, re-running {len(challengers)} agents in parallel ──")

                    # Inject the synthesizer's findings as context for the re-run
                    rerun_context = {
                        "hypothesis_evolution": synthesized_thesis.hypothesis_evolution,
                        "biggest_surprise": synthesized_thesis.biggest_surprise,
                        "revised_thesis": synthesized_thesis.core_thesis,
                        "original_hypothesis": research_directive.initial_hypothesis if research_directive else "",
                    }
                    base_inp.macro_context.setdefault("research_directive", {})
                    base_inp.macro_context["research_directive"]["_rerun_context"] = rerun_context
                    # TODO-X3: under --fast, even iterative re-analysis stays
                    # standard depth. The re-run still benefits from the
                    # second-pass context but skips the heavy narrative.
                    base_inp.macro_context["research_directive"]["_depth"] = (
                        "standard" if getattr(config, "fast_pipeline", False) else "deep"
                    )

                    # Build accumulated findings from ALL first-pass agents for context
                    base_inp.previous_agent_findings = cumulative_findings.copy() if 'cumulative_findings' in dir() else []

                    # Parallel execution: each challenger gets its own thread
                    # (LLM calls are IO-bound, GIL doesn't serialize them).
                    from concurrent.futures import ThreadPoolExecutor as _ITPE, as_completed as _iac
                    import copy as _icopy

                    def _run_one_challenger(agent_name: str):
                        llm_cls = llm_class_map.get(agent_name) if 'llm_class_map' in dir() else None
                        if not llm_cls:
                            return agent_name, None, None
                        agent = llm_cls(llm_config=llm_config)
                        # TODO-X3 续: challengers are by definition agents that
                        # contradicted the thesis — bump them to premium
                        # regardless of normal heavy/light routing, since the
                        # second pass needs the strongest reasoning.
                        agent._llm = llm_premium_client
                        # BUG-51: deepcopy not shallow. macro_context and
                        # other nested dicts would be shared across threads
                        # under shallow copy, and any agent that mutates
                        # them would cause cross-thread pollution. The BUG-32
                        # parallel path (first-pass batches) already uses
                        # deepcopy for this reason — match that here.
                        local_inp = _icopy.deepcopy(base_inp)
                        out = agent.run(local_inp)
                        return agent_name, out, None

                    challenger_list = [c for c in challengers
                                        if (('llm_class_map' in dir()) and llm_class_map.get(c))]
                    if challenger_list:
                        with _ITPE(max_workers=max(1, len(challenger_list))) as _iex:
                            _fut_map = {_iex.submit(_run_one_challenger, c): c for c in challenger_list}
                            try:
                                for _fut in _iac(_fut_map, timeout=720):
                                    _cname = _fut_map[_fut]
                                    try:
                                        _, out, _err = _fut.result(timeout=0)
                                        if out is None:
                                            continue
                                        agents_results[_cname] = out
                                        _log(f"    {_cname} [RE-RUN DEEP]: {len(out.judgment.observations)} obs, "
                                             f"{len(out.judgment.inferences)} inf"
                                             f"{f' +narrative({len(out.narrative_supplement)}ch)' if out.narrative_supplement else ''}")
                                    except Exception as _re:
                                        _log(f"    ⚠ {_cname} re-run failed ({_re}), keeping first-pass results")
                            except Exception as _be:
                                _log(f"    ⚠ iterative re-analysis batch timeout / error ({_be})")

                    # Clean up rerun context
                    base_inp.macro_context["research_directive"].pop("_rerun_context", None)
                    base_inp.macro_context["research_directive"].pop("_depth", None)

                    # Re-synthesize with updated agent results (including new narrative supplements)
                    _log(f"  Re-synthesizing thesis with {len(challengers)} updated agent results...")
                    all_judgments_v2 = [r.judgment for r in agents_results.values() if hasattr(r, 'judgment')]
                    narr_supps_v2 = {}
                    for aname, aout in agents_results.items():
                        narr = getattr(aout, "narrative_supplement", "")
                        if narr:
                            narr_supps_v2[aname] = narr
                    try:
                        with _agent_watchdog(_agent_watchdog_timeout_s, "thesis_synthesizer[re-synth]"):
                            synthesized_thesis = synthesizer.synthesize(
                                entity_id=entity_id,
                                entity_name=entity_name,
                                directive=research_directive,
                                judgments=all_judgments_v2,
                                computed_metrics=computed_metrics,
                                market_data=market_data,
                                scenarios=scenarios,
                                implied_growth=implied_growth,
                                sensitivity_rankings=sensitivity_rankings,
                                meta_facts=meta_facts,
                                narrative_supplements=narr_supps_v2,
                            )
                        _log(f"  Re-synthesis complete: core_thesis length={len(synthesized_thesis.core_thesis)}")
                        validated_v2 = "CONFIRMED" if synthesized_thesis.hypothesis_validated else "STILL REVISED"
                        _log(f"  Hypothesis after re-analysis: {validated_v2}")
                    except Exception as resynth_e:
                        _log(f"  ⚠ Re-synthesis failed ({resynth_e}), keeping first-pass thesis")

                    # AUDIT-C4: the re-run replaced entries in agents_results,
                    # but `all_judgments` still held the superseded round-1
                    # judgment objects — the decision engine, HTML agent
                    # cards, and replay cache all consumed stale first-pass
                    # analysis while the thesis used round-2 (report
                    # self-contradiction; premium DEEP narratives dropped).
                    # Rebuild the list (same agents_results insertion order as
                    # the first pass) and re-attach the BUG-Y29 narrative /
                    # fallback runtime attrs onto the fresh judgment objects.
                    all_judgments = [out.judgment for out in agents_results.values()]
                    _attach_judgment_runtime_attrs()
                    _judgments_updated_after_iteration = True
                    _log(f"  Judgments rebuilt after iterative re-analysis "
                         f"({len(challenger_list)} agent(s) updated) — decision/HTML/replay now use v2")

            except Exception as e:
                _log(f"  ⚠ Thesis Synthesizer failed ({e}), building director-anchored fallback thesis")
                # BUG-A6 (2026-05-04): when synthesis fails, the prior code
                # set synthesized_thesis=None which made the Report Editor
                # skip entirely (`if config.use_llm and synthesized_thesis is
                # not None`), leaving headline / lede / executiveParagraphs
                # / thesis grid all empty in the rendered HTML. Build a
                # minimal SynthesizedThesis from the Director's directive +
                # the strongest-stance agent so downstream layers have real
                # content to render.
                try:
                    from aegis.core.chief_analyst.thesis_synthesizer import SynthesizedThesis
                    _dir = research_directive
                    _opening = getattr(_dir, "opening_angle", "") or ""
                    _hyp = getattr(_dir, "initial_hypothesis", "") or ""
                    _consensus = getattr(_dir, "what_consensus_likely_believes", "") or ""
                    _why_now = getattr(_dir, "why_now", "") or ""
                    _key_vars = getattr(_dir, "key_variables", []) or []
                    _controversy = getattr(_dir, "key_controversy", "") or ""
                    # Pull a representative agent thesis (highest non-zero
                    # score, prefer variant_analyst then risk_analyst).
                    # AUDIT bonus (2026-07): this loop was dead code —
                    # `all_judgments` is a *list* (never a dict), so the old
                    # `.get(_name) if isinstance(..., dict)` always yielded
                    # None; and it read `.claim` when the Inference schema
                    # field is `text` (same slip BUG-Y36 fixed in
                    # replay_from_cache.py). my_variant/counter_thesis were
                    # always empty on this path. Match by agent_name and read
                    # `.text`, dict-tolerant for replayed pickles.
                    _variant_text = ""
                    _counter_text = ""
                    _by_name = {
                        (getattr(_j, "agent_name", None)
                         or (_j.get("agent_name") if isinstance(_j, dict) else None)): _j
                        for _j in (all_judgments or [])
                    }
                    for _name in ("variant_analyst", "risk_analyst", "valuation_analyst"):
                        _j = _by_name.get(_name)
                        if _j is None:
                            continue
                        _infs = getattr(_j, "inferences", None) or (
                            _j.get("inferences", []) if isinstance(_j, dict) else []
                        )
                        if _infs:
                            _first = _infs[0]
                            _txt = (getattr(_first, "text", None)
                                    or (_first.get("text", "") if isinstance(_first, dict) else "")
                                    or "")
                            if _txt and not _variant_text:
                                _variant_text = _txt
                            elif _txt and not _counter_text:
                                _counter_text = _txt
                    synthesized_thesis = SynthesizedThesis(
                        core_thesis=_opening or _hyp or "（合成失败：缺少核心论点）",
                        my_variant=_variant_text,
                        variant_magnitude="",
                        variant_decomposition_narrative="",
                        why_now=_why_now,
                        market_implied_story=_consensus,
                        key_assumption_disagreement=_controversy,
                        counter_thesis=_counter_text,
                        why_market_is_wrong="",
                        what_would_change_my_mind="；".join(_key_vars[:3]) if _key_vars else "",
                        edge_source="research_director_fallback",
                        edge_durability="short_term",
                        unresolved_tensions=list(_key_vars[:4]),
                        conviction_narrative="（Synthesizer 调用失败，本节由 Director 摘要兜底）",
                        hypothesis_validated=False,
                        hypothesis_evolution=f"Synthesizer LLM 调用失败：{str(e)[:120]}",
                    )
                    _log("  ✓ Fallback thesis built from Director directive + agent inferences")
                except Exception as fb_e:
                    _log(f"  ⚠ Fallback thesis construction also failed: {fb_e}")
                    synthesized_thesis = None

        # ── Checkpoint: dump pipeline state for fast replay ─────────
        # All expensive LLM steps (agents, critics, synthesizer, iterative
        # re-analysis) are complete. Cache everything needed for downstream
        # debugging of gate/decision/editor/html logic.
        try:
            import pickle
            from pathlib import Path as _Path
            # TODO-1: route smoke-mode cache into a sub-dir so plumbing
            # validation runs don't overwrite the LLM-quality replay state
            # used by `python scripts/replay_from_cache.py <ticker>`.
            cache_dir = _Path(".cache/smoke") if getattr(config, "smoke_mode", False) else _Path(".cache")
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_file = cache_dir / f"{config.ticker.lower()}_replay_state.pkl"
            # AUDIT follow-up (2026-07-10): critic_context carries the live
            # shared_llm_client (BUG-Y40 cost accounting). DeepSeek's OpenAI
            # SDK client holds a _thread.RLock — pickling it killed the whole
            # replay dump on every LLM run ("cannot pickle '_thread.RLock'").
            # Drop runtime-only handles from a shallow copy; replay can't use
            # a live client anyway.
            _cc_picklable = critic_context
            if isinstance(critic_context, dict):
                _cc_picklable = {
                    k: v for k, v in critic_context.items()
                    if k != "shared_llm_client"
                }
            state = {
                "entity_id": entity_id,
                "entity_name": entity_name,
                "run_id": run_id,
                "config": config,
                "all_judgments": all_judgments,
                "critic_results": critic_results,
                "critic_context": _cc_picklable,
                "synthesized_thesis": synthesized_thesis,
                "research_directive": research_directive,
                "gate_result_first_pass": gate_result,  # may re-evaluate
                "meta_facts": meta_facts,
                "computed_metrics": computed_metrics,
                "market_data": market_data,
                "scenarios": scenarios,
                "scenario_probabilities": scenario_probabilities,
                "edge_assessment_dict": edge_assessment_dict,
                "dcf_output": dcf_output,
                "dcf_input_flat": dcf_input_flat,
                "dcf_projections": dcf_projections,
                "sensitivity_table": sensitivity_table,
                "sensitivity_rankings": sensitivity_rankings,
                "segment_detail": segment_detail,
                "segment_projections_data": segment_projections_data,
                "consensus_estimates": consensus_estimates,
                "earnings_history": earnings_history,
                "peer_fundamentals": peer_fundamentals,
                "price_target_consensus": price_target_consensus,
                "earnings_call_insights": earnings_call_insights,
                "historical_valuation": historical_valuation,
                "catalyst_timeline": catalyst_timeline,
                "insider_summary": insider_summary,
                "news_sentiment_insights": news_sentiment_insights,
                "implied_growth": implied_growth,
                "open_questions": open_questions,
                "bridge_result_warnings": bridge_result.warnings,
                "log": log,
                # FRED-backed macro snapshot so replays can render the Macro
                # section without re-hitting the FRED API. None for A-shares.
                "macro_snapshot": us_snap if not is_a_share else None,
            }
            try:
                _blob = pickle.dumps(state)
            except Exception:
                # Belt-and-braces: a single unpicklable object must not kill
                # the whole replay cache. Drop offending keys, keep the rest.
                _dropped = []
                for _k in list(state.keys()):
                    try:
                        pickle.dumps(state[_k])
                    except Exception:
                        _dropped.append(_k)
                        state[_k] = None
                _log(f"  ⚠ Replay cache: dropped unpicklable keys {_dropped}")
                _blob = pickle.dumps(state)
            with cache_file.open("wb") as f:
                f.write(_blob)
            _log(f"💾 Replay cache saved: {cache_file} ({cache_file.stat().st_size // 1024}KB)")
            # ── Stage checkpoint: agents（Aegis 2.0 Phase 2 C1）──────
            # 与 replay cache 同缝同 payload（7 agent + critics + 合成器
            # 完成处），外加 agents 输入 digest 供 --update 复用判定。
            # 复用命中的 run 不重写该 checkpoint——保留原分析的 run 时间，
            # 报告标注「智能体分析引用自 {原 run 时间}」才不漂移。
            if _agents_reuse is None:
                dump_stage_checkpoint(
                    config.ticker, "agents", state,
                    digest=_agents_digest,
                    run_id=run_id,
                    smoke_mode=getattr(config, "smoke_mode", False),
                    log=_log,
                )
        except Exception as cache_err:
            _log(f"  ⚠ Replay cache dump failed: {cache_err}")

        # ── Step 13: Decision Engine ─────────────────────────────────
        from aegis.core.decision_engine import DecisionEngine
        from aegis.data_contracts.edge_assessment_schema import EdgeAssessment

        try:
            edge = EdgeAssessment.model_validate(edge_assessment_dict)
        except Exception as edge_err:
            _log(f"  ⚠ EdgeAssessment validation failed ({edge_err}), using defaults")
            # Ensure all string values are plain strings (not Pydantic objects)
            sanitized = {k: str(v) if v is not None else v for k, v in edge_assessment_dict.items()}
            edge = EdgeAssessment.model_validate(sanitized)
        de = DecisionEngine()
        decision = de.decide(
            entity_id, run_id, all_judgments, critic_results,
            gate_result.publishable,
            context={
                "edge_assessment": edge,
                "scenarios": scenarios,
                "macro_dependency": f"US {config.cycle_phase}",
                "sector_cycle_position": "Auto-detected from sector pack",
                "dcf_projections_base": dcf_projections,
                "dcf_assumptions": {
                    "revenue_growth_path": list(dcf_input_flat.revenue_growth_path),
                    "operating_margin_path": list(dcf_input_flat.operating_margin_path),
                    "wacc": dcf_input_flat.wacc,
                    "terminal_growth_rate": dcf_input_flat.terminal_growth_rate,
                    "segment_dcf": segment_projections_data is not None,
                },
                "tv_pct": dcf_output.pv_terminal_value / dcf_output.enterprise_value
                    if dcf_output.enterprise_value else 0,
                "sensitivity_rankings": sensitivity_rankings,
                "sensitivity_table": sensitivity_table,
                "open_questions": open_questions,
                # AUDIT-C4: True when Step 12c replaced first-pass judgments
                # with DEEP re-run results — the engine (and anyone reading
                # the decision context downstream) operates on post-iteration
                # judgments, not the refuted round-1 set.
                "judgments_updated_after_iteration": _judgments_updated_after_iteration,
                # AUDIT 2026-07-12 (B4): gates skipped for missing inputs →
                # confidence cap (published+skips 不得 high).
                "gate_skipped_names": _gate_skipped_names,
            },
            synthesized_thesis=synthesized_thesis,
        )

        # ── BUG-40: DCF data-artifact gate ───────────────────────────
        # If synthesizer's biggest_surprise indicates the DCF was built on
        # a data artifact (wrong share count, wrong FCF basis, capex spike
        # mistaken for steady state), the per-share scenarios in the report
        # are unreliable even if all critics passed. The orchestrator does
        # not currently re-run DCF mid-pipeline, so we downgrade the
        # publishing decision to force human review rather than ship a
        # report whose headline contradicts its own DCF table.
        #
        # BUG-42: the first version of this gate was too loose — any surprise
        # text containing "per share" or "dcf assumption" triggered downgrade,
        # which meant every DCF sensitivity discussion false-positived.
        # Fixed: require co-occurrence of an ERROR term and a DCF REFERENCE
        # term within the same field. "The DCF's per-share value is wrong
        # because share count was undercounted" → HIT. "The DCF is sensitive
        # to capex — per share drops to $120 if capex stays high" → MISS
        # (no error term).
        _DCF_ERROR_TERMS = [
            "data artifact", "wrong basis", "incorrect", "miscounted",
            "mis-stated", "misstated", "undercounted", "overstated",
            "mistaken", "wrong share count", "wrong per-share",
            "wrong per share", "faulty", "erroneous",
            "artifact caused by", "consolidated net income was",
            "stock split not", "dual class not", "dual-class not",
        ]
        _DCF_REFERENCE_TERMS = [
            "dcf", "per-share", "per share", "fair value", "share count",
            "consolidated net income", "fcf basis", "diluted shares",
        ]

        def _is_dcf_artifact(text: str) -> list[str]:
            """Return matched error terms only if an error term co-occurs with
            a DCF reference term in the same text. Empty list = no hit."""
            t = (text or "").lower()
            if not t:
                return []
            err_hits = [e for e in _DCF_ERROR_TERMS if e in t]
            if not err_hits:
                return []
            ref_hits = [r for r in _DCF_REFERENCE_TERMS if r in t]
            if not ref_hits:
                return []
            # Both sides present — high signal that the synthesizer is
            # calling the DCF output itself broken.
            return err_hits

        if synthesized_thesis is not None:
            surprise_hits = _is_dcf_artifact(synthesized_thesis.biggest_surprise)
            evolution_hits = _is_dcf_artifact(synthesized_thesis.hypothesis_evolution)
            artifact_hits = surprise_hits or evolution_hits
            if artifact_hits and decision.publishing_status == "published":
                _log(f"  ⚠ DCF ARTIFACT DETECTED in synthesizer surprise/evolution "
                     f"(matched: {artifact_hits[:3]}) — downgrading to needs_review")
                try:
                    decision = decision.model_copy(update={
                        "publishing_status": "needs_review",
                        "confidence_bucket": (
                            "low" if decision.confidence_bucket in ("high", "medium")
                            else decision.confidence_bucket
                        ),
                    })
                except Exception:
                    # Fallback if decision is not a Pydantic model
                    try:
                        decision.publishing_status = "needs_review"
                    except Exception:
                        pass
        _log(f"Decision: {decision.publishing_status}, confidence={decision.confidence_bucket}")

        # ── Step 14: Portfolio Signal ────────────────────────────────
        from aegis.core.portfolio.portfolio_integration import PortfolioIntegration

        pi = PortfolioIntegration()
        signal = pi.generate_signal(
            decision,
            scenario_weights={"bear": scenario_probabilities["bear"],
                              "base": scenario_probabilities["base"],
                              "bull": scenario_probabilities["bull"]},
        )
        _log(f"Signal: {signal.direction}, conviction={signal.conviction}")

        # ── Step 14a (Aegis 2.0 Phase 2 C3): Thesis 持久化 ────────────
        # 全量与 --update run 结束都 build_thesis_contract +
        # save_thesis_version（append-only JSONL 版本链，.cache/thesis/；
        # smoke 隔离到 .cache/smoke/thesis/，测试可用 AEGIS_THESIS_DIR
        # 重定向）。失败不阻断主流程。
        thesis_version_num: int | None = None
        try:
            import os as _os_thesis
            from aegis.core.thesis import (
                build_thesis_contract,
                save_thesis_version,
            )
            _thesis_dir = _os_thesis.environ.get("AEGIS_THESIS_DIR", "").strip() or (
                ".cache/smoke/thesis" if getattr(config, "smoke_mode", False)
                else ".cache/thesis"
            )
            _contract = build_thesis_contract(
                entity_id=entity_id,
                run_id=run_id,
                synthesized_thesis=synthesized_thesis,
                frontier=meta_facts.get("__expectations_frontier"),
                regime=meta_facts.get("__pricing_regime"),
                verification_results=meta_facts.get("__verification"),
                kill_criteria=getattr(decision, "kill_criteria", None),
                # AUDIT 2026-07-12 (B1/B5): 证伪触发器与 bias 真值随合约落库
                disconfirming_triggers=getattr(
                    decision, "disconfirming_triggers", None),
                scenarios=scenarios,
                publishing_status=getattr(decision, "publishing_status", "draft"),
                confidence=getattr(decision, "confidence_bucket", "medium"),
                bias_check_status=getattr(decision, "bias_check_status", None),
                market_id="cn" if is_a_share else "us",
                accounting_standard="CAS" if is_a_share else "US_GAAP",
            )
            _thesis_rec = save_thesis_version(
                entity_id, _contract, run_id, dir=_thesis_dir,
                # Phase 3: 激活沉睡字段——较上一版的中文变更摘要 + 触发原因
                compute_change_summary=True,
                version_change_trigger=(
                    getattr(config, "update_trigger", "") or None
                ),
                # Phase 3: 记论点建立时现价，供 90 天回看复盘算真实收益
                anchor_price=(market_data.get("current_price") or None),
            )
            thesis_version_num = int(_thesis_rec["version"])
            _log(f"Thesis 持久化: 观点版本 v{thesis_version_num} → "
                 f"{_thesis_dir}/{_contract.entity_id}.jsonl")
        except Exception as _thesis_err:
            _log(f"  ⚠ Thesis 持久化失败（不阻断）: {_thesis_err}")

        # ── Step 14b: Chief Analyst — Report Editor (LLM editorial) ──
        edited_report = None
        if _agents_reuse is not None:
            # C2 增量复用：复用路径零 LLM 调用——Editor 是 LLM 步骤且其
            # 文案内嵌渲染时数字，复用旧稿会把过期价格写进新报告，故整体
            # 跳过，报告走标准版式（与 replay_from_cache 缺省行为一致）。
            _log("增量复用: 跳过 Report Editor（零 LLM 调用，报告用标准版式）")
        elif config.use_llm and synthesized_thesis is not None:
            try:
                from aegis.core.chief_analyst import ReportEditor
                editor = ReportEditor()
                editor_client = self._resolve_llm_client(config, _log, quiet=True)
                editor._llm = editor_client
                # BUG-43: watchdog editor (LLM call)
                # 2026-04-23: bumped 360 → 900 for subprocess/Sonnet A-share.
                with _agent_watchdog(_agent_watchdog_timeout_s, "report_editor"):
                    edited_report = editor.edit(
                        entity_name=entity_name,
                        synthesized_thesis=synthesized_thesis,
                        directive=research_directive,
                        computed_metrics=computed_metrics,
                        market_data=market_data,
                        scenarios=scenarios,
                        meta_facts=meta_facts,
                        segment_detail=segment_detail,
                    )
                _log(f"Report Editor: headline='{edited_report.headline[:80]}...'")
                _log(f"  Section order: {edited_report.section_order[:4]}...")
                _log(f"  Key exhibits: {len(edited_report.key_exhibits)}")
            except Exception as e:
                _log(f"  ⚠ Report Editor failed ({e}), using standard report layout")
                edited_report = None

        # ── Step 15: HTML Report ─────────────────────────────────────
        html_path = None
        if config.generate_html:
            from aegis.core.reports.html_report import generate_html_report

            entity_name_clean, risk_warning_prefix = normalize_entity_display(entity_name)
            html = generate_html_report(
                decision=decision,
                scenarios=scenarios,
                computed_metrics=computed_metrics,
                market_data=market_data,
                agent_judgments=all_judgments,
                critic_results=critic_results,
                meta_facts=meta_facts,
                dcf_projections=dcf_projections,
                sensitivity_table=sensitivity_table,
                sensitivity_rankings=sensitivity_rankings,
                entity_name=entity_name,
                entity_name_clean=entity_name_clean,
                risk_warning_prefix=risk_warning_prefix,
                segment_detail=segment_detail,
                segment_projections=segment_projections_data,
                consensus_estimates=consensus_estimates,
                earnings_history=earnings_history,
                peer_fundamentals=peer_fundamentals,
                price_target_consensus=price_target_consensus,
                edited_report=edited_report,
                research_directive=research_directive,
                synthesized_thesis=synthesized_thesis,
                earnings_call_insights=earnings_call_insights,
                historical_valuation=historical_valuation,
                catalyst_timeline=catalyst_timeline,
                insider_summary=insider_summary,
                news_sentiment_insights=news_sentiment_insights,
                # v2 template-renderer extras (ignored by legacy renderer via shim filter)
                period=config.period,
                dcf_output=dcf_output,
                model_name=self._effective_model_name(config),
                pipeline_duration=_fmt_duration(datetime.now(timezone.utc) - _pipeline_start),
                # Step 8 populates us_snap from FRED (or config fallback).
                # Template hides the section when None (e.g. A-share path).
                macro_snapshot=us_snap if not is_a_share else None,
            )

            # ── Aegis 2.0 Phase 2 (C2/C3): masthead 附近的运行标注 ────
            # 中文小标注（A 股中文化铁律；标注本身为产品级固定文案，两个
            # 市场统一用中文）：观点版本 v{N} + 增量复用时的分析时点声明。
            # 渲染器（html_report_v2）不在本任务名下文件，故在编排层对
            # 成品 HTML 做 <body> 顶部注入，不改渲染器。
            _note_bits: list[str] = []
            if _agents_reuse is not None:
                _reuse_at = str(
                    _agents_reuse.get("created_at")
                    or _agents_reuse.get("run_id") or "上次运行"
                ).replace("T", " ")
                _note_bits.append(
                    f"智能体分析引用自 {_reuse_at}（基本面输入未变）"
                )
            if thesis_version_num is not None:
                _note_bits.append(f"观点版本 v{thesis_version_num}")
            if _note_bits:
                _note_html = (
                    '<div class="aegis-run-note" style="max-width:1180px;'
                    'margin:10px auto 0;padding:6px 16px;font-size:12px;'
                    'line-height:1.6;color:#8a6d1a;'
                    'background:rgba(240,190,60,.12);'
                    'border:1px solid rgba(240,190,60,.4);border-radius:6px;">'
                    + " ｜ ".join(_note_bits) + "</div>"
                )
                _body_at = html.find("<body")
                _body_gt = html.find(">", _body_at) if _body_at != -1 else -1
                if _body_gt != -1:
                    html = html[:_body_gt + 1] + _note_html + html[_body_gt + 1:]
                else:
                    html = _note_html + html

            # TODO-1: smoke mode redirects HTML output to demos/smoke/ so a
            # plumbing test never overwrites the LLM-quality production report.
            if config.output_dir:
                out_dir = Path(config.output_dir)
            elif getattr(config, "smoke_mode", False):
                out_dir = Path("demos/smoke")
            else:
                out_dir = Path("demos")
            out_dir.mkdir(parents=True, exist_ok=True)
            html_file = out_dir / f"{config.ticker.lower()}_{config.period.lower()}_auto_report.html"
            html_file.write_text(html, encoding="utf-8")
            html_path = str(html_file)
            _log(f"HTML report saved to {html_path}")

        # ── Stage checkpoint: report（Aegis 2.0 Phase 2 C1）──────────
        # 渲染完成缝。report 永远重渲染（吃最新价格与估值），digest 只作
        # 追溯记录：本次报告消费的 agents 输入 + 渲染时价格。
        dump_stage_checkpoint(
            config.ticker, "report",
            {
                "html_path": html_path,
                "publishing_status": str(getattr(decision, "publishing_status", "")),
                "confidence_bucket": str(getattr(decision, "confidence_bucket", "")),
                "signal_direction": str(getattr(signal, "direction", "")),
                "agents_digest": _agents_digest,
                "agents_reused": _agents_reuse is not None,
            },
            digest=compute_stage_digest({
                "agents_digest": _agents_digest,
                "current_price": market_data.get("current_price"),
                "period": config.period,
            }),
            run_id=run_id,
            smoke_mode=getattr(config, "smoke_mode", False),
            log=_log,
        )

        # ── Step 16: Record prediction for calibration tracking ────────
        try:
            from aegis.core.memory.calibration_loop import CalibrationLoop
            cal_loop = CalibrationLoop()
            pred_record = cal_loop.record_thesis(decision, signal, market_data)
            _log(f"Prediction recorded: {pred_record.thesis_id} "
                 f"(direction={pred_record.direction}, "
                 f"confidence={pred_record.confidence_bucket})")
        except Exception as e:
            _log(f"Prediction recording skipped: {e}")

        # ── Step 17: LLM cost summary (BUG-33) ─────────────────────────
        # Print aggregate token usage and estimated cost. Each LLM client
        # has its own CostTracker; we walk all cached clients and sum them.
        try:
            client_objs: list[Any] = []
            if getattr(self, "_cached_llm_client", None) is not None:
                client_objs.append(self._cached_llm_client)
            if getattr(self, "_cached_fast_llm_client", None) is not None:
                # Avoid double-counting if fast == premium (fast_agents disabled)
                if self._cached_fast_llm_client is not self._cached_llm_client:
                    client_objs.append(self._cached_fast_llm_client)
            total_calls = 0
            total_input = 0
            total_output = 0
            total_cache_hit = 0
            total_reasoning = 0
            total_cost = 0.0
            disk_hits = 0
            disk_misses = 0
            models_seen: set[str] = set()
            for c in client_objs:
                # TODO-3: surface disk-cache hit/miss when CachedLLMClient is
                # in play. Counters live on the wrapper; inner cost_tracker
                # only sees misses (hits skip the API call entirely).
                if hasattr(c, "_hits") and hasattr(c, "_misses"):
                    disk_hits += c._hits
                    disk_misses += c._misses
                ct = getattr(c, "cost_tracker", None)
                if ct is None:
                    continue
                total_calls += ct.call_count
                total_input += ct.total_input_tokens
                total_output += ct.total_output_tokens
                total_cost += ct.total_cost_usd
                for rec in getattr(ct, "_records", []):
                    if getattr(rec, "model_id", None):
                        models_seen.add(rec.model_id)
                    total_cache_hit += getattr(rec, "cache_read_tokens", 0) or 0
                    total_reasoning += getattr(rec, "reasoning_tokens", 0) or 0
            if total_calls > 0:
                models_str = ",".join(sorted(models_seen)) if models_seen else "?"
                _cache_pct = (
                    f" cache_hit={total_cache_hit:,} ({100.0 * total_cache_hit / total_input:.0f}%)"
                    if total_input > 0 and total_cache_hit > 0 else ""
                )
                # TODO-X6: surface reasoning_tokens share so operators can see
                # how much of the output budget thinking is consuming.
                _think_str = (
                    f" think={total_reasoning:,} ({100.0 * total_reasoning / total_output:.0f}% of out)"
                    if total_output > 0 and total_reasoning > 0 else ""
                )
                _disk_str = (
                    f" disk_cache={disk_hits}/{disk_hits + disk_misses} hits"
                    if (disk_hits + disk_misses) > 0 else ""
                )
                _log(
                    f"LLM cost: {total_calls} calls, "
                    f"in={total_input:,} out={total_output:,} tokens, "
                    f"est=${total_cost:.4f}{_cache_pct}{_think_str}{_disk_str} ({models_str})"
                )
            # TODO-X5: write per-call diagnostics (reasoning previews, token
            # counts) to .cache/llm_trace/<run>.jsonl for post-mortem of
            # 0-inference / empty-args failures. Toggleable via env so we
            # don't pay the disk write on every routine run.
            import os as _os
            if _os.environ.get("AEGIS_DUMP_LLM_TRACE", "").strip() not in ("", "0", "false", "False"):
                try:
                    from pathlib import Path as _Path
                    trace_dir = _Path(".cache/llm_trace")
                    trace_dir.mkdir(parents=True, exist_ok=True)
                    trace_path = trace_dir / f"{config.ticker.lower()}_{run_id}.jsonl"
                    for c in client_objs:
                        ct = getattr(c, "cost_tracker", None)
                        if ct is None or not hasattr(ct, "dump_trace"):
                            continue
                        ct.dump_trace(str(trace_path))
                    _log(f"LLM trace written: {trace_path}")
                except Exception as _te:
                    _log(f"LLM trace dump skipped: {_te}")
        except Exception as e:
            _log(f"Cost summary skipped: {e}")

        return ResearchResult(
            ticker=config.ticker,
            entity_id=entity_id,
            run_id=run_id,
            entity_name=entity_name,
            meta_facts=meta_facts,
            computed_metrics=computed_metrics,
            dcf_per_share=dcf_output.per_share_value,
            scenarios=scenarios,
            implied_growth=implied_growth,
            decision=decision,
            signal=signal,
            segment_detail=segment_detail,
            html_path=html_path,
            bridge_warnings=bridge_result.warnings,
            pipeline_log=log,
            consensus_estimates=consensus_estimates,
            earnings_history=earnings_history,
            peer_fundamentals=peer_fundamentals,
            price_target_consensus=price_target_consensus,
        )

    @staticmethod
    def _extract_key_finding(agent_name: str, out: Any) -> dict[str, Any]:
        """Extract the single most important finding from an agent's output for downstream agents."""
        judgment = out.judgment

        # Pick the highest-confidence inference as the key finding
        key_text = ""
        confidence = "medium"
        if judgment.inferences:
            # Sort by confidence: high > medium > low
            conf_order = {"high": 3, "medium": 2, "low": 1}
            best = max(judgment.inferences, key=lambda i: conf_order.get(i.confidence, 0))
            key_text = best.text
            confidence = best.confidence

        # Detect red flags: any observation with certain keywords or high-severity signals
        # BUG-Y45 (2026-05-06): EN-only keyword set missed every Chinese
        # narrative red flag. CN agents would still surface strong
        # counterarguments (line 3372-3373 below catches those) but the
        # keyword path was silent for A-share. Now both alphabets match,
        # so inter-agent "red_flag from X" cumulative findings are
        # populated even when the LLM speaks Chinese.
        red_flag = False
        red_flag_en = {"concern", "risk", "unusual", "aggressive", "deteriorat",
                       "warning", "questionable", "declining", "negative", "red flag",
                       "fabricated", "implausible", "overstated"}
        red_flag_zh = {
            "风险", "异常", "激进", "恶化", "警示", "存疑", "下滑",
            "下行", "压力", "红旗", "脆弱", "可疑", "造假", "粉饰",
            "虚增", "高估", "过度", "不可持续", "不合理",
        }
        for obs in judgment.observations:
            txt_lc = obs.text.lower()
            if any(kw in txt_lc for kw in red_flag_en):
                red_flag = True
                break
            if any(kw in obs.text for kw in red_flag_zh):
                red_flag = True
                break
        # Also flag if any counterargument is "strong"
        if any(c.strength == "strong" for c in judgment.counterarguments):
            red_flag = True

        return {
            "agent": agent_name,
            "key_finding": key_text[:300],  # Truncate to keep prompts manageable
            "red_flag": red_flag,
            "confidence": confidence,
            "num_observations": len(judgment.observations),
        }

    @staticmethod
    def _classify_out_of_scope(fq: Any) -> str | None:
        """Classify follow-up questions that are fundamentally outside our
        data scope (annual XBRL filings + yfinance market data).

        Returns the OOS reason if matched, None if the question MIGHT be
        answerable from our data and should go through _try_answer_follow_up.

        Why this matters: agents legitimately ask for quarterly trends,
        customer concentration, qualitative policy text, etc. — but our
        pipeline only ingests annual 10-K filings. Without classification,
        these questions show up as "Open Research Questions" in the report,
        making it look like we failed to answer something, when in reality
        the question is structurally unanswerable. Classifying them lets the
        report split into "actionable gaps" vs "out-of-scope follow-ups".
        """
        q = (fq.question or "").lower()
        key = (fq.data_key or "").lower()
        full = q + " " + key
        # Quarterly data — we only have annual
        if any(p in full for p in ("quarterly", "past 8 quarter", "past eight quarter",
                                    "by quarter", "qoq", "q-o-q", "trailing twelve",
                                    "ttm", "last 4 quarter", "last four quarter")):
            return "quarterly_data_not_available"
        # Customer concentration — not in 10-K segment disclosures
        if any(p in full for p in ("customer concentration", "top 4 customer",
                                    "top four customer", "top 5 customer",
                                    "top five customer", "top 10 customer",
                                    "by customer", "per customer", "customer breakdown",
                                    "hyperscaler customer", "by end customer",
                                    "by end-customer")):
            return "customer_concentration_not_disclosed"
        # Qualitative policy / 10-K text body — we extract numbers, not narrative
        if any(p in full for p in ("revenue recognition polic", "accounting polic",
                                    "disclosed polic", "policy for", "language about",
                                    "narrative about", "discussion of",
                                    "management commentary", "10-k discussion")):
            return "qualitative_text_not_extracted"
        # Sub-line-item breakdowns of aggregate balance sheet items
        if any(p in full for p in ("breakdown of other current",
                                    "breakdown of other non-current",
                                    "breakdown of other assets",
                                    "breakdown of other liabilities",
                                    "components of other",
                                    "decomposition of other")):
            return "balance_sheet_subline_not_disclosed"
        # Pricing / unit / volume — not in financial filings
        if any(p in full for p in ("asp by ", "average selling price by",
                                    "unit shipment", "units shipped",
                                    "volume by ", "price per unit")):
            return "unit_pricing_not_disclosed"
        # Segment-level historical trends — we have current-year segments and
        # multi-year consolidated, but not multi-year segment breakdowns.
        if any(p in full for p in ("attach rate", "cohort", "segment trend",
                                    "by segment over", "segment growth over",
                                    "segment margin over", "compute-to-networking",
                                    "networking-to-compute")):
            return "segment_history_not_available"
        if "segment" in full and any(p in full for p in (
            "over the past", "over the last", "over time", "trended",
            "trend over", "historical trend",
        )):
            return "segment_history_not_available"
        return None

    @staticmethod
    def _dedupe_segment_detail(
        segment_detail: dict[str, Any],
        company_revenue: float,
    ) -> dict[str, Any]:
        """Clean up segment_detail so each axis category has non-overlapping
        members that sum to company revenue.

        XBRL dimensional facts often include both parent roll-ups AND child
        components on the same axis (e.g. GOOGL reports "Google Advertising
        Revenue" = $294B AND its sub-members Search/YouTube/Network). The
        parser cannot distinguish parent from children without the calculation
        linkbase, so we apply a subset-sum heuristic per category:

        - If sum(members) > 1.10 × company_revenue → roll-up contamination.
          Pick the subset whose sum is closest to company_revenue within
          [0.85×, 1.10×]. Exponential but N is small (<12).
        - If sum(members) < 0.85 × company_revenue after dedup → axis is
          incomplete (e.g. GOOGL business_segment missing Google Network).
          Insert a synthetic 'other_unallocated' entry to close the gap.

        BUG-46 (v2): previously dedup only ran on a local copy for Segment-DCF.
        Now mutates segment_detail in place so HTML/agents/LLM all see the
        cleaned version and totals line up with reported revenue.
        """
        if not segment_detail or company_revenue <= 0:
            return segment_detail

        for category, segments in list(segment_detail.items()):
            if not isinstance(segments, dict) or not segments:
                continue

            # Only keep items with positive revenue for dedup analysis
            with_rev = [
                (sid, sdata) for sid, sdata in segments.items()
                if isinstance(sdata, dict) and sdata.get("revenue", 0) > 0
            ]
            if not with_rev:
                continue

            total = sum(s.get("revenue", 0) for _, s in with_rev)

            # Step 1: detect parent/child hierarchy contamination
            if total > company_revenue * 1.10 and len(with_rev) <= 12:
                n = len(with_rev)
                best_subset = None
                # Tie-breaker: prefer subsets closer to revenue, then larger
                # (more granular breakdown is more informative to readers).
                best_key: tuple[float, int] = (float("inf"), 0)
                for mask in range(1, 1 << n):
                    subset_rev = sum(
                        with_rev[i][1].get("revenue", 0)
                        for i in range(n) if mask & (1 << i)
                    )
                    if 0.85 * company_revenue <= subset_rev <= 1.10 * company_revenue:
                        diff = abs(subset_rev - company_revenue)
                        # Bucket diffs into ~1% of revenue bins so tie-breaker
                        # on member count kicks in for near-equivalent subsets.
                        diff_bin = round(diff / (company_revenue * 0.05))
                        count = bin(mask).count("1")
                        key = (diff_bin, -count)  # smaller diff_bin, then more members
                        if key < best_key:
                            best_key = key
                            best_subset = mask

                if best_subset is not None:
                    kept: dict[str, Any] = {}
                    for i, (sid, sdata) in enumerate(with_rev):
                        if best_subset & (1 << i):
                            kept[sid] = sdata
                    # Preserve zero-revenue entries (non-revenue segment facts)
                    for sid, sdata in segments.items():
                        if sid not in kept and (
                            not isinstance(sdata, dict) or sdata.get("revenue", 0) <= 0
                        ):
                            kept[sid] = sdata
                    segment_detail[category] = kept
                    segments = kept
                    total = sum(
                        s.get("revenue", 0) for s in kept.values()
                        if isinstance(s, dict)
                    )

            # Step 2: close gap with synthetic "other_unallocated" if the
            # axis is incomplete. Only apply when gap > 5% of revenue.
            gap = company_revenue - total
            if gap > company_revenue * 0.05 and "other_unallocated" not in segments:
                segment_detail[category]["other_unallocated"] = {
                    "revenue": gap,
                    "_synthetic": True,
                }

        return segment_detail

    @staticmethod
    def _try_answer_follow_up(
        fq: Any,
        segment_detail: dict[str, Any],
        computed_metrics: dict[str, float],
        meta_facts: dict[str, Any],
        historical_data: dict[int, dict[str, float]],
    ) -> Any:
        """Try to answer a follow-up question from already-available data.

        Returns the answer value if found, None if the data doesn't exist,
        or a string starting with "OUT_OF_SCOPE:" if the question is
        fundamentally unanswerable from our data sources.
        """
        oos_reason = AutoResearchOrchestrator._classify_out_of_scope(fq)
        if oos_reason:
            return f"OUT_OF_SCOPE:{oos_reason}"

        key = fq.data_key
        dtype = fq.data_type

        if dtype == "metric":
            if key in computed_metrics:
                return computed_metrics[key]
            # Try fuzzy match (e.g. "gross_margin" in "gross_margin_ttm")
            for mk, mv in computed_metrics.items():
                if key in mk or mk in key:
                    return mv

        elif dtype == "segment":
            # Look for segment-level data across all categories
            for category, segments in segment_detail.items():
                if not isinstance(segments, dict):
                    continue
                # Check if key matches a category or if data can be extracted
                if key.replace("_by_segment", "").replace("_by_", "_") in category:
                    return segments
                # Build a lookup by metric name across segments
                for seg_id, seg_data in segments.items():
                    metric_name = key.replace("_by_segment", "").replace("_by_product", "").replace("_by_geography", "")
                    if isinstance(seg_data, dict) and metric_name in seg_data:
                        # Found the metric at segment level — build full answer
                        result = {}
                        for sid, sd in segments.items():
                            if isinstance(sd, dict) and metric_name in sd:
                                result[sid] = sd[metric_name]
                        if result:
                            return result

        elif dtype == "fact":
            if key in meta_facts:
                return meta_facts[key]
            # Try without underscores / with alternate naming
            alt_key = key.replace("_", "")
            for fk, fv in meta_facts.items():
                if fk.replace("_", "") == alt_key:
                    return fv

        elif dtype == "time_series":
            if historical_data:
                # Try to extract a time series for the requested key
                series = {}
                for year, year_data in sorted(historical_data.items()):
                    if isinstance(year_data, dict):
                        val = year_data.get(key)
                        if val is not None:
                            series[year] = val
                if series:
                    return series

        return None

    def _check_llm_backend_health(self, config: Any) -> str | None:
        """Cheap smoke test for the chosen LLM backend.

        Returns None on success, or an error string describing what's wrong.
        Intentionally conservative: we only flag definitive failures (e.g.
        401 from DeepSeek, claude CLI not on PATH, missing key for SDK). A
        transient network blip would still let the pipeline start and let
        per-agent retries handle it.
        """
        import os
        import subprocess as _sp
        import urllib.request as _ur
        import urllib.error as _uerr

        backend = config.llm_backend
        if backend == "auto":
            has_deepseek = bool(config.deepseek_api_key or os.environ.get("DEEPSEEK_API_KEY"))
            has_grok = bool(config.grok_api_key or os.environ.get("GROK_API_KEY") or os.environ.get("XAI_API_KEY"))
            has_api = bool(os.environ.get("ANTHROPIC_API_KEY"))
            has_oauth = bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))
            in_session = bool(os.environ.get("CLAUDE_CODE_ENTRYPOINT"))
            if has_deepseek:
                backend = "deepseek"
            elif has_grok:
                backend = "grok"
            elif has_api or (has_oauth and not in_session):
                backend = "sdk"
            else:
                backend = "subprocess"

        if backend == "deepseek":
            key = (config.deepseek_api_key
                   or os.environ.get("DEEPSEEK_API_KEY"))
            if not key:
                return "backend=deepseek but no DEEPSEEK_API_KEY in env"
            try:
                req = _ur.Request(
                    "https://api.deepseek.com/v1/models",
                    headers={"Authorization": f"Bearer {key}"},
                )
                with _ur.urlopen(req, timeout=8) as resp:
                    if resp.status != 200:
                        return f"DeepSeek /v1/models returned HTTP {resp.status}"
            except _uerr.HTTPError as e:
                if e.code == 401:
                    return (f"DeepSeek API key rejected (HTTP 401). "
                            f"Key starts with {key[:8]}... — rotate or switch backend.")
                return f"DeepSeek probe HTTP {e.code}: {str(e)[:120]}"
            except Exception as e:
                return f"DeepSeek probe network error: {type(e).__name__}: {str(e)[:120]}"

        elif backend == "grok":
            key = (config.grok_api_key
                   or os.environ.get("GROK_API_KEY")
                   or os.environ.get("XAI_API_KEY"))
            if not key:
                return "backend=grok but no GROK_API_KEY / XAI_API_KEY in env"
            # Probe the same endpoint GrokClient uses so probe and client
            # can never disagree about where a key authenticates.
            from aegis.core.llm.grok_client import GROK_BASE_URL
            try:
                req = _ur.Request(
                    f"{GROK_BASE_URL}/models",
                    headers={"Authorization": f"Bearer {key}"},
                )
                with _ur.urlopen(req, timeout=8) as resp:
                    if resp.status != 200:
                        return f"Grok {GROK_BASE_URL}/models returned HTTP {resp.status}"
            except _uerr.HTTPError as e:
                if e.code in (401, 403):
                    return (f"Grok API key rejected (HTTP {e.code}) at {GROK_BASE_URL}. "
                            f"Key starts with {key[:8]}... — rotate or switch backend.")
                return f"Grok probe HTTP {e.code}: {str(e)[:120]}"
            except Exception as e:
                # api.x.ai needs proxy access from the user's CN network —
                # a transient proxy/DNS blip must not kill the run. Per the
                # docstring, only definitive failures abort; per-agent
                # retries handle the rest. Degrade to a warning on stderr.
                import sys as _sys
                print(f"  ⚠ Grok probe network error (non-fatal): "
                      f"{type(e).__name__}: {str(e)[:120]}", file=_sys.stderr)

        elif backend == "subprocess":
            # claude CLI must be on PATH and callable outside a nested
            # Claude Code session. We don't issue a real LLM call here
            # (would add cost/latency); just check the binary runs.
            try:
                from aegis.core.llm.subprocess_client import SubprocessLLMClient
                claude_path = SubprocessLLMClient._find_claude()
            except Exception as e:
                return f"claude CLI not found on PATH: {e}"
            env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
            try:
                r = _sp.run(
                    [claude_path, "--version"],
                    capture_output=True, text=True, timeout=10, env=env,
                )
                if r.returncode != 0:
                    return f"claude --version failed (rc={r.returncode}): {r.stderr[:200]}"
            except _sp.TimeoutExpired:
                return "claude --version timed out (10s) — CLI may be hung"
            except Exception as e:
                return f"claude --version error: {type(e).__name__}: {str(e)[:120]}"

        elif backend == "sdk":
            has_api = bool(os.environ.get("ANTHROPIC_API_KEY"))
            has_oauth = bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))
            if not (has_api or has_oauth):
                return ("backend=sdk but neither ANTHROPIC_API_KEY nor "
                        "CLAUDE_CODE_OAUTH_TOKEN is set")

        return None

    def _resolved_backend_kind(self, config: Any) -> str:
        """AUDIT-D1/D4: which backend `_resolve_llm_client` will pick, without
        instantiating a client. Mirrors that method's "auto" branch exactly.

        Used to select the backend-tiered concurrency cap and timeouts:
        API backends (deepseek/grok/sdk) are minutes-per-call, subprocess
        CLI is tens of minutes — one set of knobs cannot fit both.
        """
        import os
        backend = config.llm_backend
        if backend != "auto":
            return backend
        if bool(config.deepseek_api_key or os.environ.get("DEEPSEEK_API_KEY")):
            return "deepseek"
        if bool(getattr(config, "grok_api_key", None) or os.environ.get("GROK_API_KEY")
                or os.environ.get("XAI_API_KEY")):
            return "grok"
        if bool(os.environ.get("ANTHROPIC_API_KEY")):
            return "sdk"
        if (bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))
                and not bool(os.environ.get("CLAUDE_CODE_ENTRYPOINT"))):
            return "sdk"
        return "subprocess"

    def _resolve_llm_client(self, config: Any, _log: Any, quiet: bool = False) -> Any:
        """Resolve LLM client based on config. Cached after first call."""
        if hasattr(self, '_cached_llm_client') and self._cached_llm_client is not None:
            return self._cached_llm_client

        import os
        backend = config.llm_backend

        if backend == "auto":
            has_deepseek_key = bool(config.deepseek_api_key or os.environ.get("DEEPSEEK_API_KEY"))
            has_grok_key = bool(getattr(config, "grok_api_key", None) or os.environ.get("GROK_API_KEY") or os.environ.get("XAI_API_KEY"))
            has_api_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
            has_oauth = bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))
            in_claude_session = bool(os.environ.get("CLAUDE_CODE_ENTRYPOINT"))

            if has_deepseek_key:
                backend = "deepseek"
            elif has_grok_key:
                backend = "grok"
            elif has_api_key:
                backend = "sdk"
            elif has_oauth and not in_claude_session:
                backend = "sdk"
            else:
                backend = "subprocess"

        if backend == "deepseek":
            from aegis.core.llm.deepseek_client import DeepSeekClient
            ds_key = config.deepseek_api_key or os.environ.get("DEEPSEEK_API_KEY", "")
            client = DeepSeekClient(model=config.deepseek_model, api_key=ds_key)
            if not quiet:
                _log(f"LLM client: DeepSeek (model={config.deepseek_model})")
        elif backend == "grok":
            from aegis.core.llm.grok_client import GrokClient
            grok_key = getattr(config, "grok_api_key", None) or os.environ.get("GROK_API_KEY", "") or os.environ.get("XAI_API_KEY", "")
            client = GrokClient(model=getattr(config, "grok_model", None), api_key=grok_key)
            if not quiet:
                _log(f"LLM client: Grok (model={client.model})")
        elif backend == "sdk":
            from aegis.core.llm.sdk_client import SDKClient
            client = SDKClient(model=config.llm_model)
            if not quiet:
                _log(f"LLM client: SDK (model={config.llm_model})")
        else:
            from aegis.core.llm.subprocess_client import SubprocessLLMClient
            client = SubprocessLLMClient(model=config.llm_model)
            if not quiet:
                _log(f"LLM client: subprocess (model={config.llm_model})")

        # TODO-3 (2026-05-05): opt-in transparent disk cache. Enabled via
        # `AEGIS_LLM_CACHE=1`; cache root via `AEGIS_LLM_CACHE_DIR`. Lets
        # iterative debugging avoid re-paying for unchanged calls when only
        # downstream code (renderer / heuristic) was tweaked. Cost tracker
        # and `model` attribute are forwarded so the rest of the pipeline
        # is unaware of the wrapper.
        try:
            from aegis.core.llm.cached_client import maybe_wrap_with_cache
            client = maybe_wrap_with_cache(client, config.ticker)
            if not quiet and hasattr(client, "_hits"):
                _log(f"  LLM disk cache: enabled at .cache/llm_calls/{config.ticker.lower()}/")
        except Exception as _ce:
            if not quiet:
                _log(f"  LLM disk cache: disabled ({_ce})")

        self._cached_llm_client = client
        return client

    # TODO-X3 续 (2026-05-05): per-agent hybrid routing for DeepSeek V4.
    # `pro` runs ~2x slower than `flash` and DEEP-mode agents on pro routinely
    # take 5-10 min each. Pro is needed for the central thesis-driving agents
    # (valuation, variant, accounting) where reasoning depth matters; the
    # context / risk / business / management agents do well on flash. Wall-
    # clock saved on a typical 7-agent + Director + Synthesizer + Editor run:
    # roughly 40 min → 22 min (4 of 7 specialists drop from pro → flash).
    _HEAVY_AGENTS: frozenset[str] = frozenset({
        "valuation_analyst",
        "variant_analyst",
        "accounting_analyst",
        # chief_analyst components — synthesis quality matters more than speed
        "research_director",
        "thesis_synthesizer",
        "report_editor",
        "scenario_architect",
    })

    def _resolve_fast_llm_client(self, config: Any, _log: Any) -> Any:
        """Resolve a cheaper/faster LLM client for specialist agents.

        Honoured by the DeepSeek backend (deepseek-v4-pro →
        deepseek-v4-flash by default, overridable via fast_agent_model).
        For Grok / SDK / subprocess this is a no-op (no published/validated
        cheap tier wired yet).
        """
        if not config.fast_agents:
            return self._resolve_llm_client(config, _log, quiet=True)

        if hasattr(self, '_cached_fast_llm_client') and self._cached_fast_llm_client is not None:
            return self._cached_fast_llm_client

        import os
        backend = config.llm_backend
        if backend == "auto":
            has_deepseek_key = bool(config.deepseek_api_key or os.environ.get("DEEPSEEK_API_KEY"))
            if has_deepseek_key:
                backend = "deepseek"

        if backend == "deepseek":
            # TODO-X3 续: DeepSeek V4 actually exposes two tiers. Use flash.
            from aegis.core.llm.deepseek_client import DeepSeekClient
            ds_key = config.deepseek_api_key or os.environ.get("DEEPSEEK_API_KEY", "")
            fast_model = getattr(config, "fast_agent_model", None) or "deepseek-v4-flash"
            client = DeepSeekClient(model=fast_model, api_key=ds_key)
            _log(f"Agent LLM: DeepSeek (model={fast_model}, fast mode)")
        else:
            # Other backends: fall back to premium (no fast variant)
            client = self._resolve_llm_client(config, _log, quiet=True)

        # Same disk-cache wrap as premium path so flash calls also benefit.
        try:
            from aegis.core.llm.cached_client import maybe_wrap_with_cache
            client = maybe_wrap_with_cache(client, config.ticker)
        except Exception:
            pass

        self._cached_fast_llm_client = client
        return client

    def _resolve_per_agent_llm_client(
        self, config: Any, agent_name: str, premium: Any, fast: Any,
    ) -> Any:
        """Per-agent client routing: heavy agents always use premium tier,
        light agents use the fast tier when fast_agents/fast_pipeline is on.

        This is the core of TODO-X3 续 hybrid routing — a flat lookup with
        no model-instantiation cost (clients are already cached). Falls back
        to `premium` for unknown agent names (safer default than fast).
        """
        if not getattr(config, "fast_agents", False):
            return premium
        if premium is fast:
            return premium  # backend has no fast variant
        return premium if agent_name in self._HEAVY_AGENTS else fast

    def _compute_metrics(
        self, engine: Any, facts: dict, entity_id: str, period: str,
    ) -> dict[str, float]:
        """Compute financial metrics from meta_facts using FormulaEngine."""
        computed: dict[str, float] = {}

        # Formula engine calls (safe: skip if inputs missing)
        formula_calls = [
            ("gross_margin_v1", {"gross_profit": "gross_profit", "revenue": "revenue"}),
            ("operating_margin_v1", {"operating_income": "operating_income", "revenue": "revenue"}),
            ("net_margin_v1", {"net_income": "net_income", "revenue": "revenue"}),
            ("ebitda_margin_v1", {"ebitda": "ebitda", "revenue": "revenue"}),
            ("roe_v1", {"net_income": "net_income", "avg_shareholders_equity": "total_equity"}),
            ("roa_v1", {"net_income": "net_income", "avg_total_assets": "total_assets"}),
            ("sbc_to_revenue_v1", {"sbc": "sbc", "revenue": "revenue"}),
        ]

        for def_id, input_map in formula_calls:
            inputs = {}
            fact_ids = {}
            skip = False
            for param_name, fact_key in input_map.items():
                val = facts.get(fact_key)
                if val is None:
                    skip = True
                    break
                inputs[param_name] = val
                fact_ids[param_name] = f"fact:{fact_key}:{entity_id}:{period}"
            if skip:
                continue

            result = engine.compute(
                definition_id=def_id, formula_version=1,
                entity_id=entity_id, period=period,
                period_type="duration", currency="USD",
                inputs=inputs, input_fact_ids=fact_ids,
            )
            metric_name = def_id.replace("_v1", "")
            if result and result.value == result.value:  # not NaN
                computed[metric_name] = result.value

        # Direct computations
        safe_div = lambda a, b: a / b if b else 0

        if "capex" in facts and "revenue" in facts:
            computed["capex_to_revenue"] = safe_div(facts["capex"], facts["revenue"])
        if "current_assets" in facts and "current_liabilities" in facts:
            computed["current_ratio"] = safe_div(facts["current_assets"], facts["current_liabilities"])
            computed["nwc"] = facts["current_assets"] - facts["current_liabilities"]

        # Net debt
        total_debt = facts.get("total_debt", 0)
        cash = facts.get("cash_and_equivalents", 0)
        computed["net_debt"] = total_debt - cash

        # Refactor 3: skip net_debt/EBITDA when EBITDA ≤ 0 (multiple of a
        # loss is meaningless; renderers used to render "-X.X×" as if it
        # were a leverage signal).
        _ebitda_for_lev = facts.get("ebitda")
        if _ebitda_for_lev and _ebitda_for_lev > 0:
            computed["net_debt_to_ebitda"] = safe_div(computed["net_debt"], _ebitda_for_lev)

        if facts.get("operating_income") and facts.get("total_equity"):
            nd = computed["net_debt"]
            invested = facts["total_equity"] + nd
            computed["roic"] = safe_div(
                facts["operating_income"] * (1 - 0.21), invested,
            )

        if facts.get("net_income") and facts.get("operating_cash_flow"):
            computed["accruals_ratio"] = safe_div(
                facts["net_income"] - facts["operating_cash_flow"], facts.get("total_assets", 1),
            )
            computed["cfo_to_net_income"] = safe_div(
                facts["operating_cash_flow"], facts["net_income"],
            )

        if facts.get("free_cash_flow"):
            computed["fcf_simple"] = facts["free_cash_flow"]

        if facts.get("diluted_shares") and facts.get("basic_shares"):
            computed["dilution_rate"] = safe_div(
                facts["diluted_shares"] - facts["basic_shares"], facts["basic_shares"],
            )

        return computed

    def _build_dcf_input(
        self, config: ResearchConfig, facts: dict, metrics: dict,
        market_data: dict, sector_pack: dict | None = None,
        consensus_data: dict | None = None,
        driver_tree: Any = None,
        log: Any = None,
    ) -> Any:
        """Build DCFInput from config, facts, computed metrics, and sector context.

        Auto-calibration logic:
        1. Revenue growth: use driver tree if available, else consensus, else sector-aware
        2. Margin path: converge from current toward sector typical range
        3. CapEx path: use actual ratio, not generic 10%
        4. Tax rate: use XBRL-extracted effective rate
        5. Buyback yield: auto-calculate from share_buybacks / market_cap
        """
        from aegis.core.truth.scenario_engine.dcf_engine import DCFInput, resolve_driver_revenue

        revenue = facts.get("revenue", 0)
        shares = facts.get("diluted_shares") or facts.get("shares_outstanding") or 0
        # Fail fast if shares is implausibly small (< 1M). Previously this
        # silently fell back to 1, making DCF compute per_share = equity_value
        # (i.e. billions of dollars per share). Step 6 should have populated
        # meta_facts from live snapshot; if we got here with no real value,
        # something upstream is broken and DCF output would be meaningless.
        if shares < 1e6:
            raise ValueError(
                f"Cannot build DCF input: shares_outstanding is missing or "
                f"implausibly small ({shares}). Step 6 should have fetched live "
                f"shares from market data; check that meta_facts['diluted_shares'] "
                f"or ['shares_outstanding'] was populated."
            )
        net_debt = metrics.get("net_debt", 0)
        da = facts.get("depreciation_amortization", 0)
        sp = sector_pack or {}

        # ── Revenue Growth Calibration ──
        growth_path = config.revenue_growth_path

        # Priority 0: Use driver tree if available
        if growth_path is None and driver_tree is not None and revenue > 0:
            growth_path, _ = resolve_driver_revenue(revenue, driver_tree)
            # BUG-Y23 (2026-05-06): the driver-tree path has NO aggregate
            # bound. For hyper-growth issuers (Cambricon FY2025 +453% YoY),
            # the LLM-generated driver growth rates compound to absurd Y10
            # revenue (Cambricon: ¥8333亿 by Y10, 128× the ¥65亿 base, 62%
            # CAGR — more aggressive than TSMC's actual 14-year history).
            # That sails into the DCF and produces a phantom ¥4475/股 base
            # value. Cap the cumulative scaling at MAX_TERMINAL_RATIO = 30×
            # (≈ 41% 10-year CAGR — still extremely aggressive but plausible
            # for a real hyper-growth name) by switching to terminal_growth
            # once cumulative scaling crosses the threshold.
            # AUDIT-A4: cap logic extracted to module-level
            # `cap_cumulative_growth_path` so the bear/bull driver_deltas
            # path applies the identical bound.
            tg = config.terminal_growth_rate
            pre_cap_path = list(growth_path)
            # R3-1：封顶按公司规模分档（mega 3× / large 6× / mid 12× /
            # small 30×），A 股按 ticker 全数字判定 CNY 口径。
            _max_ratio = max_cumulative_growth_ratio(
                revenue, str(config.ticker).isdigit(),
            )
            facts["__growth_cap_ratio"] = _max_ratio
            growth_path, capped_year = cap_cumulative_growth_path(
                growth_path, tg, max_ratio=_max_ratio,
            )
            if capped_year >= 0:
                old_y10 = revenue * 1.0
                for g in pre_cap_path:
                    old_y10 *= (1 + g)
                new_y10 = revenue * 1.0
                for g in growth_path:
                    new_y10 *= (1 + g)
                # AUDIT-A10: `_build_dcf_input` runs twice for the non-segment
                # path (dcf_input + dcf_input_flat share the same `facts`
                # dict) — only warn the first time the cap trips.
                already_warned = bool(facts.get("__growth_path_capped"))
                facts["__growth_path_capped"] = True
                facts["__growth_path_capped_year"] = capped_year + 1
                if not already_warned:
                    msg = (
                        f"  ⚠ Driver-tree growth path capped at Y{capped_year + 1} "
                        f"(cumulative {_max_ratio:.0f}× scale-tier threshold). "
                        f"Pre-cap Y10 revenue would have been "
                        f"{old_y10/revenue:.0f}× base; post-cap {new_y10/revenue:.0f}×."
                    )
                    if log is not None:
                        # AUDIT-A10: route through pipeline_log when the
                        # caller passes `_log` (stderr print was invisible to
                        # the pipeline log / replay).
                        log(msg)
                    else:
                        import sys as _sys
                        print(msg, file=_sys.stderr)
        if growth_path is None:
            tg = config.terminal_growth_rate
            cd = consensus_data or {}

            # Priority 1: Use consensus revenue estimates to derive growth rates
            consensus_fy_current = cd.get("revenue_FY_Current", {}).get("mean")
            consensus_fy_next = cd.get("revenue_FY_Next", {}).get("mean")

            # Hard caps to prevent explosive compounding:
            # No single-year growth above 35% (even from consensus)
            # because 35% over 10 years already implies 20x revenue.
            MAX_YR1 = 0.35
            MAX_YR2 = 0.28
            # After year 2, accelerate decay — mature companies rarely sustain
            # double-digit growth beyond year 5.
            DECAY_WINDOW = 6  # years to reach terminal after year 2

            if consensus_fy_current and revenue > 0:
                # Consensus-calibrated near-term growth (capped)
                raw_yr1 = (consensus_fy_current - revenue) / revenue
                raw_yr2 = ((consensus_fy_next - consensus_fy_current) / consensus_fy_current
                           if consensus_fy_next and consensus_fy_current else raw_yr1 * 0.75)
                yr1_growth = min(max(raw_yr1, tg), MAX_YR1)
                yr2_growth = min(max(raw_yr2, tg), MAX_YR2)

                # Build path: Y1-Y2 from (capped) consensus, then faster decay
                growth_path = [round(yr1_growth, 4), round(yr2_growth, 4)]
                for yr in range(2, 10):
                    pct = min((yr - 2) / DECAY_WINDOW, 1.0)
                    g = yr2_growth * (1 - pct) + (tg + 0.005) * pct
                    growth_path.append(round(max(g, tg), 4))
            else:
                # Priority 2: Historical CAGR with maturity adjustment.
                # Skip CAGR entirely when marked unreliable (early-stage
                # hyper-growth, regime change, base-year too small, etc.) —
                # the cap doesn't help when the underlying number is junk.
                # Fall through to size-bucket defaults instead.
                hist_cagr = facts.get("__revenue_cagr")
                cagr_unreliable = facts.get("__revenue_cagr_unreliable", False)
                # BUG-28 (2026-05-04): the size-bucket fallback assumes a
                # healthy small company growing at 20% Y1. For a distressed
                # / loss-making issuer (operating_margin << 0) that's a bad
                # default — combined with negative margins it makes losses
                # COMPOUND faster and inverts bear/bull scenarios. Use a
                # turnaround-baseline growth (5%) anchored on macro tg
                # instead. Caller LLM scenario architect can still override
                # via the bull/bear delta layer.
                _distressed = (metrics.get("operating_margin") or 0) < -0.05
                if (hist_cagr is not None and hist_cagr > -0.10
                        and not cagr_unreliable):
                    # Cap near-term growth at 35% (same as consensus path)
                    near_term_growth = min(max(hist_cagr, tg + 0.01), MAX_YR1)
                elif _distressed:
                    # Distressed/turnaround anchor: assume macro-rate
                    # recovery, not size-bucket aggressive growth. Cap at
                    # last-year YoY when available (stronger signal than
                    # CAGR for regime-change names).
                    _last_yoy = facts.get("__revenue_last_yoy")
                    if isinstance(_last_yoy, (int, float)) and -0.10 < _last_yoy < 0.20:
                        near_term_growth = max(_last_yoy, tg + 0.005)
                    else:
                        near_term_growth = 0.05  # Conservative recovery baseline
                elif revenue > 200e9:
                    near_term_growth = 0.06  # Mature mega-cap
                elif revenue > 50e9:
                    near_term_growth = 0.10
                elif revenue > 10e9:
                    near_term_growth = 0.15
                else:
                    near_term_growth = 0.20  # High-growth

                # Faster decay over 6 years (not 10)
                growth_path = []
                for yr in range(10):
                    pct = min(yr / DECAY_WINDOW, 1.0)
                    g = near_term_growth * (1 - pct) + (tg + 0.01) * pct
                    growth_path.append(round(max(g, tg), 4))

        # ── Margin Path Calibration ──
        margin_path = config.operating_margin_path
        if margin_path is None:
            current_margin = metrics.get("operating_margin", 0.20)

            # Determine margin target from sector pack
            typical = sp.get("cost_structure", {}).get("typical_margins", {})
            target_range = typical.get("operating_margin", None)

            # BUG-26b (2026-05-04): distressed companies (current_margin < -5%)
            # need explicit mean-reversion to a realistic recovery target,
            # not "current + 2%". The old rule kept the path stuck at deep
            # negative margins for 10 years; combined with positive revenue
            # growth this produced a perversely-monotone DCF where MORE growth
            # meant MORE losses, inverting bear/bull and giving negative
            # per-share targets that are not interpretable as prices.
            if current_margin < -0.05:
                if isinstance(target_range, list) and len(target_range) >= 2:
                    # Use sector low end, but never assume sub-breakeven recovery.
                    margin_target = max(target_range[0], 0.05)
                else:
                    # Sector-agnostic recovery floor: 5% operating margin.
                    # This is a "what would breakeven look like" anchor — actual
                    # outcome depends on restructuring path (which the LLM
                    # narrative covers qualitatively).
                    margin_target = 0.05
            elif isinstance(target_range, list) and len(target_range) >= 2:
                margin_target = (target_range[0] + target_range[1]) / 2
            elif current_margin > 0.45:
                # For extreme margin businesses (NVDA 60%+), assume meaningful
                # normalization toward typical semiconductor/tech peak of ~40%.
                # This prevents DCF from assuming peak margins persist forever.
                margin_target = 0.40
            elif current_margin > 0.30:
                # Healthy tech margins — assume modest compression
                margin_target = current_margin * 0.85
            else:
                # Below-average margins — assume modest expansion
                margin_target = current_margin + 0.02

            # Convergence rate scales with the gap: larger gaps converge faster
            # because extreme margins tend to normalize faster in competitive markets.
            gap = abs(current_margin - margin_target)
            if gap > 0.20:
                # Distressed recovery: aggressive mean reversion (turnaround math)
                convergence_factor = 0.70
            elif gap > 0.15:
                convergence_factor = 0.60  # Strong compression for extreme margins
            elif gap > 0.08:
                convergence_factor = 0.45
            else:
                convergence_factor = 0.30  # Gentle for already-normal margins

            margin_path = []
            for yr in range(10):
                pct = yr / 9.0
                blend = pct * convergence_factor
                m = current_margin * (1 - blend) + margin_target * blend
                margin_path.append(round(m, 4))

        # ── CapEx Path ── use actual ratio, normalized if in a spike cycle.
        # BUG-39: Flat-lining current capex is wrong when the current year is
        # a visible spike (e.g. Alphabet FY2025 22.7% for AI infra build-out;
        # historical long-run 12%). If capex > 15%, decay toward 12% over
        # 4 years — matches big-tech capex cycle observations (META 2022→2024
        # peak, Alphabet 2024→2026 AI cycle).
        capex_path = config.capex_to_revenue_path
        if capex_path is None:
            # BUG-26: CAS cashflow reports capex as negative (outflow). Take
            # abs so the path stays a positive "% of revenue spent on capex".
            current_capex_ratio = abs(metrics.get("capex_to_revenue", 0.05) or 0.05)
            if current_capex_ratio > 0.15:
                # Normalize spike: decay current → target over DECAY_YEARS
                target_capex = 0.12
                DECAY_YEARS = 4
                capex_path = []
                for yr in range(10):
                    pct = min(yr / DECAY_YEARS, 1.0)
                    cx = current_capex_ratio * (1 - pct) + target_capex * pct
                    capex_path.append(round(cx, 4))
            else:
                # Low/moderate capex — keep flat (structural)
                capex_path = [round(current_capex_ratio, 4)] * 10
        else:
            # BUG-26 (2026-05-04): config-supplied paths (user override or
            # scenario-architect injection) bypassed the abs() guard above
            # and could carry CAS-style negative outflow values straight into
            # dcf_engine, tripping its "capex_to_revenue_path contains
            # negative values" UserWarning. Sanitize defensively here so the
            # DCF engine and sensitivity analyzer always see positive ratios.
            capex_path = [abs(v) for v in capex_path]

        # ── Tax Rate ── use XBRL-reported effective rate if available
        effective_tax = config.effective_tax_rate
        xbrl_tax = facts.get("effective_tax_rate")
        if xbrl_tax and 0.05 < xbrl_tax < 0.50:
            effective_tax = xbrl_tax

        # ── SBC & Dilution ──
        # BUG-FIX (2026-04-15): previously defaulted to 2% annual dilution even
        # when SBC was zero, silently knocking 22% off per-share DCF over 10y.
        # This was especially wrong for A-shares (Chinese companies rarely have
        # material SBC dilution). Now: only apply dilution if SBC actually > 0.
        sbc = facts.get("sbc", 0)
        sbc_to_rev = sbc / revenue if revenue else 0
        if sbc and sbc > 0:
            dilution = metrics.get("dilution_rate", 0.02)
        else:
            dilution = metrics.get("dilution_rate", 0.0)

        # ── Buyback Yield ──
        buyback_yield = 0.0
        if facts.get("share_buybacks") and market_data.get("market_cap"):
            buyback_yield = facts["share_buybacks"] / market_data["market_cap"]

        return DCFInput(
            base_revenue=revenue,
            revenue_growth_path=growth_path,
            operating_margin_path=margin_path,
            capex_to_revenue_path=capex_path,
            effective_tax_rate=effective_tax,
            nwc_to_revenue_delta=0.01,
            wacc=config.wacc,
            terminal_growth_rate=config.terminal_growth_rate,
            shares_outstanding=shares,
            net_debt=net_debt,
            sbc_to_revenue=0.0,  # SBC already deducted in operating margin
            dilution_rate_annual=dilution,
            horizon_years=10,
            sbc_treatment="dilution_only",  # SBC in margin → only count share dilution
            buyback_yield_annual=buyback_yield,
            base_depreciation=da,
            capex_useful_life_years=5.0,
            currency=facts.get("__currency", "USD"),
        )

    def _build_segment_dcf(
        self, config: ResearchConfig, facts: dict, metrics: dict,
        market_data: dict, sector_pack: dict, product_segs: dict,
        consensus_data: dict | None = None,
    ) -> tuple:
        """Build and run segment-level DCF (Sum-of-Parts).

        Returns (dcf_input_for_reverse, dcf_output, segment_projections_dict).
        """
        from aegis.core.truth.scenario_engine.dcf_engine import (
            DCFEngine, DCFInput, SegmentDCFInput, ConsolidatedDCFInput,
        )

        revenue = facts.get("revenue", 0)
        shares = facts.get("diluted_shares") or facts.get("shares_outstanding") or 0
        # Fail fast if shares is implausibly small (< 1M). Previously this
        # silently fell back to 1, making DCF compute per_share = equity_value
        # (i.e. billions of dollars per share). Step 6 should have populated
        # meta_facts from live snapshot; if we got here with no real value,
        # something upstream is broken and DCF output would be meaningless.
        if shares < 1e6:
            raise ValueError(
                f"Cannot build DCF input: shares_outstanding is missing or "
                f"implausibly small ({shares}). Step 6 should have fetched live "
                f"shares from market data; check that meta_facts['diluted_shares'] "
                f"or ['shares_outstanding'] was populated."
            )
        net_debt = metrics.get("net_debt", 0)
        da = facts.get("depreciation_amortization", 0)
        tg = config.terminal_growth_rate
        horizon = 10

        # Build per-segment inputs with data-driven assumptions.
        # Instead of hardcoded Apple-specific segments, derive growth and margin
        # from actual company-level metrics and segment revenue share.
        company_om = metrics.get("operating_margin", 0.20)
        company_gm = metrics.get("gross_margin", 0.40)
        # Skip CAGR if marked unreliable (early-stage scaling, regime change,
        # etc.) — see CAGR sanity-check block in Step 4. Treat as None so the
        # downstream priority chain falls through to consensus / sector defaults.
        hist_cagr = facts.get("__revenue_cagr")
        if facts.get("__revenue_cagr_unreliable"):
            hist_cagr = None

        # BUG-39: derive company-level near-term growth from CONSENSUS first,
        # falling back to historical CAGR. Same priority order as flat
        # `_build_dcf_input`. Without this, segment DCF was always using
        # historical CAGR (~12% for GOOGL FY2025), ignoring sell-side
        # consensus that prices in 17%/15% Y1/Y2 — produced $56 base value
        # vs $321 market.
        cd = consensus_data or {}
        consensus_fy_current = cd.get("revenue_FY_Current", {}).get("mean")
        consensus_fy_next = cd.get("revenue_FY_Next", {}).get("mean")
        # Tighter caps than flat DCF: segment DCF compounds across multiple
        # segments with their own 1.15x small-segment lift, so a 35% cap
        # produces double-counted optimism. NVDA at 30% Y1 / 22% Y2 still
        # produces ~3.5x revenue growth over 10 years — plenty.
        SEG_MAX_YR1 = 0.30
        SEG_MAX_YR2 = 0.22
        consensus_yr1 = None
        consensus_yr2 = None
        if consensus_fy_current and revenue > 0:
            raw_yr1 = (consensus_fy_current - revenue) / revenue
            raw_yr2 = ((consensus_fy_next - consensus_fy_current) / consensus_fy_current
                       if consensus_fy_next and consensus_fy_current else raw_yr1 * 0.85)
            consensus_yr1 = min(max(raw_yr1, tg), SEG_MAX_YR1)
            consensus_yr2 = min(max(raw_yr2, tg), SEG_MAX_YR2)

        segments: dict[str, SegmentDCFInput] = {}
        for seg_id, seg_data in product_segs.items():
            seg_rev = seg_data.get("revenue", 0)
            if seg_rev <= 0:
                continue

            seg_name = seg_id.replace("_", " ").title()
            rev_pct = seg_rev / revenue if revenue else 0

            # Data-driven segment assumptions:
            # Use segment-level operating income if available, else company average
            seg_oi = seg_data.get("operating_income")
            if seg_oi is not None and seg_rev > 0:
                margin_now = max(seg_oi / seg_rev, 0.05)
            else:
                # Approximate: larger revenue segments tend to have higher margins
                margin_now = company_om

            # Growth: cap at 35% for any segment (match company-level cap).
            # Hyper-growth segments (200%+ historical) must be tempered because
            # they cannot compound indefinitely without hitting TAM limits.
            # BUG-39: blend consensus near-term growth (when available) with
            # historical CAGR. Consensus is forward-looking and incorporates
            # mgmt guidance + analyst views; pure historical CAGR underestimates
            # mature-but-accelerating businesses (e.g. GOOGL Cloud + AI lift).
            SEG_MAX_GROWTH = 0.35
            if consensus_yr1 is not None:
                # Consensus drives Y1; smaller (sub-30%-of-revenue) segments
                # get a 15% lift since they typically grow faster than blend.
                base_growth = consensus_yr1
                if rev_pct < 0.3:
                    near_growth = min(base_growth * 1.15, SEG_MAX_GROWTH)
                else:
                    near_growth = base_growth
            elif hist_cagr is not None and hist_cagr > 0:
                base_growth = min(hist_cagr, SEG_MAX_GROWTH)
                if rev_pct < 0.3:
                    near_growth = min(base_growth * 1.15, SEG_MAX_GROWTH)
                else:
                    near_growth = base_growth
            else:
                near_growth = 0.10

            # Margin target: only compress when margin is at industry-peak level
            # (>45%) — typical of semiconductor cycle highs (NVDA 60%+).
            # BUG-39: previously compressed ANY margin >30% by 15%, which
            # incorrectly normalized GOOGL/META 32-40% mega-cap margins DOWN
            # despite no peer-cycle reason. For mature 25-45% margins, hold
            # roughly stable with mild expansion (mgmt typically guides flat
            # to +1pp annually for software/ads businesses).
            if margin_now > 0.50:
                seg_margin_target = 0.40  # peak-cycle normalization
            elif margin_now > 0.45:
                seg_margin_target = margin_now * 0.92  # mild compression
            elif margin_now > 0.20:
                seg_margin_target = margin_now + 0.02  # mild expansion
            else:
                seg_margin_target = margin_now + 0.03  # higher expansion for low-margin segments

            # Faster decay (6-year window) to prevent runaway compounding.
            # BUG-39: when consensus is available, Y1=near_growth, Y2=consensus_yr2,
            # then decay from Y3. This pins Y1+Y2 to consensus and avoids
            # immediately decaying from Y1 (which underweights the consensus signal).
            DECAY_WINDOW = 6
            growth_path = []
            if consensus_yr2 is not None:
                seg_yr2 = consensus_yr2 * 1.15 if rev_pct < 0.3 else consensus_yr2
                seg_yr2 = min(seg_yr2, SEG_MAX_GROWTH)
                growth_path.append(round(max(near_growth, tg), 4))
                growth_path.append(round(max(seg_yr2, tg), 4))
                for yr in range(2, horizon):
                    pct = min((yr - 2) / DECAY_WINDOW, 1.0)
                    g = seg_yr2 * (1 - pct) + (tg + 0.005) * pct
                    growth_path.append(round(max(g, tg), 4))
            else:
                for yr in range(horizon):
                    pct = min(yr / DECAY_WINDOW, 1.0)
                    g = near_growth * (1 - pct) + (tg + 0.005) * pct
                    growth_path.append(round(max(g, tg), 4))

            # Margin: faster convergence for extreme margins
            seg_gap = abs(margin_now - seg_margin_target)
            if seg_gap > 0.15:
                seg_blend_factor = 0.60
            elif seg_gap > 0.08:
                seg_blend_factor = 0.45
            else:
                seg_blend_factor = 0.30

            margin_path = []
            for yr in range(horizon):
                pct = yr / 9.0
                blend = pct * seg_blend_factor
                m = margin_now * (1 - blend) + seg_margin_target * blend
                margin_path.append(round(m, 4))

            # BUG-39: same capex spike normalization as flat DCF.
            # Segments inherit the company-level capex normalization since
            # segment-level capex is rarely disclosed in XBRL.
            # BUG-26: guard against CAS-reported negative capex.
            capex_ratio = abs(metrics.get("capex_to_revenue", 0.025) or 0.025)
            if capex_ratio > 0.15:
                target_capex = 0.12
                DECAY_YEARS_CX = 4
                capex_path = []
                for yr in range(horizon):
                    pct_cx = min(yr / DECAY_YEARS_CX, 1.0)
                    cx = capex_ratio * (1 - pct_cx) + target_capex * pct_cx
                    capex_path.append(round(cx, 4))
            else:
                capex_path = [round(capex_ratio, 4)] * horizon

            segments[seg_id] = SegmentDCFInput(
                segment_id=seg_id,
                segment_name=seg_name,
                base_revenue=seg_rev,
                revenue_growth_path=growth_path,
                operating_margin_path=margin_path,
                capex_to_revenue_path=capex_path,
                terminal_growth_rate=tg,
                horizon_years=horizon,
            )

        # Effective tax rate
        effective_tax = config.effective_tax_rate
        xbrl_tax = facts.get("effective_tax_rate")
        if xbrl_tax and 0.05 < xbrl_tax < 0.50:
            effective_tax = xbrl_tax

        # Buyback yield
        buyback_yield = 0.0
        if facts.get("share_buybacks") and market_data.get("market_cap"):
            buyback_yield = facts["share_buybacks"] / market_data["market_cap"]

        dilution = metrics.get("dilution_rate", 0.02)

        consolidated_input = ConsolidatedDCFInput(
            segments=segments,
            wacc=config.wacc,
            effective_tax_rate=effective_tax,
            nwc_to_revenue_delta=0.01,
            sbc_to_revenue=0.0,
            sbc_treatment="expense_in_fcf",
            dilution_rate_annual=dilution,
            buyback_yield_annual=buyback_yield,
            shares_outstanding=shares,
            net_debt=net_debt,
            horizon_years=horizon,
            base_depreciation=da,
            capex_useful_life_years=5.0,
        )

        dcf = DCFEngine()
        output = dcf.compute_consolidated_dcf(consolidated_input)

        # Build segment projections for report display
        seg_proj = {}
        if hasattr(output, "segments") and output.segments:
            for sid, seg_out in output.segments.items():
                seg_proj[sid] = [
                    {"year": p.year, "revenue": p.revenue, "operating_income": p.operating_income,
                     "segment_name": segments[sid].segment_name if sid in segments else sid}
                    for p in seg_out.projections
                ]

        # Also build a company-level DCFInput for reverse DCF / sensitivity
        dcf_input_flat = self._build_dcf_input(config, facts, metrics, market_data, sector_pack)

        return dcf_input_flat, output, seg_proj

    @staticmethod
    def _extract_a_share_historical(
        ticker: str, entity_id: str, _log: Any,
    ) -> tuple[dict[int, dict[str, float]], list[tuple[int, float]]]:
        """Extract historical annual data for A-share from yfinance multi-year financials.

        Returns (historical_data, revenue_series).
        """
        import ssl
        ssl._create_default_https_context = ssl._create_unverified_context
        import yfinance as yf

        from aegis.core.acquisition.connectors.cninfo_connector import CninfoConnector
        yf_ticker = CninfoConnector._to_yfinance_ticker(
            ticker.replace(".SS", "").replace(".SZ", ""),
        ) or ticker

        stock = yf.Ticker(yf_ticker)
        historical_data: dict[int, dict[str, float]] = {}
        rev_series: list[tuple[int, float]] = []

        fin = stock.financials
        if fin is not None and not fin.empty:
            for col in fin.columns:
                try:
                    year = col.year
                except Exception:
                    continue

                year_data: dict[str, float] = {}
                for idx in fin.index:
                    val = fin.at[idx, col]
                    if val is not None and val == val:  # not NaN
                        year_data[str(idx)] = float(val)

                if year_data:
                    historical_data[year] = year_data

                # Extract revenue
                rev = (year_data.get("Total Revenue")
                       or year_data.get("Operating Revenue", 0))
                if rev and rev > 0:
                    rev_series.append((year, rev))

        # Sort revenue series by year
        rev_series.sort(key=lambda x: x[0])

        if historical_data:
            _log(f"A-share historical: {len(historical_data)} years, "
                 f"revenue series: {len(rev_series)} points")

        return historical_data, rev_series

    @staticmethod
    def _is_a_share_ticker(ticker: str) -> bool:
        """Detect if a ticker is a China A-share stock code.

        Matches: 6-digit codes (600519, 000858, 300750, 688981)
        or yfinance-style suffixed codes (600519.SS, 000858.SZ).
        """
        clean = ticker.replace(".SS", "").replace(".SZ", "").strip()
        return len(clean) == 6 and clean.isdigit()

    @staticmethod
    def _to_yfinance_symbol(ticker: str) -> str:
        """Convert A-share ticker to yfinance format.

        600519 → 600519.SS (Shanghai)
        000858 → 000858.SZ (Shenzhen)
        300750 → 300750.SZ (ChiNext)
        688981 → 688981.SS (STAR Market)
        Already-formatted tickers pass through unchanged.
        """
        if ".SS" in ticker or ".SZ" in ticker:
            return ticker
        clean = ticker.strip()
        if len(clean) == 6 and clean.isdigit():
            if clean.startswith("6"):
                return f"{clean}.SS"
            elif clean.startswith(("0", "3")):
                return f"{clean}.SZ"
        return ticker

    # Ticker → sector pack auto-mapping
    TICKER_SECTOR_MAP: dict[str, str] = {
        # Consumer Electronics
        "AAPL": "sp_consumer_electronics_v1",
        # Ad Platforms
        "META": "sp_ad_platform_v1", "GOOGL": "sp_ad_platform_v1",
        "GOOG": "sp_ad_platform_v1", "SNAP": "sp_ad_platform_v1",
        # SaaS
        "CRM": "sp_saas_v1", "NOW": "sp_saas_v1", "SNOW": "sp_saas_v1",
        # Semiconductors
        "NVDA": "sp_semiconductor_v1", "AMD": "sp_semiconductor_v1",
        "INTC": "sp_semiconductor_v1", "AVGO": "sp_semiconductor_v1",
        "TSM": "sp_semiconductor_v1",
        # Banking
        "JPM": "sp_banking_v1", "BAC": "sp_banking_v1", "GS": "sp_banking_v1",
        # Pharma
        "LLY": "sp_biotech_pharma_v1", "JNJ": "sp_biotech_pharma_v1",
        "PFE": "sp_biotech_pharma_v1", "ABBV": "sp_biotech_pharma_v1",
        # Consumer Staples
        "PG": "sp_consumer_staples_v1", "KO": "sp_consumer_staples_v1",
        "PEP": "sp_consumer_staples_v1",
        # Energy
        "XOM": "sp_energy_v1", "CVX": "sp_energy_v1",
        # Industrial
        "GE": "sp_industrial_v1", "HON": "sp_industrial_v1", "CAT": "sp_industrial_v1",
        # REITs
        "PLD": "sp_reits_v1", "AMT": "sp_reits_v1",
        # E-commerce
        "AMZN": "sp_ecommerce_v1", "BABA": "sp_ecommerce_v1",
        "JD": "sp_ecommerce_v1", "PDD": "sp_ecommerce_v1",
        "SE": "sp_ecommerce_v1", "MELI": "sp_ecommerce_v1",
        # China Internet
        "NFLX": "sp_saas_v1",
        "MSFT": "sp_saas_v1",
        "TSLA": "sp_consumer_electronics_v1",
        # A-share: 白酒 (Baijiu)
        "600519": "sp_baijiu_cn_v1",  # 贵州茅台
        "000858": "sp_baijiu_cn_v1",  # 五粮液
        "000568": "sp_baijiu_cn_v1",  # 泸州老窖
        "002304": "sp_baijiu_cn_v1",  # 洋河股份
        "603369": "sp_baijiu_cn_v1",  # 今世缘
        # A-share: 银行 (Banking)
        "600036": "sp_banking_cn_v1",  # 招商银行
        "601166": "sp_banking_cn_v1",  # 兴业银行
        "000001": "sp_banking_cn_v1",  # 平安银行
        "601398": "sp_banking_cn_v1",  # 工商银行
        "601288": "sp_banking_cn_v1",  # 农业银行
        "601818": "sp_banking_cn_v1",  # 光大银行
        # A-share: 新能源 (New Energy)
        "300750": "sp_new_energy_cn_v1",  # 宁德时代
        "002594": "sp_new_energy_cn_v1",  # 比亚迪
        "601238": "sp_new_energy_cn_v1",  # 广汽集团
        "601127": "sp_new_energy_cn_v1",  # 赛力斯
        # A-share: 医药 (Pharma)
        "600276": "sp_pharma_cn_v1",  # 恒瑞医药
        "000538": "sp_pharma_cn_v1",  # 云南白药
        "300015": "sp_pharma_cn_v1",  # 爱尔眼科
        "600196": "sp_pharma_cn_v1",  # 复星医药
        "300122": "sp_pharma_cn_v1",  # 智飞生物
    }

    def _effective_model_name(self, config: Any) -> str:
        """Resolve the model name actually used for this run, for HTML metadata.

        Prior code hardcoded one backend's model field even when the run
        actually used another backend. Reflect the resolved backend.
        """
        import os
        backend = config.llm_backend
        if backend == "auto":
            if config.deepseek_api_key or os.environ.get("DEEPSEEK_API_KEY"):
                backend = "deepseek"
            elif getattr(config, "grok_api_key", None) or os.environ.get("GROK_API_KEY") or os.environ.get("XAI_API_KEY"):
                backend = "grok"
            elif os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
                backend = "sdk"
            else:
                backend = "subprocess"
        if backend == "deepseek":
            return config.deepseek_model
        if backend == "grok":
            from aegis.core.llm.grok_client import default_grok_model
            return getattr(config, "grok_model", None) or default_grok_model()
        return config.llm_model

    # BUG-A14 (2026-05-05): A-share industry-string → sector pack mapping.
    # akshare returns a Chinese industry label (e.g. "白酒", "城商行") in
    # `company_info["industry"]`. When a ticker isn't in TICKER_SECTOR_MAP
    # (the explicit hand-curated whitelist) we substring-match this label
    # against the table below to fall through to a domain pack instead of
    # the bare "General" template.
    A_SHARE_INDUSTRY_KEYWORDS: list[tuple[tuple[str, ...], str]] = [
        # Order matters — first match wins. Put more-specific patterns first.
        (("白酒",), "sp_baijiu_cn_v1"),
        (("银行", "城商行", "农商行"), "sp_banking_cn_v1"),
        (("电池", "锂电", "动力电池", "新能源", "光伏", "风电"), "sp_new_energy_cn_v1"),
        (("汽车", "整车", "乘用车"), "sp_new_energy_cn_v1"),
        (("医药", "生物", "制药", "中药", "医疗"), "sp_pharma_cn_v1"),
        (("半导体", "集成电路", "芯片"), "sp_semiconductor_v1"),
        (("软件", "互联网", "云计算", "SaaS"), "sp_saas_v1"),
        (("石油", "石化", "天然气", "煤炭", "煤化工"), "sp_energy_v1"),
        (("房地产", "REIT", "物业"), "sp_reits_v1"),
        (("食品", "饮料", "乳品", "肉制品", "调味"), "sp_consumer_staples_v1"),
        (("电商", "零售", "百货"), "sp_ecommerce_v1"),
        (("机械", "工程机械", "电气设备", "重工"), "sp_industrial_v1"),
        (("家电", "消费电子"), "sp_consumer_electronics_v1"),
    ]

    def _infer_sector_pack_from_industry(self, industry: str) -> str | None:
        """Match an akshare industry string to a known sector pack id.

        Substring match (Chinese labels are short and unambiguous enough).
        Returns None if no keyword group hits — caller falls back to General.
        """
        if not industry:
            return None
        s = industry.strip()
        for keywords, pack_id in self.A_SHARE_INDUSTRY_KEYWORDS:
            if any(k in s for k in keywords):
                return pack_id
        return None

    # BUG-Y18 (2026-05-06): when akshare's `stock_individual_info_em`
    # endpoint is unreachable (eastmoney push2 host known-flaky behind
    # Clash Verge), the connector falls back to sina spot for price/shares
    # but does NOT pick up the `行业` field. Sector pack then defaults to
    # General → wrong margin/capex assumptions → DCF base value collapses
    # by ~25× (Cambricon: ¥4475 → ¥179). To recover, infer a sector from
    # the company name itself for well-known A-share tickers. Fragment
    # match keeps this short and stable; extend as needed.
    NAME_FRAGMENT_TO_INDUSTRY: list[tuple[tuple[str, ...], str]] = [
        (("寒武纪",), "半导体"),
        (("中芯国际", "韦尔股份", "兆易创新", "卓胜微", "北方华创",
          "中微公司", "拓荆科技", "海光信息", "澜起科技"), "半导体"),
        (("贵州茅台", "五粮液", "山西汾酒", "泸州老窖", "洋河股份",
          "古井贡酒", "今世缘"), "白酒"),
        (("宁德时代", "比亚迪", "亿纬锂能", "天齐锂业", "赣锋锂业",
          "隆基", "通威", "阳光电源", "金风科技"), "新能源"),
        (("恒瑞医药", "迈瑞医疗", "药明康德", "百济神州", "片仔癀",
          "云南白药", "同仁堂", "智飞生物", "长春高新", "华兰生物"),
         "医药"),
        (("招商银行", "工商银行", "建设银行", "农业银行", "中国银行",
          "兴业银行", "平安银行", "民生银行", "光大银行", "浦发银行"),
         "银行"),
        (("中国石油", "中国石化", "中海油", "中国神华", "兖矿"),
         "石油"),
        (("万科", "保利", "招商蛇口"), "房地产"),
        (("美的", "格力", "海尔"), "家电"),
        # BUG-Y18 v2 (2026-05-06): added missing sector buckets so all 13
        # A-share sector packs are reachable via name fallback.
        (("用友网络", "金山办公", "金蝶国际", "广联达", "恒生电子",
          "同花顺", "深信服", "奇安信"), "软件"),
        (("海天味业", "伊利股份", "双汇发展", "蒙牛", "光明乳业",
          "三全食品", "安琪酵母", "千禾味业"), "食品"),
        (("永辉超市", "苏宁易购", "国美电器", "家家悦", "重庆百货",
          "天虹商场"), "零售"),
        (("三一重工", "中联重科", "徐工机械", "潍柴动力", "中国中车"),
         "机械"),
    ]

    def _infer_industry_from_name(self, company_name: str) -> str | None:
        """Last-resort industry inference when akshare didn't supply one.

        Uses a small hardcoded ticker→industry map for ~30 well-known
        A-share names. Returns the industry KEYWORD that
        `_infer_sector_pack_from_industry` then matches (e.g. "半导体").
        """
        if not company_name:
            return None
        for fragments, industry in self.NAME_FRAGMENT_TO_INDUSTRY:
            if any(frag in company_name for frag in fragments):
                return industry
        return None

    def _load_sector_pack(
        self,
        pack_id: str | None,
        ticker: str = "",
        a_share_industry: str = "",
    ) -> dict:
        """Load a sector pack from configs/sector_packs/.

        Auto-detects from ticker if pack_id not specified, then falls back
        to A-share industry-string keyword match if still unresolved.
        Returns a default generic pack if not found.
        """
        # Auto-detect from ticker if not specified
        if not pack_id and ticker:
            pack_id = self.TICKER_SECTOR_MAP.get(ticker.upper())
        if not pack_id and a_share_industry:
            pack_id = self._infer_sector_pack_from_industry(a_share_industry)

        # BUG-Y41 (2026-05-06): the YAML loader had no exception handling
        # and didn't validate that the parsed root was a dict. A malformed
        # YAML or an empty file would respectively raise `yaml.YAMLError`
        # straight up the call stack or return `None`, which crashed
        # downstream `sector_pack.get(...)` with `AttributeError: NoneType`.
        # Now we tolerate both failures and fall back to the generic pack.
        if pack_id:
            import yaml

            pack_path = Path("configs/sector_packs") / f"{pack_id}.yaml"
            if pack_path.exists():
                try:
                    with open(pack_path) as f:
                        loaded = yaml.safe_load(f)
                except yaml.YAMLError as ye:
                    import sys as _sys
                    print(f"  ⚠ Sector pack {pack_id}: YAML parse error ({ye}). "
                          f"Falling back to generic pack.", file=_sys.stderr)
                    loaded = None
                except OSError as oe:
                    import sys as _sys
                    print(f"  ⚠ Sector pack {pack_id}: file read error ({oe}). "
                          f"Falling back to generic pack.", file=_sys.stderr)
                    loaded = None
                if isinstance(loaded, dict):
                    return loaded
                # Non-dict root (empty file → None, or list root → list) is
                # treated as broken pack — fall through to generic.

        # Default generic sector pack
        return {
            "sector_pack_id": pack_id or "sp_generic",
            "sector_name": "General",
            "key_kpis": [],
            "cycle_characteristics": {"cyclicality": "moderate"},
            "competitive_dynamics": {},
            "accounting_considerations": [],
        }

    def _build_driver_tree(
        self,
        sector_pack: dict | None,
        meta_facts: dict,
        computed_metrics: dict,
        entity_id: str,
        consensus_data: dict | None = None,
        terminal_growth: float = 0.03,
    ) -> Any:
        """Build a RevenueDriverTree from sector pack driver definitions.

        Returns None if sector pack has no revenue_drivers.decomposition.
        """
        sp = sector_pack or {}
        decomp = sp.get("revenue_drivers", {}).get("decomposition", {})
        if not decomp or not decomp.get("tree"):
            return None

        from aegis.core.truth.scenario_engine.dcf_engine import (
            RevenueDriver, RevenueDriverTree,
        )

        formula = decomp.get("formula", "")
        tree_nodes = decomp.get("tree", [])
        revenue = meta_facts.get("revenue", 0)
        horizon = 10

        drivers = []
        for node in tree_nodes:
            name = node.get("name", "")
            base_value = node.get("base_value")
            near_growth = node.get("near_growth", 0.03)
            long_growth = node.get("long_growth", terminal_growth)
            unit = node.get("unit", "")

            if base_value is None or base_value == 0:
                continue

            # Calibrate growth path: decay from near_growth to long_growth
            growth_path = []
            for yr in range(horizon):
                pct = yr / 9.0
                g = near_growth * (1 - pct) + long_growth * pct
                growth_path.append(round(g, 5))

            drivers.append(RevenueDriver(
                name=name,
                base_value=base_value,
                growth_path=growth_path,
                unit=unit,
                note=node.get("growth_driver", ""),
            ))

        if len(drivers) < 2:
            return None

        return RevenueDriverTree(
            entity_id=entity_id,
            sector_pack_id=sp.get("sector_pack_id", ""),
            decomposition_formula=formula,
            drivers=drivers,
            horizon_years=horizon,
        )
