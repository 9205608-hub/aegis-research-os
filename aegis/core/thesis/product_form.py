"""R5-L4（2026-07-12 平台期突破杠杆）：产品形态诚实化。

四轮 Grok 复审的平台期结论（GROK_REAUDIT_2026-07-12.md）：审计者反复说
"作为问题清单/监控框架值 5-6 分，作为可下单 thesis 只值 3-4 分"——数据缺口
大的票，系统产出的其实是高质量观察框架，却自我标榜为投资论点，按后者的
尺子挨打（北方华创 R3 判词亲口指路："若去掉 DCF 目标价族与伪精确，残余
行业常识价值大约在 5 分"）。

本模块把形态判定做成**确定性规则**（不调 LLM）：估值失配 / 证据缺口 /
发布被阻断或待复核 / 降级且待解问题堆积的产出物，自我标注为「条件化观察
框架 + 监控合约」。纯函数、零依赖，四处消费同一真源：

- orchestrator（决策后盖章进 ``scenarios["product_form"]``）
- thesis persistence（落 ``ThesisContract.product_form``，随审计材料送审）
- Report Editor（观察框架票的语气注入）
- HTML 渲染（报告顶部形态声明横幅）
"""

from __future__ import annotations

from typing import Any

INVESTMENT_THESIS = "investment_thesis"
OBSERVATION_FRAMEWORK = "observation_framework"

# 发布状态里"未通过发布门/待人工复核"的取值。needs_review 是 orchestrator
# BUG-40（DCF artifact gate）直接写在 decision 上的状态，不在
# PublishingStatus 枚举里；under_review 是枚举里的对应值——两个都认。
_REVIEW_STATUSES = ("blocked", "needs_review", "under_review")

# downgraded 票的待解问题达到该数即视为"数据缺口大"——降级本身说明存在
# 未解决冲突，再叠加成堆的 open_questions 时论点的关键变量事实上未闭合。
DOWNGRADED_OPEN_QUESTION_THRESHOLD = 3

_LABEL_ZH = "条件化观察框架 + 监控合约"
_LABEL_EN = "conditional observation framework + monitoring contract"


def derive_product_form(
    *,
    valuation_mismatch: bool = False,
    evidence_gap_hits: int = 0,
    publishing_status: str = "",
    open_question_count: int = 0,
) -> dict[str, Any]:
    """确定性判定产出物形态。

    返回 ``{"form", "label_zh", "label_en", "reason_zh", "reason_en",
    "signals"}``。form 为 ``investment_thesis`` 时 reason_* 为 None
    （干净发布的票无需自我声明）。
    """
    status = str(publishing_status or "").strip().lower()
    n_gaps = max(0, int(evidence_gap_hits or 0))
    n_open = max(0, int(open_question_count or 0))

    reasons_zh: list[str] = []
    reasons_en: list[str] = []
    if valuation_mismatch:
        reasons_zh.append("DCF 估值失配（超出可信带，应视为模型口径问题）")
        reasons_en.append("the DCF failed its magnitude sanity check (model artifact)")
    if n_gaps > 0:
        # R6 措辞修正：R5 比亚迪判词讥"证据缺口不是 1 处点缀"——计数现指
        # 核心叙事引用未闭合研究问题的处数（引擎 B3 同源重叠检查），
        # 表述为系统性缺口而非点状瑕疵。
        reasons_zh.append(
            f"证据缺口（核心叙事有 {n_gaps} 处引用仍未闭合的研究问题，"
            "论点把自承未知当已证实事实使用）")
        reasons_en.append(
            f"evidence gap ({n_gaps} core claim(s) cite still-open research "
            "questions as established fact)")
    if status in _REVIEW_STATUSES:
        reasons_zh.append(f"发布状态 {status}（未通过发布门 / 待人工复核）")
        reasons_en.append(f"publishing status is {status}")
    elif status == "downgraded" and n_open >= DOWNGRADED_OPEN_QUESTION_THRESHOLD:
        reasons_zh.append(f"降级发布且待解问题 {n_open} 项（关键变量未闭合）")
        reasons_en.append(f"downgraded with {n_open} unresolved open questions")

    signals = {
        "valuation_mismatch": bool(valuation_mismatch),
        "evidence_gap_hits": n_gaps,
        "publishing_status": status,
        "open_question_count": n_open,
    }
    if not reasons_zh:
        return {
            "form": INVESTMENT_THESIS,
            "label_zh": "投资论点",
            "label_en": "investment thesis",
            "reason_zh": None,
            "reason_en": None,
            "signals": signals,
        }
    reason_zh = (
        f"本产出物为「{_LABEL_ZH}」，而非可执行投资论点："
        + "；".join(reasons_zh)
        + "。其价值在于给出可监控的验证/证伪路径（监控点、阈值、证伪触发）；"
        "数据缺口闭合并通过发布门后方可升级为投资论点。"
    )
    reason_en = (
        f"This deliverable is a {_LABEL_EN}, not an actionable investment "
        "thesis: " + "; ".join(reasons_en) + ". Its value is the monitoring "
        "plan (what to watch, thresholds, falsifiers); it upgrades to an "
        "investment thesis only after the data gaps close and it clears the "
        "publish gate."
    )
    return {
        "form": OBSERVATION_FRAMEWORK,
        "label_zh": _LABEL_ZH,
        "label_en": _LABEL_EN,
        "reason_zh": reason_zh,
        "reason_en": reason_en,
        "signals": signals,
    }
