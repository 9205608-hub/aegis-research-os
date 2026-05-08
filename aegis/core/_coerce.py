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
