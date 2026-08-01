"""Degraded-input (LLM-fallback) issue classification — shared helper.

问题背景 (2026-08-01)：agent 的 LLM 调用全部失败后，llm_agent_base 会退化到
MockLLMClient 的规则模板输出（llm_agent_base.py `run()` 的 fallback 分支），
并把 `AgentOutput.is_llm_fallback=True` 盖章；orchestrator 再用
`object.__setattr__` 把该标记转印到 frozen 的 JudgmentContract 上
（auto_research.py 3829-3837）。critic 面对这些模板判断会因证据不可闭合而
记 warn/block —— 这些是"输入退化"的系统性伪警告，不是分析缺陷，却曾被
publish_gate 的 warn 累计阈值和 decision_engine 的置信度扣分不分真伪地
一并计数，把整条 run 推向 blocked → 置信度封顶 low。

本模块给 publish_gate 与 decision_engine 提供同一套消费端分类器，把
degraded-input 来源的 issue 从"分析缺陷"中分离计数。

为什么在消费端分类、而不在 CriticIssue 上打 origin 标签：
- CriticIssue / CriticResult 是 frozen 的 pydantic StrictModel
  (extra="forbid")——issue 构造后不可变更，创建时打标则需要改动所有 critic
  的 issue 生成点；
- 缓存复用路径（auto_research C2 增量复用 / replay_from_cache）会原样反序列化
  历史 CriticResult，新增字段对旧缓存不存在。消费端按 judgment 归属分类，
  对新旧数据行为一致。

识别是双层的：
1. 结构化标记：`getattr(judgment, "is_llm_fallback", False)`——权威来源，
   但它是动态附加属性，不进 schema，序列化后丢失；
2. 文本标记：MockLLMClient 模板自带 "[rule-based fallback"（英文）/
   "[规则模板兜底"（中文，mock_client.py `FB` 前缀）——可穿越任何缓存
   round-trip。

issue 归类规则：`offending_judgment_ids` 非空且全部指向 degraded judgment
才算 degraded；空 ids（跨判断/全局 issue）或混合指向一律按真实分析问题
处理（保守——真实分析 warn 的行为一个都不能变）。
"""

from __future__ import annotations

from dataclasses import dataclass

# MockLLMClient 模板的机器可读前缀（前缀匹配，兼容
# "[rule-based fallback]" 与 "[rule-based fallback: timed out for …]"、
# "[规则模板兜底·调用超时]" 等变体）。
DEGRADED_TEXT_MARKERS: tuple[str, ...] = (
    "[rule-based fallback",
    "[规则模板兜底",
)


def is_degraded_judgment(judgment) -> bool:
    """判断是否为 LLM 兜底（mock 规则模板）产物。"""
    if getattr(judgment, "is_llm_fallback", False):
        return True
    texts: list[str] = []
    for obs in getattr(judgment, "observations", None) or []:
        texts.append(getattr(obs, "text", "") or "")
    for inf in getattr(judgment, "inferences", None) or []:
        texts.append(getattr(inf, "text", "") or "")
    for ca in getattr(judgment, "counterarguments", None) or []:
        texts.append(getattr(ca, "text", "") or "")
    return any(
        marker in text for text in texts for marker in DEGRADED_TEXT_MARKERS
    )


def degraded_judgment_ids(judgments) -> set[str]:
    """收集所有 degraded judgment 的 id 集合。"""
    return {
        j.judgment_id for j in (judgments or []) if is_degraded_judgment(j)
    }


def is_degraded_issue(issue, degraded_ids: set[str]) -> bool:
    """issue 归类：非空 offending ids 且全部落在 degraded 集合内。"""
    if not degraded_ids:
        return False
    ids = getattr(issue, "offending_judgment_ids", None) or []
    return bool(ids) and all(jid in degraded_ids for jid in ids)


@dataclass(frozen=True)
class DegradedIssueSplit:
    """warn/block 真伪分离计数结果。"""

    real_warns: int = 0
    degraded_warns: int = 0
    real_blocks: int = 0
    degraded_blocks: int = 0
    degraded_judgment_count: int = 0

    @property
    def degraded_total(self) -> int:
        return self.degraded_warns + self.degraded_blocks


def split_issue_counts(critic_results, judgments) -> DegradedIssueSplit:
    """按 issue 归属把 warn/block 计数拆成真实分析 vs 输入退化两列。"""
    degraded_ids = degraded_judgment_ids(judgments)
    real_warns = degraded_warns = real_blocks = degraded_blocks = 0
    for cr in critic_results or []:
        for issue in getattr(cr, "issues", None) or []:
            severity = getattr(issue, "severity", "")
            if severity not in ("warn", "block"):
                continue
            degraded = is_degraded_issue(issue, degraded_ids)
            if severity == "warn":
                if degraded:
                    degraded_warns += 1
                else:
                    real_warns += 1
            else:
                if degraded:
                    degraded_blocks += 1
                else:
                    real_blocks += 1
    return DegradedIssueSplit(
        real_warns=real_warns,
        degraded_warns=degraded_warns,
        real_blocks=real_blocks,
        degraded_blocks=degraded_blocks,
        degraded_judgment_count=len(degraded_ids),
    )
