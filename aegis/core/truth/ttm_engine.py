"""TTM 引擎 — A 股季报累计值 → 滚动 4 季（Aegis 2.0 Phase 1, Wave 3）.

DESIGN_2.0 §三.B「TTM 引擎规格（A 股特有坑，写死）」的落地：

- **A 股季报是年初累计值**（YTD），不是单季值。滚动 4 季必须用累计差分：

      TTM = FY_prev + YTD_cur − YTD_prev_same

  例（康达新材 002669，2026-07-10 实测东财 F10 数据）：
  TTM 营收 @2026Q1 = FY2025 52.37亿 + 2026Q1 11.56亿 − 2025Q1 8.77亿 ≈ 55.16亿。

- **FY 边界**：最新报告期就是年报（12-31）时 TTM = 该 FY 值本身，不做差分。

- **红线 #4（设计红线，违反=返工）**：
  1. TTM **只对流量科目**——本引擎的概念目录是封闭常量
     :data:`TTM_FLOW_CONCEPTS`（利润表流量三键），不提供任意 concept 的
     TTM 入口，资产负债表时点科目在结构上就进不来；
  2. 必须**累计差分**（见上式），禁止「最近 4 个单季相加」或
     「YTD 年化」等近似；
  3. **归母/扣非双轨**：ttm_net_income（归母）与 ttm_net_income_deducted
     （扣非归母）并行输出（康达基准：归母 1.25亿 vs 扣非 1672万，差 7 倍，
     单轨输出会掩盖利润成色）。

- **跨重述窗口**：同一报告期存在多版本（重述）时取最新版本；同一期
  快报（unaudited）与正式报告并存时正式报告优先——与
  :meth:`aegis.pit.store.PITStore.latest_value` / verification 的
  选取纪律同源。

- **降级语义（宁缺勿假）**：三元组（FY_prev / YTD_cur / YTD_prev_same）
  任一缺失 → 该概念 TTM 输出 None 并在 ``basis`` 里给出中文原因，
  绝不用年化/外推顶替。:func:`ttm_snapshot` **永不 raise**。

下游对接（红线 #8 豁免路径）：orchestrator ``run_quarterly_pit_step`` 经
``_default_ttm_snapshot`` seam 调用本引擎，把三个 TTM 值写入 meta_facts
固定键 ``ttm_revenue / ttm_net_income / ttm_net_income_deducted``——这是
Phase 1 唯一允许新增的 meta_facts 键（衍生 TTM 汇总值豁免），除此之外
新数据一律只住 PIT 层。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "TTM_FLOW_CONCEPTS",
    "TTMSnapshot",
    "ttm_snapshot",
]

#: 封闭概念目录（红线 #4：TTM 只对流量科目）。
#: meta_facts 固定键 → PIT 层利润表概念。资产负债表时点科目
#: （total_assets / total_liabilities / inventory …）绝不允许出现在这里。
TTM_FLOW_CONCEPTS: dict[str, str] = {
    "ttm_revenue": "revenue",
    "ttm_net_income": "net_income",                     # 归母口径
    "ttm_net_income_deducted": "net_income_deducted",   # 扣非归母（双轨）
}


@dataclass(frozen=True)
class TTMSnapshot:
    """一次 TTM 计算的结果（全部字段 JSON 可序列化）。

    - ``ttm_*``: 滚动 4 季值；无法凑齐累计差分三元组时为 None（宁缺勿假）；
    - ``latest_period``: 流量概念中最新的报告期末（"2026-03-31"），
      供 ``__data_freshness`` 数据截至行使用；全库无流量事实时为 None；
    - ``basis``: 概念 → 计算依据（method / periods / 或降级原因 reason_zh），
      可审计性字段，orchestrator 只读 ttm_* 与 latest_period。
    """

    entity_id: str
    latest_period: str | None = None
    ttm_revenue: float | None = None
    ttm_net_income: float | None = None
    ttm_net_income_deducted: float | None = None
    basis: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "latest_period": self.latest_period,
            "ttm_revenue": self.ttm_revenue,
            "ttm_net_income": self.ttm_net_income,
            "ttm_net_income_deducted": self.ttm_net_income_deducted,
            "basis": {k: dict(v) for k, v in self.basis.items()},
        }


# ---------------------------------------------------------------------------
# 事实选取（与 PITStore.latest_value / verification 同源的纪律）
# ---------------------------------------------------------------------------

def _latest_per_period(store: Any, entity_id: str, concept: str) -> dict[str, Any]:
    """concept 的 {period: 该期最新事实}。

    同 period 多版本时：审计值优先（正式报告自然取代快报），其余按
    (as_of, fact_version, id) 取最新（跨重述窗口永远最新版本）。
    store 为 None / 查询失败 → 空 dict（调用方降级输出 None）。
    """
    if store is None:
        return {}
    try:
        facts = store.get_facts(entity_id, concept)
    except Exception as e:  # noqa: BLE001 — 只读端失败不打断快照
        logger.debug(f"ttm_engine: get_facts({concept}) failed: {e}")
        return {}
    by_period: dict[str, Any] = {}
    for f in facts:
        cur = by_period.get(f.period)
        if cur is None:
            by_period[f.period] = f
            continue
        if cur.unaudited and not f.unaudited:
            by_period[f.period] = f
        elif cur.unaudited == f.unaudited and (
            (f.as_of, f.fact_version, f.id) > (cur.as_of, cur.fact_version, cur.id)
        ):
            by_period[f.period] = f
    return by_period


def _value(fact: Any) -> float | None:
    """事实数值（非有限数按缺失处理——NaN 穿透有前科，红线 5 同族纪律）。"""
    v = getattr(fact, "value", None)
    if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v):
        return float(v)
    return None


# ---------------------------------------------------------------------------
# 单概念 TTM（纯函数，离线可测）
# ---------------------------------------------------------------------------

def _ttm_for_concept(
    by_period: dict[str, Any],
) -> tuple[float | None, dict[str, Any]]:
    """一个流量概念的 TTM 值 + 计算依据。

    返回 ``(ttm, basis)``；无法计算时 ``(None, {"reason_zh": …})``。
    """
    if not by_period:
        return None, {"reason_zh": "PIT 库无该概念的季报事实"}
    latest = max(by_period)
    if len(latest) != 10:
        return None, {"reason_zh": f"报告期格式异常: {latest!r}"}

    cur = _value(by_period[latest])
    if cur is None:
        return None, {"reason_zh": f"{latest} 期数值缺失或非有限数"}

    # FY 边界：最新期即年报 → TTM = FY 本身（不做差分）。
    if latest[5:] == "12-31":
        return cur, {"method": "fy_direct", "periods": [latest]}

    # 累计差分：TTM = FY_prev + YTD_cur − YTD_prev_same（红线 #4）。
    try:
        year = int(latest[:4])
    except ValueError:
        return None, {"reason_zh": f"报告期年份无法解析: {latest!r}"}
    prior_fy = f"{year - 1}-12-31"
    prior_same = f"{year - 1}{latest[4:]}"

    fy_fact = by_period.get(prior_fy)
    same_fact = by_period.get(prior_same)
    fy_val = _value(fy_fact) if fy_fact is not None else None
    same_val = _value(same_fact) if same_fact is not None else None
    missing = [p for p, v in ((prior_fy, fy_val), (prior_same, same_val)) if v is None]
    if missing:
        return None, {
            "reason_zh": (
                "累计差分三元组不完整（缺 " + "、".join(missing) + " 期），"
                "宁缺勿假不做年化外推"
            ),
        }
    return fy_val + cur - same_val, {
        "method": "cumulative_diff",
        "periods": [prior_fy, latest, prior_same],
        "formula": f"FY_prev({prior_fy}) + YTD_cur({latest}) − YTD_prev_same({prior_same})",
    }


# ---------------------------------------------------------------------------
# 主入口（orchestrator seam：_default_ttm_snapshot 按此函数名发现）
# ---------------------------------------------------------------------------

def ttm_snapshot(store: Any, entity_id: str) -> TTMSnapshot:
    """对一个实体计算全部流量概念的 TTM 快照。**永不 raise**。

    Parameters
    ----------
    store: :class:`aegis.pit.PITStore`（duck-typed：只需 ``get_facts``）。
    entity_id: PIT 库实体 id（A 股 6 位裸代码）。
    """
    values: dict[str, float | None] = {}
    basis: dict[str, dict[str, Any]] = {}
    latest_period: str | None = None
    for meta_key, concept in TTM_FLOW_CONCEPTS.items():
        try:
            by_period = _latest_per_period(store, entity_id, concept)
            ttm, why = _ttm_for_concept(by_period)
        except Exception as e:  # noqa: BLE001 — 单概念失败不打断快照
            logger.warning(
                f"ttm_engine: {concept} TTM failed for {entity_id}: "
                f"{type(e).__name__}: {e}"
            )
            ttm, why = None, {"reason_zh": f"计算异常（{type(e).__name__}）"}
            by_period = {}
        values[meta_key] = ttm
        basis[meta_key] = why
        if by_period:
            newest = max(by_period)
            if latest_period is None or newest > latest_period:
                latest_period = newest

    snap = TTMSnapshot(
        entity_id=str(entity_id),
        latest_period=latest_period,
        ttm_revenue=values.get("ttm_revenue"),
        ttm_net_income=values.get("ttm_net_income"),
        ttm_net_income_deducted=values.get("ttm_net_income_deducted"),
        basis=basis,
    )
    ok = [k for k, v in values.items() if v is not None]
    logger.info(
        f"ttm_engine: {entity_id} snapshot latest_period={latest_period} "
        f"computed={ok or 'none'}"
    )
    return snap
