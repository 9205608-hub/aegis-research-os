"""Variant Analyst Agent — Section 19.8.

Responsibilities:
- Consensus likely view assessment
- What is already priced in
- Variant location identification
- Variant magnitude estimation
- Catalyst identification & timeline
- Edge classification

Prohibitions:
- No "different from sell-side" = variant (must explain WHY different)
- No "good company" = "high alpha stock"
- No actionable variant without catalyst
- No thesis without edge assessment
"""

from __future__ import annotations

from aegis.core.agents.base import AgentBase, AgentInput, is_zh_input
from aegis.data_contracts.judgment_schema import (
    CognitiveBiasSelfCheck,
    Counterargument,
    DisconfirmingTrigger,
    Inference,
    Observation,
)


class VariantAnalyst(AgentBase):
    """Specialist agent for variant identification and edge assessment."""

    AGENT_NAME = "variant_analyst"
    AGENT_VERSION = "0.1.0"

    def _extract_observations(self, inp: AgentInput) -> list[Observation]:
        observations: list[Observation] = []
        zh = is_zh_input(inp)

        # Priced-in assumptions from macro context
        priced_in = (inp.macro_context or {}).get("priced_in", {})
        if priced_in:
            # AUDIT-A9 (BUG-Y20 third path): when the reverse DCF hit a
            # bisection boundary the implied growth is a fake-clean edge
            # value (e.g. 0.50). The orchestrator now nulls it out and sets
            # `implied_growth_unreliable`; guard here too so a stale/other
            # caller can never turn the artifact into a rendered Observation.
            if (priced_in.get("implied_revenue_growth") is not None
                    and not priced_in.get("implied_growth_unreliable")):
                observations.append(Observation(
                    text=(f"市场隐含营收增速: {priced_in['implied_revenue_growth']:.2%}" if zh
                          else f"Market-implied revenue growth: {priced_in['implied_revenue_growth']:.2%}"),
                    source_ids=["reverse_dcf:implied_growth"],
                ))

            # Rich revision signal (from MarketExpectationsLayer)
            rev_sig = priced_in.get("revision_signal", {})
            if rev_sig and rev_sig.get("momentum"):
                if zh:
                    parts = [f"一致预期修正动能: {rev_sig['momentum']}"]
                    if rev_sig.get("breadth") and rev_sig["breadth"] != "unknown":
                        parts.append(f"广度={rev_sig['breadth']}")
                    if rev_sig.get("acceleration") and rev_sig["acceleration"] != "stable":
                        parts.append(f"节奏 {rev_sig['acceleration']}")
                    if rev_sig.get("revision_1m_pct") is not None:
                        parts.append(f"近 1 个月修正={rev_sig['revision_1m_pct']:.1%}")
                else:
                    parts = [f"Consensus revision momentum: {rev_sig['momentum']}"]
                    if rev_sig.get("breadth") and rev_sig["breadth"] != "unknown":
                        parts.append(f"breadth={rev_sig['breadth']}")
                    if rev_sig.get("acceleration") and rev_sig["acceleration"] != "stable":
                        parts.append(f"pace {rev_sig['acceleration']}")
                    if rev_sig.get("revision_1m_pct") is not None:
                        parts.append(f"1m revision={rev_sig['revision_1m_pct']:.1%}")
                observations.append(Observation(
                    text=", ".join(parts),
                    source_ids=["consensus:revision_signal"],
                ))
            elif priced_in.get("revision_momentum"):
                # Fallback to simple string
                observations.append(Observation(
                    text=(f"一致预期修正动能: {priced_in['revision_momentum']}" if zh
                          else f"Consensus revision momentum: {priced_in['revision_momentum']}"),
                    source_ids=["consensus:revision_momentum"],
                ))

            if priced_in.get("pe_ratio_fwd") is not None:
                observations.append(Observation(
                    text=(f"预期市盈率 (Forward P/E): {priced_in['pe_ratio_fwd']:.1f}x" if zh
                          else f"Forward P/E: {priced_in['pe_ratio_fwd']:.1f}x"),
                    source_ids=["consensus:pe_fwd"],
                ))

        # Scenario data
        scenarios = (inp.macro_context or {}).get("scenarios", {})
        _scenario_zh = {"bear": "熊市", "base": "基准", "bull": "牛市"}
        for name in ("bear", "base", "bull"):
            val = scenarios.get(f"{name}_value")
            if val is not None:
                observations.append(Observation(
                    text=(f"{_scenario_zh[name]}情景估值: {val:.2f}" if zh
                          else f"{name.capitalize()} case value: {val:.2f}"),
                    source_ids=[f"scenario_engine:{name}"],
                ))

        # Current price
        current_price = (inp.macro_context or {}).get("current_price")
        if current_price is not None:
            observations.append(Observation(
                text=(f"当前市价: {current_price:.2f}" if zh
                      else f"Current market price: {current_price:.2f}"),
                source_ids=["market_data:price"],
            ))

        # Key assumption disagreements
        disagreements = (inp.macro_context or {}).get("disagreements", [])
        for d in disagreements:
            if isinstance(d, dict) and d.get("this_is_the_variant"):
                observations.append(Observation(
                    text=(f"差异化假设: '{d.get('assumption', '')}'——"
                          f"市场隐含 {d.get('market_implied', '')}，"
                          f"我们的观点: {d.get('my_view', '')}"
                          if zh else
                          f"Variant assumption: '{d.get('assumption', '')}' — "
                          f"market implies {d.get('market_implied', '')}, "
                          f"our view: {d.get('my_view', '')}"),
                    source_ids=["analysis:key_assumption_disagreement"],
                ))

        # Evidence
        for ep in inp.evidence_packets:
            if ep.get("assertion_type") in ("variant", "catalyst", "edge",
                                             "consensus_view", "positioning"):
                observations.append(Observation(
                    text=ep.get("assertion_text", ""),
                    source_ids=[ep.get("evidence_id", "")],
                ))

        # Prior judgments from other agents
        for pj in inp.prior_judgments:
            if pj.agent_name in ("valuation_analyst", "business_analyst"):
                for inf in pj.inferences:
                    if any(kw in inf.text.lower() for kw in ("moat", "pricing power", "growth")) \
                            or any(kw in inf.text for kw in ("护城河", "定价能力", "定价权", "增速", "增长")):
                        observations.append(Observation(
                            text=f"[{pj.agent_name}] {inf.text}",
                            source_ids=[pj.judgment_id],
                        ))

        return observations

    def _derive_inferences(
        self, observations: list[Observation], inp: AgentInput
    ) -> list[Inference]:
        inferences: list[Inference] = []
        zh = is_zh_input(inp)

        # Variant location from disagreements
        variant_obs = [i for i, o in enumerate(observations)
                       if "variant assumption" in o.text.lower() or "差异化假设" in o.text]
        if variant_obs:
            inferences.append(Inference(
                text=("已识别差异化观点：我们与市场隐含假设的分歧落在具体、可验证的维度上"
                      if zh else
                      "Variant identified: our view differs from market-implied assumptions "
                      "on a specific, testable dimension"),
                based_on_observation_indices=variant_obs,
                confidence="medium",
            ))

        # Price vs scenario range
        price_obs = [i for i, o in enumerate(observations)
                     if "current market price" in o.text.lower() or "当前市价" in o.text]
        scenario_obs = [i for i, o in enumerate(observations)
                        if "case value" in o.text.lower() or "情景估值" in o.text]
        if price_obs and scenario_obs:
            scenarios = (inp.macro_context or {}).get("scenarios", {})
            price = (inp.macro_context or {}).get("current_price", 0)
            base = scenarios.get("base_value", 0)
            bear = scenarios.get("bear_value", 0)
            bull = scenarios.get("bull_value", 0)

            if price > 0 and base > 0:
                upside = (base - price) / price
                if upside > 0.15:
                    inferences.append(Inference(
                        text=(f"基准情景隐含 {upside:.0%} 上行空间——现价低于我们的内在价值估计"
                              if zh else
                              f"Base case implies {upside:.0%} upside — "
                              "price below our intrinsic value estimate"),
                        based_on_observation_indices=price_obs + scenario_obs[:1],
                        confidence="medium",
                    ))
                elif upside < -0.10:
                    inferences.append(Inference(
                        text=(f"基准情景隐含 {upside:.0%} 下行风险——现价高于我们的内在价值估计"
                              if zh else
                              f"Base case implies {upside:.0%} downside — "
                              "price above our intrinsic value estimate"),
                        based_on_observation_indices=price_obs + scenario_obs[:1],
                        confidence="medium",
                    ))

        # Consensus momentum alignment with variant direction
        rev_sig = (inp.macro_context or {}).get("priced_in", {}).get("revision_signal", {})
        momentum_obs = [i for i, o in enumerate(observations)
                        if "revision momentum" in o.text.lower() or "修正动能" in o.text]
        if rev_sig and rev_sig.get("momentum") and momentum_obs:
            momentum = rev_sig["momentum"]
            breadth = rev_sig.get("breadth", "unknown")
            acceleration = rev_sig.get("acceleration", "stable")
            # Check if we have a directional view
            base_val = (inp.macro_context or {}).get("scenarios", {}).get("base_value", 0)
            current_p = (inp.macro_context or {}).get("current_price", 0)
            our_direction = "bullish" if (base_val and current_p and base_val > current_p) else "bearish"

            if our_direction == "bullish" and momentum == "positive":
                txt = (
                    f"一致预期动能一致性: 顺风——预期修正为正（{breadth}），"
                    f"我们的看多观点顺应现有分析师情绪。催化剂兑现周期更短，"
                    f"但一致预期快速跟上会侵蚀超额收益空间"
                ) if zh else (
                    f"Consensus momentum alignment: FAVORABLE — revisions are positive"
                    f" ({breadth}), our bullish variant rides existing analyst enthusiasm. "
                    f"Shorter duration to catalyst, but edge vulnerable to consensus catch-up"
                )
                conf = "medium"
            elif our_direction == "bullish" and momentum == "negative":
                txt = (
                    f"一致预期动能一致性: 逆向——预期修正为负（{breadth}），"
                    f"我们的看多观点与一致预期趋势相悖。风险更高、拐点更远，"
                    f"但若判断正确则超额收益更大"
                ) if zh else (
                    f"Consensus momentum alignment: CONTRARIAN — revisions are negative"
                    f" ({breadth}), our bullish variant goes against consensus trajectory. "
                    f"Higher risk, longer inflection, but larger edge if correct"
                )
                conf = "low"
            elif our_direction == "bearish" and momentum == "positive":
                txt = (
                    f"一致预期动能一致性: 逆向——预期修正为正（{breadth}），"
                    f"我们的看空观点与分析师上调趋势相抗。催化剂时点风险更高"
                ) if zh else (
                    f"Consensus momentum alignment: CONTRARIAN — revisions are positive"
                    f" ({breadth}), our bearish variant fights analyst upgrades. "
                    f"Higher catalyst timing risk"
                )
                conf = "low"
            elif our_direction == "bearish" and momentum == "negative":
                txt = (
                    f"一致预期动能一致性: 顺风——预期修正为负（{breadth}），"
                    f"我们的看空观点与下调动能同向"
                ) if zh else (
                    f"Consensus momentum alignment: FAVORABLE — revisions are negative"
                    f" ({breadth}), our bearish variant aligns with downgrade momentum"
                )
                conf = "medium"
            else:
                txt = (
                    f"一致预期动能: {momentum}（{breadth}）——"
                    f"差异化观点需要新信息或催化剂来打破僵局"
                ) if zh else (
                    f"Consensus momentum: {momentum} ({breadth}) — "
                    f"variant depends on new information or catalyst to break stasis"
                )
                conf = "medium"

            if acceleration == "accelerating":
                txt += ("。修正节奏正在加速——动能信号增强" if zh
                        else ". Revision pace is ACCELERATING — momentum signal strengthening")
            elif acceleration == "decelerating":
                txt += ("。修正节奏正在放缓——动能或趋于衰减" if zh
                        else ". Revision pace is DECELERATING — momentum may be fading")

            inferences.append(Inference(
                text=txt,
                based_on_observation_indices=momentum_obs,
                confidence=conf,
            ))

        # Edge classification
        all_obs = list(range(len(observations)))
        if observations:
            inferences.append(Inference(
                text=("需完成优势(edge)归类：论点发布前必须先界定优势属于分析型、"
                      "信息型、行为型还是结构型"
                      if zh else
                      "Edge assessment required: must classify whether edge is analytical, "
                      "informational, behavioral, or structural before thesis can publish"),
                based_on_observation_indices=[0],
                confidence="high",
            ))

        # Variant decomposition using sensitivity rankings
        sensitivity = (inp.macro_context or {}).get("sensitivity_rankings", [])
        disagreements = (inp.macro_context or {}).get("disagreements", [])
        if sensitivity and disagreements and price_obs:
            decomp_lines = [
                "差异化归因分解 (ΔV = ΔV_增长 + ΔV_利润率 + ΔV_再投资):" if zh
                else "Variant decomposition (ΔV = ΔV_growth + ΔV_margin + ΔV_reinvestment):"
            ]
            for d in disagreements:
                if isinstance(d, dict):
                    assumption = d.get("assumption", "")
                    market_val = d.get("market_implied", "")
                    my_val = d.get("my_view", "")
                    # Find matching sensitivity for this assumption
                    impact_str = ""
                    for s in sensitivity:
                        if s.get("assumption", "") in assumption.lower().replace("_fy26", ""):
                            impact_pct = s.get("impact_pct", 0)
                            impact_str = (f"（敏感性: 每 10% 冲击对应 {impact_pct:.1%}）" if zh
                                          else f" (sensitivity: {impact_pct:.1%} per 10% shock)")
                            break
                    decomp_lines.append(
                        f"  - {assumption}: 市场={market_val}, 我们={my_val}{impact_str}" if zh
                        else f"  - {assumption}: market={market_val}, ours={my_val}{impact_str}"
                    )
            if len(decomp_lines) > 1:
                anchor = price_obs[0] if price_obs else (variant_obs[0] if variant_obs else 0)
                inferences.append(Inference(
                    text="\n".join(decomp_lines),
                    based_on_observation_indices=[anchor],
                    confidence="medium",
                ))

        return inferences

    def _generate_counterarguments(
        self, inferences: list[Inference], inp: AgentInput
    ) -> list[Counterargument]:
        counterargs: list[Counterargument] = []
        zh = is_zh_input(inp)

        for inf in inferences:
            t = inf.text.lower()
            if "variant identified" in t or "已识别差异化观点" in inf.text:
                counterargs.append(Counterargument(
                    text=("市场可能已通过知情交易部分消化该观点——需核查持仓与资金流数据"
                          if zh else
                          "Market may already be partially pricing this view through informed "
                          "trading — check positioning and flow data"),
                    strength="moderate",
                    evidence_ids=[],
                ))
            elif "upside" in t or "上行空间" in inf.text:
                counterargs.append(Counterargument(
                    text=("表观低估可能对应我们低估了的风险——市场折价或许是合理的"
                          if zh else
                          "Apparent undervaluation may reflect risks we underestimate — "
                          "market discount could be warranted"),
                    strength="moderate",
                    evidence_ids=[],
                ))

        counterargs.append(Counterargument(
            text=("缺乏近期催化剂的差异化论点可能在持有期内无法收敛——"
                  "「市场保持非理性的时间，可以比你保持偿付能力的时间更长」"
                  if zh else
                  "Variant thesis without near-term catalyst may fail to converge within horizon — "
                  "'the market can stay irrational longer than you can stay solvent'"),
            strength="strong",
            evidence_ids=[],
        ))

        return counterargs

    def _identify_disconfirming_triggers(
        self, inferences: list[Inference], inp: AgentInput
    ) -> list[DisconfirmingTrigger]:
        zh = is_zh_input(inp)
        triggers = [
            DisconfirmingTrigger(
                text=("一致预期向我们的差异化观点收敛（优势被市场吸收）" if zh
                      else "Consensus converges to our variant view (edge absorbed by market)"),
                monitorable=True,
                check_frequency="monthly",
            ),
            DisconfirmingTrigger(
                text=("催化剂时间窗过去而事件未兑现" if zh
                      else "Catalyst timeline passes without materialization"),
                monitorable=True,
                check_frequency="monthly",
            ),
            DisconfirmingTrigger(
                text=("新信息推翻差异化论点的立论基础" if zh
                      else "New information invalidates the basis of our variant thesis"),
                monitorable=True,
                check_frequency="weekly",
            ),
            DisconfirmingTrigger(
                text=("一致预期修正动能向不利于我们观点的方向急剧反转"
                      "（如看多论点遭遇大面积下调，或看空论点遭遇大面积上调）"
                      if zh else
                      "Consensus revision momentum reverses sharply against our variant "
                      "direction (e.g. broad downgrade for bullish thesis, or broad "
                      "upgrade for bearish thesis)"),
                monitorable=True,
                check_frequency="monthly",
            ),
        ]
        return triggers

    def _cognitive_bias_self_check(self, inp: AgentInput) -> CognitiveBiasSelfCheck:
        zh = is_zh_input(inp)
        return CognitiveBiasSelfCheck(
            anchoring_risk="high",
            confirmation_bias_risk="high",
            recency_bias_risk="medium",
            narrative_fallacy_risk="high",
            mitigation_steps_taken=(
                [
                    "先锚定市场隐含假设，再形成自身观点",
                    "要求差异化维度具体、可验证",
                    "强制要求识别催化剂——不接受「无期限」的差异化主张",
                    "已纳入关于市场有效性的强反方论证",
                ] if zh else [
                    "Anchored on market-implied assumptions before forming own view",
                    "Required specific, testable variant dimension",
                    "Demanded catalyst identification — no 'timeless' variant claims",
                    "Included strong counterargument about market efficiency",
                ]
            ),
        )

    def _report_uncertainties(self, inp: AgentInput) -> list[str]:
        if is_zh_input(inp):
            return [
                "差异化幅度为估计值——实际错误定价程度可能不同",
                "催化剂时点不确定——优势可能在兑现前衰减",
                "市场持仓数据可能无法覆盖所有知情参与者",
            ]
        return [
            "Variant magnitude is estimated — actual mispricing may differ",
            "Catalyst timing is uncertain — edge may decay before realization",
            "Market positioning data may not capture all informed participants",
        ]
