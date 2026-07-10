"""验证点核验器 v1 — 封闭目录数据规则检查（Aegis 2.0 Phase 1 任务 D2）.

DESIGN_2.0 §三.C（monitorables 封闭目录制）在 Phase 1 的最小落地：
Phase 0 报告的「市场在定价什么」区块里，验证点清单全部标「未核验」——
本模块补上核验能力。**红线：LLM 不参与，纯数据规则**；每个检查器输出
{检查项, 状态: 通过/未通过/数据不足, 依据数值, 中文一句话}。

检查器封闭目录（v1，共 6 个型号）：

===========================  =====================================
check_id                     规则（阈值写死在常量区，可审计）
===========================  =====================================
receivables_vs_revenue       应收增速 − 营收增速 缺口 > 20pp → 未通过
inventory_vs_revenue         存货增速 − 营收增速 缺口 > 25pp → 未通过
cfo_to_net_income            CFO/归母净利 < 0.5（CFO_NI_FLOOR 同口径）→ 未通过
deducted_to_attributable     扣非/归母 < 0.5（利润主要来自非经常性损益）→ 未通过
leverage_trend               资产负债率同比上升 > 5pp → 未通过
forecast_vs_consensus        业绩预告中值 vs 一致预期缺口 > 20% → 未通过
===========================  =====================================

输入与降级语义：

- **PIT store**（:class:`aegis.pit.PITStore`，duck-typed）：季报累计值来源。
  增速类检查用「累计同比」（本期累计 vs 上年同期累计），天然规避 A 股
  年初累计披露口径的季节性（红线 #4 的同族纪律：不对时点科目做流量差分）。
  concept 不在库（如 accounts_receivable / inventory / total_liabilities，
  quarterly_cn v1 尚未摄取）→ 该检查如实输出「数据不足」，摄取端补齐概念后
  自动点亮，本模块零改动。
- **em_events 结果**（RecentEvents dataclass 或其 asdict，均接受）：
  预告 vs 一致预期检查的输入。一致预期覆盖 gate（红线 5）不过 → 该项
  「数据不足」，禁止引用残缺一致预期数字。
- 任何检查内部异常 → 「数据不足」并附异常摘要，:func:`run_verification`
  永不 raise。

下游对接：orchestrator 把结果写 ``meta_facts["__verification"]``；
渲染层用 :func:`annotate_verification_focus` 把 pricing_regime 的
verification_focus 清单标注为「已核验·通过/未通过」+依据（凡有数据），
无数据维持「未核验」/「数据不足」。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "VerificationResult",
    "run_verification",
    "annotate_verification_focus",
    "CHECK_NAMES_ZH",
]

# ── 阈值常量（可审计；改动必须配回归测试）────────────────────────────
#: 应收增速超出营收增速的容忍缺口（20 个百分点）。
RECEIVABLES_GAP_MAX = 0.20
#: 存货增速超出营收增速的容忍缺口（25pp——备货可以是策略性的，略宽）。
INVENTORY_GAP_MAX = 0.25
#: CFO/归母净利的红旗线（与 pricing_regime / accounting_analyst 的
#: CFO_NI_FLOOR 同口径）。
CFO_NI_FLOOR = 0.5
#: 扣非/归母比红旗线：低于此值 = 归母利润主要由非经常性损益贡献
#: （康达基准：归母 1.25 亿 vs 扣非 1672 万 → 0.13，必须命中）。
DEDUCTED_RATIO_FLOOR = 0.5
#: 资产负债率同比上升的容忍幅度（5 个百分点）。
LEVERAGE_RISE_MAX = 0.05
#: 业绩预告中值 vs 一致预期的容忍相对缺口（±20%）。
FORECAST_CONSENSUS_GAP_MAX = 0.20

#: 检查项中文名（封闭目录——新增型号必须同步补进此表与 run_verification）。
CHECK_NAMES_ZH: dict[str, str] = {
    "receivables_vs_revenue": "应收增速 vs 营收增速",
    "inventory_vs_revenue": "存货增速 vs 营收增速",
    "cfo_to_net_income": "经营现金流 / 归母净利润",
    "deducted_to_attributable": "扣非 / 归母净利润比",
    "leverage_trend": "资产负债率趋势",
    "forecast_vs_consensus": "业绩预告 vs 一致预期",
}

_STATUS_ZH = {"pass": "通过", "fail": "未通过", "insufficient": "数据不足"}


@dataclass(frozen=True)
class VerificationResult:
    """一个检查器的核验结果（全部字段 JSON 可序列化）。"""

    check_id: str
    name_zh: str
    status: str                 # "pass" | "fail" | "insufficient"
    detail_zh: str              # 中文一句话（含依据数值的人读形式）
    evidence: dict[str, Any] = field(default_factory=dict)  # 依据数值（机器可读）

    @property
    def status_zh(self) -> str:
        return _STATUS_ZH.get(self.status, self.status)

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "name_zh": self.name_zh,
            "status": self.status,
            "status_zh": self.status_zh,
            "detail_zh": self.detail_zh,
            "evidence": dict(self.evidence),
        }


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _get(obj: Any, key: str, default: Any = None) -> Any:
    """dict 与 dataclass/对象双形态取值（em_events 结果可能是任一形态）。"""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _fmt_cny(value: float) -> str:
    """CNY 金额 → 亿/万 人读单位（与 em_events/渲染层口径一致）。"""
    a = abs(value)
    if a >= 1e8:
        return f"{value / 1e8:.2f}亿元"
    if a >= 1e4:
        return f"{value / 1e4:.0f}万元"
    return f"{value:.2f}元"


def _prior_year_period(period: str) -> str:
    """``"2026-03-31"`` → ``"2025-03-31"``（A 股报告期末月-日逐年不变）。"""
    return f"{int(period[:4]) - 1}{period[4:]}"


def _latest_per_period(store: Any, entity_id: str, concept: str) -> dict[str, Any]:
    """concept 的 {period: 该期最新事实}。

    同 period 多版本/多 source 时套用 :meth:`PITStore.latest_value` 的
    选取纪律：审计值优先（正式报告自然取代快报），其余按
    (as_of, fact_version, id) 取最新（重述链上永远最新版本）。
    store 为 None / 查询失败 → 空 dict（调用方输出「数据不足」）。
    """
    if store is None:
        return {}
    try:
        facts = store.get_facts(entity_id, concept)
    except Exception as e:  # noqa: BLE001 — 只读端失败不打断核验
        logger.debug(f"verification: get_facts({concept}) failed: {e}")
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
    v = getattr(fact, "value", None)
    return float(v) if isinstance(v, (int, float)) else None


def _insufficient(check_id: str, reason_zh: str) -> VerificationResult:
    return VerificationResult(
        check_id=check_id,
        name_zh=CHECK_NAMES_ZH[check_id],
        status="insufficient",
        detail_zh=reason_zh,
    )


# ---------------------------------------------------------------------------
# 检查器（纯数据规则，LLM 不参与）
# ---------------------------------------------------------------------------

def _check_growth_gap(
    check_id: str,
    numerator_series: dict[str, Any],
    revenue_series: dict[str, Any],
    *,
    subject_zh: str,
    gap_max: float,
) -> VerificationResult:
    """「X 增速 vs 营收增速」型检查：累计同比，同一报告期成对比较。"""
    if not numerator_series:
        return _insufficient(check_id, f"PIT 库无{subject_zh}数据（摄取端未覆盖该科目）")
    if not revenue_series:
        return _insufficient(check_id, "PIT 库无营收数据")
    for period in sorted(set(numerator_series) & set(revenue_series), reverse=True):
        prior_p = _prior_year_period(period)
        vals = (
            _value(numerator_series.get(period)),
            _value(numerator_series.get(prior_p)),
            _value(revenue_series.get(period)),
            _value(revenue_series.get(prior_p)),
        )
        if any(v is None for v in vals):
            continue
        num_cur, num_prior, rev_cur, rev_prior = vals  # type: ignore[misc]
        if num_prior <= 0 or rev_prior <= 0:
            continue  # 基数非正 → 增速口径失效，找更早的成对期
        num_g = (num_cur - num_prior) / num_prior
        rev_g = (rev_cur - rev_prior) / rev_prior
        gap = num_g - rev_g
        evidence = {
            "period": period,
            f"{check_id.split('_')[0]}_growth": round(num_g, 4),
            "revenue_growth": round(rev_g, 4),
            "gap_pp": round(gap * 100, 1),
        }
        if gap > gap_max:
            return VerificationResult(
                check_id=check_id,
                name_zh=CHECK_NAMES_ZH[check_id],
                status="fail",
                detail_zh=(
                    f"{period} 期{subject_zh}同比 {num_g:+.1%}，显著快于营收同比 "
                    f"{rev_g:+.1%}（缺口 {gap * 100:.0f}pp，超 {gap_max * 100:.0f}pp 阈值）"
                ),
                evidence=evidence,
            )
        return VerificationResult(
            check_id=check_id,
            name_zh=CHECK_NAMES_ZH[check_id],
            status="pass",
            detail_zh=(
                f"{period} 期{subject_zh}同比 {num_g:+.1%} vs 营收同比 {rev_g:+.1%}"
                f"（缺口 {gap * 100:.0f}pp，阈值内）"
            ),
            evidence=evidence,
        )
    return _insufficient(check_id, f"{subject_zh}与营收缺少可成对的本期/上年同期数据")


def _check_cfo_to_ni(
    cfo_series: dict[str, Any], ni_series: dict[str, Any],
) -> VerificationResult:
    """CFO/归母净利比。只用 H1/Q3/FY 累计期——Q1 仅 3 个月，经营现金流
    季节性噪声过大，据其判红旗会产生假阳性。"""
    check_id = "cfo_to_net_income"
    if not cfo_series or not ni_series:
        return _insufficient(check_id, "PIT 库缺经营现金流或归母净利润数据")
    common = sorted(set(cfo_series) & set(ni_series), reverse=True)
    usable = [
        p for p in common
        if getattr(ni_series[p], "fiscal_period", None) != "Q1"
    ]
    if not usable:
        return _insufficient(
            check_id, "仅有一季度累计数据，CFO/净利比口径噪声过大，不予判定")
    period = usable[0]
    cfo, ni = _value(cfo_series[period]), _value(ni_series[period])
    if cfo is None or ni is None:
        return _insufficient(check_id, f"{period} 期 CFO 或归母净利润数值缺失")
    if ni <= 0:
        return _insufficient(
            check_id,
            f"{period} 期归母净利润为 {_fmt_cny(ni)}（非正），比率口径失效",
        )
    ratio = cfo / ni
    evidence = {"period": period, "cfo": cfo, "net_income": ni,
                "ratio": round(ratio, 2)}
    if ratio < CFO_NI_FLOOR:
        return VerificationResult(
            check_id=check_id, name_zh=CHECK_NAMES_ZH[check_id], status="fail",
            detail_zh=(
                f"{period} 期经营现金流 {_fmt_cny(cfo)} / 归母净利润 "
                f"{_fmt_cny(ni)} = {ratio:.2f}，低于 {CFO_NI_FLOOR} 红旗线，"
                "账面利润未获现金流支撑"
            ),
            evidence=evidence,
        )
    return VerificationResult(
        check_id=check_id, name_zh=CHECK_NAMES_ZH[check_id], status="pass",
        detail_zh=(
            f"{period} 期经营现金流 {_fmt_cny(cfo)} / 归母净利润 "
            f"{_fmt_cny(ni)} = {ratio:.2f}，现金流对利润的覆盖正常"
        ),
        evidence=evidence,
    )


def _check_deducted_ratio(
    ni_series: dict[str, Any], deducted_series: dict[str, Any],
) -> VerificationResult:
    """扣非/归母比（A 股双轨口径，红线 #4）。"""
    check_id = "deducted_to_attributable"
    if not ni_series or not deducted_series:
        return _insufficient(check_id, "PIT 库缺归母或扣非归母净利润数据")
    common = sorted(set(ni_series) & set(deducted_series), reverse=True)
    if not common:
        return _insufficient(check_id, "归母与扣非缺少同报告期数据")
    period = common[0]
    ni, ded = _value(ni_series[period]), _value(deducted_series[period])
    if ni is None or ded is None:
        return _insufficient(check_id, f"{period} 期归母或扣非数值缺失")
    if ni <= 0:
        return _insufficient(
            check_id,
            f"{period} 期归母净利润为 {_fmt_cny(ni)}（非正），比率口径失效",
        )
    ratio = ded / ni
    evidence = {"period": period, "net_income": ni,
                "net_income_deducted": ded, "ratio": round(ratio, 2)}
    if ratio < DEDUCTED_RATIO_FLOOR:
        return VerificationResult(
            check_id=check_id, name_zh=CHECK_NAMES_ZH[check_id], status="fail",
            detail_zh=(
                f"{period} 期扣非归母 {_fmt_cny(ded)} 仅为归母净利润 "
                f"{_fmt_cny(ni)} 的 {ratio:.0%}，利润主要由非经常性损益贡献"
            ),
            evidence=evidence,
        )
    return VerificationResult(
        check_id=check_id, name_zh=CHECK_NAMES_ZH[check_id], status="pass",
        detail_zh=(
            f"{period} 期扣非/归母 = {ratio:.0%}"
            f"（扣非 {_fmt_cny(ded)} vs 归母 {_fmt_cny(ni)}），主业利润成色正常"
        ),
        evidence=evidence,
    )


def _check_leverage_trend(
    liabilities_series: dict[str, Any], assets_series: dict[str, Any],
) -> VerificationResult:
    """资产负债率同比趋势（时点科目取时点值，红线 #4：禁做流量差分）。"""
    check_id = "leverage_trend"
    if not liabilities_series:
        return _insufficient(
            check_id, "PIT 库无负债合计数据（摄取端未覆盖该科目）")
    if not assets_series:
        return _insufficient(check_id, "PIT 库无资产总计数据")
    ratios: dict[str, float] = {}
    for period in set(liabilities_series) & set(assets_series):
        tl, ta = _value(liabilities_series[period]), _value(assets_series[period])
        if tl is not None and ta is not None and ta > 0:
            ratios[period] = tl / ta
    for period in sorted(ratios, reverse=True):
        prior_p = _prior_year_period(period)
        if prior_p not in ratios:
            continue
        cur, prior = ratios[period], ratios[prior_p]
        delta = cur - prior
        evidence = {"period": period, "ratio": round(cur, 4),
                    "prior_ratio": round(prior, 4),
                    "delta_pp": round(delta * 100, 1)}
        if delta > LEVERAGE_RISE_MAX:
            return VerificationResult(
                check_id=check_id, name_zh=CHECK_NAMES_ZH[check_id],
                status="fail",
                detail_zh=(
                    f"{period} 期资产负债率 {cur:.1%}，较上年同期 {prior:.1%} "
                    f"上升 {delta * 100:.1f}pp（超 {LEVERAGE_RISE_MAX * 100:.0f}pp 阈值）"
                ),
                evidence=evidence,
            )
        return VerificationResult(
            check_id=check_id, name_zh=CHECK_NAMES_ZH[check_id], status="pass",
            detail_zh=(
                f"{period} 期资产负债率 {cur:.1%}，较上年同期 {prior:.1%} "
                f"变动 {delta * 100:+.1f}pp，趋势平稳"
            ),
            evidence=evidence,
        )
    return _insufficient(check_id, "资产负债率缺少本期/上年同期成对数据")


def _check_forecast_vs_consensus(recent_events: Any) -> VerificationResult:
    """业绩预告中值 vs 一致预期归母净利润缺口。

    红线 5：一致预期覆盖 gate（insufficient_coverage）不过 → 数据不足，
    禁止引用残缺一致预期数字。
    """
    check_id = "forecast_vs_consensus"
    if recent_events is None:
        return _insufficient(check_id, "近事件切片不可用（数据源获取失败或未启用）")
    consensus = _get(recent_events, "consensus")
    if consensus is None:
        return _insufficient(check_id, "一致预期数据不可用")
    if _get(consensus, "insufficient_coverage", True):
        org_count = _get(consensus, "org_count", 0)
        return _insufficient(
            check_id,
            f"无有效一致预期（近6个月覆盖机构 {org_count} 家，未达使用门槛），"
            "缺口无法核验",
        )
    forecasts = _get(recent_events, "forecasts") or []
    # 归母净利润口径的预告行（排除扣非/每股收益等指标，与一致预期
    # net_profit 的归母口径对齐）。
    candidates = []
    for f in forecasts:
        indicator = str(_get(f, "indicator", "") or "")
        if "净利润" not in indicator or "扣除" in indicator:
            continue
        lo, hi = _get(f, "value_low"), _get(f, "value_high")
        bounds = [v for v in (lo, hi) if isinstance(v, (int, float))]
        if not bounds:
            continue
        candidates.append((f, sum(bounds) / len(bounds)))
    if not candidates:
        return _insufficient(check_id, "无归母净利润口径的业绩预告（或未披露预告区间）")
    fc, mid = candidates[0]
    period = str(_get(fc, "report_period", "") or "")
    try:
        fc_year = int(period[:4])
    except ValueError:
        return _insufficient(check_id, f"业绩预告报告期无法解析: {period!r}")
    cons_np = None
    for p in (_get(consensus, "predictions") or []):
        np_ = _get(p, "net_profit")
        if _get(p, "year") == fc_year and isinstance(np_, (int, float)):
            cons_np = float(np_)
            break
    if cons_np is None or cons_np == 0:
        return _insufficient(
            check_id, f"一致预期无 {fc_year} 年度归母净利润预测，缺口无法核验")
    gap = (mid - cons_np) / abs(cons_np)
    evidence = {"year": fc_year, "forecast_mid": mid,
                "consensus_net_profit": cons_np, "gap_pct": round(gap * 100, 1)}
    if abs(gap) > FORECAST_CONSENSUS_GAP_MAX:
        direction = "低于" if gap < 0 else "高于"
        return VerificationResult(
            check_id=check_id, name_zh=CHECK_NAMES_ZH[check_id], status="fail",
            detail_zh=(
                f"{fc_year} 年业绩预告中值 {_fmt_cny(mid)} {direction}一致预期 "
                f"{_fmt_cny(cons_np)} 达 {abs(gap):.0%}"
                f"（超 {FORECAST_CONSENSUS_GAP_MAX:.0%} 阈值），预期存在显著缺口"
            ),
            evidence=evidence,
        )
    return VerificationResult(
        check_id=check_id, name_zh=CHECK_NAMES_ZH[check_id], status="pass",
        detail_zh=(
            f"{fc_year} 年业绩预告中值 {_fmt_cny(mid)} 与一致预期 "
            f"{_fmt_cny(cons_np)} 缺口 {gap:+.0%}，在阈值内"
        ),
        evidence=evidence,
    )


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def run_verification(
    *,
    store: Any = None,
    entity_id: str = "",
    recent_events: Any = None,
) -> list[VerificationResult]:
    """跑完封闭目录全部 6 个检查器。**永不 raise**。

    Parameters
    ----------
    store: :class:`aegis.pit.PITStore`（duck-typed）；None → PIT 类检查全部
        「数据不足」。
    entity_id: PIT 库实体 id（A 股 6 位裸代码）。
    recent_events: em_events 的 :class:`RecentEvents` 或其 asdict。
    """
    def series(concept: str) -> dict[str, Any]:
        return _latest_per_period(store, entity_id, concept)

    rev = series("revenue")
    checks: list[tuple[str, Any]] = [
        ("receivables_vs_revenue", lambda: _check_growth_gap(
            "receivables_vs_revenue",
            series("accounts_receivable") or series("notes_and_accounts_receivable"),
            rev, subject_zh="应收账款", gap_max=RECEIVABLES_GAP_MAX)),
        ("inventory_vs_revenue", lambda: _check_growth_gap(
            "inventory_vs_revenue", series("inventory"), rev,
            subject_zh="存货", gap_max=INVENTORY_GAP_MAX)),
        ("cfo_to_net_income", lambda: _check_cfo_to_ni(
            series("cfo"), series("net_income"))),
        ("deducted_to_attributable", lambda: _check_deducted_ratio(
            series("net_income"), series("net_income_deducted"))),
        ("leverage_trend", lambda: _check_leverage_trend(
            series("total_liabilities"), series("total_assets"))),
        ("forecast_vs_consensus",
         lambda: _check_forecast_vs_consensus(recent_events)),
    ]
    results: list[VerificationResult] = []
    for check_id, fn in checks:
        try:
            results.append(fn())
        except Exception as e:  # noqa: BLE001 — 单检查失败不打断目录
            logger.warning(
                f"verification: {check_id} crashed: {type(e).__name__}: {e}")
            results.append(_insufficient(
                check_id, f"核验过程异常（{type(e).__name__}），按数据不足处理"))
    return results


# ---------------------------------------------------------------------------
# 验证点清单标注（对接 pricing_regime.verification_focus）
# ---------------------------------------------------------------------------

#: 验证点文案关键词 → 检查器型号（一个验证点可对应多个检查器）。
_FOCUS_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("应收", "receivables_vs_revenue"),
    ("存货", "inventory_vs_revenue"),
    ("CFO/净利", "cfo_to_net_income"),
    ("盈利质量", "cfo_to_net_income"),
    ("盈利质量", "deducted_to_attributable"),
    ("现金消耗", "cfo_to_net_income"),
    ("自由现金流", "cfo_to_net_income"),
    ("债务", "leverage_trend"),
    ("流动性压力", "leverage_trend"),
    ("业绩预告", "forecast_vs_consensus"),
    ("预告", "forecast_vs_consensus"),
)

#: 渲染层显示状态（三态 + 无匹配检查器的「未核验」）。
_DISPLAY_ZH = {
    "pass": "已核验·通过",
    "fail": "已核验·未通过",
    "insufficient": "数据不足",
    "unverified": "未核验",
}


def annotate_verification_focus(
    focus_texts: list[str],
    results: list[Any],
) -> list[dict[str, Any]]:
    """把 pricing_regime 的验证点清单标注为核验状态 + 依据。

    Parameters
    ----------
    focus_texts: RegimeAssessment.verification_focus（中文文案清单）。
    results: :func:`run_verification` 结果（VerificationResult 或 to_dict 后
        的 dict，均接受——replay 路径从 meta_facts 读到的是 dict）。

    Returns
    -------
    ``[{"text", "status", "state", "evidence"}]``：
    - 命中检查器且任一「未通过」→ 已核验·未通过；
    - 否则任一「通过」→ 已核验·通过；
    - 命中但全部数据不足 → 数据不足；
    - 未命中任何检查器 → 未核验（Phase 0 语义保留）。
    有数据的落单检查器（未被任何验证点文案吸收）追加为独立条目，
    核验信息不丢失。
    """
    norm: list[dict[str, Any]] = []
    for r in results or []:
        d = r.to_dict() if hasattr(r, "to_dict") else (r if isinstance(r, dict) else None)
        if d and d.get("check_id"):
            norm.append(d)
    by_id = {d["check_id"]: d for d in norm}

    out: list[dict[str, Any]] = []
    matched_ids: set[str] = set()
    for text in focus_texts or []:
        hit_ids: list[str] = []
        for kw, check_id in _FOCUS_KEYWORDS:
            if kw in text and check_id in by_id and check_id not in hit_ids:
                hit_ids.append(check_id)
        matched_ids.update(hit_ids)
        hits = [by_id[i] for i in hit_ids]
        determinate = [h for h in hits if h.get("status") in ("pass", "fail")]
        if not hits:
            state = "unverified"
        elif any(h.get("status") == "fail" for h in determinate):
            state = "fail"
        elif determinate:
            state = "pass"
        else:
            state = "insufficient"
        out.append({
            "text": str(text),
            "state": state,
            "status": _DISPLAY_ZH[state],
            "evidence": "；".join(
                str(h.get("detail_zh", "")) for h in determinate if h.get("detail_zh")
            ),
        })

    # 落单但有数据的检查器 → 追加独立条目（核验事实不因文案措辞丢失）。
    for d in norm:
        if d["check_id"] in matched_ids or d.get("status") not in ("pass", "fail"):
            continue
        state = d["status"]
        out.append({
            "text": str(d.get("name_zh", d["check_id"])),
            "state": state,
            "status": _DISPLAY_ZH[state],
            "evidence": str(d.get("detail_zh", "")),
        })
    return out
