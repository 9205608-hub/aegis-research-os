"""红旗关键词分档回归（2026-08-01，降测量误伤）。

`AutoResearchOrchestrator._extract_key_finding` 旧版对 agent 观察文本做
单层关键词匹配，中文集合含"风险/压力/下行/下滑"等泛化词——风险分析师
正常履职必然命中，每轮 run 固定出现"5 agent 红旗"，污染 cumulative_findings
的 red_flag 字段（下游消费点：llm_agent_base._build_message 的
"⚠ RED FLAG" prompt 标记 + orchestrator 的 "Red flags from:" 日志），
使 synthesizer 过度防御。

分档语义（红旗 = "超预期/结构性异常"，不是"提到了风险这个词"）：
  - 强信号词（造假/粉饰/虚增/存疑/可疑/异常/急剧恶化/不可持续/警示；
    fabricated/implausible/overstated/red flag/questionable）：
    单条观察命中即举旗
  - 弱信号词（风险/压力/下行/下滑/declining/negative/risk/concern 等）：
    须 ≥2 条不同观察各自命中，或弱词命中与该 agent 自身
    confidence=="low" 同时出现才举旗
  - 任一 counterargument.strength=="strong" 直接举旗（既有规则保留）
  - BUG-Y45 中英双语覆盖精神保留：两个字母表都能触发，只是阈值提高
"""

from types import SimpleNamespace

from aegis.core.orchestrator.auto_research import AutoResearchOrchestrator


def _make_out(
    observations=(),
    confidence="medium",
    counter_strengths=(),
):
    """构造 _extract_key_finding 所需的最小 agent 输出形态。"""
    inferences = []
    if confidence is not None:
        inferences = [SimpleNamespace(text="关键推断", confidence=confidence)]
    judgment = SimpleNamespace(
        observations=[SimpleNamespace(text=t) for t in observations],
        inferences=inferences,
        counterarguments=[SimpleNamespace(strength=s) for s in counter_strengths],
    )
    return SimpleNamespace(judgment=judgment)


def _extract(**kwargs):
    return AutoResearchOrchestrator._extract_key_finding(
        "risk_analyst", _make_out(**kwargs))


class TestStrongSignalSingleHit:
    """强信号词：单条观察命中即举旗。"""

    def test_strong_zh_single_observation_flags(self):
        f = _extract(observations=["存货周转与收入增速不匹配，收入确认存疑"])
        assert f["red_flag"] is True

    def test_strong_zh_fraud_words_flag(self):
        for word in ("造假", "粉饰", "虚增", "可疑", "异常", "警示",
                     "不可持续", "急剧恶化"):
            f = _extract(observations=[f"应收账款科目出现{word}迹象"])
            assert f["red_flag"] is True, word

    def test_strong_en_single_observation_flags(self):
        f = _extract(
            observations=["Revenue appears overstated relative to cash collection"])
        assert f["red_flag"] is True

    def test_strong_en_red_flag_phrase(self):
        f = _extract(
            observations=["Receivables growth outpacing revenue is a red flag"])
        assert f["red_flag"] is True


class TestWeakSignalThreshold:
    """弱信号词：单发不举旗；≥2 条不同观察或伴随低置信度才举旗。"""

    def test_weak_zh_single_observation_no_flag(self):
        # 风险分析师正常履职的典型句——旧版必误伤
        f = _extract(observations=["行业竞争加剧带来一定风险"])
        assert f["red_flag"] is False

    def test_multiple_weak_words_in_one_observation_no_flag(self):
        # 同一条观察里堆多个弱词仍算 1 条命中——不举旗
        f = _extract(observations=["需求存在下行压力，毛利率有下滑风险"])
        assert f["red_flag"] is False

    def test_weak_en_single_observation_no_flag(self):
        f = _extract(
            observations=["Competition risk is rising in the handset segment"])
        assert f["red_flag"] is False

    def test_two_distinct_weak_zh_observations_flag(self):
        f = _extract(observations=["毛利率持续下滑", "经营现金流面临压力"])
        assert f["red_flag"] is True

    def test_two_distinct_weak_en_observations_flag(self):
        f = _extract(observations=[
            "Margins have been declining for three years",
            "Free cash flow turned negative in FY2025",
        ])
        assert f["red_flag"] is True

    def test_mixed_alphabet_weak_observations_flag(self):
        # BUG-Y45 双语覆盖精神：中英各一条弱命中也算 2 条
        f = _extract(observations=[
            "毛利率持续下滑",
            "Customer concentration remains a concern",
        ])
        assert f["red_flag"] is True

    def test_weak_single_with_low_confidence_flags(self):
        f = _extract(
            observations=["行业需求存在下行压力"], confidence="low")
        assert f["red_flag"] is True
        assert f["confidence"] == "low"

    def test_weak_single_with_high_confidence_no_flag(self):
        f = _extract(
            observations=["行业需求存在下行压力"], confidence="high")
        assert f["red_flag"] is False

    def test_low_confidence_without_weak_hit_no_flag(self):
        # 低置信度本身不举旗，须与弱词命中同时出现
        f = _extract(observations=["公司发布了年度报告"], confidence="low")
        assert f["red_flag"] is False


class TestNeutralAndCounterargument:

    def test_neutral_text_no_flag(self):
        f = _extract(observations=[
            "公司发布了 FY2025 年度报告",
            "Revenue grew 12% year over year",
        ])
        assert f["red_flag"] is False

    def test_no_observations_no_flag(self):
        f = _extract(observations=[])
        assert f["red_flag"] is False

    def test_strong_counterargument_flags_alone(self):
        # 既有规则保留：strong counterargument 直接举旗，与关键词无关
        f = _extract(
            observations=["公司发布了年度报告"],
            counter_strengths=("strong",),
        )
        assert f["red_flag"] is True

    def test_weak_and_moderate_counterarguments_no_flag(self):
        f = _extract(
            observations=["公司发布了年度报告"],
            counter_strengths=("weak", "moderate"),
        )
        assert f["red_flag"] is False


class TestFindingShapeContract:
    """消费方契约：llm_agent_base._build_message 与 orchestrator 日志
    依赖的字段形态不变。"""

    def test_finding_dict_keys_preserved(self):
        f = _extract(observations=["毛利率持续下滑", "现金流面临压力"])
        assert set(f) == {
            "agent", "key_finding", "red_flag", "confidence",
            "num_observations",
        }
        assert f["agent"] == "risk_analyst"
        assert f["num_observations"] == 2
        assert isinstance(f["red_flag"], bool)
