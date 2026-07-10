"""Shared JSON repair / truncation recovery for LLM clients.

History: this logic was originally embedded in `deepseek_client.py` (BUG-A20
v1-v4 series, 2026-05-05) where it salvaged ~5 truncations / run for V4-pro.
TODO-Y1 (2026-05-06): the same repair chain belongs on every LLM backend
because if DeepSeek is rate-limited and we fall back to Grok/SDK/Subprocess,
the user used to drop straight to a 17-min mock burn.

Two pieces:

- `repair_json(raw)` — incremental repair chain (control-char strip, trailing-
  comma cleanup, missing-comma between adjacent values, depth-0 search for a
  complete top-level object, then array-truncation salvage).

- `repair_truncated_array(raw)` — handles the dominant V4 failure mode where
  a deep array was cut mid-element. Walks the structure, finds the last safe
  drop point (after a complete element + comma at array depth), truncates
  there and closes outstanding `[`/`{` in stack order.
"""
from __future__ import annotations

import json


def repair_truncated_array(raw: str) -> dict:
    """Salvage a truncated JSON object by closing partial structures.

    Handles the dominant V4 failure mode: 8-10K-char output cut mid-array,
    looking like ``{"observations": [{...}, {...}, {"text": "abc``. The
    standard depth-0 repair finds nothing because no top-level brace
    ever closed.

    Strategy: walk forward with a structural stack, remember the last
    "safe drop point" (after a complete element + comma at array depth).
    On EOF, truncate to that point, close arrays then objects in stack
    order. If the stack has any "key without value" frames (``"foo":``
    seen but no value), pop them too.

    Raises ``json.JSONDecodeError`` if no salvage point exists.
    """
    stack: list[str] = []
    in_str = False
    escape = False
    last_safe = -1
    awaiting_value = False

    for i, c in enumerate(raw):
        if escape:
            escape = False
            continue
        if c == '\\' and in_str:
            escape = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if c in '{[':
            stack.append(c)
            awaiting_value = False
        elif c in '}]':
            if not stack:
                break
            opener = stack.pop()
            if (opener == '{' and c != '}') or (opener == '[' and c != ']'):
                break
            if stack and stack[-1] == '[':
                last_safe = i + 1
            awaiting_value = False
        elif c == ',':
            if stack and stack[-1] == '[':
                last_safe = i
            awaiting_value = False
        elif c == ':':
            awaiting_value = True

    if last_safe < 0:
        depth = 0
        in_s = False
        esc = False
        last_obj_end = -1
        for i, c in enumerate(raw):
            if esc:
                esc = False
                continue
            if c == '\\' and in_s:
                esc = True
                continue
            if c == '"':
                in_s = not in_s
                continue
            if in_s:
                continue
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth >= 1:
                    last_obj_end = i + 1
        if last_obj_end < 0:
            raise json.JSONDecodeError("array-truncation repair found no boundary", raw, 0)
        last_safe = last_obj_end

    truncated = raw[:last_safe].rstrip().rstrip(',')
    open_stack: list[str] = []
    in_str = False
    escape = False
    for c in truncated:
        if escape:
            escape = False
            continue
        if c == '\\' and in_str:
            escape = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if c in '{[':
            open_stack.append(c)
        elif c == '}' and open_stack and open_stack[-1] == '{':
            open_stack.pop()
        elif c == ']' and open_stack and open_stack[-1] == '[':
            open_stack.pop()

    closer_map = {'{': '}', '[': ']'}
    closing = "".join(closer_map[c] for c in reversed(open_stack))
    candidate = truncated + closing
    return json.loads(candidate)


def repair_json(raw: str) -> dict:
    """Incremental repair chain on malformed JSON from LLM output.

    Tries: control-char strip, trailing-comma cleanup, missing-comma fix,
    depth-0 search for a complete top-level object, array-truncation
    salvage. Each step is independent and falls through on failure.
    """
    import re

    fixed = re.sub(r'[\x00-\x1f]', ' ', raw)
    fixed = re.sub(r',\s*([}\]])', r'\1', fixed)
    try:
        return json.loads(fixed)
    except json.JSONDecodeError:
        pass

    fixed = re.sub(r'(\})\s*(\{)', r'\1,\2', fixed)
    fixed = re.sub(r'(\])\s*(\[)', r'\1,\2', fixed)
    fixed = re.sub(r'(")\s*\n\s*(")', r'\1,\2', fixed)
    fixed = re.sub(r'((?:true|false|null|\d+|"))\s*\n\s*(")', r'\1,\2', fixed)
    try:
        return json.loads(fixed)
    except json.JSONDecodeError:
        pass

    depth = 0
    last_valid = -1
    in_str = False
    escape = False
    for i, c in enumerate(fixed):
        if escape:
            escape = False
            continue
        if c == '\\' and in_str:
            escape = True
            continue
        if c == '"' and not escape:
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                last_valid = i
                break
    if last_valid > 0:
        try:
            return json.loads(fixed[:last_valid + 1])
        except json.JSONDecodeError:
            pass

    try:
        return repair_truncated_array(fixed)
    except json.JSONDecodeError:
        pass

    raise json.JSONDecodeError("All JSON repair attempts failed", raw, 0)
