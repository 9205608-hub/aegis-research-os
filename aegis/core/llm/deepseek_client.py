"""DeepSeek LLM Client — uses DeepSeek API (OpenAI-compatible).

Direct access from China, low cost. Supports tool_use/function calling for
structured output. Shares the same client surface as the SDK / subprocess
backends so the orchestrator can swap transparently.

Models: deepseek-chat (always points to latest GA chat model, currently V4),
deepseek-reasoner (R-series reasoning model).
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from aegis.core.llm.config import CostTracker, UsageRecord
from aegis.core.llm._recovery import (
    repair_json as _shared_repair_json,
    repair_truncated_array as _shared_repair_truncated_array,
)


class DeepSeekContentFilterError(RuntimeError):
    """DeepSeek API 400 content_filter rejection. Raised so the orchestrator
    can distinguish content-filter from generic network errors and avoid
    retrying the exact same prompt."""


# DeepSeek V4 GA exposes two tiers via /v1/models (verified 2026-05-05):
#   - deepseek-v4-pro   — flagship analytical model (best reasoning, slower)
#   - deepseek-v4-flash — speed-optimized variant (cheaper, ~2x faster)
# `deepseek-chat` and `deepseek-reasoner` are legacy V3 IDs that still
# auto-route on the API but are NOT the canonical V4 names. Pin the
# canonical V4 IDs by default so cost/perf is predictable.
DEEPSEEK_MODEL_MAP = {
    # Canonical V4 IDs (passthrough)
    "deepseek-v4-pro": "deepseek-v4-pro",
    "deepseek-v4-flash": "deepseek-v4-flash",
    # Convenience aliases — all "best chat model" requests route to v4-pro
    "v4": "deepseek-v4-pro",
    "deepseek-v4": "deepseek-v4-pro",
    "deepseek": "deepseek-v4-pro",
    "latest": "deepseek-v4-pro",
    "pro": "deepseek-v4-pro",
    "max": "deepseek-v4-pro",
    "flash": "deepseek-v4-flash",
    # Legacy V3 IDs — kept routable for older configs / replay caches.
    # Auto-route to v4-pro since the V3 "deepseek-chat" endpoint silently
    # bumps to current GA anyway.
    "deepseek-chat": "deepseek-v4-pro",
    "deepseek-reasoner": "deepseek-v4-pro",
    "reasoner": "deepseek-v4-pro",
    "r1": "deepseek-v4-pro",
}

DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"


# TODO-Y1 (2026-05-06): the `_repair_json` and `_repair_truncated_array`
# helpers used to live here. They were extracted to `aegis.core.llm._recovery`
# so SDK/Subprocess can share the same logic. Kept the names below as
# thin wrappers / module-level aliases for back-compat.
_repair_truncated_array = _shared_repair_truncated_array


class DeepSeekClient:
    """LLM client using DeepSeek API (OpenAI-compatible).

    Direct access from China, no proxy needed. Low cost.
    """

    def __init__(
        self,
        model: str = "deepseek-v4-pro",
        api_key: str | None = None,
        max_retries: int = 3,
        retry_base_delay: float = 1.0,
    ) -> None:
        self.model = DEEPSEEK_MODEL_MAP.get(model, model)
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        self.cost_tracker = CostTracker()

        self._api_key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
        if not self._api_key:
            raise RuntimeError("No DeepSeek API key. Set DEEPSEEK_API_KEY env var.")

        from openai import OpenAI
        self._client = OpenAI(
            api_key=self._api_key,
            base_url=DEEPSEEK_BASE_URL,
        )

    def call_structured(
        self,
        system_prompt: str,
        user_message: str,
        tool_schema: dict[str, Any],
        tool_name: str = "output",
        role: str = "specialist_agent",
        max_tokens_hint: int | None = None,
        **kwargs,
    ) -> dict[str, Any]:
        """Call DeepSeek with function calling for structured output.

        AUDIT-D3: ``max_tokens_hint`` lets callers size the starting output
        budget by agent depth (light=8K / standard=16K / deep=32K). Without
        a hint the cold start stays at the historical flat 32768 — and per
        the BUG-A20 notes below, a bigger budget makes the model think
        longer, so light calls were paying a long-tail latency tax for
        headroom they never used. The empty-shrink / truncated-grow ladders
        derive from the start budget, so the no-hint default reproduces the
        old [32768,16384] / [32768,65536] behavior exactly.
        """
        tool_def = {
            "type": "function",
            "function": {
                "name": tool_name,
                "description": f"Output structured {tool_name} data",
                "parameters": tool_schema,
            },
        }

        # tool_choice (2026-05-05 finding): DeepSeek-V4 (both -pro and
        # -flash) are reasoning models that DO NOT support forced or
        # required tool_choice — server returns 400 "deepseek-reasoner
        # does not support this tool_choice". Only "auto" works. We
        # accept the reliability hit (~30% of complex prompts return
        # empty tool_call args) and rely on retry logic + the JSON-mode
        # fallback path to recover.
        #
        # BUG-A20 (2026-05-05 baseline run, REVISED): V4 has TWO failure
        # modes that look similar but need OPPOSITE fixes:
        #   (a) Empty response: tool_calls=[] AND content="" — model burnt
        #       its thinking budget and emitted nothing. Symptom: args=""
        #       or response.choices[0].finish_reason in ("stop","tool_calls")
        #       with no actual content. Fix: SHRINK max_tokens on retry to
        #       force the model to commit faster.
        #   (b) Truncated response: tool_calls present, args has 8-10K
        #       chars but JSON cuts off mid-array. Symptom: finish_reason=
        #       "length" with non-empty raw. Fix: GROW max_tokens on retry
        #       to give the model room to finish.
        # Treating both with the same shrink-budget policy makes (b) worse,
        # not better. Branch by finish_reason on first failure.
        # AUDIT-D3: ladders anchored on the (possibly hinted) start budget.
        # No hint → identical to the historical constants.
        _start_budget = int(max_tokens_hint) if max_tokens_hint else 32768
        BUDGET_EMPTY = [_start_budget, max(_start_budget // 2, 4096)]   # thinking too long → shrink
        BUDGET_TRUNCATED = [_start_budget, min(_start_budget * 2, 65536)]  # output too long → grow
        empty_response_attempts = 0
        truncated_response_attempts = 0
        for attempt in range(self.max_retries):
            try:
                strategy = "auto"
                # BUG-A20: pick budget by which failure mode we're recovering
                # from. Empty → shrink (force commit); truncated → grow.
                # Default 32K on cold start (no prior failure observed).
                if truncated_response_attempts > 0:
                    _budget = (
                        BUDGET_TRUNCATED[truncated_response_attempts]
                        if truncated_response_attempts < len(BUDGET_TRUNCATED)
                        else BUDGET_TRUNCATED[-1]
                    )
                elif empty_response_attempts > 0:
                    _budget = (
                        BUDGET_EMPTY[empty_response_attempts]
                        if empty_response_attempts < len(BUDGET_EMPTY)
                        else BUDGET_EMPTY[-1]
                    )
                else:
                    _budget = _start_budget
                create_kwargs = dict(
                    model=self.model,
                    max_tokens=_budget,
                    temperature=0.2,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_message},
                    ],
                    tools=[tool_def],
                    tool_choice="auto",
                )
                response = self._client.chat.completions.create(**create_kwargs)

                if response.usage:
                    # DeepSeek exposes cache hits in two equivalent forms
                    # depending on API version:
                    #   - prompt_cache_hit_tokens (top-level)
                    #   - prompt_tokens_details.cached_tokens (nested,
                    #     OpenAI-compatible naming, V4 default)
                    # Read both and prefer whichever is non-zero.
                    _cache_hit = getattr(response.usage, "prompt_cache_hit_tokens", 0) or 0
                    if not _cache_hit:
                        _details = getattr(response.usage, "prompt_tokens_details", None)
                        if _details is not None:
                            _cache_hit = (
                                getattr(_details, "cached_tokens", 0)
                                or (_details.get("cached_tokens", 0) if isinstance(_details, dict) else 0)
                                or 0
                            )
                    # TODO-X6: completion_tokens_details.reasoning_tokens
                    # carries the thinking-budget portion of the output.
                    # Already counted in completion_tokens for billing — we
                    # surface it separately for diagnostics ("how much of
                    # this call was thinking vs answer?").
                    _reasoning_tk = 0
                    _cdetails = getattr(response.usage, "completion_tokens_details", None)
                    if _cdetails is not None:
                        _reasoning_tk = (
                            getattr(_cdetails, "reasoning_tokens", 0)
                            or (_cdetails.get("reasoning_tokens", 0) if isinstance(_cdetails, dict) else 0)
                            or 0
                        )
                    # TODO-X5: capture a snippet of the chain-of-thought
                    # (`reasoning_content`) so post-mortem of empty-args
                    # failures has signal beyond "tool args were blank".
                    _reasoning_prev = ""
                    try:
                        _rc = getattr(response.choices[0].message, "reasoning_content", None) or ""
                        if _rc:
                            _reasoning_prev = _rc[:600]
                    except Exception:
                        pass
                    usage = UsageRecord(
                        model_id=self.model,
                        input_tokens=response.usage.prompt_tokens or 0,
                        output_tokens=response.usage.completion_tokens or 0,
                        cache_read_tokens=_cache_hit,
                        cache_creation_tokens=0,  # DeepSeek caches automatically; no explicit creation
                        reasoning_tokens=_reasoning_tk,
                        reasoning_preview=_reasoning_prev,
                    )
                    self.cost_tracker.record(usage)

                msg = response.choices[0].message
                # BUG-A20: capture finish_reason so empty-response diagnosis
                # has a real signal beyond "raw was blank". finish_reason=
                # "length" means we ran out of max_tokens (likely all spent
                # on thinking); "stop" with empty content means model just
                # decided not to answer.
                _fr = getattr(response.choices[0], "finish_reason", "?")
                if msg.tool_calls:
                    for tc_obj in msg.tool_calls:
                        if tc_obj.function.name == tool_name:
                            raw = tc_obj.function.arguments
                            if not raw or not raw.strip():
                                # BUG-A20: deterministic-failure path. Cap
                                # this at len(BUDGET_EMPTY) attempts
                                # (default 2) instead of full max_retries
                                # (default 3) — retrying same prompt at
                                # same budget gives same empty result.
                                empty_response_attempts += 1
                                if empty_response_attempts < len(BUDGET_EMPTY):
                                    print(f"    ⏳ DeepSeek empty tool args "
                                          f"(finish={_fr}, attempt {empty_response_attempts}/"
                                          f"{len(BUDGET_EMPTY)}), "
                                          f"retry with max_tokens={BUDGET_EMPTY[empty_response_attempts]}")
                                    time.sleep(1)
                                    continue
                                # Exhausted empty-retry budget → try JSON-mode
                                # (no tool_use) as last resort before raising.
                                print(f"    ⏳ DeepSeek tool_use empty after "
                                      f"{empty_response_attempts} attempts; "
                                      f"falling through to JSON-mode (no tool_use)")
                                return self._call_json_mode_fallback(
                                    system_prompt, user_message, tool_schema,
                                )
                            try:
                                return json.loads(raw)
                            except json.JSONDecodeError:
                                # Surface a snippet of the unparseable raw on
                                # final-attempt failure so debugging doesn't
                                # require enabling client-side request logging.
                                try:
                                    return self._repair_json(raw)
                                except json.JSONDecodeError as rep_e:
                                    # BUG-A20 (revised v3, 2026-05-05):
                                    # validation run showed `finish_reason`
                                    # is NOT reliably "length" on V4 even
                                    # when args are clearly truncated mid-
                                    # JSON (validation log: len=9120 / 9346
                                    # both went through OLD same-budget
                                    # retry, never hit my length branch).
                                    # Use raw length as the truncation
                                    # signal instead — repair fails AND raw
                                    # has substantial content (>500 chars)
                                    # → almost certainly truncated, grow.
                                    is_truncated = len(raw) > 500
                                    if is_truncated:
                                        truncated_response_attempts += 1
                                        if truncated_response_attempts < len(BUDGET_TRUNCATED):
                                            import sys as _sys
                                            print(f"    ⏳ DeepSeek args truncated "
                                                  f"(finish={_fr}, len={len(raw)}, "
                                                  f"attempt {truncated_response_attempts}/"
                                                  f"{len(BUDGET_TRUNCATED)}), "
                                                  f"retry with max_tokens={BUDGET_TRUNCATED[truncated_response_attempts]}",
                                                  file=_sys.stderr)
                                            time.sleep(1)
                                            continue
                                        # Truncated-budget exhausted → JSON-mode
                                        print(f"    ⏳ DeepSeek args truncated after "
                                              f"{truncated_response_attempts} attempts "
                                              f"at growing budget; falling through to JSON-mode")
                                        return self._call_json_mode_fallback(
                                            system_prompt, user_message, tool_schema,
                                        )
                                    if attempt < self.max_retries - 1:
                                        import sys as _sys
                                        print(f"    ⏳ DeepSeek args unparseable (strategy={strategy}, len={len(raw)}, preview={raw[:120]!r}), retry {attempt+1}/{self.max_retries}",
                                              file=_sys.stderr)
                                        time.sleep(2 ** attempt)
                                        continue
                                    raise rep_e

                if msg.content:
                    text = msg.content
                    try:
                        return json.loads(text)
                    except json.JSONDecodeError:
                        pass
                    import re
                    json_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
                    if json_match:
                        try:
                            return json.loads(json_match.group(1))
                        except json.JSONDecodeError:
                            pass
                    brace_match = re.search(r'\{.*\}', text, re.DOTALL)
                    if brace_match:
                        try:
                            return json.loads(brace_match.group(0))
                        except json.JSONDecodeError:
                            pass

                # BUG-A20: completely empty response (no tool_calls AND no
                # content). Treat same as empty tool_args — short-circuit
                # to JSON-mode fallback rather than retry-burn.
                if not msg.tool_calls and not (msg.content or "").strip():
                    empty_response_attempts += 1
                    if empty_response_attempts < len(BUDGET_EMPTY):
                        print(f"    ⏳ DeepSeek empty response (no tool_calls, "
                              f"no content; finish={_fr}, attempt "
                              f"{empty_response_attempts}/{len(BUDGET_EMPTY)}), "
                              f"retry with max_tokens={BUDGET_EMPTY[empty_response_attempts]}")
                        time.sleep(1)
                        continue
                    print(f"    ⏳ DeepSeek empty response after "
                          f"{empty_response_attempts} attempts; "
                          f"falling through to JSON-mode")
                    return self._call_json_mode_fallback(
                        system_prompt, user_message, tool_schema,
                    )

                raise ValueError(f"No tool call or parseable JSON in DeepSeek response (strategy={strategy}, finish={_fr}, content_len={len(msg.content or '')})")

            except Exception as e:
                error_str = str(e).lower()
                is_retryable = any(
                    k in error_str for k in (
                        "rate_limit", "429", "timeout", "connection",
                        "overloaded", "503", "502", "504",
                        # Also retry parse failures — flips strategy on next attempt
                        "json", "parseable", "unparseable", "tool call arguments",
                        "no tool call",
                    )
                )
                is_content_filter = (
                    "content_filter" in error_str
                    or ("400" in error_str and "high risk" in error_str)
                    or ("400" in error_str and "rejected" in error_str)
                )
                if is_content_filter:
                    raise DeepSeekContentFilterError(str(e)) from e
                if is_retryable and attempt < self.max_retries - 1:
                    wait = self.retry_base_delay * (2 ** attempt)
                    print(f"    ⏳ DeepSeek retry {attempt+1}/{self.max_retries} in {wait:.0f}s")
                    time.sleep(wait)
                    continue
                raise

        raise RuntimeError("DeepSeek max retries exceeded")

    def _call_json_mode_fallback(
        self,
        system_prompt: str,
        user_message: str,
        tool_schema: dict[str, Any],
    ) -> dict[str, Any]:
        """BUG-A20: last-resort path when tool_use returned empty repeatedly.

        Uses `response_format={"type":"json_object"}` instead of tools=[].
        Different code path on the server — sometimes succeeds where
        tool_use produced empty (the model commits to plain JSON output
        instead of negotiating a tool call). Schema is embedded in the
        user message as a hint, not enforced server-side.

        max_tokens=12K because by this point we've already burnt the bigger
        budgets twice; if 12K isn't enough the call is hopeless and the
        caller should fall to mock.
        """
        import json as _json
        schema_hint = _json.dumps(tool_schema, ensure_ascii=False, indent=2)
        amended_user = (
            user_message
            + "\n\n=== JSON OUTPUT REQUIRED ===\n"
            + "Respond with a single JSON object that conforms to this schema. "
            + "No prose, no markdown fence — JUST the JSON.\n\n"
            + schema_hint
        )
        try:
            resp = self._client.chat.completions.create(
                model=self.model,
                max_tokens=12288,
                temperature=0.2,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": amended_user},
                ],
                response_format={"type": "json_object"},
            )
        except Exception as e:
            raise ValueError(
                f"DeepSeek JSON-mode fallback also failed: {e!s}"
            ) from e

        if resp.usage:
            self.cost_tracker.record(UsageRecord(
                model_id=self.model,
                input_tokens=resp.usage.prompt_tokens or 0,
                output_tokens=resp.usage.completion_tokens or 0,
            ))

        msg = resp.choices[0].message
        text = (msg.content or "").strip()
        if not text:
            raise ValueError(
                "DeepSeek JSON-mode fallback returned empty content"
            )
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        try:
            return self._repair_json(text)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"DeepSeek JSON-mode fallback unparseable "
                f"(len={len(text)}, preview={text[:160]!r}): {e!s}"
            ) from e

    @staticmethod
    def _repair_json(raw: str) -> dict:
        """Delegate to shared repair chain (TODO-Y1)."""
        return _shared_repair_json(raw)

    def call_text(
        self,
        system_prompt: str,
        user_message: str,
        role: str = "specialist_agent",
    ) -> str:
        """Simple text completion via DeepSeek."""
        response = self._client.chat.completions.create(
            model=self.model,
            max_tokens=4096,
            temperature=0.2,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
        )

        if response.usage:
            _reasoning_tk = 0
            _cdetails = getattr(response.usage, "completion_tokens_details", None)
            if _cdetails is not None:
                _reasoning_tk = (
                    getattr(_cdetails, "reasoning_tokens", 0)
                    or (_cdetails.get("reasoning_tokens", 0) if isinstance(_cdetails, dict) else 0)
                    or 0
                )
            self.cost_tracker.record(UsageRecord(
                model_id=self.model,
                input_tokens=response.usage.prompt_tokens or 0,
                output_tokens=response.usage.completion_tokens or 0,
                reasoning_tokens=_reasoning_tk,
            ))

        return response.choices[0].message.content or ""

    @staticmethod
    def is_available() -> bool:
        return bool(os.environ.get("DEEPSEEK_API_KEY"))
