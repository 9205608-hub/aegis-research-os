"""Robust coercion helpers for LLM output parse boundaries.

LLM outputs (DeepSeek V4, Grok, Claude tool_use) occasionally serialize
list-typed schema fields as STRINGS containing JSON arrays
('["a", "b", "c"]') instead of returning the array directly. Downstream code
that does ``for x in lst`` then iterates character-by-character — symptom:
"Re-running 101 agents in parallel" when the actual list has 5 entries; or
log lines that read ``Key variables: [, ", a, ", ,, ", b, ", ]``.

Originally lived in `chief_analyst/thesis_synthesizer.py`; promoted to a
shared module (TODO-Y26) once we found ~17 unsafe call sites across
chief_analyst components and agent base.

Use:

    from aegis.core._coerce import coerce_list

    items = coerce_list(raw.get("scenarios", []))
    for s in items:
        ...
"""
from __future__ import annotations

import json
from typing import Any


def coerce_list(val: Any) -> list:
    """Robustly coerce LLM output to a Python list.

    Coerces:
      - None / missing → []
      - already-list → unchanged (passes through)
      - JSON-string of a list → parsed list
      - comma-separated string → split + strip (with quote-strip)
      - any other scalar → [scalar]

    Never raises. Stringification of a single dict element is preserved
    intact (the caller handles dict→model conversion).
    """
    if val is None:
        return []
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return []
        # Try JSON-encoded list first
        if s.startswith("[") and s.endswith("]"):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, list):
                    return parsed
            except (json.JSONDecodeError, ValueError):
                pass
        # Fall back to comma-split with quote/whitespace stripping
        return [p.strip().strip('"').strip("'") for p in s.split(",") if p.strip()]
    if isinstance(val, (tuple, set)):
        return list(val)
    return [val]


def coerce_dict(val: Any) -> dict:
    """Robustly coerce LLM output to a Python dict.

    BUG-Y25 的 dict 版（2026-07-13，R7 宁德实锤）：Director 的
    ``agent_depth`` / ``agent_emphasis`` 是 dict 字段，LLM 偶发把它序列化成
    JSON 字符串（'{"business_analyst": "deep", ...}'）。dataclass 不验类型，
    字符串一路传到 orchestrator ``agent_depth.get(n)`` 才炸——
    "'str' object has no attribute 'get'"，整条 run 报废。列表字段当年
    同款事故由 coerce_list 收口，dict 字段此前无人设防。

    Coerces:
      - None / missing → {}
      - already-dict → unchanged
      - JSON-string of an object → parsed dict
      - anything else → {}

    Never raises.
    """
    if isinstance(val, dict):
        return val
    if isinstance(val, str):
        s = val.strip()
        if s.startswith("{") and s.endswith("}"):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, dict):
                    return parsed
            except (json.JSONDecodeError, ValueError):
                pass
    return {}


def coerce_bool(val: Any, default: bool = False) -> bool:
    """Robustly coerce an LLM-emitted boolean field to a Python bool.

    hypothesis_validated 字符串真值 bug（2026-08-01）：SYNTHESIS_TOOL_SCHEMA
    已把 ``hypothesis_validated`` 声明为 ``"type": "boolean"``，但 LLM 不守
    schema 是本仓库的既定事实——偶发吐出字符串 ``"false"`` / ``"true"``。
    Python 真值规则下非空字符串恒为 True，``"false"`` 也不例外，于是
    orchestrator Step 12 的 CONFIRMED/REFUTED 判定永远看不到被推翻的假设。

    Coerces:
      - bool → unchanged (passes through)
      - string "true"/"yes"/"1" (case-insensitive, whitespace-stripped) → True
      - string "false"/"no"/"0" (case-insensitive, whitespace-stripped) → False
      - any other type, None, or unrecognized string → ``default``

    Never raises.
    """
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        s = val.strip().lower()
        if s in ("true", "yes", "1"):
            return True
        if s in ("false", "no", "0"):
            return False
    return default


def normalize_low_med_high(val: Any) -> str:
    """Normalize an LLM-emitted bucket value to low/medium/high.

    BUG-Y24/Y27: LLMs (DeepSeek V4 and other reasoning backends) occasionally emit compound bucket
    values like ``medium_high`` / ``high_medium`` / ``mediumlow`` that fail
    the strict ``^(low|medium|high)$`` Pydantic patterns on
    Inference.confidence, FollowUpQuestion.priority and all 4
    CognitiveBiasSelfCheck risk fields — collapsing the whole agent to mock.

    AUDIT-E1 (2026-07-09): promoted from a nested closure in
    ``llm_agent_base.run()`` to this shared module so it is directly
    unit-testable. Behavior unchanged.

    Returns the input as-is (lowercased) when already valid; maps compound
    values to the closest bucket; defaults unknown values to "medium".
    Never raises.
    """
    v = str(val or "").strip().lower().replace("-", "_").replace(" ", "_")
    if v in ("low", "medium", "high"):
        return v
    if v in ("medium_high", "high_medium", "med_high", "high_med", "mediumhigh", "very_high"):
        return "high"
    if v in ("medium_low", "low_medium", "med_low", "low_med", "mediumlow", "very_low"):
        return "low"
    return "medium"  # safe default


def normalize_strength(val: Any) -> str:
    """Normalize an LLM-emitted strength value to weak/moderate/strong.

    AUDIT-B4 (2026-07-09): Counterargument.strength has pattern
    ``^(weak|moderate|strong)$`` while the sibling confidence fields use
    ``low|medium|high`` — two enums in one schema, and LLMs mix them up
    (``strength="medium"`` is the BUG-Y24 failure mode all over again: one
    off-enum value used to ValidationError the entire agent back to the
    rule-based template). Same style as :func:`normalize_low_med_high`.

    Maps: medium/moderate_strong → moderate; very_strong/strongest (any
    case) → strong; mild/very_weak → weak; cross-enum low/high → weak/strong;
    unknown defaults to "moderate". Never raises.
    """
    v = str(val or "").strip().lower().replace("-", "_").replace(" ", "_")
    if v in ("weak", "moderate", "strong"):
        return v
    if v in ("medium", "moderate_strong", "strong_moderate", "med", "middle", "average"):
        return "moderate"
    if v in ("very_strong", "strongest", "stronger", "high", "very_high"):
        return "strong"
    if v in ("mild", "very_weak", "weakest", "weaker", "low", "very_low"):
        return "weak"
    return "moderate"  # safe default
