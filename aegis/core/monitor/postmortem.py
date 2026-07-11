"""90 天回看复盘 — Aegis 2.0 Phase 3 任务 B3.

事件循环的校准闭环收口：一条 thesis 落盘时会写下 ``review_date``（创建日
+ 90 天，见 :mod:`aegis.core.thesis.persistence`）。到期后，本模块把这条
「沉睡合同」复活成一份 :class:`~aegis.data_contracts.postmortem_schema.PostMortem`
——对比论点建立时 vs 回看时的价格与关键假设兑现，落 JSON 文件。

这是「新框架是否更准」的唯一结构化证据来源：只有把每条论点的隐含方向、
发布状态、置信度与真实回看收益逐条记档，才能事后统计新老框架的命中率。

设计取舍（与 Phase 3 其他 monitor 模块一致）：

- **存储用 JSON 文件**（设计红线 10）：每条复盘落
  ``{POSTMORTEM_DIR}/{entity}_v{N}.json``，与 thesis JSONL 链风格一致。
- **确定性启发，不调 LLM**：``thesis_survived`` / ``variant_realized`` /
  ``edge_realized`` 全部用「论点隐含方向（基准每股价值 vs 建仓价）× 回看收益
  方向 × 发布状态」的确定性规则派生。无法判定时给保守默认，并在
  ``what_was_right`` / ``what_was_wrong`` 里如实说明（不编造结论）。
- **幂等**：已存在复盘文件的版本直接跳过，重复运行不会重写 / 重复。
- **永不 raise 到调用方**：:func:`due_records` / :func:`run_postmortems` 对
  坏链、坏价格、取价失败一律降级跳过 + 日志。:func:`build_postmortem` 只在
  价格 ``<= 0`` 时抛 :class:`ValueError`（由 :func:`run_postmortems` 捕获跳过）。

中文化铁律：所有面向人的自然语言（``what_was_right`` / ``what_was_wrong`` /
``lessons_for_system`` / 日志文案）一律简体中文，只保留国际通用缩写。
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

from aegis.core.thesis.persistence import (
    DEFAULT_THESIS_DIR,
    load_latest,
    normalize_entity_id,
)
from aegis.data_contracts.common import ConfidenceBucket, EdgeType
from aegis.data_contracts.postmortem_schema import PostMortem

logger = logging.getLogger(__name__)

__all__ = [
    "POSTMORTEM_DIR",
    "REVIEW_AFTER_DAYS",
    "DIRECTION_THRESHOLD",
    "due_records",
    "build_postmortem",
    "run_postmortems",
]

#: 复盘文件默认落盘目录（设计红线 10：JSON 文件存储）。
POSTMORTEM_DIR = Path(".cache/postmortems")

#: 回看周期，与 :data:`persistence.REVIEW_AFTER_DAYS` 对齐（仅用于兜底日期推算）。
REVIEW_AFTER_DAYS = 90

#: 方向判定的中性带：|基准价值/建仓价 - 1| 或 |回看收益| 超过此值才算有方向。
#: 挡住估值与现价接近、或价格微幅波动被误判成「方向兑现 / 证伪」。
DIRECTION_THRESHOLD = 0.02


# ---------------------------------------------------------------------------
# 小工具（全部容错，永不 raise）
# ---------------------------------------------------------------------------

def _get(obj: Any, key: str, default: Any = None) -> Any:
    """dict 与 dataclass/对象双形态取值（与 persistence._get 同风格）。"""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _num(v: Any) -> float | None:
    """取数值（bool 不当数字）；非数值 → None。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip())
        except (TypeError, ValueError):
            return None
    return None


def _record_version(record: dict[str, Any]) -> int:
    """链 record 的版本号（缺失 / 坏值兜底 1）。"""
    v = record.get("version")
    if isinstance(v, int) and v >= 1:
        return v
    thesis = record.get("thesis") or {}
    tv = thesis.get("thesis_version") if isinstance(thesis, dict) else None
    if isinstance(tv, int) and tv >= 1:
        return tv
    return 1


def _record_date(record: dict[str, Any], thesis: dict[str, Any]) -> date | None:
    """论点建立日：优先 record.created_at，退 thesis.review_date - 90 天。"""
    raw = str(record.get("created_at") or "").strip()
    if raw:
        try:
            return datetime.fromisoformat(raw).date()
        except ValueError:
            try:
                return date.fromisoformat(raw[:10])
            except ValueError:
                pass
    review = str(thesis.get("review_date") or "").strip()
    if review:
        try:
            from datetime import timedelta
            return date.fromisoformat(review[:10]) - timedelta(days=REVIEW_AFTER_DAYS)
        except ValueError:
            pass
    return None


def _postmortem_path(entity_id: str, version: int) -> Path:
    """复盘文件路径（读模块级 :data:`POSTMORTEM_DIR`，便于测试 monkeypatch）。"""
    return POSTMORTEM_DIR / f"{normalize_entity_id(entity_id)}_v{version}.json"


def _coerce_price(quote: Any) -> float | None:
    """把 quote_fn 的返回容错成正现价：接受 float / CNQuote 对象 / dict。

    非正数 / 无法解析 → None（调用方据此跳过该票）。
    """
    if quote is None:
        return None
    n = _num(quote)
    if n is not None:
        return n if n > 0 else None
    v = _get(quote, "current_price")
    if v is None:
        v = _get(quote, "price")
    n = _num(v)
    if n is not None and n > 0:
        return n
    return None


def _extract_anchor_price(record: dict[str, Any]) -> float | None:
    """从 record 顶层 / thesis 里找建仓价锚（都没有 → None，由 price_lookup 兜底）。"""
    thesis = record.get("thesis") if isinstance(record.get("thesis"), dict) else {}
    for src in (record, thesis):
        if not isinstance(src, dict):
            continue
        for key in ("anchor_price", "price_at_thesis", "current_price", "entry_price"):
            n = _num(src.get(key))
            if n is not None and n > 0:
                return n
    return None


def _default_quote_fn(ticker: str) -> Any:
    """默认取价：腾讯 / 新浪 A 股现价兜底（不可达 / 报错一律 None，不连测试网络）。"""
    try:
        from aegis.core.acquisition.connectors.tencent_sina_quote import fetch_cn_quote
        return fetch_cn_quote(ticker)
    except Exception as e:  # noqa: BLE001 — 取价失败降级 None，永不打断复盘循环
        logger.warning("postmortem: 默认取价 %s 失败: %s", ticker, e)
        return None


# ---------------------------------------------------------------------------
# 兑现判定启发（确定性，不调 LLM）
# ---------------------------------------------------------------------------

#: 论点已在建立时被否决 / 未获正式发布的发布状态。
_DEAD_STATUSES = frozenset({"blocked", "killed", "expired", "downgraded"})

#: 方向英文 → 中文（用于 what_was_right/wrong 文案）。
_DIR_ZH = {"up": "上行", "down": "下行", "flat": "中性", "unknown": "未知"}


def _thesis_direction(thesis: dict[str, Any], price_at_thesis: float) -> str:
    """论点隐含方向：基准每股价值 vs 建仓价（无锚 → unknown）。"""
    base = _num(thesis.get("base_case_value"))
    if base is None or price_at_thesis <= 0:
        return "unknown"
    ratio = base / price_at_thesis - 1.0
    if ratio > DIRECTION_THRESHOLD:
        return "up"
    if ratio < -DIRECTION_THRESHOLD:
        return "down"
    return "flat"


def _return_sign(total_return: float) -> str:
    """回看收益方向（中性带 ±DIRECTION_THRESHOLD）。"""
    if total_return > DIRECTION_THRESHOLD:
        return "up"
    if total_return < -DIRECTION_THRESHOLD:
        return "down"
    return "flat"


def _edge_type(thesis: dict[str, Any]) -> EdgeType:
    """从 thesis.edge_classification.primary_edge_type 取；缺失 / 坏值 → ANALYTICAL。"""
    ec = thesis.get("edge_classification")
    raw = _get(ec, "primary_edge_type") if ec is not None else None
    try:
        return EdgeType(str(raw or "").strip().lower())
    except ValueError:
        return EdgeType.ANALYTICAL


def _confidence_bucket(thesis: dict[str, Any]) -> ConfidenceBucket:
    """从 thesis.confidence_bucket 归一；坏值 → MEDIUM（任务要求）。"""
    raw = str(thesis.get("confidence_bucket") or "").strip().lower()
    raw = raw.replace("-", "_").replace(" ", "_")
    try:
        return ConfidenceBucket(raw)
    except ValueError:
        return ConfidenceBucket.MEDIUM


# ---------------------------------------------------------------------------
# 到期扫描
# ---------------------------------------------------------------------------

def due_records(
    *,
    thesis_dir: Path | str | None = None,
    today: str | None = None,
) -> list[dict[str, Any]]:
    """遍历 thesis 目录下所有 ``{entity}.jsonl`` 链的最新版 record，返回到期未复盘的。

    「到期」= ``thesis.review_date <= today``；「未复盘」= 对应
    ``{entity}_v{N}.json`` 尚不存在（N = 链版本号）。``today`` 缺省
    ``date.today().isoformat()``。坏链 / 无 review_date 一律跳过，永不 raise。
    """
    base = Path(thesis_dir) if thesis_dir is not None else DEFAULT_THESIS_DIR
    today_str = (today or date.today().isoformat()).strip()
    out: list[dict[str, Any]] = []
    if not base.exists():
        return out
    try:
        files = sorted(base.glob("*.jsonl"))
    except OSError as e:
        logger.warning("postmortem: 扫描 thesis 目录 %s 失败: %s", base, e)
        return out
    for path in files:
        try:
            record = load_latest(path.stem, dir=base)
            if not record:
                continue
            thesis = record.get("thesis")
            if not isinstance(thesis, dict):
                continue
            review = str(thesis.get("review_date") or "").strip()
            if not review or review > today_str:
                continue  # 无回看日 / 尚未到期
            entity = normalize_entity_id(thesis.get("entity_id") or path.stem)
            version = _record_version(record)
            if _postmortem_path(entity, version).exists():
                continue  # 已复盘 → 幂等跳过
            # 审查发现 #4：thesis 缺 entity_id 时（坏链/手工链/旧 schema），把
            # 已解析的 path.stem 回退 entity 回写进 record，使下游 build/run
            # 用同一 entity（否则幂等检查用 path.stem、落盘用 'unknown'，既破
            # 幂等又让不同标的撞进同一 unknown_v{N}.json 互相覆盖）。
            if not str(thesis.get("entity_id") or "").strip():
                record = {**record, "thesis": {**thesis, "entity_id": entity}}
            out.append(record)
        except Exception as e:  # noqa: BLE001 — 单条坏链不打断整轮扫描
            logger.warning("postmortem: 处理链 %s 失败，跳过: %s", path, e)
    return out


# ---------------------------------------------------------------------------
# 复盘构建
# ---------------------------------------------------------------------------

def build_postmortem(
    record: dict[str, Any],
    *,
    price_at_thesis: float,
    price_at_review: float,
    today: str | None = None,
) -> PostMortem:
    """从一条 thesis record + 两个价格构造 :class:`PostMortem`（字段填齐）。

    - ``total_return = price_at_review / price_at_thesis - 1``；
    - ``thesis_survived`` / ``variant_realized`` / ``edge_realized`` 用确定性
      启发（论点隐含方向 × 回看收益方向 × 发布状态）派生，保守默认在
      ``what_was_right`` / ``what_was_wrong`` 里说明；
    - 价格 ``<= 0`` 抛 :class:`ValueError`（由 run 层捕获跳过）。
    """
    p_thesis = _num(price_at_thesis)
    p_review = _num(price_at_review)
    if p_thesis is None or p_thesis <= 0:
        raise ValueError(f"price_at_thesis 必须 > 0，收到 {price_at_thesis!r}")
    if p_review is None or p_review <= 0:
        raise ValueError(f"price_at_review 必须 > 0，收到 {price_at_review!r}")

    record = record if isinstance(record, dict) else {}
    thesis = record.get("thesis") if isinstance(record.get("thesis"), dict) else {}

    entity = normalize_entity_id(thesis.get("entity_id") or "unknown")
    version = _record_version(record)
    total_return = p_review / p_thesis - 1.0

    status = str(thesis.get("publishing_status") or "").strip().lower()
    already_dead = status in _DEAD_STATUSES
    direction = _thesis_direction(thesis, p_thesis)
    rsign = _return_sign(total_return)
    dir_zh = _DIR_ZH.get(direction, "未知")
    pct = total_return * 100.0

    # -- thesis_survived：论点是否未被证伪 --------------------------------
    if already_dead:
        thesis_survived = False
    elif direction == "unknown":
        thesis_survived = True  # 无锚可证伪 → 保守视为存续
    else:
        contradicted = (
            (direction == "up" and rsign == "down")
            or (direction == "down" and rsign == "up")
        )
        thesis_survived = not contradicted

    # -- variant_realized / edge_realized：需正面证据，保守默认 False ------
    variant_realized = direction in ("up", "down") and rsign == direction
    edge_realized = variant_realized

    # -- what_was_right / what_was_wrong（各至少一条中文，schema min1）-----
    right: list[str] = []
    wrong: list[str] = []

    if variant_realized:
        right.append(
            f"差异化观点方向兑现：回看期收益 {pct:+.1f}% 与论点隐含方向"
            f"（{dir_zh}）一致。"
        )
    elif direction == "unknown":
        wrong.append(
            "缺少基准每股价值锚，无法判定差异化观点方向是否兑现（保守记为未兑现）。"
        )
    elif direction == "flat":
        wrong.append(
            f"论点隐含方向接近中性，回看期收益 {pct:+.1f}%，差异化观点未形成明确兑现。"
        )
    else:
        wrong.append(
            f"差异化观点方向未兑现：论点隐含方向为{dir_zh}，"
            f"回看期收益 {pct:+.1f}% 未同向。"
        )

    if already_dead:
        wrong.append(
            f"论点在建立时发布状态为「{status}」，本身未获正式发布或已被否决。"
        )
    elif thesis_survived:
        right.append(
            f"论点在 90 天回看期内未被证伪（发布状态：{status or '未知'}，"
            f"回看收益 {pct:+.1f}%）。"
        )

    if not right:
        right.append(
            f"已完成一次 90 天回看校准记录（回看收益 {pct:+.1f}%，无更强正面结论）。"
        )
    if not wrong:
        wrong.append(
            "本次回看未发现明显偏差（后续仍需人工复核关键假设的实际兑现度）。"
        )

    lessons: list[str] = []
    if direction == "unknown":
        lessons.append(
            "建议在论点落盘时同步记录建仓价锚（anchor_price），提升回看方向判定的可靠性。"
        )

    thesis_id = str(thesis.get("thesis_id") or "").strip() or f"thesis_{entity}"
    run_id = str(record.get("run_id") or thesis.get("run_id") or "").strip() or "run_unknown"
    original_date = _record_date(record, thesis) or date.fromisoformat(
        (today or date.today().isoformat())[:10]
    )
    review_date = date.fromisoformat((today or date.today().isoformat())[:10])

    return PostMortem(
        postmortem_id=f"postmortem_{entity}_v{version}",
        thesis_id=thesis_id,
        thesis_version=version,
        original_thesis_date=original_date,
        review_date=review_date,
        price_at_thesis=p_thesis,
        price_at_review=p_review,
        total_return=total_return,
        thesis_survived=thesis_survived,
        variant_realized=variant_realized,
        edge_type=_edge_type(thesis),
        edge_realized=edge_realized,
        what_was_right=right,
        what_was_wrong=wrong,
        original_confidence_bucket=_confidence_bucket(thesis),
        original_run_id=run_id,
        lessons_for_system=lessons,
    )


# ---------------------------------------------------------------------------
# 批量运行（到期 → 生成 → 落盘）
# ---------------------------------------------------------------------------

def run_postmortems(
    *,
    quote_fn: Callable[[str], Any] | None = None,
    thesis_dir: Path | str | None = None,
    today: str | None = None,
    price_lookup: Callable[[dict[str, Any]], Any] | None = None,
) -> list[PostMortem]:
    """对所有到期 record 生成复盘并写 ``{POSTMORTEM_DIR}/{entity}_v{N}.json``。

    - ``price_at_review`` 由 ``quote_fn(ticker)`` 取（默认腾讯/新浪现价），取不到 → 跳过该票；
    - ``price_at_thesis`` 优先从 record 顶层 / thesis 的价格锚取，退 ``price_lookup(record)``，
      仍无 → 跳过该票；
    - 价格 ``<= 0`` / 构造失败 → 跳过；已存在复盘文件 → :func:`due_records` 已过滤。

    永不 raise；返回成功生成的 :class:`PostMortem` 列表。
    """
    qf = quote_fn or _default_quote_fn
    today_str = (today or date.today().isoformat()).strip()
    out: list[PostMortem] = []

    for record in due_records(thesis_dir=thesis_dir, today=today_str):
        try:
            thesis = record.get("thesis") if isinstance(record.get("thesis"), dict) else {}
            entity = normalize_entity_id(thesis.get("entity_id") or "unknown")
            version = _record_version(record)
            ticker = str(
                record.get("ticker")
                or thesis.get("entity_id")
                or entity
            ).strip()

            # -- 回看现价 -------------------------------------------------
            try:
                quote = qf(ticker)
            except Exception as e:  # noqa: BLE001 — 取价异常降级跳过
                logger.warning("postmortem: %s 取价异常，跳过: %s", entity, e)
                continue
            price_review = _coerce_price(quote)
            if price_review is None:
                logger.warning("postmortem: %s 回看现价获取失败，跳过", entity)
                continue

            # -- 建仓价锚 -------------------------------------------------
            price_thesis = _extract_anchor_price(record)
            if price_thesis is None and price_lookup is not None:
                try:
                    price_thesis = _num(price_lookup(record))
                except Exception as e:  # noqa: BLE001 — 兜底取价异常降级
                    logger.warning("postmortem: %s price_lookup 异常: %s", entity, e)
                    price_thesis = None
            if price_thesis is None or price_thesis <= 0:
                logger.warning("postmortem: %s 建仓价锚缺失，跳过", entity)
                continue

            # -- 构造 + 落盘 ----------------------------------------------
            try:
                pm = build_postmortem(
                    record,
                    price_at_thesis=price_thesis,
                    price_at_review=price_review,
                    today=today_str,
                )
            except Exception as e:  # noqa: BLE001 — 价格<=0/schema 校验失败跳过
                logger.warning("postmortem: %s 构造失败，跳过: %s", entity, e)
                continue

            path = _postmortem_path(entity, version)
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps(pm.model_dump(mode="json"), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except OSError as e:
                logger.warning("postmortem: %s 落盘失败，跳过: %s", entity, e)
                continue

            out.append(pm)
        except Exception as e:  # noqa: BLE001 — 单票任何异常不打断整轮
            logger.warning("postmortem: 处理 record 失败，跳过: %s", e)

    return out
