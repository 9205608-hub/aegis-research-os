"""输入退化（LLM 兜底）伪警告与分析缺陷分离计数 — 回归测试 (2026-08-01)。

背景：agent LLM 全部失败后退化到 MockLLMClient 规则模板，critic 对这些
模板判断记的 warn/block 是"输入退化"的系统性伪警告，不是分析缺陷。此前
publish_gate 的 warn 累计阈值（20）与 decision_engine 的置信度扣分不分
真伪一并计数，把整条 run 推向 blocked → 置信度封顶 low。

本文件锁定四组行为：
1. 伪警告不计入 warn 累计阈值（单列统计并在 message 里报告）；
2. 真实分析 warn/block 的行为一个都不变；
3. degraded 输入存在时置信度封顶 medium（与 gate_skipped_count 同语义）；
4. logic_critic 中文利润表述（净利润/归母净利/毛利/营业利润 +
   ¥X亿 / X亿元 / X万元 数字形态）命中 segment ceiling 且不误伤。
"""

from aegis.core.critics.degraded_input import (
    DegradedIssueSplit,
    is_degraded_judgment,
    split_issue_counts,
)
from aegis.core.critics.logic_critic.critic import LogicCritic
from aegis.core.decision_engine.engine import DecisionEngine
from aegis.core.publish_gate.gate import PublishGate
from aegis.data_contracts.critic_result_schema import CriticIssue, CriticResult
from aegis.data_contracts.judgment_schema import (
    CognitiveBiasSelfCheck,
    Counterargument,
    Inference,
    JudgmentContract,
    Observation,
)


# ── 构造工具 ──────────────────────────────────────────────────────────


def _judgment(
    jid: str,
    obs_texts: list[str] | None = None,
    inf_texts: list[str] | None = None,
    n_evidence: int = 0,
) -> JudgmentContract:
    obs_texts = obs_texts or ["常规观察：毛利率稳定。"]
    inf_texts = inf_texts or ["综合观察，结论中性。"]
    return JudgmentContract(
        judgment_id=jid,
        agent_name="test_agent",
        agent_version="v1_test",
        question_id="q_test",
        run_id="run_test",
        judgment_status="complete",
        observations=[
            Observation(text=t, source_ids=["fact:test"]) for t in obs_texts
        ],
        inferences=[
            Inference(
                text=t, confidence="medium", based_on_observation_indices=[0],
            )
            for t in inf_texts
        ],
        counterarguments=[Counterargument(text="反方观点", strength="moderate")],
        used_evidence_ids=[f"ev{i}" for i in range(n_evidence)],
        cognitive_bias_self_check=CognitiveBiasSelfCheck(
            anchoring_risk="low",
            confirmation_bias_risk="low",
            recency_bias_risk="low",
            narrative_fallacy_risk="low",
        ),
    )


def _stamped_fallback(jid: str) -> JudgmentContract:
    """模拟 orchestrator（auto_research.py 3829）转印结构化标记。"""
    j = _judgment(jid)
    object.__setattr__(j, "is_llm_fallback", True)
    return j


def _zh_fallback(jid: str) -> JudgmentContract:
    """中文规则模板兜底文本标记（mock_client.py FB 前缀）。"""
    return _judgment(
        jid,
        obs_texts=["[规则模板兜底·调用超时] 基于财务数据的关键观察待补"],
        inf_texts=["[规则模板兜底·调用超时] 完整推断需要 LLM 二次分析"],
    )


def _en_fallback(jid: str) -> JudgmentContract:
    """英文规则模板兜底文本标记。"""
    return _judgment(
        jid,
        inf_texts=[
            "Management demonstrates strong capital allocation "
            "[rule-based fallback: timed out for risk_analyst]"
        ],
    )


def _issue(sev: str, jids: list[str], code: str = "EVIDENCE_NONE") -> CriticIssue:
    return CriticIssue(
        issue_code=code,
        severity=sev,
        offending_judgment_ids=jids,
        message="test issue",
    )


def _cr(issues: list[CriticIssue]) -> CriticResult:
    return CriticResult(
        critic_id="critic_test",
        critic_type="evidence_critic",
        issues=issues,
        block_publish=any(i.severity == "block" for i in issues),
        overall_risk="low",
    )


# ── 1. degraded 识别（结构化标记 + 文本标记双层）─────────────────────


class TestDegradedIdentification:
    def test_stamped_attr_detected(self):
        assert is_degraded_judgment(_stamped_fallback("j_fb")) is True

    def test_zh_text_marker_detected(self):
        assert is_degraded_judgment(_zh_fallback("j_fb")) is True

    def test_en_text_marker_detected(self):
        assert is_degraded_judgment(_en_fallback("j_fb")) is True

    def test_clean_judgment_not_degraded(self):
        assert is_degraded_judgment(_judgment("j_ok")) is False

    def test_split_counts(self):
        fb = _zh_fallback("j_fb")
        ok = _judgment("j_ok")
        crs = [_cr([
            _issue("warn", ["j_fb"]),
            _issue("warn", ["j_fb"]),
            _issue("block", ["j_fb"]),
            _issue("warn", ["j_ok"]),
            _issue("block", ["j_ok"]),
            _issue("warn", []),              # 空 ids → 按真实处理
            _issue("warn", ["j_fb", "j_ok"]),  # 混合指向 → 按真实处理
        ])]
        split = split_issue_counts(crs, [fb, ok])
        assert split == DegradedIssueSplit(
            real_warns=3, degraded_warns=2,
            real_blocks=1, degraded_blocks=1,
            degraded_judgment_count=1,
        )
        assert split.degraded_total == 3


# ── 2. warn 累计门：伪警告不计入阈值 ─────────────────────────────────


class TestWarnAccumulationGate:
    def _gate(self):
        return PublishGate()

    def test_degraded_warns_do_not_block(self):
        fb = _zh_fallback("j_fb")
        crs = [_cr([_issue("warn", ["j_fb"]) for _ in range(25)]
                   + [_issue("warn", ["j_ok"]) for _ in range(3)])]
        chk = self._gate()._warn_accumulation_gate(crs, [fb, _judgment("j_ok")])
        assert chk.passed is True
        assert "另有 25 条输入退化警告" in chk.message
        assert "3" in chk.message

    def test_real_warns_still_block(self):
        # 真实分析 warn 达阈值 → 行为与改动前一致：block。
        crs = [_cr([_issue("warn", ["j_ok"]) for _ in range(25)])]
        chk = self._gate()._warn_accumulation_gate(crs, [_judgment("j_ok")])
        assert chk.passed is False
        assert "25 warns" in chk.message

    def test_mixed_attribution_counts_as_real(self):
        # 混合指向（含真实 judgment）的 warn 保守按真实计。
        fb = _zh_fallback("j_fb")
        ok = _judgment("j_ok")
        crs = [_cr([_issue("warn", ["j_fb", "j_ok"]) for _ in range(20)])]
        chk = self._gate()._warn_accumulation_gate(crs, [fb, ok])
        assert chk.passed is False

    def test_real_at_threshold_blocks_despite_degraded_note(self):
        fb = _zh_fallback("j_fb")
        crs = [_cr([_issue("warn", ["j_ok"]) for _ in range(20)]
                   + [_issue("warn", ["j_fb"]) for _ in range(5)])]
        chk = self._gate()._warn_accumulation_gate(crs, [fb, _judgment("j_ok")])
        assert chk.passed is False
        assert "另有 5 条输入退化警告" in chk.message

    def test_clean_run_message_unchanged(self):
        # 无 degraded 时 message 与旧格式逐字一致（无退化注记）。
        crs = [_cr([_issue("warn", ["j_ok"]) for _ in range(3)])]
        chk = self._gate()._warn_accumulation_gate(crs, [_judgment("j_ok")])
        assert chk.passed is True
        assert chk.message == "Warning count acceptable: 3 (threshold: 20)"

    def test_evaluate_wiring_passes_judgments(self):
        # 全链路：25 条伪 warn 不得把 warn_accumulation_gate 推进 blocked_by。
        fb = _zh_fallback("j_fb")
        crs = [_cr([_issue("warn", ["j_fb"]) for _ in range(25)])]
        result = PublishGate().evaluate([fb], crs, context={})
        assert "warn_accumulation_gate" not in result.blocked_by


# ── 3. 置信度：伪警告不扣分，degraded 存在则封顶 medium ──────────────


class TestConfidenceDegradedIsolation:
    def _confidence(self, judgments, crs, **kw):
        kw.setdefault("publishing_status", "published")
        kw.setdefault("publish_gate_passed", True)
        kw.setdefault("gate_skipped_count", 0)
        return DecisionEngine()._determine_confidence(judgments, crs, **kw)

    def _strong(self):
        # 20 evidence + 10 obs → score 70+20+3 = 93 → high（无 cap 时）。
        return _judgment("j_strong", obs_texts=[f"obs{i}" for i in range(10)],
                         n_evidence=20)

    def test_clean_strong_run_stays_high(self):
        assert self._confidence([self._strong()], []) == "high"

    def test_degraded_warns_do_not_deduct_but_cap_medium(self):
        # 30 伪 warn + 5 伪 block：不扣分（否则 93-15-10=68 → medium 之下
        # 还可能被拖到 low），但 degraded 存在 → 封顶 medium。
        fb = _zh_fallback("j_fb")
        crs = [_cr(
            [_issue("warn", ["j_fb"]) for _ in range(30)]
            + [_issue("block", ["j_fb"]) for _ in range(5)]
        )]
        assert self._confidence([self._strong(), fb], crs) == "medium"

    def test_degraded_present_zero_issues_still_caps_medium(self):
        # 输入不完整不该 high —— 与 gate_skipped_count 封顶同语义。
        fb = _stamped_fallback("j_fb")
        assert self._confidence([self._strong(), fb], []) == "medium"

    def test_heavy_degraded_run_not_dragged_to_very_low(self):
        # 旧行为：7 个 agent 全兜底 → 大量伪 block 扣到 low/very_low。
        # 新行为：伪 issue 不扣分，落在 medium 或以下由真实信号决定。
        fbs = [_zh_fallback(f"j_fb{i}") for i in range(7)]
        crs = [_cr(
            [_issue("block", [f"j_fb{i}"]) for i in range(7) for _ in range(3)]
            + [_issue("warn", [f"j_fb{i}"]) for i in range(7) for _ in range(4)]
        )]
        bucket = self._confidence([self._strong()] + fbs, crs)
        assert bucket == "medium"

    def test_real_blocks_still_deduct(self):
        # 真实 block 扣分行为不变：93 - 20×2 = 53 → low。
        crs = [_cr([_issue("block", ["j_strong"]) for _ in range(20)])]
        assert self._confidence([self._strong()], crs) == "low"

    def test_real_warns_still_deduct(self):
        # 真实 warn 扣分行为不变：93 - 70×0.5 = 58 → low。
        crs = [_cr([_issue("warn", ["j_strong"]) for _ in range(70)])]
        assert self._confidence([self._strong()], crs) == "low"

    def test_degraded_cap_stacks_with_blocked_status(self):
        # blocked 封顶 low 仍然更严，degraded cap 不放松既有约束。
        fb = _zh_fallback("j_fb")
        bucket = self._confidence(
            [self._strong(), fb], [],
            publishing_status="blocked", publish_gate_passed=False,
        )
        assert bucket == "low"


# ── 4. logic_critic 中文利润表述 segment ceiling ─────────────────────


class TestLogicCriticZhProfitCoverage:
    """TODO-Y6 closure：`¥3亿净利润` 类中文表述不再绕过 ceiling 检查。"""

    def _ctx(self, with_ni: bool = True):
        meta = {
            "operating_income": 30e8,
            "gross_profit": 45e8,
            "__display": {"currency": "CNY", "symbol": "¥"},
        }
        if with_ni:
            meta["net_income"] = 20e8
        return {
            "meta_facts": meta,
            "segment_detail": {
                "product": {"云端产品线": {"revenue": 60e8}},
            },
        }

    def _seg_issues(self, text: str, ctx=None):
        j = _judgment("j_zh_seg", obs_texts=[text])
        res = LogicCritic().review([j], ctx if ctx is not None else self._ctx())
        return [i for i in res.issues if i.issue_code.startswith("LOGIC_SEGMENT")]

    # —— 命中：净利润 / 归母净利（¥亿、亿元、裸亿、万元 四种形态）——

    def test_net_profit_yen_yi_blocked(self):
        hits = self._seg_issues("云端产品线净利润¥25.0亿，表现亮眼。")
        assert [i.issue_code for i in hits] == ["LOGIC_SEGMENT_ABS_NI_IMPOSSIBLE"]
        assert hits[0].severity == "block"

    def test_net_profit_yiyuan_no_sigil_blocked(self):
        hits = self._seg_issues("云端产品线归母净利25.0亿元创新高。")
        assert [i.issue_code for i in hits] == ["LOGIC_SEGMENT_ABS_NI_IMPOSSIBLE"]

    def test_net_profit_bare_yi_adjacent_keyword_blocked(self):
        hits = self._seg_issues("云端产品线净利润25亿，同比大增。")
        assert [i.issue_code for i in hits] == ["LOGIC_SEGMENT_ABS_NI_IMPOSSIBLE"]

    def test_net_profit_wanyuan_blocked(self):
        # 250000万元 = 25亿 > 归母净利 20亿 × 1.05
        hits = self._seg_issues("云端产品线净利润250000万元。")
        assert [i.issue_code for i in hits] == ["LOGIC_SEGMENT_ABS_NI_IMPOSSIBLE"]

    def test_net_profit_within_ceiling_not_flagged(self):
        assert self._seg_issues("云端产品线净利润¥18.0亿。") == []

    def test_net_profit_skipped_when_no_consolidated_ni(self):
        # 无归母净利口径可比 → 保守不判（净利润不受营业利润上界约束）。
        hits = self._seg_issues(
            "云端产品线净利润¥25.0亿。", ctx=self._ctx(with_ni=False),
        )
        assert hits == []

    # —— 命中：毛利 / 营业利润 ——

    def test_gross_profit_abs_blocked(self):
        hits = self._seg_issues("云端产品线毛利¥50.0亿，远超同业。")
        assert [i.issue_code for i in hits] == ["LOGIC_SEGMENT_ABS_GP_IMPOSSIBLE"]

    def test_operating_profit_abs_still_blocked(self):
        # 既有行为回归：经营利润 ¥ 形态仍然命中。
        hits = self._seg_issues("云端产品线经营利润高达¥45.0亿，盈利能力突出。")
        assert [i.issue_code for i in hits] == ["LOGIC_SEGMENT_ABS_OI_IMPOSSIBLE"]

    # —— 命中：中文利润率百分比 ——

    def test_zh_operating_margin_pct_blocked(self):
        # 62% × 60亿营收 = 37.2亿 > 营业利润 30亿 × 1.05
        hits = self._seg_issues("云端产品线营业利润率62%，盈利强劲。")
        assert [i.issue_code for i in hits] == ["LOGIC_SEGMENT_MARGIN_IMPOSSIBLE"]

    def test_zh_gross_margin_pct_blocked_only_when_impossible(self):
        # 62% → 37.2亿 < 毛利 45亿×1.05：不误伤
        assert self._seg_issues("云端产品线毛利率62%。") == []
        # 90% → 54亿 > 47.25亿：命中
        hits = self._seg_issues("云端产品线毛利率90%，几无成本。")
        assert [i.issue_code for i in hits] == [
            "LOGIC_SEGMENT_GROSS_MARGIN_IMPOSSIBLE"
        ]

    # —— 不误伤 ——

    def test_segment_revenue_citation_not_blocked(self):
        # AUDIT (logic:338) 回归：营业收入是收入不是利润。
        assert self._seg_issues("云端产品线营业收入¥60.0亿，同比增长45%。") == []

    def test_share_count_not_blocked(self):
        assert self._seg_issues("公司总股本8亿股，云端产品线净利润稳健。") == []

    def test_revenue_near_margin_rate_word_not_blocked(self):
        # 毛利率(含"毛利")在窗口内 + 大额营收数字：紧邻收入词 → 跳过。
        assert self._seg_issues("云端产品线毛利率提升，营收¥60.0亿。") == []

    def test_rnd_spend_not_classified_as_profit(self):
        # 亿元金额但无利润关键词 → 不构成 claim。
        assert self._seg_issues("云端产品线研发投入12.0亿元持续加码。") == []
