"""定价体制感知 v1 回归测试（Phase 0, DESIGN_2.0 §三.A / §五.4）。

核心是一张 ~20 行的手工构造特征向量混淆矩阵：每行是一个体制原型
（茅台型稳态 / 成长消费型 / 寒武纪型题材 / 康达型反转·题材混合…），
断言 dominant 落对格子。另配：

- 权重不变量（和为 1、全在 (0,1)、禁 one-hot）
- 迟滞带（dead band）生效：边界特征从 steady→mixed→growth 过渡，
  不允许跳过 mixed 直接翻转
- 缺失/NaN 特征的中性处理
- 叙事框架 zh/en 与验证点清单（中文）内容检查
- 设计红线 1 写入 docstring 的存在性检查
"""

import math

import pytest

from aegis.core.truth import pricing_regime
from aegis.core.truth.pricing_regime import (
    REGIMES,
    RegimeAssessment,
    assess_pricing_regime,
)


def _features(**overrides):
    """默认一个"平庸"特征集，测试用例只写差异项。"""
    base = dict(
        dcf_gap=0.0,
        fcf_positive=True,
        accruals_ratio=0.05,
        cfo_to_ni=1.0,
        growth_regime_break=False,
        net_debt_to_ebitda=1.0,
        terminal_value_gate_triggered=False,
    )
    base.update(overrides)
    return base


# ─────────────────────────────────────────────────────────────────
# 混淆矩阵：手工标注原型 → 期望主导体制
# 每行 = (case_id, 特征向量, 期望 dominant, 期望 top_two 集合或 None)
# ─────────────────────────────────────────────────────────────────

CONFUSION_MATRIX = [
    # ── steady 原型 ──
    # 茅台型：价差收敛、FCF 正、盈利质量干净、净现金
    ("maotai_steady", _features(dcf_gap=0.10, accruals_ratio=0.03, cfo_to_ni=1.10,
                                net_debt_to_ebitda=-0.5), "steady", None),
    # 长江电力型：轻微折价、公用事业杠杆（3×）不改变稳态定性
    ("changdian_steady", _features(dcf_gap=-0.05, cfo_to_ni=1.20,
                                   net_debt_to_ebitda=3.0), "steady", None),
    # 稳态 + terminal_value_gate 误触发：其余特征全干净时仍应判 steady
    ("steady_gate_false_alarm", _features(dcf_gap=0.05, accruals_ratio=0.04,
                                          cfo_to_ni=1.05,
                                          terminal_value_gate_triggered=True),
     "steady", None),
    # 深度折价价值股：v1 光谱把折价侧归入稳态/质量框架解读
    ("deep_value_discount", _features(dcf_gap=-0.35, accruals_ratio=0.06,
                                      net_debt_to_ebitda=1.5), "steady", None),
    # 可选特征全缺失：只有 gap + FCF 也要能出稳态判断（缺失贡献 0，不猜）
    ("steady_missing_optional",
     dict(dcf_gap=0.08, fcf_positive=True), "steady", None),

    # ── growth 原型 ──
    # 成长消费型：适度溢价（60%）、FCF 正、质量干净 → 增长溢价框架
    ("consumer_growth", _features(dcf_gap=0.60, cfo_to_ni=0.95,
                                  net_debt_to_ebitda=0.5), "growth", None),
    # 高端制造成长：溢价 90%，报表干净
    ("hitech_growth", _features(dcf_gap=0.90, accruals_ratio=0.06,
                                net_debt_to_ebitda=0.0), "growth", None),
    # 轻度质量噪音（应计 0.12 刚过 warn 线）不足以推翻增长定性
    ("growth_mild_quality_noise", _features(dcf_gap=0.75, accruals_ratio=0.12,
                                            cfo_to_ni=0.85), "growth", None),
    # 数值型 break 信号：8pp 背离低于 10pp 起算线 → 等同无突变
    ("growth_numeric_break_small", _features(dcf_gap=0.65,
                                             growth_regime_break=0.08,
                                             net_debt_to_ebitda=0.5),
     "growth", None),

    # ── story 原型 ──
    # 寒武纪型：极端溢价（5.4×）、FCF 负、净利为负故 cfo_to_ni 口径失效、
    # 增速跳变、终值门触发 → 题材叙事主导
    ("cambricon_story",
     dict(dcf_gap=5.40, fcf_positive=False, accruals_ratio=0.05, cfo_to_ni=None,
          growth_regime_break=0.60, net_debt_to_ebitda=None,
          terminal_value_gate_triggered=True),
     "story", ("story", "turnaround")),
    # 3× 溢价 + 负 FCF + 终值门，无增速突变
    ("story_gap3",
     dict(dcf_gap=3.00, fcf_positive=False, accruals_ratio=0.06, cfo_to_ni=None,
          growth_regime_break=False, net_debt_to_ebitda=None,
          terminal_value_gate_triggered=True),
     "story", None),
    # 报表质量干净不妨碍 story 判定——题材看的是价格与现金流的脱钩程度
    ("story_good_quality", _features(dcf_gap=4.00, fcf_positive=False,
                                     accruals_ratio=0.04, cfo_to_ni=0.90,
                                     growth_regime_break=True,
                                     net_debt_to_ebitda=0.0,
                                     terminal_value_gate_triggered=True),
     "story", None),
    # 终值门未触发但 4.5× 溢价 + 负 FCF 仍是 story
    ("story_no_gate",
     dict(dcf_gap=4.50, fcf_positive=False, accruals_ratio=0.05, cfo_to_ni=None,
          growth_regime_break=0.50, net_debt_to_ebitda=None,
          terminal_value_gate_triggered=False),
     "story", None),

    # ── turnaround 原型 ──
    # 经典反转：质量恶化（应计 0.28 / CFO比 0.2）+ 增长突变 + 失血 + 高杠杆，
    # 溢价温和（80%）→ 反转主导而非题材
    ("classic_turnaround", _features(dcf_gap=0.80, fcf_positive=False,
                                     accruals_ratio=0.28, cfo_to_ni=0.20,
                                     growth_regime_break=True,
                                     net_debt_to_ebitda=4.0),
     "turnaround", None),
    ("leveraged_turnaround", _features(dcf_gap=1.20, fcf_positive=False,
                                       accruals_ratio=0.22, cfo_to_ni=0.30,
                                       growth_regime_break=True,
                                       net_debt_to_ebitda=5.0),
     "turnaround", None),
    # 质量塌方但溢价不大：市场还没讲出故事，纯反转框架
    ("quality_rot_no_gap", _features(dcf_gap=0.30, fcf_positive=False,
                                     accruals_ratio=0.35, cfo_to_ni=0.10,
                                     growth_regime_break=True,
                                     net_debt_to_ebitda=3.5),
     "turnaround", None),

    # ── mixed 原型 ──
    # 康达型：极端溢价（¥13.76 vs ¥2.15 ≈ 5.4×）+ 失血 + 盈利质量差
    # （归母 1.25 亿 vs 扣非 1672 万）+ 并购拉动的增长突变 + 高杠杆 +
    # 终值门触发 → 反转与题材两个框架都成立，落迟滞带报 mixed
    ("kangda_mixed", _features(dcf_gap=5.40, fcf_positive=False,
                               accruals_ratio=0.25, cfo_to_ni=-9.0,
                               growth_regime_break=True,
                               net_debt_to_ebitda=5.0,
                               terminal_value_gate_triggered=True),
     "mixed", ("turnaround", "story")),
    # 扩张期消费：溢价 70% 但 FCF 转负 → 增长/反转之间的模糊带
    ("expansion_mixed", _features(dcf_gap=0.70, fcf_positive=False,
                                  accruals_ratio=0.08, cfo_to_ni=0.90),
     "mixed", None),
]


class TestConfusionMatrix:
    """~20 个手工标注特征向量的混淆矩阵校验。"""

    @pytest.mark.parametrize(
        "case_id,features,expected,expected_top_two",
        CONFUSION_MATRIX,
        ids=[c[0] for c in CONFUSION_MATRIX],
    )
    def test_dominant_regime(self, case_id, features, expected, expected_top_two):
        result = assess_pricing_regime(**features)
        assert result.dominant == expected, (
            f"{case_id}: expected {expected}, got {result.dominant} "
            f"(weights={result.weights})"
        )
        if expected_top_two is not None:
            assert result.top_two == expected_top_two


class TestWeightInvariants:
    """连续权重的结构性不变量。"""

    @pytest.mark.parametrize(
        "case_id,features,expected,expected_top_two",
        CONFUSION_MATRIX,
        ids=[c[0] for c in CONFUSION_MATRIX],
    )
    def test_weights_sum_to_one_and_bounded(self, case_id, features, expected,
                                            expected_top_two):
        result = assess_pricing_regime(**features)
        assert set(result.weights) == set(REGIMES)
        assert math.isclose(sum(result.weights.values()), 1.0, abs_tol=1e-9)
        for regime, w in result.weights.items():
            # softmax 输出严格在 (0,1) 开区间——禁 one-hot 硬分类
            assert 0.0 < w < 1.0, f"{case_id}/{regime}: weight {w} not in (0,1)"

    def test_no_overconfident_onehot_even_at_extremes(self):
        """特征全部拉满也不许输出接近 1 的假自信权重。"""
        result = assess_pricing_regime(
            dcf_gap=10.0, fcf_positive=False, accruals_ratio=0.0,
            cfo_to_ni=None, growth_regime_break=True,
            net_debt_to_ebitda=None, terminal_value_gate_triggered=True,
        )
        assert max(result.weights.values()) < 0.90

    def test_dominant_matches_top_weight_or_mixed(self):
        result = assess_pricing_regime(**_features(dcf_gap=0.10))
        top = max(result.weights, key=result.weights.get)
        assert result.dominant in (top, "mixed")
        assert result.top_two[0] == top


class TestHysteresisBand:
    """迟滞带：steady→growth 的边界过渡必须经过 mixed，不许直接翻转。"""

    def _at_gap(self, gap):
        return assess_pricing_regime(**_features(
            dcf_gap=gap, accruals_ratio=0.04, cfo_to_ni=1.05,
            net_debt_to_ebitda=0.5))

    def test_low_gap_is_steady(self):
        assert self._at_gap(0.10).dominant == "steady"

    def test_boundary_gap_is_mixed(self):
        result = self._at_gap(0.35)
        assert result.dominant == "mixed"
        assert set(result.top_two) == {"steady", "growth"}

    def test_high_gap_is_growth(self):
        assert self._at_gap(0.70).dominant == "growth"

    def test_transition_passes_through_mixed(self):
        """扫过边界：非 mixed 的主导标签只允许单调换挡一次，且换挡点
        两侧之间必须出现 mixed —— 证明 dead band 吸收了抖动。"""
        gaps = [0.05, 0.15, 0.25, 0.35, 0.45, 0.55, 0.65, 0.75]
        labels = [self._at_gap(g).dominant for g in gaps]
        # 不允许 steady 与 growth 直接相邻
        for a, b in zip(labels, labels[1:]):
            if {a, b} == {"steady", "growth"}:
                pytest.fail(f"direct flip without mixed: {labels}")
        assert "mixed" in labels

    def test_jitter_inside_band_stays_mixed(self):
        """迟滞带内 ±0.03 的特征抖动不改变 mixed 判定。"""
        for gap in (0.32, 0.35, 0.38):
            assert self._at_gap(gap).dominant == "mixed", f"gap={gap}"


class TestFeatureHandling:
    """缺失 / NaN / 非法特征的中性处理。"""

    def test_missing_optionals_neutral(self):
        result = assess_pricing_regime(dcf_gap=0.08, fcf_positive=True)
        assert result.dominant == "steady"
        assert result.features["accruals_ratio"] is None
        assert result.features["derived_quality_poor"] == 0.0

    def test_nan_inputs_treated_as_missing(self):
        nan = float("nan")
        result = assess_pricing_regime(
            dcf_gap=0.08, fcf_positive=True, accruals_ratio=nan,
            cfo_to_ni=nan, growth_regime_break=nan, net_debt_to_ebitda=nan,
        )
        assert result.dominant == "steady"
        assert result.features["derived_quality_poor"] == 0.0
        assert result.features["derived_break_mag"] == 0.0
        assert result.features["derived_leverage_high"] == 0.0

    def test_infinite_gap_clipped(self):
        result = assess_pricing_regime(dcf_gap=float("inf"), fcf_positive=False,
                                       terminal_value_gate_triggered=True)
        # inf 被清洗为缺失 → gap 按 0 处理，不许 crash 也不许传播 inf
        assert all(math.isfinite(v) for v in result.weights.values())
        assert result.features["derived_gap_clipped"] == 0.0

    def test_bool_and_numeric_break_equivalence_at_saturation(self):
        r_bool = assess_pricing_regime(**_features(growth_regime_break=True))
        r_num = assess_pricing_regime(**_features(growth_regime_break=0.50))
        assert r_bool.features["derived_break_mag"] == 1.0
        assert r_num.features["derived_break_mag"] == 1.0

    def test_features_dict_echoes_raw_inputs(self):
        """设计红线要求分类决策可追溯：features 必须原样回显输入。"""
        kw = _features(dcf_gap=0.42, accruals_ratio=0.13, net_debt_to_ebitda=2.5)
        result = assess_pricing_regime(**kw)
        for key, val in kw.items():
            assert result.features[key] == val


class TestNarrativeAndVerification:
    """叙事框架与验证点清单（中文铁律）。"""

    def _contains_chinese(self, text):
        return any("一" <= ch <= "鿿" for ch in text)

    @pytest.mark.parametrize(
        "case_id,features,expected,expected_top_two",
        CONFUSION_MATRIX,
        ids=[c[0] for c in CONFUSION_MATRIX],
    )
    def test_narratives_and_focus_present(self, case_id, features, expected,
                                          expected_top_two):
        result = assess_pricing_regime(**features)
        assert self._contains_chinese(result.narrative_frame_zh)
        assert result.narrative_frame_en
        assert not self._contains_chinese(result.narrative_frame_en)
        assert result.verification_focus
        for item in result.verification_focus:
            assert self._contains_chinese(item)

    def test_mixed_narrative_names_both_regimes(self):
        result = assess_pricing_regime(**_features(
            dcf_gap=5.40, fcf_positive=False, accruals_ratio=0.25,
            cfo_to_ni=-9.0, growth_regime_break=True, net_debt_to_ebitda=5.0,
            terminal_value_gate_triggered=True))
        assert result.dominant == "mixed"
        assert "困境反转" in result.narrative_frame_zh
        assert "题材叙事" in result.narrative_frame_zh
        assert "turnaround" in result.narrative_frame_en
        assert "story" in result.narrative_frame_en or "narrative" in result.narrative_frame_en

    def test_mixed_verification_focus_unions_top_two(self):
        result = assess_pricing_regime(**_features(
            dcf_gap=5.40, fcf_positive=False, accruals_ratio=0.25,
            cfo_to_ni=-9.0, growth_regime_break=True, net_debt_to_ebitda=5.0,
            terminal_value_gate_triggered=True))
        single = assess_pricing_regime(**_features(
            dcf_gap=0.80, fcf_positive=False, accruals_ratio=0.28,
            cfo_to_ni=0.20, growth_regime_break=True, net_debt_to_ebitda=4.0))
        assert single.dominant == "turnaround"
        # mixed 的清单包含 turnaround 全部验证点，且比单一体制的更长
        for item in single.verification_focus:
            assert item in result.verification_focus
        assert len(result.verification_focus) > len(single.verification_focus)
        # 去重
        assert len(result.verification_focus) == len(set(result.verification_focus))

    def test_story_focus_mentions_falsification(self):
        result = assess_pricing_regime(
            dcf_gap=4.50, fcf_positive=False, accruals_ratio=0.05,
            cfo_to_ni=None, growth_regime_break=0.5, net_debt_to_ebitda=None,
            terminal_value_gate_triggered=True)
        assert result.dominant == "story"
        joined = "".join(result.verification_focus)
        assert "可证伪" in joined


class TestRedLineDocstring:
    """设计红线 1 必须写进模块与结果类的 docstring（未来 session 的护栏）。"""

    def test_module_docstring_carries_red_line(self):
        doc = pricing_regime.__doc__
        assert "设计红线" in doc
        assert "叙事框架" in doc and "验证点" in doc
        assert "不得" in doc and "DCF-vs-price" in doc

    def test_result_dataclass_docstring_carries_red_line(self):
        doc = RegimeAssessment.__doc__
        assert "设计红线" in doc
        assert "不得" in doc

    def test_result_is_frozen(self):
        """结果不可变——调用方不许事后篡改权重再声称可追溯。"""
        result = assess_pricing_regime(**_features())
        with pytest.raises(Exception):
            result.dominant = "story"
