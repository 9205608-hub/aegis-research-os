"""Golden-master 挽具 — 拆单体（红线 #7）的前置闸门。

从 .cache/ 下的 replay pkl 出发，沿 scripts/replay_from_cache.py 已验证的
缓存缝重建 report dict（backfill → DCF 重算 → PublishGate → DecisionEngine
→ PortfolioIntegration → build_report_dict），规范化后与 tests/golden/ 下
的基线 JSON 逐路径比对。任何一步单体手术后 check 必须零 diff 才许下一步。

用法:
    python scripts/golden_master.py record            # 重建全部快照并写基线
    python scripts/golden_master.py check             # 与基线比对，非空 diff 退出码 1
    python scripts/golden_master.py record --only smoke_nvda
    python scripts/golden_master.py check --only 002669

确定性保障:
- 设 AEGIS_SKIP_SPARKLINE=1，屏蔽渲染层的行情/走势网络抓取；
- record 模式对同一 pkl 连续重建两次，规范化后必须逐字节一致才落盘
  （否则打印两次运行的 diff 并退出 1——用于揪出漏网的非确定字段）；
- NORMALIZE_RULES 固化已知的渲染时点重算字段（时间戳、时效天数等）。

已知漂移面（不在规范化范围内，长期跨月运行 check 时注意）:
- report["catalysts"] 按「今天」过滤过期事件——若基线里存在未来日期的
  催化剂，日期越过后 check 会报 removed。手术期 record/check 同日进行
  不受影响；跨月复用请先重新 record。
- staleBanner 的 gap_months 按月漂移，已整体规范化为存在性占位符。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import pickle
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# 渲染层离线开关：_fetch_quote_meta / _fetch_sparkline 走单点兜底，
# 不发任何网络请求（html_report_v2.py 内建的 offline switch）。
os.environ["AEGIS_SKIP_SPARKLINE"] = "1"

GOLDEN_DIR = PROJECT_ROOT / "tests" / "golden"

# 快照登记表：name → replay pkl 相对路径
SNAPSHOTS: dict[str, str] = {
    "002669": ".cache/002669_replay_state.pkl",
    "smoke_002669": ".cache/smoke/002669_replay_state.pkl",
    "smoke_600519": ".cache/smoke/600519_replay_state.pkl",
    "smoke_nvda": ".cache/smoke/nvda_replay_state.pkl",
}

# ── 规范化规则 ──
# 对同一 pkl 连跑两次 + 逐行审读 build_report_dict 的 datetime.now()/
# date.today() 调用点（html_report_v2.py:1309/1936/2076/2144）后固化。
# 每条 = (report dict 内的点路径, 说明)。命中且值非 None 时替换为占位符。
NORMALIZED_PLACEHOLDER = "<NORMALIZED>"
NORMALIZE_RULES: tuple[tuple[str, str], ...] = (
    ("reportDate", "渲染时刻 datetime.now() 时间戳"),
    ("price.asOf", "同上——价格区块复用同一 now 字符串"),
    ("runId", "管线 run id（record/check 环境可能不同）"),
    ("pipelineDuration", "管线墙钟耗时"),
    ("dataAsOf.days", "数据时效天数——按渲染时点 date.today() 重算"),
    ("dataAsOf.line", "整行文案内嵌时效天数，随 days 一起固化"),
    ("staleBanner", "时效警告 gap_months 按渲染月份重算"),
)


class GoldenRebuildError(RuntimeError):
    """pkl 缺关键字段等导致无法重建时抛出——调用方按「跳过该快照」处理。"""


# ─────────────────────────────────────────────────────────────────
# 重建路径（镜像 scripts/replay_from_cache.py，行为保持一致；
# 该脚本是只读参考，不允许改动，故此处平移其逻辑而非 import）
# ─────────────────────────────────────────────────────────────────

def _apply_backfills(state: dict) -> None:
    """平移 replay_from_cache.py 的全部缓存 backfill（对旧 pkl 容错）。"""
    # pe_ratio_ttm backfill
    cm = state.get("computed_metrics", {}) or {}
    if "pe_ratio_ttm" not in cm:
        hv = state.get("historical_valuation") or {}
        ttm = (hv.get("pe_stats") or {}).get("current") if isinstance(hv, dict) else None
        if ttm and ttm > 0:
            cm["pe_ratio_ttm"] = ttm
            state["computed_metrics"] = cm

    # BUG-23：pe_stats["current"] 从序列末位重导出
    hv = state.get("historical_valuation") or {}
    if isinstance(hv, dict):
        for stats_key, series_key in [("pe_stats", "pe_ratio"), ("ev_ebitda_stats", "ev_ebitda")]:
            stats = hv.get(stats_key)
            series = hv.get(series_key, [])
            if stats and series:
                latest = 0.0
                for v in reversed(series):
                    if v and float(v) > 0 and float(v) < 500:
                        latest = float(v)
                        break
                if latest > 0 and abs(latest - stats.get("current", 0)) > 0.5:
                    stats["current"] = round(latest, 1)
                    hv[stats_key] = stats
        state["historical_valuation"] = hv

    # D&A 合成字段
    mf = state.get("meta_facts") or {}
    if mf and not mf.get("depreciation_amortization"):
        dep = mf.get("depreciation") or 0
        amort = mf.get("amortization") or 0
        if dep or amort:
            mf["depreciation_amortization"] = dep + amort
            state["meta_facts"] = mf

    # __display block backfill
    mf_pre = state.get("meta_facts") or {}
    if isinstance(mf_pre, dict) and "__display" not in mf_pre and mf_pre.get("__currency"):
        _curr = mf_pre.get("__currency")
        _display_table = {
            "CNY": {"symbol": "¥", "scale": 1e8, "unit": "亿",
                    "big_scale": 1e12, "big_unit": "万亿"},
            "USD": {"symbol": "$", "scale": 1e9, "unit": "B",
                    "big_scale": 1e12, "big_unit": "T"},
            "EUR": {"symbol": "€", "scale": 1e9, "unit": "B",
                    "big_scale": 1e12, "big_unit": "T"},
            "GBP": {"symbol": "£", "scale": 1e9, "unit": "B",
                    "big_scale": 1e12, "big_unit": "T"},
            "JPY": {"symbol": "¥", "scale": 1e8, "unit": "億",
                    "big_scale": 1e12, "big_unit": "兆"},
        }
        mf_pre["__display"] = dict(_display_table.get(_curr, _display_table["USD"]))
        mf_pre["__display"]["currency"] = _curr
        state["meta_facts"] = mf_pre

    # 负 EBITDA / 负盈利倍数剔除
    cm = state.get("computed_metrics", {}) or {}
    mf = state.get("meta_facts", {}) or {}
    if cm and mf:
        _ebitda = mf.get("ebitda")
        if _ebitda is None or _ebitda <= 0:
            for k in ("ev_to_ebitda", "net_debt_to_ebitda"):
                cm.pop(k, None)
        if cm.get("pe_ratio", 0) and cm["pe_ratio"] < 0:
            cm.pop("pe_ratio", None)
        if cm.get("pe_ratio_ttm", 0) and cm["pe_ratio_ttm"] < 0:
            cm.pop("pe_ratio_ttm", None)
        state["computed_metrics"] = cm

    # BUG-A6：synthesized_thesis 为 None 时的 Director 兜底
    if state.get("synthesized_thesis") is None and state.get("research_directive") is not None:
        try:
            from aegis.core.chief_analyst.thesis_synthesizer import SynthesizedThesis
            _dir = state["research_directive"]
            _opening = getattr(_dir, "opening_angle", "") or ""
            _hyp = getattr(_dir, "initial_hypothesis", "") or ""
            _consensus = getattr(_dir, "what_consensus_likely_believes", "") or ""
            _why_now = getattr(_dir, "why_now", "") or ""
            _key_vars = getattr(_dir, "key_variables", []) or []
            _controversy = getattr(_dir, "key_controversy", "") or ""
            _variant_text = ""
            _counter_text = ""
            _all_judgments = state.get("all_judgments") or {}
            for _name in ("variant_analyst", "risk_analyst", "valuation_analyst"):
                _j = _all_judgments.get(_name) if isinstance(_all_judgments, dict) else None
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
            state["synthesized_thesis"] = SynthesizedThesis(
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
                conviction_narrative="（缓存中 Synthesizer 缺失，由 Director 摘要兜底）",
                hypothesis_validated=False,
                hypothesis_evolution="原 cache 无 SynthesizedThesis（Synthesizer 当时调用失败）",
            )
        except Exception:
            pass

    # BUG-28：FCF = OCF - abs(capex)
    mf = state.get("meta_facts", {}) or {}
    if mf:
        ocf = mf.get("operating_cash_flow")
        cap = mf.get("capex") or mf.get("capital_expenditures")
        if ocf is not None and cap is not None:
            correct_fcf = ocf - abs(cap)
            old_fcf = mf.get("free_cash_flow")
            if old_fcf is not None and abs(correct_fcf - old_fcf) > abs(old_fcf) * 0.05:
                mf["free_cash_flow"] = correct_fcf
                state["meta_facts"] = mf

    # 数据质量告警 backfill
    if mf and "__data_quality_issues" not in mf:
        try:
            from aegis.core.acquisition.fact_bridge import _run_data_quality_checks
            issues = _run_data_quality_checks(mf)
            if issues:
                mf["__data_quality_issues"] = issues
                state["meta_facts"] = mf
        except Exception:
            pass

    # BUG-25：历史 EV/EBITDA 序列按 computed 值重标定
    hv = state.get("historical_valuation") or {}
    ev_series = hv.get("ev_ebitda", []) if isinstance(hv, dict) else []
    computed_ev = (state.get("computed_metrics") or {}).get("ev_to_ebitda")
    if ev_series and computed_ev and computed_ev > 0:
        last_ev = 0
        for v in reversed(ev_series):
            if v and v > 0:
                last_ev = float(v)
                break
        if last_ev > 0 and abs(last_ev - computed_ev) / computed_ev > 0.50:
            scale = computed_ev / last_ev
            hv["ev_ebitda"] = [round(float(v) * scale, 1) if v and v > 0 else v
                               for v in ev_series]
            clean = sorted([v for v in hv["ev_ebitda"] if v and v > 0 and v < 500])
            if len(clean) >= 3:
                import statistics
                latest_rescaled = 0
                for v in reversed(hv["ev_ebitda"]):
                    if v and v > 0:
                        latest_rescaled = v
                        break
                hv["ev_ebitda_stats"] = {
                    "min": round(min(clean), 1), "max": round(max(clean), 1),
                    "median": round(statistics.median(clean), 1),
                    "mean": round(statistics.mean(clean), 1),
                    "p25": round(clean[len(clean) // 4], 1),
                    "p75": round(clean[3 * len(clean) // 4], 1),
                    "current": round(latest_rescaled, 1),
                }
            state["historical_valuation"] = hv


def _recompute_dcf(state: dict) -> None:
    """平移 replay 的 DCF 重算块（引擎修复后按缓存 input 重新出数）。"""
    try:
        from aegis.core.truth.scenario_engine.dcf_engine import (
            DCFEngine, DCFInput, ConsolidatedDCFOutput,
        )
        from aegis.core.truth.scenario_engine.sensitivity_analyzer import SensitivityAnalyzer

        dcf_input_flat = state.get("dcf_input_flat")
        dcf_output_cached = state.get("dcf_output")
        if dcf_input_flat is None or dcf_output_cached is None:
            return
        mf = state.get("meta_facts", {}) or {}

        if mf.get("depreciation_amortization") and dcf_input_flat.base_depreciation == 0:
            dcf_input_flat = DCFInput(**{
                **{k: getattr(dcf_input_flat, k) for k in dcf_input_flat.__dataclass_fields__},
                "base_depreciation": mf["depreciation_amortization"],
            })
            state["dcf_input_flat"] = dcf_input_flat
            state["_da_backfilled"] = True

        dcf = DCFEngine()
        is_segment_dcf = (isinstance(dcf_output_cached, ConsolidatedDCFOutput)
                          and not state.get("_da_backfilled", False))

        if is_segment_dcf:
            old_ps = dcf_output_cached.per_share_value
            old_equity = dcf_output_cached.equity_value
            new_ps = (old_equity / dcf_input_flat.shares_outstanding
                      if dcf_input_flat.shares_outstanding > 0 else old_ps)
            new_output = dcf_output_cached
        else:
            new_output = dcf.compute_dcf(dcf_input_flat)
            old_ps = dcf_output_cached.per_share_value
            new_ps = new_output.per_share_value

        if abs(new_ps - old_ps) / max(abs(old_ps), 1) > 0.01:
            if not is_segment_dcf:
                state["dcf_output"] = new_output
                raw_projs = new_output.projections
                state["dcf_projections"] = [
                    {"year": p.year, "revenue": p.revenue,
                     "operating_income": p.operating_income,
                     "nopat": p.nopat, "depreciation": p.depreciation,
                     "capex": p.capex, "sbc": p.sbc,
                     "change_in_nwc": p.change_in_nwc,
                     "fcff": p.fcff, "pv_fcff": p.pv_fcff,
                     "discount_factor": p.discount_factor}
                    for p in raw_projs
                ]
            scenarios = state.get("scenarios")
            if isinstance(scenarios, dict):
                scenarios["dcf_bridge"] = {
                    "pv_fcff_sum": new_output.pv_fcff_sum,
                    "pv_terminal_value": new_output.pv_terminal_value,
                    "enterprise_value": new_output.enterprise_value,
                    "net_debt": dcf_input_flat.net_debt,
                    "equity_value": new_output.equity_value,
                    "future_shares": new_output.future_shares,
                    "per_share_value": new_output.per_share_value,
                }
                ratio = 1
                if "base" in scenarios:
                    scenarios["base"]["per_share_value"] = new_ps
                if "base_value" in scenarios:
                    old_base = scenarios["base_value"]
                    scenarios["base_value"] = new_ps
                    if old_base and old_base > 0:
                        ratio = new_ps / old_base
                        for variant in ("bear", "bull"):
                            vk = f"{variant}_value"
                            if vk in scenarios and scenarios[vk]:
                                scenarios[vk] = scenarios[vk] * ratio

                scenarios["_old_pw"] = scenarios.get("probability_weighted_value")
                probs = state.get("scenario_probabilities") or {}
                bear_v = scenarios.get("bear_value") or (scenarios.get("bear", {}) or {}).get("per_share_value", 0)
                base_v = new_ps
                bull_v = scenarios.get("bull_value") or (scenarios.get("bull", {}) or {}).get("per_share_value", 0)
                if bear_v or bull_v:
                    pw = (probs.get("bear", 0.3) * bear_v
                          + probs.get("base", 0.45) * base_v
                          + probs.get("bull", 0.25) * bull_v)
                    scenarios["probability_weighted_value"] = pw
                state["scenarios"] = scenarios

            if not is_segment_dcf:
                sa = SensitivityAnalyzer()
                new_rankings = sa.rank_assumptions(dcf_input_flat)
                state["sensitivity_rankings"] = [
                    {"assumption": r.assumption, "impact_pct": r.impact_pct,
                     "signed_impact_pct": getattr(r, "signed_impact_pct", r.impact_pct),
                     "base_per_share": r.base_per_share,
                     "shocked_per_share": r.shocked_per_share}
                    for r in new_rankings
                ]
                try:
                    state["sensitivity_table"] = sa.two_way_table(
                        dcf_input_flat, "wacc", "terminal_growth_rate",
                    )
                except Exception:
                    pass
            else:
                share_ratio = new_ps / old_ps if old_ps else 1
                for r in state.get("sensitivity_rankings", []) or []:
                    r["base_per_share"] = r.get("base_per_share", 0) * share_ratio
                    r["shocked_per_share"] = r.get("shocked_per_share", 0) * share_ratio
                old_table = state.get("sensitivity_table")
                if isinstance(old_table, dict) and "matrix" in old_table:
                    old_table["matrix"] = [
                        [cell * share_ratio for cell in row]
                        for row in old_table["matrix"]
                    ]
    except Exception:
        # 与 replay 一致：DCF 重算失败不阻断，报告用缓存值
        pass


def rebuild_report(pkl_path: Path) -> dict:
    """从 replay pkl 重建 report dict（不渲染 HTML、不跑 Editor/LLM）。

    返回 {"summary": {...}, "report": {...}}，均为纯 JSON 结构。
    """
    if not pkl_path.exists():
        raise GoldenRebuildError(f"pkl 不存在: {pkl_path}")
    try:
        with pkl_path.open("rb") as f:
            state = pickle.load(f)
    except Exception as e:  # 旧代码序列化的 pkl 可能缺类定义
        raise GoldenRebuildError(f"pkl 反序列化失败: {pkl_path}: {e}") from e
    if not isinstance(state, dict):
        raise GoldenRebuildError(f"pkl 顶层不是 dict: {pkl_path}")

    for key in ("all_judgments", "critic_results", "scenarios"):
        if state.get(key) is None:
            raise GoldenRebuildError(f"pkl 缺关键字段 {key!r}: {pkl_path}")

    _apply_backfills(state)
    _recompute_dcf(state)

    # ── PublishGate ──
    from aegis.core.publish_gate import PublishGate
    gate_result = PublishGate().evaluate(
        state["all_judgments"],
        state["critic_results"],
        context={"run_manifest_id": state.get("run_id")},
    )

    # ── DecisionEngine ──
    from aegis.core.decision_engine import DecisionEngine
    from aegis.data_contracts.edge_assessment_schema import EdgeAssessment

    edge = None
    edge_dict = state.get("edge_assessment_dict")
    if edge_dict:
        try:
            edge = EdgeAssessment.model_validate(edge_dict)
        except Exception:
            try:
                sanitized = {k: str(v) if v is not None else v
                             for k, v in edge_dict.items()}
                edge = EdgeAssessment.model_validate(sanitized)
            except Exception:
                edge = None

    dcf_output = state.get("dcf_output")
    dcf_input_flat = state.get("dcf_input_flat")
    decision_context: dict[str, Any] = {
        "scenarios": state.get("scenarios"),
        "dcf_projections_base": state.get("dcf_projections"),
        "tv_pct": (
            dcf_output.pv_terminal_value / dcf_output.enterprise_value
            if dcf_output is not None and getattr(dcf_output, "enterprise_value", 0) else 0
        ),
        "sensitivity_rankings": state.get("sensitivity_rankings"),
        "sensitivity_table": state.get("sensitivity_table"),
        "open_questions": state.get("open_questions"),
    }
    if edge is not None:
        decision_context["edge_assessment"] = edge
    if dcf_input_flat is not None:
        decision_context["dcf_assumptions"] = {
            "revenue_growth_path": list(dcf_input_flat.revenue_growth_path),
            "operating_margin_path": list(dcf_input_flat.operating_margin_path),
            "wacc": dcf_input_flat.wacc,
            "terminal_growth_rate": dcf_input_flat.terminal_growth_rate,
            "segment_dcf": state.get("segment_projections_data") is not None,
        }

    decision = DecisionEngine().decide(
        state.get("entity_id"), state.get("run_id"),
        state["all_judgments"], state["critic_results"],
        gate_result.publishable,
        context=decision_context,
        synthesized_thesis=state.get("synthesized_thesis"),
    )

    # ── Portfolio Signal ──
    from aegis.core.portfolio.portfolio_integration import PortfolioIntegration
    probs = state.get("scenario_probabilities") or {}
    signal = PortfolioIntegration().generate_signal(
        decision,
        scenario_weights={
            "bear": probs.get("bear", 0.3),
            "base": probs.get("base", 0.45),
            "bull": probs.get("bull", 0.25),
        },
    )

    # ── report dict（与 replay 的 generate_html_report 调用同参；
    #     model_name 固定 None 保证与环境变量无关）──
    from aegis.core.reports.html_report_v2 import build_report_dict
    config = state.get("config")
    report = build_report_dict(
        decision=decision,
        computed_metrics=state.get("computed_metrics"),
        market_data=state.get("market_data"),
        agent_judgments=state.get("all_judgments"),
        critic_results=state.get("critic_results"),
        meta_facts=state.get("meta_facts"),
        dcf_projections=state.get("dcf_projections"),
        dcf_output=state.get("dcf_output"),
        sensitivity_table=state.get("sensitivity_table"),
        sensitivity_rankings=state.get("sensitivity_rankings"),
        entity_name=state.get("entity_name"),
        entity_id=str(getattr(decision, "entity_id", "") or ""),
        segment_detail=state.get("segment_detail"),
        edited_report=None,  # Editor 是 LLM 步骤，golden 路径固定跳过
        synthesized_thesis=state.get("synthesized_thesis"),
        catalyst_timeline=state.get("catalyst_timeline"),
        scenarios=state.get("scenarios"),
        run_id=getattr(decision, "run_id", None),
        period=getattr(config, "period", None),
        open_questions=getattr(decision, "open_questions", None) or [],
        pipeline_duration=None,
        model_name=None,
        macro_snapshot=state.get("macro_snapshot"),
    )

    summary = {
        "entity_id": str(state.get("entity_id") or ""),
        "gate_publishable": bool(gate_result.publishable),
        "gate_checks_passed": sum(1 for c in gate_result.checks if c.passed),
        "gate_checks_total": len(gate_result.checks),
        "publishing_status": str(getattr(decision, "publishing_status", "")),
        "confidence_bucket": str(getattr(decision, "confidence_bucket", "")),
        "bias_check_status": str(getattr(decision, "bias_check_status", "")),
        "signal_direction": str(getattr(signal, "direction", "")),
        "signal_conviction": str(getattr(signal, "conviction", "")),
        "signal_sizing_tier": str(getattr(signal, "sizing_tier", "")),
        "kill_criteria_count": len(getattr(decision, "kill_criteria", []) or []),
        "monitorables_count": len(getattr(decision, "monitorables", []) or []),
    }
    return {"summary": summary, "report": _jsonify(report)}


# ─────────────────────────────────────────────────────────────────
# 规范化 + diff
# ─────────────────────────────────────────────────────────────────

def _jsonify(obj: Any) -> Any:
    """转成纯 JSON 结构：键统一 str、非有限 float 归 None、其他对象 str()。"""
    if isinstance(obj, dict):
        return {str(k): _jsonify(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonify(v) for v in obj]
    if isinstance(obj, bool) or obj is None:
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, (int, str)):
        return obj
    return str(obj)


def _apply_rule(node: Any, parts: list[str]) -> None:
    """沿点路径把叶子值替换为占位符；路径缺失/中途为 None 时静默跳过。"""
    if not isinstance(node, dict) or not parts:
        return
    key = parts[0]
    if key not in node:
        return
    if len(parts) == 1:
        if node[key] is not None:
            node[key] = NORMALIZED_PLACEHOLDER
        return
    _apply_rule(node[key], parts[1:])


def normalize_golden(golden: dict) -> dict:
    """对 rebuild_report 的输出应用 NORMALIZE_RULES（返回深拷贝）。"""
    out = json.loads(json.dumps(golden, ensure_ascii=False, default=str))
    report = out.get("report")
    if isinstance(report, dict):
        for path, _why in NORMALIZE_RULES:
            _apply_rule(report, path.split("."))
    return out


def canonical_json(golden: dict) -> str:
    """键排序的规范 JSON 文本（基线落盘格式，末尾带换行）。"""
    return json.dumps(golden, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _summarize(value: Any, limit: int = 80) -> str:
    s = json.dumps(value, ensure_ascii=False, default=str)
    return s if len(s) <= limit else s[:limit] + "…"


def diff_structures(base: Any, cur: Any, path: str = "$") -> list[dict]:
    """递归比对，返回 [{kind: added|removed|changed, path, base, cur}]。"""
    diffs: list[dict] = []
    if isinstance(base, dict) and isinstance(cur, dict):
        for k in sorted(base.keys() | cur.keys()):
            p = f"{path}.{k}"
            if k not in cur:
                diffs.append({"kind": "removed", "path": p,
                              "base": _summarize(base[k]), "cur": None})
            elif k not in base:
                diffs.append({"kind": "added", "path": p,
                              "base": None, "cur": _summarize(cur[k])})
            else:
                diffs.extend(diff_structures(base[k], cur[k], p))
    elif isinstance(base, list) and isinstance(cur, list):
        if len(base) != len(cur):
            diffs.append({"kind": "changed", "path": f"{path}(len)",
                          "base": str(len(base)), "cur": str(len(cur))})
        for i, (b, c) in enumerate(zip(base, cur)):
            diffs.extend(diff_structures(b, c, f"{path}[{i}]"))
    else:
        if base != cur or type(base) is not type(cur):
            diffs.append({"kind": "changed", "path": path,
                          "base": _summarize(base), "cur": _summarize(cur)})
    return diffs


def rebuild_normalized(name: str) -> dict:
    """一站式：pkl → report dict → 规范化。供 CLI 与 pytest 复用。"""
    pkl = PROJECT_ROOT / SNAPSHOTS[name]
    return normalize_golden(rebuild_report(pkl))


# ─────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────

def _select_names(only: str | None) -> list[str]:
    if only:
        if only not in SNAPSHOTS:
            print(f"未知快照名: {only}（可选: {', '.join(SNAPSHOTS)}）")
            raise SystemExit(2)
        return [only]
    return list(SNAPSHOTS)


def cmd_record(only: str | None) -> int:
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    failures = 0
    recorded = 0
    for name in _select_names(only):
        pkl = PROJECT_ROOT / SNAPSHOTS[name]
        if not pkl.exists():
            print(f"[skip] {name}: pkl 不存在 ({pkl})")
            continue
        try:
            # 确定性守卫：同一 pkl 连建两次，规范化后必须逐字节一致
            first = rebuild_normalized(name)
            second = rebuild_normalized(name)
        except GoldenRebuildError as e:
            print(f"[skip] {name}: 无法重建 — {e}")
            continue
        if canonical_json(first) != canonical_json(second):
            print(f"[fail] {name}: 两次重建结果不一致（存在漏网的非确定字段）")
            for d in diff_structures(first, second)[:20]:
                print(f"    {d['kind']:8s} {d['path']}: {d['base']} → {d['cur']}")
            failures += 1
            continue
        out = GOLDEN_DIR / f"{name}.json"
        out.write_text(canonical_json(first), encoding="utf-8")
        print(f"[ok]   {name}: 基线已写入 {out.relative_to(PROJECT_ROOT)}")
        recorded += 1
    if failures:
        return 1
    if recorded == 0:
        print("没有任何基线被记录（pkl 全部缺失或不可重建）")
        return 2
    return 0


def cmd_check(only: str | None) -> int:
    dirty = 0
    checked = 0
    for name in _select_names(only):
        pkl = PROJECT_ROOT / SNAPSHOTS[name]
        baseline_file = GOLDEN_DIR / f"{name}.json"
        if not pkl.exists():
            print(f"[skip] {name}: pkl 不存在 ({pkl})")
            continue
        if not baseline_file.exists():
            print(f"[fail] {name}: 基线缺失 ({baseline_file.relative_to(PROJECT_ROOT)})，先跑 record")
            dirty += 1
            continue
        try:
            current = rebuild_normalized(name)
        except GoldenRebuildError as e:
            print(f"[fail] {name}: 无法重建 — {e}")
            dirty += 1
            continue
        baseline = json.loads(baseline_file.read_text(encoding="utf-8"))
        diffs = diff_structures(baseline, current)
        checked += 1
        if diffs:
            dirty += 1
            print(f"[FAIL] {name}: {len(diffs)} 处差异")
            for d in diffs[:40]:
                print(f"    {d['kind']:8s} {d['path']}")
                print(f"             base: {d['base']}")
                print(f"             cur:  {d['cur']}")
            if len(diffs) > 40:
                print(f"    ... 及另外 {len(diffs) - 40} 处")
        else:
            print(f"[ok]   {name}: 零 diff")
    if dirty:
        return 1
    if checked == 0:
        print("没有任何快照被比对（pkl 全部缺失）")
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Golden-master 挽具（红线 #7 闸门）")
    parser.add_argument("mode", choices=["record", "check"],
                        help="record=重建并写基线；check=与基线比对，非空 diff 退出码 1")
    parser.add_argument("--only", default=None,
                        help=f"只处理单个快照（可选: {', '.join(SNAPSHOTS)}）")
    args = parser.parse_args(argv)
    if args.mode == "record":
        return cmd_record(args.only)
    return cmd_check(args.only)


if __name__ == "__main__":
    sys.exit(main())
