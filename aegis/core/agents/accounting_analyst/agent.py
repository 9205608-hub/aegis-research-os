"""Accounting Analyst Agent — Section 19.2.

Responsibilities:
- Earnings quality assessment
- Owner earnings bridge
- Dilution mechanics
- Tax normalization
- Accounting red flags
- Working capital analysis
- Accrual quality
- Off-balance-sheet exposure
- Cross-standard adjustment recommendation

Prohibitions:
- No SBC + dilution double penalty
- No OCF/NI = earnings quality shortcut
- No non-GAAP replacing GAAP without explicit bridge
- No ignoring cross-standard differences
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


class AccountingAnalyst(AgentBase):
    """Specialist agent for accounting quality and earnings analysis."""

    AGENT_NAME = "accounting_analyst"
    AGENT_VERSION = "0.1.0"

    # Metrics this agent specifically inspects
    FOCUS_METRICS = frozenset({
        "gross_margin", "operating_margin", "net_margin", "ebitda_margin",
        "sbc_to_revenue", "dilution_rate", "accruals_ratio",
        "cfo_to_net_income", "nwc", "fcf_simple", "capex_to_revenue",
    })

    # Red-flag thresholds
    ACCRUAL_RATIO_WARN = 0.10
    SBC_TO_REVENUE_WARN = 0.15
    CFO_NI_FLOOR = 0.50  # CFO/NI below this is a red flag

    def _extract_observations(self, inp: AgentInput) -> list[Observation]:
        observations: list[Observation] = []
        metrics = inp.metric_results
        zh = is_zh_input(inp)

        # Earnings quality observations
        if "accruals_ratio" in metrics:
            val = metrics["accruals_ratio"]
            is_poor = abs(val) > self.ACCRUAL_RATIO_WARN
            if zh:
                text = (
                    f"应计比率为 {val:.4f}，盈利质量较差，利润含金量存疑"
                    if is_poor else
                    f"应计比率为 {val:.4f}，盈利质量尚可，应计项处于正常区间"
                )
            else:
                quality = "poor" if is_poor else "acceptable"
                text = f"Accruals ratio is {val:.4f}, indicating {quality} earnings quality"
            observations.append(Observation(
                text=text,
                source_ids=[f"metric:accruals_ratio:{inp.entity_id}"],
            ))

        if "cfo_to_net_income" in metrics:
            val = metrics["cfo_to_net_income"]
            is_low = val < self.CFO_NI_FLOOR
            if zh:
                flag = "低于现金转化警戒线，盈利现金含量不足" if is_low else "现金转化健康"
                text = f"经营现金流/净利润为 {val:.2f}——{flag}"
            else:
                flag = "below cash conversion floor" if is_low else "healthy"
                text = f"CFO/Net Income ratio is {val:.2f} — {flag}"
            observations.append(Observation(
                text=text,
                source_ids=[f"metric:cfo_to_net_income:{inp.entity_id}"],
            ))

        # Dilution observations
        if "sbc_to_revenue" in metrics:
            val = metrics["sbc_to_revenue"]
            is_elevated = val > self.SBC_TO_REVENUE_WARN
            if zh:
                level = "偏高" if is_elevated else "温和"
                text = f"SBC/营收为 {val:.2%}，股权激励摊薄程度{level}"
            else:
                level = "elevated" if is_elevated else "moderate"
                text = f"SBC/Revenue is {val:.2%}, dilution level is {level}"
            observations.append(Observation(
                text=text,
                source_ids=[f"metric:sbc_to_revenue:{inp.entity_id}"],
            ))

        if "dilution_rate" in metrics:
            val = metrics["dilution_rate"]
            observations.append(Observation(
                text=(f"年化股本摊薄率为 {val:.2%}" if zh
                      else f"Annual dilution rate is {val:.2%}"),
                source_ids=[f"metric:dilution_rate:{inp.entity_id}"],
            ))

        # Margin observations
        _margin_zh = {"gross_margin": "毛利率", "operating_margin": "营业利润率",
                      "net_margin": "净利率"}
        for margin_key in ("gross_margin", "operating_margin", "net_margin"):
            if margin_key in metrics:
                observations.append(Observation(
                    text=(f"{_margin_zh[margin_key]}为 {metrics[margin_key]:.2%}" if zh
                          else f"{margin_key} is {metrics[margin_key]:.2%}"),
                    source_ids=[f"metric:{margin_key}:{inp.entity_id}"],
                ))

        # Working capital
        if "nwc" in metrics:
            observations.append(Observation(
                text=(f"净营运资本为 {metrics['nwc']:,.0f}" if zh
                      else f"Net working capital is {metrics['nwc']:,.0f}"),
                source_ids=[f"metric:nwc:{inp.entity_id}"],
            ))

        # Evidence-based observations
        for ep in inp.evidence_packets:
            if ep.get("assertion_type") in ("accounting_quality", "earnings_quality",
                                             "related_party", "off_balance_sheet"):
                observations.append(Observation(
                    text=ep.get("assertion_text", ""),
                    source_ids=[ep.get("evidence_id", "")],
                ))

        return observations

    def _derive_inferences(
        self, observations: list[Observation], inp: AgentInput
    ) -> list[Inference]:
        inferences: list[Inference] = []
        metrics = inp.metric_results
        zh = is_zh_input(inp)

        # Earnings quality inference
        accrual_obs_idx = None
        cfo_obs_idx = None
        for i, obs in enumerate(observations):
            t = obs.text.lower()
            if "accruals_ratio" in t or "应计比率" in obs.text:
                accrual_obs_idx = i
            if "cfo/net income" in t or "经营现金流/净利润" in obs.text:
                cfo_obs_idx = i

        quality_indices = [i for i in (accrual_obs_idx, cfo_obs_idx) if i is not None]
        if quality_indices:
            high_accrual = abs(metrics.get("accruals_ratio", 0)) > self.ACCRUAL_RATIO_WARN
            low_cfo = metrics.get("cfo_to_net_income", 1.0) < self.CFO_NI_FLOOR
            if high_accrual or low_cfo:
                inferences.append(Inference(
                    text=("盈利质量欠佳——报表利润可能高估真实经营成果，利润含金量存疑" if zh
                          else "Earnings quality is below par — reported earnings may overstate economic reality"),
                    based_on_observation_indices=quality_indices,
                    confidence="medium" if (high_accrual != low_cfo) else "high",
                ))
            else:
                inferences.append(Inference(
                    text=("盈利质量整体稳健——应计项与现金转化均处于正常区间" if zh
                          else "Earnings quality appears sound — accruals and cash conversion are within normal ranges"),
                    based_on_observation_indices=quality_indices,
                    confidence="medium",
                ))

        # Dilution inference — MUST NOT double-count with SBC
        sbc_obs_idx = None
        dilution_obs_idx = None
        for i, obs in enumerate(observations):
            t = obs.text.lower()
            if "sbc/revenue" in t or "sbc/营收" in t:
                sbc_obs_idx = i
            if "dilution rate" in t or "股本摊薄率" in obs.text:
                dilution_obs_idx = i

        if sbc_obs_idx is not None and dilution_obs_idx is not None:
            inferences.append(Inference(
                text=(
                    "SBC 与股本摊薄须分开计量——估值时只能在 SBC 费用扣减与摊薄后股本"
                    "之间二选一，严禁同时计入（重复惩罚禁令）"
                    if zh else
                    "SBC and dilution are measured separately — "
                    "valuation must use EITHER SBC expense deduction OR diluted share count, "
                    "never both simultaneously (double-counting prohibition)"
                ),
                based_on_observation_indices=[sbc_obs_idx, dilution_obs_idx],
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
            if "below par" in t or "盈利质量欠佳" in inf.text:
                counterargs.append(Counterargument(
                    text=(
                        "高应计可能反映正当的成长期投入（如 IFRS 下研发支出资本化），"
                        "未必等同于利润操纵"
                        if zh else
                        "High accruals may reflect legitimate growth investment "
                        "(e.g., capitalized R&D under IFRS) rather than manipulation"
                    ),
                    strength="moderate",
                    evidence_ids=[],
                ))
            elif ("sound" in t and "earnings quality" in t) or "盈利质量整体稳健" in inf.text:
                counterargs.append(Counterargument(
                    text=(
                        "整体指标可能掩盖分部层面的问题——"
                        "单一高盈利分部可能在补贴其他亏损业务"
                        if zh else
                        "Aggregate metrics can mask segment-level issues — "
                        "a single profitable segment may subsidize losses elsewhere"
                    ),
                    strength="weak",
                    evidence_ids=[],
                ))

        # Always include at least one cross-standard counterargument
        counterargs.append(Counterargument(
            text=(
                "收入确认、研发费用处理及政府补助分类在不同会计准则下存在差异，"
                "可能影响指标的可比性"
                if zh else
                "Cross-standard differences in revenue recognition, R&D treatment, "
                "and government subsidy classification may affect metric comparability"
            ),
            strength="moderate",
            evidence_ids=[],
        ))

        return counterargs

    def _identify_disconfirming_triggers(
        self, inferences: list[Inference], inp: AgentInput
    ) -> list[DisconfirmingTrigger]:
        triggers: list[DisconfirmingTrigger] = []
        zh = is_zh_input(inp)

        triggers.append(DisconfirmingTrigger(
            text=("前期财务报表发生重述" if zh
                  else "Restatement of prior-period financials"),
            monitorable=True,
            check_frequency="quarterly",
        ))
        triggers.append(DisconfirmingTrigger(
            text=("更换审计机构或被出具非标准审计意见" if zh
                  else "Auditor change or qualified audit opinion"),
            monitorable=True,
            check_frequency="annually",
        ))
        triggers.append(DisconfirmingTrigger(
            text=("披露此前未报告的重大关联交易" if zh
                  else "Material related-party transaction disclosure not previously reported"),
            monitorable=True,
            check_frequency="quarterly",
        ))

        return triggers

    def _cognitive_bias_self_check(self, inp: AgentInput) -> CognitiveBiasSelfCheck:
        zh = is_zh_input(inp)
        return CognitiveBiasSelfCheck(
            anchoring_risk="medium",
            confirmation_bias_risk="low",
            recency_bias_risk="medium",
            narrative_fallacy_risk="low",
            mitigation_steps_taken=(
                [
                    "采用应计比率与经营现金流/净利润双指标交叉验证盈利质量，而非依赖单一指标",
                    "已明确提示 SBC 与股本摊薄的重复计量风险",
                    "已提示跨会计准则可比性问题",
                ] if zh else [
                    "Used multiple earnings quality metrics (accruals + CFO/NI) rather than single indicator",
                    "Explicitly flagged SBC/dilution double-counting risk",
                    "Included cross-standard comparability caveat",
                ]
            ),
        )

    def _report_uncertainties(self, inp: AgentInput) -> list[str]:
        zh = is_zh_input(inp)
        uncertainties = [
            "应计质量的判断标准因会计准则与行业惯例而异" if zh
            else "Accruals quality may vary by reporting standard and industry norms",
        ]
        if inp.sector_pack and inp.sector_pack.get("sector_name") in ("China Internet",):
            uncertainties.append(
                "中国会计准则下政府补助的列报方式可能掩盖经营性盈利质量" if zh
                else "CAS government subsidies classification may obscure operating earnings quality"
            )
        return uncertainties
