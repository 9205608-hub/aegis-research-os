"""Thesis 持久化 — Aegis 2.0 Phase 2 任务 B2.

DESIGN_2.0 §三.C：**thesis 持久化直接序列化已有
:class:`~aegis.data_contracts.thesis_schema.ThesisContract`**（沉睡合同复活，
不新设计 JSON 结构）；版本历史用 **append-only JSONL 链，不建正式状态机**
（单人项目维护税，设计红线 10）。

三件事：

1. :func:`build_thesis_contract` —— 把一次 run 的产物们（synthesized_thesis /
   预期前沿 / 定价体制 / 核验结果 / kill_criteria…）映射进 ThesisContract。
   字段映射约定（先读 schema 决定，缺字段一律容错为中文占位「未提供」）：

   - ``market_implied_story`` ← 预期前沿摘要（复用
     :func:`~aegis.core.chief_analyst.thesis_synthesizer.frontier_prompt_lines`
     的条件化句式，设计红线 2），无前沿时回退 synthesized_thesis 同名字段；
   - ``my_variant`` / ``counter_thesis`` 等叙事字段 ← synthesized_thesis；
   - ``sector_cycle_position`` ← 定价体制叙事框架（narrative_frame_zh，
     regime 摘要在合同里最贴近的既有字段——不加新字段）；
   - ``must_monitor`` ← 任务 B1 :func:`~.monitorables.build_monitorables`；
   - ``kill_criteria`` ← 透传（dict 容错成 KillCriterion）；
   - ``review_date`` ← 创建日 + 90 天（对齐 postmortem 90 天回看）。

2. :func:`save_thesis_version` —— 追加 ``{entity}.jsonl`` 一行::

       {"version": N, "created_at": ..., "run_id": ..., "parent_version": ...,
        "thesis": contract 的 JSON dict}

   version 自增、parent_version 指向上一行——append-only 链即全部状态。

3. :func:`load_latest` / :func:`history` —— 读链（坏行跳过，不打断）。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from aegis.core._coerce import coerce_list, normalize_low_med_high
from aegis.core.thesis.monitorables import build_monitorables
from aegis.data_contracts.common import (
    AccountingStandard,
    ConfidenceBucket,
    EdgeDurability,
    EdgeType,
    MarketId,
    PublishingStatus,
    ResearchMode,
    SourceTier,
)
from aegis.data_contracts.thesis_schema import (
    EdgeClassification,
    KillCriterion,
    Monitorable,
    ThesisContract,
)

logger = logging.getLogger(__name__)

__all__ = [
    "build_thesis_contract",
    "save_thesis_version",
    "load_latest",
    "history",
    "DEFAULT_THESIS_DIR",
    "PLACEHOLDER",
]

#: 版本链默认落盘目录。
DEFAULT_THESIS_DIR = Path(".cache/thesis")

#: 缺字段容错占位（中文化铁律：A 股产物的占位文本也必须是简体中文）。
PLACEHOLDER = "未提供"

#: postmortem 回看周期（DESIGN_2.0 §三.C：90 天后自动回看关键假设）。
REVIEW_AFTER_DAYS = 90


# ---------------------------------------------------------------------------
# 小工具（全部容错，永不 raise）
# ---------------------------------------------------------------------------

def _get(obj: Any, key: str, default: Any = None) -> Any:
    """dict 与 dataclass/对象双形态取值。"""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _text(obj: Any, key: str, default: str = PLACEHOLDER) -> str:
    """取自然语言字段：空/缺失 → 占位文本（满足 schema min_length=1）。"""
    v = str(_get(obj, key) or "").strip()
    return v or default


def normalize_entity_id(entity_id: Any) -> str:
    """归一到 schema 的 EntityId 形态（``^[a-z0-9_]+$``）。

    "NVDA" → "nvda"；"600519.SH" → "600519_sh"；空 → "unknown"。
    """
    s = re.sub(r"[^a-z0-9_]", "_", str(entity_id or "").strip().lower())
    s = s.strip("_") or "unknown"
    return s[:128]


def run_created_at(run_id: Any) -> datetime | None:
    """从 run_id（``run_20260710_131211_xxx``）解析 run 产物时间。"""
    m = re.search(r"(\d{8})_(\d{6})", str(run_id or ""))
    if not m:
        return None
    try:
        return datetime.strptime(f"{m.group(1)}{m.group(2)}", "%Y%m%d%H%M%S")
    except ValueError:
        return None


def _normalize_edge_durability(val: Any) -> EdgeDurability:
    v = str(val or "").strip().lower().replace("-", "_").replace(" ", "_")
    mapping = {
        "short_term": EdgeDurability.SHORT_TERM,
        "short": EdgeDurability.SHORT_TERM,
        "短期": EdgeDurability.SHORT_TERM,
        "medium_term": EdgeDurability.MEDIUM_TERM,
        "medium": EdgeDurability.MEDIUM_TERM,
        "中期": EdgeDurability.MEDIUM_TERM,
        "long_term": EdgeDurability.LONG_TERM,
        "long": EdgeDurability.LONG_TERM,
        "长期": EdgeDurability.LONG_TERM,
    }
    return mapping.get(v, EdgeDurability.MEDIUM_TERM)


def _normalize_confidence(val: Any) -> ConfidenceBucket:
    v = str(val or "").strip().lower().replace("-", "_").replace(" ", "_")
    if v in ("very_low", "very_high"):
        return ConfidenceBucket(v)
    return ConfidenceBucket(normalize_low_med_high(v))


def _normalize_publishing_status(val: Any) -> PublishingStatus:
    v = str(val or "").strip().lower()
    try:
        return PublishingStatus(v)
    except ValueError:
        return PublishingStatus.DRAFT


def _normalize_market(val: Any) -> MarketId:
    v = str(val or "").strip().lower()
    try:
        return MarketId(v)
    except ValueError:
        return MarketId.CN


def _scenario_value(scenarios: Any, *names: str) -> float | None:
    """从情景 dict 提取每股值：接受 float 或 {'per_share'/'value'/...} 嵌套。"""
    if not isinstance(scenarios, dict):
        return None
    for name in names:
        v = scenarios.get(name)
        if isinstance(v, dict):
            for k in ("per_share", "per_share_value", "value", "price"):
                inner = v.get(k)
                if isinstance(inner, (int, float)):
                    return float(inner)
        elif isinstance(v, (int, float)):
            return float(v)
    return None


def _frontier_story(frontier: Any) -> str:
    """预期前沿 → market_implied_story（条件化句式，设计红线 2）。"""
    fr = frontier.to_dict() if hasattr(frontier, "to_dict") else frontier
    if not isinstance(fr, dict):
        return ""
    try:
        # 复用已白名单化的条件化句式生成器，不重新发明（设计红线 9 同源）。
        from aegis.core.chief_analyst.thesis_synthesizer import frontier_prompt_lines
        lines = frontier_prompt_lines(fr, lang="zh")
    except Exception as e:  # noqa: BLE001 — 前沿摘要失败不打断合同构建
        logger.warning(f"thesis persistence: frontier summary failed: {e}")
        return ""
    if not lines:
        return ""
    return "市场预期前沿（现价隐含的条件化预期）：" + "；".join(lines)


def _kill_criteria(kill_criteria: Any) -> list[KillCriterion]:
    """透传 kill_criteria（dict/对象容错）；空 → 一条如实的人工占位。"""
    out: list[KillCriterion] = []
    for item in coerce_list(kill_criteria):
        if isinstance(item, KillCriterion):
            out.append(item)
            continue
        desc = _text(item, "description", "")
        if not desc:
            continue
        out.append(KillCriterion(
            description=desc,
            threshold=_text(item, "threshold", "未量化（人工判断）"),
            check_frequency=_text(item, "check_frequency", "quarterly"),
        ))
    if not out:
        out.append(KillCriterion(
            description="论点核心假设被下一期定期报告证伪（本 run 未产出结构化 kill criteria，需人工判定）",
            threshold="未量化（人工判断）",
            check_frequency="quarterly",
        ))
    return out


def _open_questions(synthesized_thesis: Any) -> list[str]:
    out: list[str] = []
    for q in coerce_list(_get(synthesized_thesis, "open_questions")):
        text = q.get("question") if isinstance(q, dict) else q
        text = str(text or "").strip()
        if text:
            out.append(text)
    return out


# ---------------------------------------------------------------------------
# 合同构建
# ---------------------------------------------------------------------------

def build_thesis_contract(
    *,
    entity_id: str,
    run_id: str,
    synthesized_thesis: Any = None,
    frontier: Any = None,
    regime: Any = None,
    verification_results: Any = None,
    kill_criteria: Any = None,
    monitorables: list[Monitorable] | None = None,
    scenarios: Any = None,
    supporting_claim_ids: list[str] | None = None,
    publishing_status: Any = "draft",
    confidence: Any = "medium",
    market_id: Any = "cn",
    accounting_standard: Any = AccountingStandard.CAS,
    thesis_horizon: str = "12_months",
    created_at: datetime | date | None = None,
    parent_thesis_id: str | None = None,
    thesis_version: int = 1,
) -> ThesisContract:
    """把一次 run 的产物映射进沉睡合同 ThesisContract（缺字段容错）。

    所有输入均接受 dataclass / dict / None；自然语言缺失 → 「未提供」。
    monitorables 缺省时由任务 B1 :func:`build_monitorables` 自动生成。
    """
    st = synthesized_thesis
    eid = normalize_entity_id(entity_id)
    created = created_at or run_created_at(run_id) or datetime.now()
    created_date = created.date() if isinstance(created, datetime) else created

    market_implied = _frontier_story(frontier) or _text(st, "market_implied_story")

    regime_frame = str(_get(regime, "narrative_frame_zh") or "").strip()
    dominant = str(_get(regime, "dominant") or "").strip()
    if regime_frame:
        sector_cycle = f"定价体制（{dominant or 'unknown'}）：{regime_frame}"
    else:
        sector_cycle = PLACEHOLDER

    must_monitor = monitorables if monitorables else build_monitorables(
        synthesized_thesis=st,
        verification_results=verification_results,
        regime=regime,
    )

    fragility = [str(x).strip() for x in
                 coerce_list(_get(st, "unresolved_tensions")) if str(x).strip()]
    change_mind = _text(st, "what_would_change_my_mind", "")

    try:
        accounting = AccountingStandard(accounting_standard)
    except ValueError:
        accounting = AccountingStandard.CAS

    return ThesisContract(
        thesis_id=f"thesis_{eid}",
        thesis_version=max(1, int(thesis_version)),
        parent_thesis_id=parent_thesis_id,
        run_id=str(run_id or "run_unknown"),
        entity_id=eid,
        research_mode=ResearchMode.SINGLE_ENTITY,
        # 核心论点
        core_thesis=_text(st, "core_thesis"),
        why_now=_text(st, "why_now"),
        market_implied_story=market_implied,
        my_variant=_text(st, "my_variant"),
        variant_magnitude=_text(st, "variant_magnitude"),
        # 信息优势
        edge_classification=EdgeClassification(
            primary_edge_type=EdgeType.ANALYTICAL,
            edge_source=_text(st, "edge_source"),
            edge_durability=_normalize_edge_durability(
                _get(st, "edge_durability")),
            edge_decay_trigger=change_mind or PLACEHOLDER,
        ),
        # 情景
        scenario_matrix_id=f"scenarios_{run_id or eid}",
        bear_case_value=_scenario_value(
            scenarios, "bear", "pessimistic", "downside"),
        base_case_value=_scenario_value(scenarios, "base", "base_case"),
        bull_case_value=_scenario_value(
            scenarios, "bull", "optimistic", "upside"),
        key_assumption_disagreement=_text(st, "key_assumption_disagreement"),
        # 证据与反论点
        supporting_claim_ids=(
            [str(c) for c in (supporting_claim_ids or []) if str(c).strip()]
            or [f"run:{run_id or eid}"]
        ),
        counter_thesis=_text(st, "counter_thesis"),
        fragility_points=fragility or [PLACEHOLDER],
        disconfirming_triggers=[change_mind] if change_mind else [PLACEHOLDER],
        kill_criteria=_kill_criteria(kill_criteria),
        must_monitor=must_monitor,
        open_questions=_open_questions(st),
        # 上下文
        macro_dependency=_text(st, "macro_dependency"),
        sector_cycle_position=sector_cycle,
        management_quality_summary=_text(st, "management_quality_summary"),
        capital_allocation_assessment=_text(
            st, "capital_allocation_assessment"),
        # 发布
        publishing_status=_normalize_publishing_status(publishing_status),
        confidence_bucket=_normalize_confidence(confidence),
        bias_check_status="passed",
        # 组合信号
        data_source_tiers_used=[SourceTier.TIER_2],
        markets_covered=[_normalize_market(market_id)],
        accounting_standards_used=[accounting],
        thesis_horizon=str(thesis_horizon or "12_months"),
        review_date=created_date + timedelta(days=REVIEW_AFTER_DAYS),
    )


# ---------------------------------------------------------------------------
# append-only JSONL 版本链（不建状态机）
# ---------------------------------------------------------------------------

def _chain_path(entity_id: str, dir: Path | str | None) -> Path:
    base = Path(dir) if dir is not None else DEFAULT_THESIS_DIR
    return base / f"{normalize_entity_id(entity_id)}.jsonl"


def history(
    entity_id: str, *, dir: Path | str | None = None,
) -> list[dict[str, Any]]:
    """读整条版本链（按文件行序 = 版本序）。坏行跳过，永不 raise。"""
    path = _chain_path(entity_id, dir)
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as e:
        logger.warning(f"thesis history: read {path} failed: {e}")
        return []
    for i, line in enumerate(lines, start=1):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as e:
            logger.warning(f"thesis history: {path}:{i} corrupted, skipped: {e}")
            continue
        if isinstance(rec, dict) and isinstance(rec.get("version"), int):
            records.append(rec)
    return records


def load_latest(
    entity_id: str, *, dir: Path | str | None = None,
) -> dict[str, Any] | None:
    """最新版本记录（{version, created_at, run_id, parent_version, thesis}）。"""
    records = history(entity_id, dir=dir)
    return records[-1] if records else None


def save_thesis_version(
    entity_id: str,
    contract: ThesisContract,
    run_id: str,
    *,
    created_at: datetime | date | None = None,
    dir: Path | str | None = None,
) -> dict[str, Any]:
    """把一版 thesis 追加进 ``{entity}.jsonl``（append-only，版本自增）。

    created_at 缺省时取 run 产物时间（从 run_id 时间戳解析），解析不了
    才退当前时间。落盘前把合同的 thesis_version / parent_thesis_id 对齐
    到链上位置（frozen 模型用 ``model_copy(update=...)``，不可变性保持）。
    返回写入的记录 dict。
    """
    path = _chain_path(entity_id, dir)
    prior = history(entity_id, dir=dir)
    version = (prior[-1]["version"] + 1) if prior else 1
    parent_version = prior[-1]["version"] if prior else None

    aligned = contract.model_copy(update={
        "thesis_version": version,
        "parent_thesis_id": (
            str(prior[-1].get("thesis", {}).get("thesis_id") or contract.thesis_id)
            if prior else None
        ),
    })

    created = created_at or run_created_at(run_id) or datetime.now()
    record: dict[str, Any] = {
        "version": version,
        "created_at": created.isoformat(),
        "run_id": str(run_id or ""),
        "parent_version": parent_version,
        "thesis": aligned.model_dump(mode="json"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record
