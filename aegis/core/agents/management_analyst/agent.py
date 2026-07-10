"""Management Analyst Agent — Section 19.5.

Responsibilities:
- Management track record
- Capital allocation history
- Insider transaction analysis
- Compensation alignment
- Communication quality
- Board composition
- Succession risk
- Related-party transaction risk assessment (especially China market)

Prohibitions:
- No equating "famous CEO" with "excellent management"
- No ignoring dual-class / VIE governance impact
- No replacing quantitative track record with qualitative impressions
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


class ManagementAnalyst(AgentBase):
    """Specialist agent for management quality and governance analysis."""

    AGENT_NAME = "management_analyst"
    AGENT_VERSION = "0.1.0"

    # Capital allocation scoring thresholds
    ROIC_GOOD = 0.12
    ROIC_POOR = 0.06

    def _extract_observations(self, inp: AgentInput) -> list[Observation]:
        observations: list[Observation] = []
        metrics = inp.metric_results
        zh = is_zh_input(inp)

        # Capital allocation track record (quantitative)
        if "roic" in metrics:
            observations.append(Observation(
                text=(f"ROIC 为 {metrics['roic']:.2%}——反映资本配置成效" if zh
                      else f"ROIC is {metrics['roic']:.2%} — reflects capital allocation effectiveness"),
                source_ids=[f"metric:roic:{inp.entity_id}"],
            ))

        if "roe" in metrics:
            observations.append(Observation(
                text=(f"ROE 为 {metrics['roe']:.2%}" if zh
                      else f"ROE is {metrics['roe']:.2%}"),
                source_ids=[f"metric:roe:{inp.entity_id}"],
            ))

        # SBC as compensation alignment signal
        if "sbc_to_revenue" in metrics:
            val = metrics["sbc_to_revenue"]
            observations.append(Observation(
                text=(f"SBC/营收为 {val:.2%}——管理层薪酬绑定程度的代理指标" if zh
                      else f"SBC/Revenue is {val:.2%} — proxy for management compensation alignment"),
                source_ids=[f"metric:sbc_to_revenue:{inp.entity_id}"],
            ))

        # Related-party transactions from relationships
        for rel in inp.entity_relationships:
            rel_type = rel.get("relationship_type", "")
            if "related_party" in rel_type or "ownership" in rel_type:
                other = rel.get("entity_b") if rel.get("entity_a") == inp.entity_id else rel.get("entity_a", "")
                rev_sig = rel.get("revenue_significance", {})
                pct = rev_sig.get("a_revenue_from_b_pct", 0)
                if zh:
                    note = f"，收入占比: {pct:.1%}" if pct else ""
                    text = f"与 {other} 存在关联方关系（{rel_type}）{note}"
                else:
                    note = f", revenue significance: {pct:.1%}" if pct else ""
                    text = f"Related-party relationship with {other} ({rel_type}){note}"
                observations.append(Observation(
                    text=text,
                    source_ids=[rel.get("relationship_id", "")],
                ))

        # Governance-related evidence
        for ep in inp.evidence_packets:
            if ep.get("assertion_type") in ("management_quality", "insider_transaction",
                                             "board_composition", "governance",
                                             "related_party", "compensation"):
                observations.append(Observation(
                    text=ep.get("assertion_text", ""),
                    source_ids=[ep.get("evidence_id", "")],
                ))

        # Insider trading activity (from SEC Form 4 via agent_macro)
        insider = (inp.macro_context or {}).get("insider_trading")
        if insider:
            buy_ct = insider.get("buy_count", 0)
            sell_ct = insider.get("sell_count", 0)
            buy_val = insider.get("total_buy_value", 0)
            sell_val = insider.get("total_sell_value", 0)
            net_val = insider.get("net_value", 0)
            sentiment = insider.get("sentiment", "neutral")

            if zh:
                direction = "净买入" if net_val > 0 else "净卖出"
                text = (
                    f"过去 12 个月，内部人共买入 {buy_ct} 笔（${buy_val:,.0f}）、"
                    f"卖出 {sell_ct} 笔（${sell_val:,.0f}）——"
                    f"{direction} ${abs(net_val):,.0f}（情绪信号: {sentiment}）"
                )
            else:
                direction = "net buying" if net_val > 0 else "net selling"
                text = (
                    f"Over the past 12 months, insiders made {buy_ct} purchases "
                    f"(${buy_val:,.0f}) and {sell_ct} sales (${sell_val:,.0f}) — "
                    f"{direction} of ${abs(net_val):,.0f} (sentiment: {sentiment})"
                )
            observations.append(Observation(
                text=text,
                source_ids=[f"form4:{inp.entity_id}"],
            ))

            if insider.get("cluster_detected"):
                observations.append(Observation(
                    text=("检测到内部人集中交易——30 天窗口内有 3 名以上内部人同向交易"
                          if zh else
                          "Cluster insider activity detected — 3+ insiders transacted "
                          "in the same direction within a 30-day window"),
                    source_ids=[f"form4:{inp.entity_id}:cluster"],
                ))

            # Notable transactions (C-suite buys are especially informative)
            for txn in insider.get("notable_transactions", [])[:3]:
                if zh:
                    txn_type = "买入" if txn["type"] == "P" else "卖出"
                    text = (
                        f"{txn['name']}（{txn['title']}）于 {txn['date']} "
                        f"{txn_type}价值 ${txn['value']:,.0f} 的股份"
                    )
                else:
                    txn_type = "purchased" if txn["type"] == "P" else "sold"
                    text = (
                        f"{txn['name']} ({txn['title']}) {txn_type} "
                        f"${txn['value']:,.0f} worth of shares on {txn['date']}"
                    )
                observations.append(Observation(
                    text=text,
                    source_ids=[f"form4:{inp.entity_id}:notable"],
                ))

        # Governance structure from sector pack
        sp = inp.sector_pack or {}
        special_risks = sp.get("special_risk_factors", {})
        if "vie_structure" in special_risks:
            observations.append(Observation(
                text=("公司采用 VIE 架构——境外投资者持有的是合同权利而非股权"
                      if zh else
                      "Entity operates under VIE structure — "
                      "foreign investors hold contractual rights, not equity ownership"),
                source_ids=[f"sector_pack:{sp.get('sector_pack_id', '')}:risk:vie_structure"],
            ))
        if "related_party" in special_risks:
            observations.append(Observation(
                text=("所处行业关联交易风险偏高——创始人控制的集团公司较为常见"
                      if zh else
                      "Sector has elevated related-party transaction risk — "
                      "founder-controlled conglomerates common"),
                source_ids=[f"sector_pack:{sp.get('sector_pack_id', '')}:risk:related_party"],
            ))

        return observations

    def _derive_inferences(
        self, observations: list[Observation], inp: AgentInput
    ) -> list[Inference]:
        inferences: list[Inference] = []
        metrics = inp.metric_results
        zh = is_zh_input(inp)

        # Capital allocation quality inference
        roic_obs_idx = None
        for i, obs in enumerate(observations):
            t = obs.text.lower()
            if "roic" in t and ("capital allocation" in t or "资本配置" in obs.text):
                roic_obs_idx = i

        if roic_obs_idx is not None:
            roic = metrics.get("roic", 0)
            if roic >= self.ROIC_GOOD:
                inferences.append(Inference(
                    text=("管理层资本配置能力突出——ROIC 高于资本成本" if zh
                          else "Management demonstrates strong capital allocation — ROIC exceeds cost of capital"),
                    based_on_observation_indices=[roic_obs_idx],
                    confidence="medium",
                ))
            elif roic < self.ROIC_POOR:
                inferences.append(Inference(
                    text=("管理层资本配置记录欠佳——ROIC 水平显示存在价值毁损" if zh
                          else "Management's capital allocation track record is poor — ROIC suggests value destruction"),
                    based_on_observation_indices=[roic_obs_idx],
                    confidence="medium",
                ))

        # Related-party risk inference
        rpt_obs_indices = [
            i for i, obs in enumerate(observations)
            if "related-party" in obs.text.lower() or "related party" in obs.text.lower()
            or "关联方" in obs.text or "关联交易" in obs.text
        ]
        if rpt_obs_indices:
            # Check for material related-party exposure
            has_material_rpt = any(
                "revenue significance" in observations[i].text.lower()
                or "收入占比" in observations[i].text
                for i in rpt_obs_indices
            )
            if has_material_rpt:
                inferences.append(Inference(
                    text=("检测到重大关联交易——估值应计入治理折价" if zh
                          else "Material related-party transactions detected — governance discount warranted"),
                    based_on_observation_indices=rpt_obs_indices,
                    confidence="high",
                ))

        # VIE governance inference
        vie_obs_indices = [i for i, obs in enumerate(observations) if "vie" in obs.text.lower()]
        if vie_obs_indices:
            inferences.append(Inference(
                text=("VIE 架构带来治理风险——中小股东的法律追索手段有限" if zh
                      else "VIE structure creates governance risk — "
                           "minority shareholders have limited legal recourse"),
                based_on_observation_indices=vie_obs_indices,
                confidence="high",
            ))

        # Insider trading inferences
        insider_obs_indices = [
            i for i, obs in enumerate(observations)
            if "form4:" in ",".join(obs.source_ids)
        ]
        if insider_obs_indices:
            insider = (inp.macro_context or {}).get("insider_trading", {})
            sentiment = insider.get("sentiment", "neutral")
            cluster = insider.get("cluster_detected", False)

            if sentiment == "bullish":
                conf = "high" if cluster else "medium"
                inferences.append(Inference(
                    text=("内部人增持传递管理层信心——内部人正以当前价格投入自有资金" if zh
                          else "Insider buying signals management confidence — "
                               "insiders are deploying personal capital at current prices"),
                    based_on_observation_indices=insider_obs_indices,
                    confidence=conf,
                ))
            elif sentiment == "bearish":
                inferences.append(Inference(
                    text=("内部人大额减持或反映管理层担忧——但减持也可能出于分散配置或税务筹划"
                          if zh else
                          "Significant insider selling may indicate management concern — "
                          "though sales can also reflect diversification or tax planning"),
                    based_on_observation_indices=insider_obs_indices,
                    confidence="medium",
                ))
            elif sentiment == "mixed":
                inferences.append(Inference(
                    text=("内部人交易方向分化——有买有卖，管理层内部观点或存分歧"
                          if zh else
                          "Mixed insider activity — some buying and selling suggest "
                          "divergent views among management team"),
                    based_on_observation_indices=insider_obs_indices,
                    confidence="low",
                ))

            if cluster:
                cluster_obs = [
                    i for i, obs in enumerate(observations)
                    if "cluster" in obs.text.lower() or "集中交易" in obs.text
                ]
                if cluster_obs:
                    if zh:
                        direction = "增持" if sentiment == "bullish" else "减持"
                        text = (f"内部人集中{direction}的信号强度高于个别交易——"
                                f"多名内部人独立得出了相同结论")
                    else:
                        direction = "buying" if sentiment == "bullish" else "selling"
                        text = (f"Cluster insider {direction} is a stronger signal than "
                                f"individual transactions — multiple insiders independently "
                                f"reached the same conclusion")
                    inferences.append(Inference(
                        text=text,
                        based_on_observation_indices=cluster_obs,
                        confidence="high",
                    ))

        return inferences

    def _generate_counterarguments(
        self, inferences: list[Inference], inp: AgentInput
    ) -> list[Counterargument]:
        counterargs: list[Counterargument] = []
        zh = is_zh_input(inp)

        for inf in inferences:
            t = inf.text.lower()
            if "strong capital allocation" in t or "资本配置能力突出" in inf.text:
                counterargs.append(Counterargument(
                    text=("历史 ROIC 反映的是过去的决策——现任管理层的战略可能不同。"
                          "应更多权重考察 CEO 任期与近期重大决策。"
                          if zh else
                          "Historical ROIC reflects past decisions — current management's strategy may differ. "
                          "CEO tenure and recent major decisions should be weighted more heavily."),
                    strength="moderate",
                    evidence_ids=[],
                ))
            elif "value destruction" in t or "价值毁损" in inf.text:
                counterargs.append(Counterargument(
                    text=("低 ROIC 可能是成长期的主动再投资，回报将在更长周期内兑现"
                          if zh else
                          "Low ROIC may reflect deliberate growth-phase reinvestment "
                          "that will generate returns over a longer horizon"),
                    strength="moderate",
                    evidence_ids=[],
                ))
            elif "governance discount" in t or "治理折价" in inf.text:
                counterargs.append(Counterargument(
                    text=("关联交易可能定价公允且披露充分——审计机构与董事会的监督或可缓释风险"
                          if zh else
                          "Related-party transactions may be at arm's length and properly disclosed — "
                          "auditor and board oversight may mitigate risk"),
                    strength="weak",
                    evidence_ids=[],
                ))

        # Always include governance structure caveat
        counterargs.append(Counterargument(
            text=("量化记录本身无法完全刻画管理层质量——沟通透明度与战略一致性同样重要"
                  if zh else
                  "Quantitative track record alone cannot capture management quality — "
                  "communication transparency and strategic consistency also matter"),
            strength="moderate",
            evidence_ids=[],
        ))

        return counterargs

    def _identify_disconfirming_triggers(
        self, inferences: list[Inference], inp: AgentInput
    ) -> list[DisconfirmingTrigger]:
        zh = is_zh_input(inp)
        triggers = [
            DisconfirmingTrigger(
                text=("CEO 或 CFO 离任/继任事件" if zh
                      else "CEO or CFO departure / succession event"),
                monitorable=True,
                check_frequency="monthly",
            ),
            DisconfirmingTrigger(
                text=("超出正常解禁节奏的内部人大额减持" if zh
                      else "Material insider selling exceeding normal vesting schedule"),
                monitorable=True,
                check_frequency="monthly",
            ),
            DisconfirmingTrigger(
                text=("新披露对收入有重大影响的关联交易" if zh
                      else "New related-party transaction disclosed with material revenue impact"),
                monitorable=True,
                check_frequency="quarterly",
            ),
            DisconfirmingTrigger(
                text=("针对公司治理或关联方的监管行动" if zh
                      else "Regulatory action targeting corporate governance or related parties"),
                monitorable=True,
                check_frequency="quarterly",
            ),
        ]
        return triggers

    def _cognitive_bias_self_check(self, inp: AgentInput) -> CognitiveBiasSelfCheck:
        zh = is_zh_input(inp)
        return CognitiveBiasSelfCheck(
            anchoring_risk="medium",
            confirmation_bias_risk="medium",
            recency_bias_risk="low",
            narrative_fallacy_risk="high",
            mitigation_steps_taken=(
                [
                    "以 ROIC 量化记录评估管理层，而非依赖声誉",
                    "已明确提示 VIE 与关联交易治理风险",
                    "未将管理层名气等同于管理层质量",
                ] if zh else [
                    "Used ROIC as quantitative track record rather than reputation-based assessment",
                    "Explicitly flagged VIE and related-party governance risks",
                    "Did not equate management reputation with management quality",
                ]
            ),
        )

    def _report_uncertainties(self, inp: AgentInput) -> list[str]:
        zh = is_zh_input(inp)
        uncertainties = (
            [
                "内部人交易数据可能滞后或不完整",
                "董事会独立性评估需要现有数据之外的定性判断",
            ] if zh else [
                "Insider transaction data may be delayed or incomplete",
                "Board independence assessment requires qualitative judgment beyond available data",
            ]
        )
        if any("vie" in obs for obs in [r.get("relationship_type", "") for r in inp.entity_relationships]):
            uncertainties.append(
                "VIE 合同在中国法律下的可执行性尚未经过大规模司法检验" if zh
                else "VIE enforceability under Chinese law remains legally untested at scale"
            )
        return uncertainties
