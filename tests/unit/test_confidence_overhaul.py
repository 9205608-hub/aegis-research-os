"""AUDIT 2026-07-12 大整改回归 — Phase A（估值与文本诚信）。

Grok 20-audit sweep 的最高频硬伤，逐条最小复现：
- 双向 valuation sanity（宁德式 base 10× price 此前静默通过所有检查）
- % 清洗自关闭（sanctioned 回报全部 |>90%| 时恰好在最需要时跳过）
- % 清洗只告警不改写（误导性 % 原样进报告）
- scrubber 只覆盖 6 个叙述字段（counter_thesis 等渲染字段藏"残影"）
- str.replace(token, tag, 1) 首次出现错位替换
- 失配时 variant_magnitude 的确定性诚实降级
"""

from aegis.core.chief_analyst.thesis_synthesizer import (
    _VALUE_CLAIM_FIELDS,
    _magnitude_mismatch_disclosure,
    _scrub_fair_value_claims,
    _valuation_sanity_verdict,
)
from aegis.core.publish_gate.gate import PublishGate
from aegis.core.truth.valuation_sanity import MISMATCH_RATIO, check_valuation_sanity

MKT = {"current_price": 349.0}

# 宁德时代式失配：DCF 三档全部在市价 8-15× 之外
MISMATCH_SCEN = {
    "currency": "CNY",
    "bear_value": 2800.0,
    "base_value": 4000.0,
    "bull_value": 5200.0,
    "probability_weighted_value": 4100.0,
}

# 正常口径：三档围绕市价 ±50% 内
SANE_SCEN = {
    "currency": "CNY",
    "bear_value": 250.0,
    "base_value": 380.0,
    "bull_value": 520.0,
    "probability_weighted_value": 390.0,
}


class TestCheckValuationSanity:
    def test_10x_over_is_mismatch(self):
        v = check_valuation_sanity(4000.0, 349.0)
        assert v["mismatch"] is True
        assert v["ratio"] > 10

    def test_deep_discount_is_mismatch(self):
        assert check_valuation_sanity(20.0, 349.0)["mismatch"] is True

    def test_band_edges_are_sane(self):
        assert check_valuation_sanity(380.0, 349.0)["mismatch"] is False
        assert check_valuation_sanity(349.0 * 2.0, 349.0)["mismatch"] is False
        assert check_valuation_sanity(349.0 / 2.0, 349.0)["mismatch"] is False

    def test_beyond_ratio_threshold_flips(self):
        assert check_valuation_sanity(349.0 * (MISMATCH_RATIO + 0.1), 349.0)["mismatch"]

    def test_nonpositive_base_is_mismatch(self):
        v = check_valuation_sanity(-0.5, 349.0)
        assert v["mismatch"] is True and v["ratio"] == 0.0

    def test_unusable_inputs_return_none(self):
        assert check_valuation_sanity(380.0, 0) is None
        assert check_valuation_sanity(None, 349.0) is None
        assert check_valuation_sanity("x", 349.0) is None


class TestValuationSanityGate:
    def test_blocks_on_mismatch(self):
        chk = PublishGate()._valuation_sanity_gate(
            {"scenarios": MISMATCH_SCEN, "market_data": MKT}
        )
        assert chk.passed is False
        assert chk.severity == "block"
        assert "sanity band" in chk.message

    def test_passes_in_band(self):
        chk = PublishGate()._valuation_sanity_gate(
            {"scenarios": SANE_SCEN, "market_data": MKT}
        )
        assert chk.passed is True

    def test_skips_as_warn_when_inputs_missing(self):
        chk = PublishGate()._valuation_sanity_gate({})
        assert chk.passed is True
        assert chk.severity == "warn"

    def test_prefers_orchestrator_stamp(self):
        # 已盖章 mismatch=False 时即使原始数字失配也放行——单一真源。
        scen = dict(MISMATCH_SCEN)
        scen["valuation_sanity"] = {
            "mismatch": False, "ratio": 1.0,
            "base_value": 4000.0, "market_price": 349.0,
        }
        chk = PublishGate()._valuation_sanity_gate(
            {"scenarios": scen, "market_data": MKT}
        )
        assert chk.passed is True

    def test_evaluate_wires_gate_into_blocked_by(self):
        result = PublishGate().evaluate(
            [], [], context={"scenarios": MISMATCH_SCEN, "market_data": MKT},
        )
        assert "valuation_sanity_gate" in result.blocked_by


class TestStrictScrub:
    def test_strict_scrubs_even_sanctioned_scenario_values(self):
        raw = {"core_thesis": "概率加权DCF基准值仅¥4100/股，对应估值缺口巨大。"}
        out, warns = _scrub_fair_value_claims(
            raw, MISMATCH_SCEN, MKT, fields=("core_thesis",), strict=True,
        )
        assert "¥4100" not in out["core_thesis"]
        assert "〔详见DCF情景估值〕" in out["core_thesis"]

    def test_normal_mode_keeps_sanctioned_value(self):
        raw = {"core_thesis": "概率加权DCF基准值仅¥4100/股，对应估值缺口巨大。"}
        out, _ = _scrub_fair_value_claims(
            raw, MISMATCH_SCEN, MKT, fields=("core_thesis",), strict=False,
        )
        assert "¥4100" in out["core_thesis"]

    def test_strict_keeps_market_price(self):
        raw = {"core_thesis": "市价¥349.00附近的定价隐含了过高的估值预期。"}
        out, _ = _scrub_fair_value_claims(
            raw, MISMATCH_SCEN, MKT, fields=("core_thesis",), strict=True,
        )
        assert "¥349.00" in out["core_thesis"]

    def test_strict_scrubs_directional_return_pct(self):
        # 失配情形下 sanctioned 回报全部 |>90%|——旧逻辑在这里自我关闭，
        # 「70%下行」原样进报告；strict 模式必须改写。
        raw = {"variant_magnitude": "我们测算该股存在70%下行空间，风险回报严重不对称。"}
        out, warns = _scrub_fair_value_claims(
            raw, MISMATCH_SCEN, MKT, fields=("variant_magnitude",), strict=True,
        )
        assert "70%" not in out["variant_magnitude"]
        assert "〔估值失配·幅度结论已停用〕" in out["variant_magnitude"]
        assert any("% RETURN CONSISTENCY" in w for w in warns)

    def test_nonstrict_worthless_dcf_concession_preserved(self):
        # 非 strict 时保留 BUG-A15 的让步：全 |>90%| 回报（DCF 视同归零）
        # 不启动 % 清洗——替代框架的 % 此时反而有信息量。
        raw = {"variant_magnitude": "我们测算该股存在70%下行空间。"}
        out, warns = _scrub_fair_value_claims(
            raw, MISMATCH_SCEN, MKT, fields=("variant_magnitude",), strict=False,
        )
        assert "70%" in out["variant_magnitude"]
        assert not any("% RETURN CONSISTENCY" in w for w in warns)

    def test_normal_mode_pct_now_rewritten_not_just_warned(self):
        # AUDIT 2026-07-12：% 检查此前只告警不改写——误导 % 停留在正文。
        raw = {"core_thesis": "我们认为存在90%下行空间。"}
        out, warns = _scrub_fair_value_claims(
            raw, SANE_SCEN, MKT, fields=("core_thesis",), strict=False,
        )
        assert "90%" not in out["core_thesis"]
        assert "〔回报口径详见DCF情景〕" in out["core_thesis"]
        assert any("rewritten in-place" in w for w in warns)


class TestFieldCoverage:
    def test_all_narrative_fields_in_scope(self):
        for f in (
            "key_assumption_disagreement", "counter_thesis",
            "conviction_narrative", "management_quality_summary",
            "capital_allocation_assessment", "edge_source",
            "hypothesis_evolution", "biggest_surprise", "why_now",
            "what_would_change_my_mind",
        ):
            assert f in _VALUE_CLAIM_FIELDS

    def test_counter_thesis_scrubbed_by_default_fields(self):
        # "残影"通道：counter_thesis 不在旧 6 字段集里，编造的 ¥800
        # 逐字进 HTML。默认字段集（fields=None）现在必须覆盖它。
        raw = {"counter_thesis": "反方认为合理估值应为¥800，仍具上行。"}
        out, warns = _scrub_fair_value_claims(raw, SANE_SCEN, MKT)
        assert "¥800" not in out["counter_thesis"]
        assert any("counter_thesis" in w for w in warns)


class TestSpanReplacement:
    def test_offending_occurrence_replaced_not_first(self):
        # 同一 token 出现两次：第一次是单位经济学叙述（fair-value 上下文
        # 窗口外，豁免），第二次是公允价值主张（命中）。旧 str.replace
        # (token, tag, 1) 会错杀第一处、放过第二处。
        text = (
            "每投入¥888可带来两倍的现金回收效率，这一单位经济表现持续改善，"
            "同时费用端弹性充足，经营韧性可观，现金转换周期同步缩短，"
            "而据此测算的合理估值应为¥888，显著高于市价。"
        )
        raw = {"core_thesis": text}
        out, warns = _scrub_fair_value_claims(
            raw, SANE_SCEN, MKT, fields=("core_thesis",),
        )
        result = out["core_thesis"]
        assert result.startswith("每投入¥888")          # 第一处保留
        assert result.count("¥888") == 1                 # 第二处被替换
        assert "〔详见DCF情景估值〕" in result


class TestMagnitudeDisclosure:
    def test_zh_disclosure_content(self):
        sanity = check_valuation_sanity(4000.0, 349.0)
        text = _magnitude_mismatch_disclosure(sanity, zh=True)
        assert "估值失配" in text
        assert "不提供目标价" in text
        assert "model bug > market bug" in text
        assert "条件化预期表" in text

    def test_en_disclosure_content(self):
        sanity = check_valuation_sanity(4000.0, 349.0)
        text = _magnitude_mismatch_disclosure(sanity, zh=False)
        assert "Valuation mismatch" in text
        assert "model bug > market bug" in text

    def test_verdict_prefers_stamp_then_recomputes(self):
        stamped = {"mismatch": True, "ratio": 11.5,
                   "base_value": 4000.0, "market_price": 349.0}
        scen = dict(SANE_SCEN)
        scen["valuation_sanity"] = stamped
        assert _valuation_sanity_verdict(scen, MKT) is stamped
        # 无盖章时按 base/price 重算（replay / 单测路径）
        v = _valuation_sanity_verdict(MISMATCH_SCEN, MKT)
        assert v and v["mismatch"] is True


# ═══ Phase B（契约语义与决策门）═══════════════════════════════════════

from types import SimpleNamespace

from aegis.core.decision_engine.engine import (
    DecisionEngine,
    EVIDENCE_GAP_CONFLICT_THRESHOLD,
    evidence_gap_hits,
)
from aegis.core.thesis import build_thesis_contract
from aegis.data_contracts.judgment_schema import (
    CognitiveBiasSelfCheck,
    Counterargument,
    DisconfirmingTrigger,
    Inference,
    JudgmentContract,
    Observation,
)


def _mk_judgment(agent="risk_analyst", triggers=None, n_evidence=0, n_obs=1):
    return JudgmentContract(
        judgment_id=f"j_{agent}",
        agent_name=agent,
        agent_version="v1",
        question_id="q",
        run_id="run_t",
        judgment_status="complete",
        observations=[
            Observation(text=f"obs{i}", source_ids=["fact:x"])
            for i in range(max(1, n_obs))
        ],
        inferences=[Inference(
            text="inf", confidence="medium", based_on_observation_indices=[0],
        )],
        counterarguments=[Counterargument(text="counter", strength="moderate")],
        disconfirming_triggers=[
            DisconfirmingTrigger(text=t) for t in (triggers or [])
        ],
        used_evidence_ids=[f"ev{i}" for i in range(n_evidence)],
        cognitive_bias_self_check=CognitiveBiasSelfCheck(
            anchoring_risk="low", confirmation_bias_risk="low",
            recency_bias_risk="low", narrative_fallacy_risk="low",
        ),
    )


class TestKillCriteriaQuantification:
    """B1：kill = 证伪触发器中带可执行量化阈值的子集。"""

    def test_quantified_trigger_promoted_with_threshold(self):
        j = _mk_judgment(triggers=["2026年任一季度营收同比增速低于80%即触发重估"])
        kc = DecisionEngine()._extract_kill_criteria([j])
        assert len(kc) == 1
        assert "低于" in kc[0]["threshold"] and "80" in kc[0]["threshold"]
        assert kc[0]["threshold"] != "trigger event"

    def test_duration_pattern_promoted(self):
        j = _mk_judgment(triggers=["存货周转天数连续两个季度环比上升"])
        kc = DecisionEngine()._extract_kill_criteria([j])
        assert len(kc) == 1 and "连续" in kc[0]["threshold"]

    def test_english_comparator_promoted(self):
        j = _mk_judgment(triggers=["CFO/NI falls below 0.6 for two quarters"])
        kc = DecisionEngine()._extract_kill_criteria([j])
        assert len(kc) == 1 and "below" in kc[0]["threshold"]

    def test_qualitative_trigger_not_promoted(self):
        j = _mk_judgment(triggers=["管理层出现诚信问题", "行业竞争格局恶化"])
        assert DecisionEngine()._extract_kill_criteria([j]) == []

    def test_qualitative_trigger_kept_in_disconfirming(self):
        j = _mk_judgment(triggers=["管理层出现诚信问题"])
        dts = DecisionEngine()._extract_disconfirming_triggers([j])
        assert dts == ["管理层出现诚信问题"]

    def test_non_risk_agent_not_in_kill_but_in_disconfirm(self):
        j = _mk_judgment(agent="business_analyst", triggers=["毛利率跌破30%"])
        assert DecisionEngine()._extract_kill_criteria([j]) == []
        assert DecisionEngine()._extract_disconfirming_triggers([j]) == ["毛利率跌破30%"]


class TestContractWiring:
    """B1/B5/A4：合约装配接真值。"""

    def test_disconfirming_triggers_carry_agent_triggers(self):
        c = build_thesis_contract(
            entity_id="600519", run_id="run_x",
            disconfirming_triggers=["单季营收增速低于80%", "毛利率环比降超3pp"],
        )
        assert "单季营收增速低于80%" in c.disconfirming_triggers
        assert "毛利率环比降超3pp" in c.disconfirming_triggers

    def test_bias_status_wired_and_normalized(self):
        c = build_thesis_contract(
            entity_id="600519", run_id="run_x", bias_check_status="warned",
        )
        assert c.bias_check_status == "warned"
        c2 = build_thesis_contract(
            entity_id="600519", run_id="run_x", bias_check_status="nonsense",
        )
        assert c2.bias_check_status == "passed"  # legacy 容错默认

    def test_valuation_assumptions_appendix(self):
        scen = dict(MISMATCH_SCEN)
        scen["dcf_assumptions"] = {
            "wacc": 0.095, "terminal_growth_rate": 0.03,
            "capex_to_revenue_path": [0.10] * 10,
        }
        scen["dcf_bridge"] = {
            "net_debt": -5.0e9, "future_shares": 4.4e9,
            "enterprise_value": 1.6e13, "pv_terminal_value": 1.2e13,
        }
        scen["valuation_sanity"] = {"mismatch": True, "ratio": 11.47}
        c = build_thesis_contract(
            entity_id="300750", run_id="run_x", scenarios=scen,
        )
        va = c.valuation_assumptions
        assert va["wacc"] == 0.095
        assert va["terminal_growth_rate"] == 0.03
        assert va["forecast_years"] == 10
        assert va["terminal_value_pct_of_ev"] == 0.75
        assert va["shares_outstanding"] == 4.4e9
        assert va["per_share_base"] == 4000.0
        assert va["valuation_sanity"] == {"mismatch": True, "ratio": 11.47}

    def test_valuation_assumptions_absent_when_no_scenarios(self):
        c = build_thesis_contract(entity_id="600519", run_id="run_x")
        assert c.valuation_assumptions is None


_EDGE_BLOB = (
    "我们的 edge 来自前两大客户的营收集中度极高这一判断——客户集中度是"
    "利润率护城河的核心变量；同时产品代际毛利率拆分显示800G溢价不可持续。"
)


class TestEvidenceGap:
    """B3：edge 建在 open_questions 上 → 不得干净发布。"""

    def test_overlapping_questions_hit(self):
        qs = [
            "前两大客户的营收集中度及应收账款集中度具体是多少？",
            "800G和1.6T光模块各自的营收占比及毛利率差异？需要产品代际维度的利润率分解。",
        ]
        hits = evidence_gap_hits(_EDGE_BLOB, qs)
        assert len(hits) == 2

    def test_unrelated_questions_do_not_hit(self):
        qs = ["公司历年分红率是多少？", "海外产能基地建设进度如何？"]
        assert evidence_gap_hits(_EDGE_BLOB, qs) == []

    def test_decide_downgrades_on_evidence_gap(self):
        from aegis.core.chief_analyst.thesis_synthesizer import SynthesizedThesis
        st = SynthesizedThesis(
            core_thesis=_EDGE_BLOB, my_variant=_EDGE_BLOB,
            variant_magnitude="方向性观点",
            variant_decomposition_narrative="分解",
            why_now="现在", market_implied_story="市场故事",
            key_assumption_disagreement="客户集中度与产品代际毛利率",
            counter_thesis="反方", why_market_is_wrong="市场错了",
            what_would_change_my_mind="改变想法",
            edge_source=_EDGE_BLOB, edge_durability="medium_term",
        )
        decision = DecisionEngine().decide(
            "300502", "run_t", [_mk_judgment(triggers=["营收增速低于80%"])],
            [], True,
            context={"open_questions": [
                {"question": "前两大客户的营收集中度及应收账款集中度具体是多少？"},
                {"question": "800G和1.6T光模块各自的营收占比及毛利率差异？需要产品代际维度的利润率分解。"},
            ]},
            synthesized_thesis=st,
        )
        assert decision.publishing_status == "downgraded"
        assert any(c.topic == "evidence_gap" for c in decision.unresolved_conflicts)
        assert decision.confidence_bucket in ("very_low", "low", "medium")

    def test_decide_publishes_clean_when_no_gap(self):
        from aegis.core.chief_analyst.thesis_synthesizer import SynthesizedThesis
        st = SynthesizedThesis(
            core_thesis="论点", my_variant="变异", variant_magnitude="幅度",
            variant_decomposition_narrative="分解", why_now="现在",
            market_implied_story="市场", key_assumption_disagreement="分歧",
            counter_thesis="反方", why_market_is_wrong="错",
            what_would_change_my_mind="改变", edge_source="来源",
            edge_durability="medium_term",
        )
        decision = DecisionEngine().decide(
            "300502", "run_t", [_mk_judgment(triggers=["营收增速低于80%"])],
            [], True,
            context={"open_questions": [
                {"question": "公司历年分红率是多少？"},
            ]},
            synthesized_thesis=st,
        )
        assert decision.publishing_status == "published"


class TestConfidenceGateSkips:
    """B4：缺数据 skip 的门 → published 也不得 high。"""

    def test_gate_skips_cap_confidence_at_medium(self):
        de = DecisionEngine()
        strong = [_mk_judgment(n_evidence=20, n_obs=10)]
        no_skip = de._determine_confidence(
            strong, [], publishing_status="published",
            publish_gate_passed=True, gate_skipped_count=0,
        )
        with_skip = de._determine_confidence(
            strong, [], publishing_status="published",
            publish_gate_passed=True, gate_skipped_count=2,
        )
        assert no_skip == "high"
        assert with_skip == "medium"


class TestDeepSeekRoleTemperature:
    """B4：温度按 role 表传递（critic 必须 0.0）。"""

    def test_critic_role_zero_temperature(self):
        from aegis.core.llm.deepseek_client import DeepSeekClient
        assert DeepSeekClient._temperature_for_role("critic") == 0.0
        assert DeepSeekClient._temperature_for_role("cognitive_bias_critic") == 0.0
        assert DeepSeekClient._temperature_for_role("specialist_agent") == 0.2
        assert DeepSeekClient._temperature_for_role("unknown_role") == 0.2


class TestLLMJudgeBlockReproduction:
    """B4：block 级 finding 必须复现两次才计入（防单次抖动翻转决策门）。"""

    @staticmethod
    def _finding():
        return {
            "issue_type": "cagr_mismatch", "agent_name": "risk_analyst",
            "claimed_value": "50%", "correct_value": "20%",
            "explanation": "cagr wrong", "claimed_text": "CAGR 50%",
            "source_label": "inf[0]",
        }

    def _judge(self, responses):
        from aegis.core.critics.llm_judge_critic.critic import LLMJudgeCritic

        class _StubJudge(LLMJudgeCritic):
            def __init__(self, rs):
                super().__init__()
                self._rs = list(rs)

            def _call_llm(self, user_message, shared_client=None):
                return self._rs.pop(0)

        return _StubJudge(responses)

    def test_unreproduced_block_demoted_to_warn(self):
        judge = self._judge([[self._finding()], []])
        result = judge.review([_mk_judgment(triggers=["x低于1%"])], context={})
        assert len(result.issues) == 1
        assert result.issues[0].severity == "warn"
        assert "did not reproduce" in result.issues[0].message
        assert result.block_publish is False

    def test_reproduced_block_stays_block(self):
        judge = self._judge([[self._finding()], [self._finding()]])
        result = judge.review([_mk_judgment(triggers=["x低于1%"])], context={})
        assert result.issues[0].severity == "block"
        assert result.block_publish is True


class TestKillBinaryEvents:
    """B1 补充：二元可观察事件（无数字）同样是可执行 kill。"""

    def test_binary_event_promoted_with_named_threshold(self):
        j = _mk_judgment(triggers=["信用评级下调或展望转负", "债务契约违约或申请豁免"])
        kc = DecisionEngine()._extract_kill_criteria([j])
        assert len(kc) == 2
        assert kc[0]["threshold"].startswith("事件触发：")

    def test_confirmation_style_signal_not_promoted(self):
        j = _mk_judgment(triggers=["获得两家头部云厂商量产认证", "新签大额订单公告"])
        assert DecisionEngine()._extract_kill_criteria([j]) == []


# ═══ Round 2（Grok 复审第一轮反馈的修复）═══════════════════════════════

from aegis.core.orchestrator.auto_research import build_margin_scenarios
from aegis.core.chief_analyst.thesis_synthesizer import valuation_constraint_block


class TestSectorMedianFakeAnchor:
    """R2-6：错配 sector pack 的中位数不得当上行锚（002371 案例）。"""

    def test_cross_model_median_dropped(self):
        # 设备商 14.7% OPM 配到 fabless pack（中位 50%）→ 只出维持现状档
        pack = {"valuation_framework": {"typical_operating_margin_range": [0.35, 0.65]}}
        scens = build_margin_scenarios(0.147, pack, zh=True)
        assert len(scens) == 1 and scens[0][0] == "维持现状"

    def test_recovery_median_kept(self):
        # 修复股（康达 2.9% → 行业 20%）合理修复情景保留三档
        pack = {"valuation_framework": {"typical_operating_margin_range": [0.15, 0.25]}}
        scens = build_margin_scenarios(0.029, pack, zh=True)
        assert len(scens) == 3

    def test_downward_median_kept(self):
        # 向下的行业档（均值回归/熊市检验）不受限
        pack = {"valuation_framework": {"typical_operating_margin_range": [0.35, 0.65]}}
        scens = build_margin_scenarios(0.667, pack, zh=True)
        assert len(scens) == 3


class TestValuationConstraintBlock:
    """R2-1/R2-7：事前注入的估值引用规则。"""

    def test_mismatch_block_forbids_dcf_citation(self):
        block = valuation_constraint_block(MISMATCH_SCEN, MKT)
        assert "FAILED its magnitude sanity check" in block
        assert "Do NOT cite ANY DCF-derived number" in block
        assert "HORIZON RULE" in block

    def test_sane_block_lists_sanctioned_values(self):
        block = valuation_constraint_block(SANE_SCEN, MKT)
        assert "380.00" in block and "390.00" in block
        assert "FAILED" not in block
        assert "HORIZON RULE" in block

    def test_empty_scenarios_no_block(self):
        assert valuation_constraint_block(None, MKT) == ""


class TestSynthesizerKillPriority:
    """R2-2：synthesizer 方向正确的结构化 kill 优先；risk 路径为回退。"""

    def _st(self, kills):
        from types import SimpleNamespace
        return SimpleNamespace(kill_criteria=kills)

    def test_synthesizer_kills_preferred(self):
        st = self._st([{
            "description": "单季营收同比增速低于80%",
            "threshold": "低于80%", "check_frequency": "quarterly",
        }])
        j = _mk_judgment(triggers=["信用评级下调或展望转负"])
        kc = DecisionEngine()._extract_kill_criteria([j], st)
        assert len(kc) == 1 and kc[0]["threshold"] == "低于80%"

    def test_unquantified_synth_kill_filtered_falls_back(self):
        st = self._st([{
            "description": "行业景气度恶化",  # 无量化锚也非二元事件
            "threshold": "观察", "check_frequency": "quarterly",
        }])
        j = _mk_judgment(triggers=["信用评级下调或展望转负"])
        kc = DecisionEngine()._extract_kill_criteria([j], st)
        assert len(kc) == 1 and kc[0]["threshold"].startswith("事件触发：")

    def test_no_synth_thesis_falls_back(self):
        j = _mk_judgment(triggers=["毛利率跌破30%即触发重估"])
        kc = DecisionEngine()._extract_kill_criteria([j], None)
        assert len(kc) == 1


class TestDisconfirmDedup:
    """R2-3：证伪触发器聚类去重 + 量化版本优先 + 封顶。"""

    def test_near_duplicates_clustered_quantified_wins(self):
        j1 = _mk_judgment(agent="risk_analyst",
                          triggers=["前两大客户的营收集中度出现下降迹象"])
        j2 = _mk_judgment(agent="business_analyst",
                          triggers=["前两大客户营收集中度下降超过10pp即证伪"])
        dts = DecisionEngine()._extract_disconfirming_triggers([j1, j2])
        assert len(dts) == 1
        assert "10pp" in dts[0]  # 量化版本胜出

    def test_cap_at_limit(self):
        js = [_mk_judgment(agent=f"agent{i}", triggers=[
            f"完全不同主题的触发器编号{i}：关于{'甲乙丙丁戊己庚辛壬癸'[i]}业务线的独立观察条目"
        ]) for i in range(10)]
        js += [_mk_judgment(agent=f"agentx{i}", triggers=[
            f"另一批不同主题触发器{i}：涉及{'子丑寅卯辰巳午未申酉'[i]}区域市场的独立信号"
        ]) for i in range(10)]
        dts = DecisionEngine()._extract_disconfirming_triggers(js)
        assert len(dts) <= DecisionEngine._DISCONFIRM_CAP


class TestStrictMultiplesScrub:
    """R2-4：strict 模式下倍数/PE 重估语言被清洗。"""

    def test_pe_rerating_scrubbed_in_strict(self):
        raw = {"core_thesis": "我们认为PE存在从22倍到30倍的重估空间，估值极具吸引力。"}
        out, warns = _scrub_fair_value_claims(
            raw, MISMATCH_SCEN, MKT, fields=("core_thesis",), strict=True,
        )
        assert "30倍" not in out["core_thesis"]

    def test_unit_econ_multiple_survives_strict(self):
        raw = {"core_thesis": "每投入一元可带来两倍的现金回收效率，运营纪律优秀。"}
        out, _ = _scrub_fair_value_claims(
            raw, MISMATCH_SCEN, MKT, fields=("core_thesis",), strict=True,
        )
        assert "两倍" in out["core_thesis"]

    def test_multiples_untouched_outside_strict(self):
        raw = {"core_thesis": "PE存在从22倍到30倍的重估空间。"}
        out, _ = _scrub_fair_value_claims(
            raw, SANE_SCEN, MKT, fields=("core_thesis",), strict=False,
        )
        assert "30倍" in out["core_thesis"]


# ═══ Round 3（引擎数字质量）═══════════════════════════════════════════

from aegis.core.orchestrator.auto_research import (
    MAX_TERMINAL_RATIO,
    cap_cumulative_growth_path,
    max_cumulative_growth_ratio,
)


class TestScaleAwareGrowthCap:
    """R3-1：累计增长封顶按规模分档——巨头不再合法滚到 GDP 量级。"""

    def test_mega_cap_3x(self):
        assert max_cumulative_growth_ratio(4237e8, is_cny=True) == 3.0
        assert max_cumulative_growth_ratio(130e9, is_cny=False) == 3.0

    def test_tiers(self):
        assert max_cumulative_growth_ratio(800e8, is_cny=True) == 6.0
        assert max_cumulative_growth_ratio(300e8, is_cny=True) == 12.0
        assert max_cumulative_growth_ratio(65e8, is_cny=True) == MAX_TERMINAL_RATIO

    def test_garbage_input_keeps_default(self):
        assert max_cumulative_growth_ratio(0, True) == MAX_TERMINAL_RATIO
        assert max_cumulative_growth_ratio(None, True) == MAX_TERMINAL_RATIO

    def test_cap_path_with_tier_ratio(self):
        # 40%/年 的路径在 3× 档下 Y4 触顶切换到终值增速
        path = [0.40] * 10
        capped, year = cap_cumulative_growth_path(path, 0.03, max_ratio=3.0)
        assert year >= 0
        cum = 1.0
        for g in capped:
            cum *= (1 + g)
        assert cum < 3.0 * 1.40 * 1.05  # 触顶年后全部退化为终值增速


class TestKillDisconfirmReconciliation:
    """R3-2：与 kill 同主题的触发器不再以另一套阈值出现在 disconfirm。"""

    def test_same_subject_trigger_dropped(self):
        kills = [{"description": "毛利率连续两个季度低于22%", "threshold": "连续两个季度"}]
        triggers = [
            "毛利率连续两季度跌破25%即证伪",     # 同主题不同阈值 → 去掉
            "欧盟对中国电池加征反补贴关税落地",   # 不同主题 → 保留
        ]
        out = DecisionEngine()._reconcile_disconfirm_with_kills(triggers, kills)
        assert out == ["欧盟对中国电池加征反补贴关税落地"]

    def test_no_kills_passthrough(self):
        triggers = ["任意触发器"]
        assert DecisionEngine()._reconcile_disconfirm_with_kills(triggers, []) == triggers


# ═══ Round 4 ═══════════════════════════════════════════════════════════

from aegis.core.truth.model_free_anchors import build_model_free_implied
from aegis.core.thesis.persistence import _model_free_story


_MFI_META = {
    "shares_outstanding": 4.4e9,
    "__relative_valuation": {
        "insufficient_peers": False,
        "target_pe_ttm": 22.4, "peer_pe_median": 33.3, "pe_percentile": 44,
        "target_pb": 5.1, "peer_pb_median": 3.2, "pb_percentile": 88,
    },
    "__recent_events": {"consensus": {
        "insufficient_coverage": False, "org_count": 32,
        "predictions": [
            {"year": 2026, "eps": 20.7, "net_profit": 948.6e8, "revenue": 5943e8},
            {"year": 2027, "eps": 25.6, "net_profit": 1171.6e8, "revenue": 7267e8},
        ],
    }},
}


class TestModelFreeAnchors:
    """R4-1：DCF 失配时的无模型隐含预期锚。"""

    def test_builds_relval_and_consensus_lines(self):
        out = build_model_free_implied(_MFI_META, {"current_price": 349.0})
        assert out is not None
        blob = " ".join(out["lines_zh"])
        assert "PE(TTM) 22.4×" in blob and "第 44 分位" in blob
        assert "2026E" in blob and "949亿" in blob
        assert "维持现价的条件化表述" in blob
        assert out["sanctioned_pcts"]

    def test_gates_respected(self):
        meta = {"shares_outstanding": 4.4e9,
                "__relative_valuation": {"insufficient_peers": True},
                "__recent_events": {"consensus": {"insufficient_coverage": True}}}
        assert build_model_free_implied(meta, {"current_price": 349.0}) is None

    def test_no_price_none(self):
        assert build_model_free_implied(_MFI_META, {}) is None

    def test_contract_story_prefers_model_free(self):
        mfi = build_model_free_implied(_MFI_META, {"current_price": 349.0})
        story = _model_free_story(mfi)
        assert story.startswith("市场隐含预期（无模型锚")
        c = build_thesis_contract(
            entity_id="300750", run_id="run_x", model_free_implied=mfi,
        )
        assert "无模型锚" in c.market_implied_story

    def test_constraint_block_points_to_anchors(self):
        meta = dict(_MFI_META)
        meta["__model_free_implied"] = {"lines_zh": ["x"]}
        block = valuation_constraint_block(MISMATCH_SCEN, MKT, meta_facts=meta)
        assert "MODEL-FREE" in block and "circular" in block


class TestKillInternalConsistency:
    """R4-4：kill 同指标去重 + 频率期限匹配。"""

    def _st(self, kills):
        return SimpleNamespace(kill_criteria=kills)

    def test_same_metric_kills_deduped(self):
        st = self._st([
            {"description": "毛利率连续两个季度低于22%", "threshold": "低于22%",
             "check_frequency": "quarterly"},
            {"description": "毛利率连续两季度跌破25%", "threshold": "低于25%",
             "check_frequency": "quarterly"},
            {"description": "欧盟反补贴关税超过20%落地", "threshold": "超过20%",
             "check_frequency": "event-driven"},
        ])
        kc = DecisionEngine()._extract_kill_criteria([], st)
        assert len(kc) == 2  # 两条毛利率合一，关税保留
        assert kc[0]["threshold"] == "低于22%"  # 第一条（synthesizer 优先级）胜出

    def test_annual_metric_frequency_corrected(self):
        st = self._st([{
            "description": "2026年全年营收低于¥5200亿",
            "threshold": "低于¥5200亿", "check_frequency": "quarterly",
        }])
        kc = DecisionEngine()._extract_kill_criteria([], st)
        assert kc[0]["check_frequency"] == "annually"


class TestEvidenceGapSensitivity:
    """R4-2：单处强重叠即降级（宁德 R3 带缺口 published 的修正）。"""

    def test_single_hit_now_downgrades(self):
        from aegis.core.chief_analyst.thesis_synthesizer import SynthesizedThesis
        st = SynthesizedThesis(
            core_thesis=_EDGE_BLOB, my_variant="变异", variant_magnitude="幅度",
            variant_decomposition_narrative="分解", why_now="现在",
            market_implied_story="市场", key_assumption_disagreement="分歧",
            counter_thesis="反方", why_market_is_wrong="错",
            what_would_change_my_mind="改变", edge_source="来源",
            edge_durability="medium_term",
        )
        decision = DecisionEngine().decide(
            "300750", "run_t", [_mk_judgment(triggers=["营收增速低于80%"])],
            [], True,
            context={"open_questions": [
                {"question": "前两大客户的营收集中度及应收账款集中度具体是多少？"},
            ]},
            synthesized_thesis=st,
        )
        assert decision.publishing_status == "downgraded"


class TestDriverTreeArchetypeGuard:
    """R5-1：原型限定的驱动分解不得套给其他商业模式（002371 业务对象错误）。"""

    _PACK = {"revenue_drivers": {"decomposition": {
        "applies_to": ["NVDA"],
        "formula": "Revenue = GPU_Units x ASP",
        "tree": [
            {"name": "GPU_Units", "base_value": 8.5, "near_growth": 0.25, "long_growth": 0.08},
            {"name": "GPU_ASP", "base_value": 25000, "near_growth": 0.10, "long_growth": 0.02},
        ],
    }}}

    def _build(self, entity):
        from aegis.core.orchestrator.auto_research import AutoResearchOrchestrator
        return AutoResearchOrchestrator._build_driver_tree(
            AutoResearchOrchestrator.__new__(AutoResearchOrchestrator),
            self._PACK, {"revenue": 3e11}, {}, entity,
        )

    def test_archetype_entity_keeps_tree(self):
        assert self._build("NVDA") is not None

    def test_non_archetype_entity_blocked(self):
        assert self._build("002371") is None

    def test_pack_without_applies_to_unchanged(self):
        pack = {"revenue_drivers": {"decomposition": {
            k: v for k, v in self._PACK["revenue_drivers"]["decomposition"].items()
            if k != "applies_to"
        }}}
        from aegis.core.orchestrator.auto_research import AutoResearchOrchestrator
        tree = AutoResearchOrchestrator._build_driver_tree(
            AutoResearchOrchestrator.__new__(AutoResearchOrchestrator),
            pack, {"revenue": 3e11}, {}, "002371",
        )
        assert tree is not None
