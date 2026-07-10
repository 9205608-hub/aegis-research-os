"""定价体制感知 v1 — Pricing regime awareness (Phase 0, DESIGN_2.0 §三.A).

回答的问题不是"这只股票值多少钱"，而是"**市场现在按哪种逻辑给这只股票
定价**"：稳态现金流（steady）、增长溢价（growth）、困境反转（turnaround）
还是题材叙事（story）。输出连续权重 + 迟滞带，供报告层选择叙事框架与
验证点清单。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
设计红线 1（DESIGN_2.0 §六，评审沉淀，不可违反）：

    本模块的输出**只用于叙事框架与验证点选择**。任何调用方不得据此
    隐藏、折扣或弱化 DCF-vs-price 差值的展示——"这是题材股所以不谈
    估值差"是循环论证（用"估值贵"作为"不谈估值"的理由）。估值差
    永远展示，差值本身就是信息；体制权重只回答"该用什么框架解读
    这个差值、去验证什么"。
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

实现说明（v1，刻意不用机器学习）：

- 特征全部来自现有管线已算好的量（DCF 概率加权价差、FCF 符号、
  盈利质量指标、terminal_value_gate 信号），签名收显式参数。
- 每个体制一组**可读的加权特征打分**（打分依据写在代码注释里），
  softmax(T=2) 归一化为连续权重——温度 >1 刻意压平分布，承认这是
  弱信号弱分类，禁止输出接近 one-hot 的假自信。
- **迟滞带（dead band）**：最高与次高权重差 < 0.15 时 dominant 标
  "mixed"，防止边界特征抖动导致体制标签来回翻转。
- 波动率/行情类特征按 DESIGN_2.0 §三.A 推迟到有历史行情数据后再加。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

REGIMES = ("steady", "growth", "turnaround", "story")

# softmax 温度：>1 压平分布。校准依据：T=2 时四个手工标注原型
# （茅台=steady / 成长消费=growth / 康达=turnaround·story mixed /
# 寒武纪=story）在混淆矩阵测试里全部落对，且康达型恰好落进迟滞带。
_TEMPERATURE = 2.0

# 迟滞带宽度：最高/次高权重差小于该值时如实报 mixed。
_MIXED_BAND = 0.15

_REGIME_ZH = {
    "steady": "稳态现金流",
    "growth": "增长溢价",
    "turnaround": "困境反转",
    "story": "题材叙事",
}

_NARRATIVE_ZH = {
    "steady": "市场主要按已实现现金流定价，DCF 是有效主锚，预期差集中在稳态假设的边际变化。",
    "growth": "市场为可见的增长支付溢价，定价核心是增速与利润率兑现路径，而非当期现金流。",
    "turnaround": "市场在为困境反转定价：当前财务已恶化，现价押注的是修复与整合兑现。",
    "story": "市场按题材叙事定价，现价远超已实现现金流所能解释的范围，定价核心是叙事的可信度。",
}
_NARRATIVE_EN = {
    "steady": "The market prices this name mainly off realized cash flows; DCF is a valid primary anchor and the debate is about marginal changes to steady-state assumptions.",
    "growth": "The market pays a premium for visible growth; pricing hinges on the delivery path of growth and margins rather than current cash flow.",
    "turnaround": "The market is pricing a turnaround: current financials have deteriorated, and today's price is a bet on repair and integration playing out.",
    "story": "The market prices this name on narrative; the current price far exceeds what realized cash flows can explain, so credibility of the story is the pricing core.",
}
_NARRATIVE_MIXED_ZH = "市场定价框架处于「{a}」与「{b}」之间的混合状态，单一框架不足以解释现价，需同时跟踪两套验证点。"
_NARRATIVE_MIXED_EN = "The pricing regime is mixed between '{a}' and '{b}'; no single frame fully explains the current price, so both verification tracks apply."
_REGIME_EN = {
    "steady": "steady-state cash flow",
    "growth": "growth premium",
    "turnaround": "turnaround",
    "story": "narrative/story",
}

# 各体制下"该去验证什么"——报告层的验证点类型清单（中文，A 股铁律）。
_VERIFICATION_FOCUS = {
    "steady": [
        "核心利润率与市场份额的稳定性（季报/年报边际变化）",
        "自由现金流与分红/回购的匹配度",
        "稳态假设的均值回归风险（提价能力、竞争格局）",
    ],
    "growth": [
        "收入增速的边际变化（最新季报/业绩预告 vs 溢价所需增速）",
        "利润率随规模扩张的兑现路径",
        "增长溢价对应的兑现时间窗与竞争壁垒证据",
    ],
    "turnaround": [
        "盈利质量修复信号（CFO/净利比、应计项目占比回落）",
        "重组/并购整合的落地证据（公告、订单、业绩承诺兑现）",
        "债务与流动性压力（净负债/EBITDA、再融资安排）",
    ],
    "story": [
        "支撑现价所需的条件化隐含增速是否有可验证的事实锚",
        "现金消耗速度与再融资依赖程度",
        "题材催化剂的可证伪时间点（公告、订单、政策节点）",
    ],
}


@dataclass(frozen=True)
class RegimeAssessment:
    """定价体制评估结果（全部字段可审计——分类决策必须可追溯）。

    Attributes:
        weights: 四个体制的连续权重，和为 1（softmax 归一化，禁硬分类）。
        dominant: 主导体制；最高与次高权重差 < 0.15 时为 "mixed"（迟滞带）。
        top_two: 权重最高的两个体制名（降序），mixed 时指明混合的双方。
        scores: softmax 前的原始体制打分（审计用）。
        features: 输入特征原始值 + 派生中间量（审计用，追溯每个权重怎么来的）。
        narrative_frame_zh / narrative_frame_en: 一句话叙事框架描述。
        verification_focus: 该体制下的验证点类型清单（中文）。

    设计红线 1：本结果只用于叙事框架与验证点选择，任何调用方不得据此
    隐藏或折扣 DCF-vs-price 差值的展示。
    """

    weights: dict[str, float]
    dominant: str
    top_two: tuple[str, str]
    scores: dict[str, float]
    features: dict[str, object]
    narrative_frame_zh: str
    narrative_frame_en: str
    verification_focus: list[str] = field(default_factory=list)


def _ramp(x: float, lo: float, hi: float) -> float:
    """线性斜坡：x<=lo → 0，x>=hi → 1，中间线性插值。"""
    if hi <= lo:
        raise ValueError(f"ramp bounds invalid: lo={lo}, hi={hi}")
    return min(1.0, max(0.0, (x - lo) / (hi - lo)))


def _clean(x: float | None) -> float | None:
    """None / NaN / inf → None（缺失特征贡献 0 分，不猜值）。"""
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return v


def assess_pricing_regime(
    *,
    dcf_gap: float,
    fcf_positive: bool,
    accruals_ratio: float | None = None,
    cfo_to_ni: float | None = None,
    growth_regime_break: float | bool = False,
    net_debt_to_ebitda: float | None = None,
    terminal_value_gate_triggered: bool = False,
) -> RegimeAssessment:
    """从现有管线特征评估当前的市场定价体制（纯函数，无 I/O）。

    Args:
        dcf_gap: (price − pw_value) / pw_value。正值 = 现价高于 DCF 概率
            加权价值（市场支付溢价）；负值 = 市场折价。
        fcf_positive: 最新期自由现金流是否为正。
        accruals_ratio: 应计项目占比（accounting_analyst 同口径，
            |x|>0.10 开始视为盈利质量警示）。缺失传 None。
        cfo_to_ni: 经营现金流/净利润（<0.5 为红旗，同 CFO_NI_FLOOR）。
            净利润为负等口径失效场景传 None。
        growth_regime_break: 增长体制突变信号——历史 CAGR 与最新 YoY 的
            背离。bool（True=确认突变）或幅度（背离绝对值，10pp 起算、
            50pp 饱和）。
        net_debt_to_ebitda: 净负债/EBITDA（>2 开始计杠杆压力，4 饱和）。
            缺失或 EBITDA≤0 传 None。
        terminal_value_gate_triggered: publish gate 的 terminal_value_gate
            是否触发（终值主导 + 高风险画像的既有信号，体制感知的种子特征）。

    Returns:
        RegimeAssessment。注意设计红线 1：结果只改叙事框架与验证点，
        永不用于抑制 DCF-vs-price 差值的展示。
    """
    # ── 特征清洗与派生中间量 ────────────────────────────────────────
    gap = _clean(dcf_gap)
    gap = 0.0 if gap is None else max(-0.99, min(10.0, gap))
    pos_gap = max(0.0, gap)  # v1 体制光谱只区分溢价侧；折价侧按稳态/质量框架解读

    acc = _clean(accruals_ratio)
    cfo = _clean(cfo_to_ni)
    # 盈利质量恶化度 ∈ [0,1]：应计占比超 0.10 warn 阈值起算（同
    # accounting_analyst.ACCRUAL_RATIO_WARN），0.30 饱和；CFO/NI 低于 1.0
    # 起算、0.5 红旗线（CFO_NI_FLOOR）封顶。两个信号取 max——任一红旗
    # 即质量存疑。缺失特征贡献 0（不猜）。
    q_acc = _ramp(abs(acc), 0.10, 0.30) if acc is not None else 0.0
    q_cfo = 1.0 - _ramp(cfo, 0.50, 1.00) if cfo is not None else 0.0
    quality_poor = max(q_acc, q_cfo)
    quality_good = 1.0 - quality_poor

    # 增长突变幅度 ∈ [0,1]：bool 直接取 0/1；数值按背离 10pp 起算、50pp 饱和。
    if isinstance(growth_regime_break, bool):
        break_mag = 1.0 if growth_regime_break else 0.0
    else:
        b = _clean(growth_regime_break)
        break_mag = _ramp(abs(b), 0.10, 0.50) if b is not None else 0.0

    ndebt = _clean(net_debt_to_ebitda)
    # 杠杆压力 ∈ [0,1]：净负债/EBITDA 2× 起算、4× 饱和（A 股工业企业惯用警戒带）。
    leverage_high = _ramp(ndebt, 2.0, 4.0) if ndebt is not None else 0.0

    gate = bool(terminal_value_gate_triggered)

    # ── 体制打分（每行注释 = 该特征计入的依据）────────────────────────
    scores: dict[str, float] = {}

    # steady：已实现现金流解释得了现价。
    scores["steady"] = (
        # 价差收敛是稳态定价的核心证据：|gap|<10% 满分，>50% 归零
        2.2 * (1.0 - _ramp(abs(gap), 0.10, 0.50))
        # 稳态资产必须自我造血
        + (0.8 if fcf_positive else -0.8)
        # 盈利质量干净 → 报表现金流可信，DCF 锚有效
        + 0.8 * quality_good
        # 增长体制突变与"稳态"直接矛盾
        - 1.0 * break_mag
        # 终值门触发 = DCF 本身高危，稳态框架失效
        - (1.0 if gate else 0.0)
        # 高杠杆侵蚀稳态现金流的可分配性
        - 0.5 * leverage_high
    )

    # growth：市场为可见增长支付**适度**溢价（溢价带 20%–80% 满分，
    # 超过 ~1.2 倍开始衰减、2.5 倍归零——极端溢价属于 story 而非 growth）。
    scores["growth"] = (
        2.0 * _ramp(pos_gap, 0.20, 0.80) * (1.0 - _ramp(pos_gap, 1.20, 2.50))
        # 健康成长股通常 FCF 为正；负 FCF 只轻罚（扩张期资本开支可解释）
        + (1.0 if fcf_positive else -0.5)
        # 增长故事需要干净的报表支撑
        + 0.5 * quality_good
        # 增速突变（尤其骤降）动摇增长定价的前提
        - 1.0 * break_mag
        # 终值门触发说明增长假设已在危险区
        - (0.5 if gate else 0.0)
    )

    # turnaround：财务已恶化、市场押注修复。
    scores["turnaround"] = (
        # 盈利质量恶化是反转叙事的入场券（康达：扣非/归母差 7 倍、CFO 为负）
        1.5 * quality_poor
        # 增长突变（并购并表、订单断崖）= 基本面处于换挡期
        + 1.2 * break_mag
        # 当期失血 → 现价只能靠"未来会修复"支撑
        + (0.8 if not fcf_positive else 0.0)
        # 反转定价伴随对 DCF 的显著溢价（50% 起算、2 倍饱和）
        + 0.8 * _ramp(pos_gap, 0.50, 2.00)
        # 高杠杆是困境股的典型标签，也放大反转赔率
        + 0.5 * leverage_high
        # 终值门触发与高危画像一致（弱信号）
        + (0.3 if gate else 0.0)
    )

    # story：现价远超已实现现金流所能解释，定价核心是叙事。
    scores["story"] = (
        # 极端溢价（1 倍起算、3 倍饱和；寒武纪型 5 倍+ 满分）
        2.0 * _ramp(pos_gap, 1.00, 3.00)
        # 负 FCF + 高溢价 = 现价与现金流彻底脱钩
        + (1.0 if not fcf_positive else 0.0)
        # 终值门触发 = DCF 告警"这个价不是现金流给的"
        + (1.0 if gate else 0.0)
        # 增速跳变常是题材叙事的点火器（弱信号）
        + 0.5 * break_mag
    )

    # ── softmax 归一化（温度压平，禁 one-hot 假自信）──────────────────
    exps = {k: math.exp(v / _TEMPERATURE) for k, v in scores.items()}
    total = sum(exps.values())
    weights = {k: exps[k] / total for k in REGIMES}

    ranked = sorted(REGIMES, key=lambda k: weights[k], reverse=True)
    top, second = ranked[0], ranked[1]
    # 迟滞带：差距不足时如实报 mixed，防止边界抖动来回翻转体制标签
    dominant = top if weights[top] - weights[second] >= _MIXED_BAND else "mixed"

    if dominant == "mixed":
        narrative_zh = _NARRATIVE_MIXED_ZH.format(a=_REGIME_ZH[top], b=_REGIME_ZH[second])
        narrative_en = _NARRATIVE_MIXED_EN.format(a=_REGIME_EN[top], b=_REGIME_EN[second])
        focus = list(_VERIFICATION_FOCUS[top])
        focus += [f for f in _VERIFICATION_FOCUS[second] if f not in focus]
    else:
        narrative_zh = _NARRATIVE_ZH[dominant]
        narrative_en = _NARRATIVE_EN[dominant]
        focus = list(_VERIFICATION_FOCUS[dominant])

    features: dict[str, object] = {
        # 原始输入（审计：分类决策可追溯）
        "dcf_gap": dcf_gap,
        "fcf_positive": bool(fcf_positive),
        "accruals_ratio": accruals_ratio,
        "cfo_to_ni": cfo_to_ni,
        "growth_regime_break": growth_regime_break,
        "net_debt_to_ebitda": net_debt_to_ebitda,
        "terminal_value_gate_triggered": gate,
        # 派生中间量（审计：打分怎么来的）
        "derived_gap_clipped": gap,
        "derived_quality_poor": quality_poor,
        "derived_break_mag": break_mag,
        "derived_leverage_high": leverage_high,
    }

    return RegimeAssessment(
        weights=weights,
        dominant=dominant,
        top_two=(top, second),
        scores=scores,
        features=features,
        narrative_frame_zh=narrative_zh,
        narrative_frame_en=narrative_en,
        verification_focus=focus,
    )
