"""Monitorables 封闭目录 — Aegis 2.0 Phase 2 任务 B1.

DESIGN_2.0 §三.C / 设计红线 6：**monitorables 封闭目录制**。LLM 生成观察点
时只许「选型号 + 填阈值」，不许自造可执行承诺；目录外的自由文本观察点
一律降级为「人工关注」（watch_only），如实标注不做可执行承诺。

目录构成（v1，共 8 个型号）：

- 6 个数据规则型 = :mod:`aegis.core.truth.verification` 封闭目录的检查器
  型号一比一映射（阈值常量直接 import 复用，单一事实源）；
- 2 个价格/事件型：``price_deviation``（股价偏离阈值）与
  ``announcement_keyword``（公告关键词命中〔并购/减值/订单〕）。

输出统一为沉睡合同 :class:`aegis.data_contracts.thesis_schema.Monitorable`
（复活，不重新发明）。该合同是封闭 schema（extra=forbid），机器可读的
型号地址编码在 ``data_source`` 字段：``"<source>:<model_id>"``
（如 ``"pit_store:cfo_to_net_income"``）；watch_only 条目固定为
``data_source="watch_only"``。:func:`monitorable_model_id` 负责反解。

生成来源（:func:`build_monitorables`，全部容错、永不 raise）：

1. **核验未通过项自动成为监控点**——verification 结果里 status=="fail"
   的检查器直接进目录监控，阈值取自检查器参数常量；
2. **LLM 自由文本观察点**（synthesized_thesis 的 monitorables /
   must_monitor / follow_ups / open_questions 等字段，若有）经
   型号名归一（复用 :mod:`aegis.core._coerce` 的容错风格）+ 关键词映射
   挂到目录型号；映射不上 → watch_only；
3. **定价体制验证点**（regime.verification_focus）经同一关键词映射
   挂到目录型号（未命中不降级——该清单已在报告展示，不重复承诺）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from aegis.core._coerce import coerce_list
from aegis.core.truth.verification import (
    CFO_NI_FLOOR,
    DEDUCTED_RATIO_FLOOR,
    FORECAST_CONSENSUS_GAP_MAX,
    INVENTORY_GAP_MAX,
    LEVERAGE_RISE_MAX,
    RECEIVABLES_GAP_MAX,
)
from aegis.data_contracts.thesis_schema import Monitorable

logger = logging.getLogger(__name__)

__all__ = [
    "CATALOG",
    "CatalogEntry",
    "build_monitorables",
    "normalize_model_id",
    "monitorable_model_id",
    "WATCH_ONLY_SOURCE",
    "ANNOUNCEMENT_KEYWORDS",
    "PRICE_DEVIATION_MAX",
]

# ── 价格/事件型阈值常量（数据规则型阈值 import 自 verification）─────────
#: 股价相对论点建立时价格的偏离容忍度（±20%，超出即触发复核）。
PRICE_DEVIATION_MAX = 0.20
#: 公告标题命中即触发复核的关键词（封闭清单）。
ANNOUNCEMENT_KEYWORDS: tuple[str, ...] = ("并购", "减值", "订单")

#: watch_only 条目的 data_source 固定值（「人工关注」，不做可执行承诺）。
WATCH_ONLY_SOURCE = "watch_only"


@dataclass(frozen=True)
class CatalogEntry:
    """一个可执行监控型号（封闭目录条目）。"""

    model_id: str
    name_zh: str
    check_frequency: str   # "daily" | "weekly" | "quarterly"
    data_source: str       # "pit_store" | "em_events" | "market_price"
    threshold_zh: str      # 默认阈值的人读中文（数值来自可审计常量）


#: 封闭目录（v1，8 个型号）。新增型号必须同步补 CATALOG + 关键词表 + 测试。
CATALOG: dict[str, CatalogEntry] = {
    e.model_id: e
    for e in (
        CatalogEntry(
            "receivables_vs_revenue", "应收增速 vs 营收增速", "quarterly",
            "pit_store",
            f"应收增速超出营收增速 {RECEIVABLES_GAP_MAX * 100:.0f}pp",
        ),
        CatalogEntry(
            "inventory_vs_revenue", "存货增速 vs 营收增速", "quarterly",
            "pit_store",
            f"存货增速超出营收增速 {INVENTORY_GAP_MAX * 100:.0f}pp",
        ),
        CatalogEntry(
            "cfo_to_net_income", "经营现金流 / 归母净利润", "quarterly",
            "pit_store", f"CFO/归母净利 低于 {CFO_NI_FLOOR}",
        ),
        CatalogEntry(
            "deducted_to_attributable", "扣非 / 归母净利润比", "quarterly",
            "pit_store", f"扣非/归母 低于 {DEDUCTED_RATIO_FLOOR}",
        ),
        CatalogEntry(
            "leverage_trend", "资产负债率趋势", "quarterly",
            "pit_store",
            f"资产负债率同比上升超 {LEVERAGE_RISE_MAX * 100:.0f}pp",
        ),
        CatalogEntry(
            "forecast_vs_consensus", "业绩预告 vs 一致预期", "weekly",
            "em_events",
            f"预告中值偏离一致预期超 ±{FORECAST_CONSENSUS_GAP_MAX:.0%}",
        ),
        CatalogEntry(
            "price_deviation", "股价偏离阈值", "daily",
            "market_price",
            f"股价相对论点建立价偏离超 ±{PRICE_DEVIATION_MAX:.0%}",
        ),
        CatalogEntry(
            "announcement_keyword", "公告关键词命中", "daily",
            "em_events",
            "公告标题命中〔" + "/".join(ANNOUNCEMENT_KEYWORDS) + "〕",
        ),
    )
}

# ── 型号名归一（LLM 乱写型号名的容错，_coerce 同风格：永不 raise）────────
#: 常见错写/简写 → 目录型号（键已经过 _canon 归一：小写、-/空格→_）。
_ALIASES: dict[str, str] = {
    # cfo_to_net_income
    "cfo_to_ni": "cfo_to_net_income",
    "cfo_ni": "cfo_to_net_income",
    "cfo_ni_ratio": "cfo_to_net_income",
    "cfo净利比": "cfo_to_net_income",
    "cfo/净利": "cfo_to_net_income",
    "operating_cash_flow": "cfo_to_net_income",
    # receivables_vs_revenue
    "receivables_growth": "receivables_vs_revenue",
    "accounts_receivable": "receivables_vs_revenue",
    "应收增速": "receivables_vs_revenue",
    # inventory_vs_revenue
    "inventory_growth": "inventory_vs_revenue",
    "存货增速": "inventory_vs_revenue",
    # deducted_to_attributable
    "deducted_ratio": "deducted_to_attributable",
    "扣非比": "deducted_to_attributable",
    "non_recurring": "deducted_to_attributable",
    # leverage_trend
    "leverage": "leverage_trend",
    "debt_ratio": "leverage_trend",
    "负债率": "leverage_trend",
    "资产负债率": "leverage_trend",
    # forecast_vs_consensus
    "forecast_gap": "forecast_vs_consensus",
    "预告缺口": "forecast_vs_consensus",
    "guidance_vs_consensus": "forecast_vs_consensus",
    # price_deviation
    "price": "price_deviation",
    "price_alert": "price_deviation",
    "股价偏离": "price_deviation",
    "股价阈值": "price_deviation",
    # announcement_keyword
    "announcement": "announcement_keyword",
    "announcements": "announcement_keyword",
    "公告关键词": "announcement_keyword",
    "公告命中": "announcement_keyword",
}

#: 自由文本关键词 → 型号（顺序即优先级；一条文本可命中多个型号）。
_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("应收", "receivables_vs_revenue"),
    ("receivable", "receivables_vs_revenue"),
    ("存货", "inventory_vs_revenue"),
    ("inventory", "inventory_vs_revenue"),
    ("现金流", "cfo_to_net_income"),
    ("经营现金", "cfo_to_net_income"),
    ("现金消耗", "cfo_to_net_income"),
    ("cfo", "cfo_to_net_income"),
    ("扣非", "deducted_to_attributable"),
    ("非经常", "deducted_to_attributable"),
    ("负债", "leverage_trend"),
    ("杠杆", "leverage_trend"),
    ("leverage", "leverage_trend"),
    ("业绩预告", "forecast_vs_consensus"),
    ("预告", "forecast_vs_consensus"),
    ("一致预期", "forecast_vs_consensus"),
    ("consensus", "forecast_vs_consensus"),
    ("股价", "price_deviation"),
    ("市价", "price_deviation"),
    ("price", "price_deviation"),
    ("公告", "announcement_keyword"),
    ("并购", "announcement_keyword"),
    ("减值", "announcement_keyword"),
    ("订单", "announcement_keyword"),
    ("announcement", "announcement_keyword"),
)


def _canon(val: Any) -> str:
    """型号名规范化：小写、去首尾空白、``-``/空格 → ``_``。"""
    return str(val or "").strip().lower().replace("-", "_").replace(" ", "_")


def _keyword_models(text: Any) -> list[str]:
    """自由文本 → 命中的目录型号列表（去重、保持关键词表优先级）。"""
    t = str(text or "").lower()
    if not t:
        return []
    hits: list[str] = []
    for kw, model_id in _KEYWORDS:
        if kw in t and model_id not in hits:
            hits.append(model_id)
    return hits


def normalize_model_id(val: Any) -> str | None:
    """LLM 输出的型号名 → 目录型号；映射不上返回 None。永不 raise。

    归一顺序：规范化后精确匹配目录 → 别名表 → 整串关键词兜底。
    """
    s = _canon(val)
    if not s:
        return None
    if s in CATALOG:
        return s
    if s in _ALIASES:
        return _ALIASES[s]
    hits = _keyword_models(val)
    return hits[0] if hits else None


def monitorable_model_id(monitorable: Any) -> str | None:
    """从 Monitorable.data_source（``"<source>:<model_id>"``）反解型号。

    watch_only 或非目录条目返回 None。dict 形态（JSONL 往返后）同样接受。
    """
    src = (
        monitorable.get("data_source")
        if isinstance(monitorable, dict)
        else getattr(monitorable, "data_source", None)
    )
    s = str(src or "")
    if ":" not in s:
        return None
    model_id = s.rsplit(":", 1)[1]
    return model_id if model_id in CATALOG else None


# ---------------------------------------------------------------------------
# Monitorable 构造
# ---------------------------------------------------------------------------

def _catalog_monitorable(model_id: str, description: str) -> Monitorable:
    entry = CATALOG[model_id]
    return Monitorable(
        description=description,
        check_frequency=entry.check_frequency,
        data_source=f"{entry.data_source}:{entry.model_id}",
    )


def _watch_only(text: str) -> Monitorable:
    return Monitorable(
        description=f"人工关注：{text}",
        check_frequency="quarterly",
        data_source=WATCH_ONLY_SOURCE,
    )


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """dict 与 dataclass/对象双形态取值。"""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _iter_llm_watch_texts(synthesized_thesis: Any) -> list[tuple[str | None, str]]:
    """从 synthesized_thesis 收集 (LLM 声称的型号名或 None, 文本) 候选对。

    候选字段（若有）：monitorables / must_monitor / follow_ups /
    follow_up_questions / watch_items / open_questions。条目可以是
    str（自由文本）或 dict（{"model"/"check_id": ..., "description"/
    "question": ..., "threshold": ...}），全部容错。
    """
    if synthesized_thesis is None:
        return []
    out: list[tuple[str | None, str]] = []
    fields = (
        "monitorables", "must_monitor", "follow_ups",
        "follow_up_questions", "watch_items", "open_questions",
    )
    for field_name in fields:
        for item in coerce_list(_get(synthesized_thesis, field_name)):
            if isinstance(item, dict):
                model_raw = next(
                    (item[k] for k in
                     ("model", "model_id", "check_id", "type", "catalog_id")
                     if item.get(k)),
                    None,
                )
                text = next(
                    (str(item[k]).strip() for k in
                     ("description", "text", "question", "item")
                     if str(item.get(k) or "").strip()),
                    "",
                )
                threshold = str(item.get("threshold") or "").strip()
                if threshold and text:
                    text = f"{text}（阈值：{threshold}）"
                elif threshold:
                    text = f"阈值：{threshold}"
                if model_raw is not None or text:
                    out.append((
                        str(model_raw) if model_raw is not None else None,
                        text,
                    ))
            else:
                text = str(item or "").strip()
                if text:
                    out.append((None, text))
    return out


def build_monitorables(
    synthesized_thesis: Any = None,
    verification_results: Any = None,
    regime: Any = None,
) -> list[Monitorable]:
    """从 run 产物自动生成监控点清单（保证非空，永不 raise）。

    Parameters
    ----------
    synthesized_thesis: :class:`SynthesizedThesis` 或其 dict（LLM 侧
        自由文本观察点来源，任意字段缺失容错）。
    verification_results: :func:`aegis.core.truth.verification.run_verification`
        的结果（VerificationResult 或 to_dict 后的 dict，均接受——replay
        路径从 meta_facts 读到的是 dict）。
    regime: :class:`RegimeAssessment` 或其 dict（verification_focus 清单
        经关键词映射挂到目录型号）。
    """
    by_model: dict[str, Monitorable] = {}
    watch_only: list[Monitorable] = []
    seen_watch_texts: set[str] = set()

    # 1) 核验未通过项自动成为监控点（阈值取自检查器参数常量）。
    for r in coerce_list(verification_results):
        d = r.to_dict() if hasattr(r, "to_dict") else (r if isinstance(r, dict) else None)
        if not d:
            continue
        check_id = str(d.get("check_id") or "")
        if check_id not in CATALOG or d.get("status") != "fail":
            continue
        entry = CATALOG[check_id]
        detail = str(d.get("detail_zh") or "").strip()
        desc = f"{entry.name_zh}（核验未通过）"
        if detail:
            desc += f"：{detail}"
        desc += f"；持续监控阈值：{entry.threshold_zh}"
        by_model[check_id] = _catalog_monitorable(check_id, desc)

    # 2) LLM 自由文本观察点：型号归一挂目录，映射不上降级 watch_only。
    for model_raw, text in _iter_llm_watch_texts(synthesized_thesis):
        model_id = normalize_model_id(model_raw) if model_raw else None
        if model_id is None and text:
            model_id = normalize_model_id(text)
        if model_id is not None:
            if model_id not in by_model:  # 核验版（带依据）优先，不覆盖
                entry = CATALOG[model_id]
                desc = f"{entry.name_zh}：{text}" if text else (
                    f"{entry.name_zh}：{entry.threshold_zh}")
                by_model[model_id] = _catalog_monitorable(model_id, desc)
            continue
        if text and text not in seen_watch_texts:
            seen_watch_texts.add(text)
            watch_only.append(_watch_only(text))

    # 3) 定价体制验证点 → 目录型号（未命中不降级，报告已展示该清单）。
    for focus in coerce_list(_get(regime, "verification_focus")):
        for model_id in _keyword_models(focus):
            if model_id not in by_model:
                entry = CATALOG[model_id]
                by_model[model_id] = _catalog_monitorable(
                    model_id,
                    f"{entry.name_zh}（体制验证点）：{entry.threshold_zh}",
                )

    results = list(by_model.values()) + watch_only
    if not results:
        # ThesisContract.must_monitor 要求 min_length=1 —— 如实兜底。
        results.append(_watch_only(
            "本期未生成可执行监控点，请在下一份定期报告披露后人工复核论点假设"))
    return results
