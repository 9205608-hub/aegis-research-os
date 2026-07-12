"""Model-free implied-expectations anchors — R4-1 (AUDIT 2026-07-12).

Grok R2/R3 复审在三只失配票上反复判同一条 P0："DCF 已宣告 model bug，
仍用同一模型反解『市场隐含预期』= 循环论证"。北方华创 R3 判词给出明路：
"若去掉 DCF 目标价族与伪精确，残余行业常识价值大约在 5 分"。

本模块在 valuation sanity 失配时提供**不依赖 DCF 的**"市场在定价什么"
量化框架（标准卖方做法）：

1. 相对估值锚：现价 PE(TTM)/PB 与同业中位、行业分位
   （aegis.core.truth.relative_valuation 已算好，红线 5 覆盖门槛已过）。
2. 一致预期倍数反推：现价对应各年度一致预期净利的前瞻 PE；以及
   "维持现价、给定前瞻 PE 档位时需要的净利"（预期优先的倍数空间版）。

两个来源各自带覆盖门槛（insufficient_peers / insufficient_coverage），
不满足就不输出该段——诚实优先，宁缺勿假。
"""

from __future__ import annotations

from typing import Any

# 倍数档位：反推"维持现价需要的净利"。取 A 股制造/科技股的常见估值
# 区间边界，仅作条件化表述（若给 X× 则需 Y），不是预测。
_INVERSE_PE_TIERS = (15.0, 25.0)


def build_model_free_implied(
    meta_facts: dict[str, Any] | None,
    market_data: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """构建无模型隐含预期块。

    Returns
    -------
    dict | None
        {"lines_zh": [...], "sanctioned_pcts": [...], "source_note": str}
        两个来源都不可用时返回 None（调用方回退到诚实的"无可用锚"表述）。
    """
    mf = meta_facts or {}
    md = market_data or {}
    price = md.get("current_price") or md.get("price")
    if not isinstance(price, (int, float)) or price <= 0:
        return None

    lines: list[str] = []
    pcts: list[float] = []

    relval = mf.get("__relative_valuation")
    if isinstance(relval, dict) and not relval.get("insufficient_peers", True):
        pe = relval.get("target_pe_ttm")
        pe_med = relval.get("peer_pe_median")
        pe_pct = relval.get("pe_percentile")
        pb = relval.get("target_pb")
        pb_med = relval.get("peer_pb_median")
        pb_pct = relval.get("pb_percentile")
        seg = []
        if isinstance(pe, (int, float)) and isinstance(pe_med, (int, float)):
            _p = f"、处于同业第 {pe_pct:.0f} 分位" if isinstance(pe_pct, (int, float)) else ""
            seg.append(f"现价对应 PE(TTM) {pe:.1f}×（同业中位 {pe_med:.1f}×{_p}）")
            pcts += [round(abs(pe), 1), round(abs(pe_med), 1)]
            if isinstance(pe_pct, (int, float)):
                pcts.append(float(round(pe_pct)))
        if isinstance(pb, (int, float)) and isinstance(pb_med, (int, float)):
            _p = f"、第 {pb_pct:.0f} 分位" if isinstance(pb_pct, (int, float)) else ""
            seg.append(f"PB {pb:.2f}×（同业中位 {pb_med:.2f}×{_p}）")
            pcts += [round(abs(pb), 2), round(abs(pb_med), 2)]
            if isinstance(pb_pct, (int, float)):
                pcts.append(float(round(pb_pct)))
        if seg:
            lines.append("；".join(seg) + "。")

    events = mf.get("__recent_events")
    cons = events.get("consensus") if isinstance(events, dict) else None
    shares = mf.get("shares_outstanding") or mf.get("diluted_shares")
    if (
        isinstance(cons, dict)
        and not cons.get("insufficient_coverage", True)
        and isinstance(shares, (int, float)) and shares > 0
    ):
        orgs = cons.get("org_count")
        preds = [
            p for p in (cons.get("predictions") or [])
            if isinstance(p, dict)
            and isinstance(p.get("net_profit"), (int, float))
            and p["net_profit"] > 0
        ]
        market_cap = price * shares
        for p in preds[:3]:
            fwd_pe = market_cap / p["net_profit"]
            lines.append(
                f"若达成 {p['year']}E 一致预期归母净利 ¥{p['net_profit']/1e8:.0f}亿"
                f"（{orgs} 家覆盖），现价对应前瞻 PE {fwd_pe:.1f}×。"
            )
            pcts.append(round(abs(fwd_pe), 1))
        if preds:
            far = preds[-1]
            tier_parts = []
            for tier in _INVERSE_PE_TIERS:
                need = market_cap / tier
                tier_parts.append(f"{tier:.0f}× 需归母净利 ¥{need/1e8:.0f}亿")
                pcts.append(round(abs(tier), 1))
            lines.append(
                f"维持现价的条件化表述：若市场给前瞻 PE {'；'.join(tier_parts)}"
                f"（对照 {far['year']}E 一致预期 ¥{far['net_profit']/1e8:.0f}亿）。"
            )

    if not lines:
        return None
    return {
        "lines_zh": lines,
        "sanctioned_pcts": sorted(set(pcts)),
        "source_note": (
            "无模型锚（相对估值分位 + 一致预期倍数反推）——本 run DCF 未过"
            "数量级检验，反向 DCF 隐含预期停用，以上为可引用的市场定价框架"
        ),
    }
