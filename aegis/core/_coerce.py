"""Robust coercion helpers for LLM output parse boundaries.

LLM outputs (DeepSeek V4, Kimi k2.6, Claude tool_use) occasionally serialize
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


def normalize_low_med_high(val: Any) -> str:
    """Normalize an LLM-emitted bucket value to low/medium/high.

    BUG-Y24/Y27: LLMs (DeepSeek V4, Kimi) occasionally emit compound bucket
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
