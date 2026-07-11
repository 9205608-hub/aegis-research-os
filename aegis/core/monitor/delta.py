"""论点 Delta 简报 — Aegis 2.0 Phase 3 事件循环.

持续监控回路每次触发 ``--update`` 复研，都会在 thesis 版本链上追加新一版。
本模块**纯函数、无 I/O**：对比前后两版 thesis payload（``record["thesis"]``，
即 :class:`~aegis.data_contracts.thesis_schema.ThesisContract` 的
``model_dump(mode="json")`` dict），产出一份中文 delta 简报——

    「什么变了、对论点什么影响、哪个监控点触发的」。

设计取舍：

- **纯函数**：只吃两个 dict，吐一个 :class:`DeltaBriefing` dataclass。落盘 /
  读链 / 触发判定都由调用方（扫描器）负责，本模块不碰文件系统、不连网络。
- **数值噪声门**：每股价值这类数值字段，变化幅度 > 1% 才算「变了」，避免
  DCF 复算的浮点抖动被误报成论点调整。
- **文本严格比对**：叙事字段（核心论点 / 差异化观点…）只要严格不等即算变。
- **监控点按 description 集合求差**：新增 / 移除哪些监控点一目了然。
- **中文化铁律**：所有面向人的文案（标签、总结、Markdown 简报）一律简体中文，
  只保留国际通用缩写。

对外三件套：:class:`FieldChange` / :class:`DeltaBriefing` +
:func:`diff_theses`（比两个 payload dict）/ :func:`summarize_change`
（比两条链 record，含首版便捷处理）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field as dataclass_field
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "FieldChange",
    "DeltaBriefing",
    "diff_theses",
    "summarize_change",
    "NUMERIC_CHANGE_THRESHOLD",
]

#: 数值字段的相对变化门槛：|after-before|/|before| 超过此值才算「变了」。
#: 挡住 DCF 复算的浮点噪声（如 10.00 → 10.05）被误报成论点调整。
NUMERIC_CHANGE_THRESHOLD = 0.01

#: 追踪的 thesis 字段：(field, label_zh, is_numeric)。顺序即简报里的展示顺序。
_FIELD_SPECS: tuple[tuple[str, str, bool], ...] = (
    ("core_thesis", "核心论点", False),
    ("my_variant", "差异化观点", False),
    ("counter_thesis", "反方论点", False),
    ("market_implied_story", "市场隐含预期", False),
    ("sector_cycle_position", "定价体制", False),
    ("publishing_status", "发布状态", False),
    ("confidence_bucket", "置信度", False),
    ("bear_case_value", "悲观每股价值", True),
    ("base_case_value", "基准每股价值", True),
    ("bull_case_value", "乐观每股价值", True),
)

#: 数值字段名集合（描述/格式化时区分处理）。
_NUMERIC_FIELDS = frozenset(f for f, _, is_num in _FIELD_SPECS if is_num)

#: label 快查表。
_FIELD_LABELS = {f: label for f, label, _ in _FIELD_SPECS}

#: 「最关键变化」优先级（越靠前越关键，总结里优先点名）。
_CRITICAL_ORDER: tuple[str, ...] = (
    "publishing_status",
    "core_thesis",
    "base_case_value",
    "confidence_bucket",
    "my_variant",
    "bear_case_value",
    "bull_case_value",
    "counter_thesis",
    "market_implied_story",
    "sector_cycle_position",
)


# ---------------------------------------------------------------------------
# dataclasses
# ---------------------------------------------------------------------------

@dataclass
class FieldChange:
    """单个 thesis 字段的前后变化。"""

    field: str          # thesis dict 里的字段名（英文标识符）
    label_zh: str       # 中文标签，如「核心论点」「基准每股价值」「发布状态」
    before: Any
    after: Any


@dataclass
class DeltaBriefing:
    """一次复核的完整 delta 简报（纯数据，可 to_dict / to_markdown）。"""

    entity_id: str
    from_version: int | None
    to_version: int
    trigger_zh: str | None                      # 触发原因（哪个监控点/事件），可 None
    changes: list[FieldChange] = dataclass_field(default_factory=list)
    monitorables_added: list[str] = dataclass_field(default_factory=list)
    monitorables_removed: list[str] = dataclass_field(default_factory=list)
    summary_zh: str = ""

    # -- 序列化 -------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """转成可 JSON 落盘的 dict（与项目 thesis JSONL 风格一致）。"""
        return {
            "entity_id": self.entity_id,
            "from_version": self.from_version,
            "to_version": self.to_version,
            "trigger_zh": self.trigger_zh,
            "changes": [
                {
                    "field": c.field,
                    "label_zh": c.label_zh,
                    "before": c.before,
                    "after": c.after,
                }
                for c in self.changes
            ],
            "monitorables_added": list(self.monitorables_added),
            "monitorables_removed": list(self.monitorables_removed),
            "summary_zh": self.summary_zh,
        }

    def to_markdown(self) -> str:
        """人读的中文 Markdown 简报：标题 + 触发 + 变更清单 + 影响总结。"""
        from_label = "首版" if self.from_version is None else f"v{self.from_version}"
        eid = self.entity_id or "未知标的"
        lines: list[str] = [
            f"# 论点 Delta 简报 · {eid} · {from_label} → v{self.to_version}",
            "",
            f"**触发来源**：{self.trigger_zh or '无特定触发（例行复核）'}",
            "",
            "## 变更清单",
        ]

        if self.changes:
            for c in self.changes:
                is_num = c.field in _NUMERIC_FIELDS
                before = _fmt_money(c.before) if is_num else _fmt_text(c.before)
                after = _fmt_money(c.after) if is_num else _fmt_text(c.after)
                lines.append(f"- **{c.label_zh}**：{before} → {after}")
        else:
            lines.append("- 论点主体字段无变化")

        lines += ["", "## 监控点调整"]
        if self.monitorables_added or self.monitorables_removed:
            for d in self.monitorables_added:
                lines.append(f"- ＋ 新增：{d}")
            for d in self.monitorables_removed:
                lines.append(f"- － 移除：{d}")
        else:
            lines.append("- 监控点无增减")

        lines += ["", "## 影响总结", "", self.summary_zh or "（无）"]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 小工具（纯函数、容错）
# ---------------------------------------------------------------------------

def _get(obj: Any, key: str, default: Any = None) -> Any:
    """dict 与对象双形态取值（与 persistence._get 同风格）。"""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _as_thesis_dict(payload: Any) -> dict[str, Any]:
    """把 thesis payload 容错成 dict；非 dict → 空 dict（永不 raise）。"""
    if isinstance(payload, dict):
        return payload
    dump = getattr(payload, "model_dump", None)
    if callable(dump):
        try:
            got = dump(mode="json")
            if isinstance(got, dict):
                return got
        except Exception as e:  # noqa: BLE001 — 序列化失败降级空 dict
            logger.warning("delta: model_dump 失败，降级空 dict: %s", e)
    return {}


def _num(v: Any) -> float | None:
    """取数值（bool 不当数字）；非数值 → None。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


def _fmt_money(v: Any) -> str:
    """每股价值的中文格式化：数值 → ``¥12.34``，None/非数值 → ``—``。"""
    n = _num(v)
    if n is None:
        return "—"
    return f"¥{n:.2f}"


def _fmt_text(v: Any, *, limit: int = 60) -> str:
    """文本字段的展示格式化：None/空 → ``—``，过长截断加省略号。"""
    s = str(v).strip() if v is not None else ""
    if not s:
        return "—"
    if len(s) > limit:
        return s[:limit] + "…"
    return f"「{s}」"


def _numeric_changed(before: Any, after: Any) -> bool:
    """数值字段是否变化：相对幅度 > 1% 才算变（None ↔ 有值算变）。"""
    b, a = _num(before), _num(after)
    if b is None and a is None:
        return bool(before != after)      # 两者都非数值（含 None ↔ None → 不变）
    if b is None or a is None:
        return True                       # 一有数一没数 → 变
    if b == 0:
        return abs(a) > 1e-9              # 从 0 变到任何非 0 → 变
    return abs(a - b) / abs(b) > NUMERIC_CHANGE_THRESHOLD


def _text_changed(before: Any, after: Any) -> bool:
    """文本字段是否变化：strip 后严格不等即算变。"""
    return str(before or "").strip() != str(after or "").strip()


def _monitor_descriptions(thesis: dict[str, Any]) -> list[str]:
    """从 thesis payload 取监控点 description（去重保序，坏项跳过）。"""
    out: list[str] = []
    raw = thesis.get("must_monitor")
    if not isinstance(raw, (list, tuple)):
        return out
    for m in raw:
        desc = _get(m, "description")
        s = str(desc).strip() if desc is not None else ""
        if s:
            out.append(s)
    return list(dict.fromkeys(out))       # 保序去重


# ---------------------------------------------------------------------------
# 核心：diff 两个 payload dict
# ---------------------------------------------------------------------------

def diff_theses(
    prev_thesis: dict,
    new_thesis: dict,
    *,
    entity_id: str = "",
    from_version: int | None = None,
    to_version: int = 0,
    trigger_zh: str | None = None,
) -> DeltaBriefing:
    """比较两个 thesis payload dict（``record["thesis"]``），产出中文 delta 简报。

    追踪字段见 :data:`_FIELD_SPECS`。数值字段（每股价值）变化 > 1% 才算变；
    文本字段严格不等即算变。监控点按 description 集合求 added / removed。
    ``summary_zh`` 用中文说清有几处变化、最关键的是什么、（若给了 trigger）
    由什么触发；无任何变化时明确写「本次复核论点无实质变化」。

    永不 raise：坏 payload 一律容错成空 dict，最坏情况报「无实质变化」。
    """
    prev = _as_thesis_dict(prev_thesis)
    new = _as_thesis_dict(new_thesis)

    # 若未显式给 entity_id，从 payload 兜底。
    eid = str(entity_id or new.get("entity_id") or prev.get("entity_id") or "").strip()

    changes: list[FieldChange] = []
    for fname, label, is_numeric in _FIELD_SPECS:
        before = prev.get(fname)
        after = new.get(fname)
        changed = (
            _numeric_changed(before, after)
            if is_numeric
            else _text_changed(before, after)
        )
        if changed:
            changes.append(FieldChange(
                field=fname, label_zh=label, before=before, after=after))

    prev_desc = _monitor_descriptions(prev)
    new_desc = _monitor_descriptions(new)
    prev_set, new_set = set(prev_desc), set(new_desc)
    added = [d for d in new_desc if d not in prev_set]
    removed = [d for d in prev_desc if d not in new_set]

    briefing = DeltaBriefing(
        entity_id=eid,
        from_version=from_version,
        to_version=to_version,
        trigger_zh=(trigger_zh or None),
        changes=changes,
        monitorables_added=added,
        monitorables_removed=removed,
    )
    briefing.summary_zh = _build_summary(briefing)
    return briefing


def summarize_change(
    prev_record: dict | None,
    new_record: dict,
    *,
    trigger_zh: str | None = None,
) -> DeltaBriefing:
    """便捷封装：吃两条链 record（含 ``version`` / ``thesis``），产出简报。

    ``prev_record`` 为 ``None``（首版）时：``from_version=None``、``changes`` 为空、
    ``summary_zh`` 写「首次建立论点」。否则委托 :func:`diff_theses`。
    """
    new_record = new_record if isinstance(new_record, dict) else {}
    new_thesis = _as_thesis_dict(new_record.get("thesis"))
    to_version = new_record.get("version")
    to_version = int(to_version) if isinstance(to_version, int) else 0
    eid = str(new_thesis.get("entity_id") or "").strip()

    if prev_record is None:
        # 首版：没有对比基准，不产出字段变化，只如实标注「首次建立论点」。
        n_monitor = len(_monitor_descriptions(new_thesis))
        summary = "首次建立论点"
        if n_monitor:
            summary += f"，共设定 {n_monitor} 个监控点"
        summary += "。"
        if trigger_zh:
            summary += f"（触发来源：{trigger_zh}）"
        return DeltaBriefing(
            entity_id=eid,
            from_version=None,
            to_version=to_version,
            trigger_zh=(trigger_zh or None),
            changes=[],
            monitorables_added=[],
            monitorables_removed=[],
            summary_zh=summary,
        )

    prev_record = prev_record if isinstance(prev_record, dict) else {}
    prev_thesis = _as_thesis_dict(prev_record.get("thesis"))
    from_version = prev_record.get("version")
    from_version = int(from_version) if isinstance(from_version, int) else None

    return diff_theses(
        prev_thesis,
        new_thesis,
        entity_id=eid,
        from_version=from_version,
        to_version=to_version,
        trigger_zh=trigger_zh,
    )


# ---------------------------------------------------------------------------
# 中文总结生成
# ---------------------------------------------------------------------------

def _describe_change(c: FieldChange) -> str:
    """把一处字段变化描述成一句中文短语。"""
    if c.field in _NUMERIC_FIELDS:
        return f"{c.label_zh}由 {_fmt_money(c.before)} 调整为 {_fmt_money(c.after)}"
    return f"{c.label_zh}由 {_fmt_text(c.before, limit=40)} 调整为 {_fmt_text(c.after, limit=40)}"


def _most_critical(changes: list[FieldChange]) -> FieldChange | None:
    """按 :data:`_CRITICAL_ORDER` 挑最关键的一处变化。"""
    if not changes:
        return None
    order = {f: i for i, f in enumerate(_CRITICAL_ORDER)}
    return min(changes, key=lambda c: order.get(c.field, len(order)))


def _build_summary(b: DeltaBriefing) -> str:
    """生成 ``summary_zh``：几处变化 / 最关键的是什么 / 监控点增减 / 触发来源。"""
    n_fields = len(b.changes)
    n_added = len(b.monitorables_added)
    n_removed = len(b.monitorables_removed)

    # 无任何实质变化（字段 + 监控点都没动）。
    if n_fields == 0 and n_added == 0 and n_removed == 0:
        summary = "本次复核论点无实质变化。"
        if b.trigger_zh:
            summary += f"（触发来源：{b.trigger_zh}）"
        return summary

    parts: list[str] = []

    if n_fields:
        parts.append(f"本次复核共识别 {n_fields} 处论点变化")
        key = _most_critical(b.changes)
        if key is not None:
            parts.append(f"其中最关键的是{_describe_change(key)}")
    else:
        parts.append("本次复核论点主体字段未变")

    mon_bits: list[str] = []
    if n_added:
        mon_bits.append(f"新增 {n_added} 个")
    if n_removed:
        mon_bits.append(f"移除 {n_removed} 个")
    if mon_bits:
        parts.append("监控点" + "、".join(mon_bits))

    summary = "；".join(parts) + "。"

    if b.trigger_zh:
        summary += f"本次复核由「{b.trigger_zh}」触发。"

    return summary
