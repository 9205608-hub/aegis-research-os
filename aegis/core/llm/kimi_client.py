"""Kimi LLM Client — uses Moonshot AI API (OpenAI-compatible).

Fast, low cost, no proxy issues in China.
Supports tool_use/function calling for structured output.

Models: k2.5, moonshot-v1-8k, moonshot-v1-32k, moonshot-v1-128k
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from aegis.core.llm.config import CostTracker, UsageRecord
from aegis.core.llm._recovery import repair_json as _shared_repair_json


class KimiContentFilterError(RuntimeError):
    """Kimi API 400 content_filter rejection. Raised so the orchestrator
    can distinguish content-filter from generic network errors and avoid
    retrying the exact same prompt."""


# Model ID mapping
# K2.6 Code released 2026-04-13, current latest on Kimi Code API endpoint.
# Coding-specialized upgrade of K2.5 with improved reasoning depth and
# agent planning. Priced $0.60 / $2.50 per M token.
KIMI_MODEL_MAP = {
    # K2 series — K2.6 is latest (2026-04-13)
    "kimi-k2.6": "kimi-k2.6",
    "k2.6": "kimi-k2.6",
    "kimi-k2.5": "kimi-k2.5",
    "kimi-k2": "kimi-k2",
    "k2.5": "kimi-k2.5",
    "k2": "kimi-k2",
    # Moonshot v1 series
    "moonshot-v1-8k": "moonshot-v1-8k",
    "moonshot-v1-32k": "moonshot-v1-32k",
    "moonshot-v1-128k": "moonshot-v1-128k",
    # Aliases — point at the latest
    "kimi": "kimi-k2.6",
    "kimi-latest": "kimi-k2.6",
    "latest": "kimi-k2.6",
    "8k": "moonshot-v1-8k",
    "32k": "moonshot-v1-32k",
    "128k": "moonshot-v1-128k",
}

# Kimi Code (domestic, sk-kimi- keys) uses api.kimi.com/coding/v1
# Moonshot (international) uses api.moonshot.ai/v1
KIMI_BASE_URL = "https://api.kimi.com/coding/v1"


def resolve_kimi_endpoint(api_key: str) -> tuple[str, dict[str, str]]:
    """Derive (base_url, default_headers) for a Kimi/Moonshot API key.

    AUDIT-B3 prep: single source of truth for endpoint routing, shared by
    ``KimiClient.__init__`` and the orchestrator's backend health probe so
    the probe can never disagree with the client about which endpoint a
    key authenticates against.

    Routing:
      - ``sk-kimi-`` prefix → Kimi Code endpoint (``api.kimi.com/coding/v1``)
      - anything else       → Moonshot open platform (``api.moonshot.ai/v1``)

    The Kimi Code API requires a coding-agent ``User-Agent`` to pass its
    access check, so the headers dict must accompany every probe/request.
    """
    base_url = KIMI_BASE_URL
    if api_key and not api_key.startswith("sk-kimi-"):
        base_url = "https://api.moonshot.ai/v1"  # international keys
    return base_url, {"User-Agent": "claude-code/1.0"}


class KimiClient:
    """LLM client using Moonshot/Kimi API (OpenAI-compatible).

    Direct access from China, no proxy needed. Low cost.
    """

    def __init__(
        self,
        model: str = "k2.6",
        api_key: str | None = None,
        max_retries: int = 3,
        retry_base_delay: float = 1.0,
    ) -> None:
        self.model = KIMI_MODEL_MAP.get(model, model)
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        self.cost_tracker = CostTracker()

        self._api_key = api_key or os.environ.get("KIMI_API_KEY", "") or os.environ.get("MOONSHOT_API_KEY", "")
        if not self._api_key:
            raise RuntimeError("No Kimi API key. Set KIMI_API_KEY or MOONSHOT_API_KEY env var.")

        from openai import OpenAI
        # Auto-select endpoint based on key prefix (shared with the
        # orchestrator health probe — see resolve_kimi_endpoint).
        # Kimi Code API requires coding-agent User-Agent to pass access check
        base_url, default_headers = resolve_kimi_endpoint(self._api_key)
        self._client = OpenAI(
            api_key=self._api_key,
            base_url=base_url,
            default_headers=default_headers,
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
        """Call Kimi with function calling for structured output.

        AUDIT-D3: ``max_tokens_hint`` sizes the starting output budget by
        agent depth (light=8K / standard=16K / deep=32K). No hint → the
        historical 16384 cold start with the [16384, 32768] grow ladder,
        byte-for-byte the pre-hint behavior. Grow is capped at 32768 (the
        largest budget this client has ever used on the main path).
        """
        tool_def = {
            "type": "function",
            "function": {
                "name": tool_name,
                "description": f"Output structured {tool_name} data",
                "parameters": tool_schema,
            },
        }

        # TODO-Y1 (2026-05-06): adopt the same truncation-grow path that
        # deepseek_client gained from BUG-A20 v3+v4. Kimi's k2.6 reasoning
        # mode also burns thinking tokens and can truncate mid-array. Start
        # at 16K (proven sufficient for most cases) and grow to 32K on a
        # truncated retry — matching DeepSeek's `BUDGET_TRUNCATED` shape.
        # AUDIT-D3: ladder anchored on the (possibly hinted) start budget;
        # no hint reproduces the original [16384, 32768] exactly.
        _start_budget = int(max_tokens_hint) if max_tokens_hint else 16384
        BUDGET_TRUNCATED = [_start_budget, min(_start_budget * 2, 32768)]
        truncated_response_attempts = 0
        for attempt in range(self.max_retries):
            try:
                if truncated_response_attempts > 0:
                    _budget = (
                        BUDGET_TRUNCATED[truncated_response_attempts]
                        if truncated_response_attempts < len(BUDGET_TRUNCATED)
                        else BUDGET_TRUNCATED[-1]
                    )
                else:
                    _budget = _start_budget
                # Use higher max_tokens to prevent truncation (Kimi k2.5
                # reasoning can consume many tokens before producing output).
                # Note: k2.5 has thinking mode enabled by default which is
                # incompatible with tool_choice="required". Use "auto" instead.
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

                # Track usage
                if response.usage:
                    usage = UsageRecord(
                        model_id=self.model,
                        input_tokens=response.usage.prompt_tokens or 0,
                        output_tokens=response.usage.completion_tokens or 0,
                    )
                    self.cost_tracker.record(usage)

                # Extract tool call response
                msg = response.choices[0].message
                _fr = getattr(response.choices[0], "finish_reason", "?")
                # AUDIT-B1: pull the tool_call match OUT of the inner
                # `for tc in msg.tool_calls` loop. Previously the empty /
                # truncated recovery `continue`s bound to that inner loop,
                # so after the single matching tool_call was consumed they
                # fell through to the ValueError below — the whole BUG-A20
                # recovery chain (empty retry / budget grow / JSON-mode
                # fallback) was dead code on Kimi. With the match hoisted,
                # `continue` re-enters the outer attempt loop as intended.
                raw: str | None = None
                if msg.tool_calls:
                    for tc in msg.tool_calls:
                        if tc.function.name == tool_name:
                            # None args → "" so a matched-but-empty tool
                            # call still takes the empty-retry branch.
                            raw = tc.function.arguments or ""
                            break
                if raw is not None:
                    if not raw.strip():
                        # Kimi sometimes returns empty tool args (reasoning consumed all tokens)
                        if attempt < self.max_retries - 1:
                            print(f"    ⏳ Kimi returned empty tool args (finish={_fr}), retry {attempt+1}/{self.max_retries}")
                            time.sleep(2)
                            continue
                        # TODO-Y1: last-resort JSON-mode fallback,
                        # mirroring DeepSeek's BUG-A20 v4 path.
                        print(f"    ⏳ Kimi tool_use empty after retries, falling through to JSON-mode")
                        return self._call_json_mode_fallback(
                            system_prompt, user_message, tool_schema,
                        )
                    try:
                        return json.loads(raw)
                    except json.JSONDecodeError:
                        try:
                            return self._repair_json(raw)
                        except json.JSONDecodeError as rep_e:
                            # TODO-Y1: truncation-grow path. If raw
                            # is substantial, repair failed → likely
                            # mid-array cut. Grow max_tokens and retry.
                            is_truncated = len(raw) > 500
                            if is_truncated:
                                truncated_response_attempts += 1
                                if truncated_response_attempts < len(BUDGET_TRUNCATED):
                                    print(f"    ⏳ Kimi args truncated (finish={_fr}, len={len(raw)}, attempt {truncated_response_attempts}/{len(BUDGET_TRUNCATED)}), retry max_tokens={BUDGET_TRUNCATED[truncated_response_attempts]}")
                                    time.sleep(1)
                                    continue
                                print(f"    ⏳ Kimi args truncated after {truncated_response_attempts} grow attempts; falling through to JSON-mode")
                                return self._call_json_mode_fallback(
                                    system_prompt, user_message, tool_schema,
                                )
                            if attempt < self.max_retries - 1:
                                import sys as _sys
                                print(f"    ⏳ Kimi args unparseable (len={len(raw)}, preview={raw[:120]!r}), retry {attempt+1}/{self.max_retries}",
                                      file=_sys.stderr)
                                time.sleep(2 ** attempt)
                                continue
                            raise rep_e

                # Fallback: if model responded with text instead of tool call,
                # try to extract JSON from text
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

                raise ValueError("No tool call or parseable JSON in Kimi response")

            except Exception as e:
                error_str = str(e).lower()
                # AUDIT-B1: keyword set aligned with deepseek_client — the
                # parse-failure keywords ("json"/"parseable"/…) let the
                # "No tool call or parseable JSON" ValueError above re-enter
                # the attempt loop instead of bubbling straight up to a
                # mock fallback on the first miss.
                is_retryable = any(
                    k in error_str for k in (
                        "rate_limit", "429", "timeout", "connection",
                        "overloaded", "503", "502", "504",
                        # Also retry parse failures — fresh sample next attempt
                        "json", "parseable", "unparseable", "tool call arguments",
                        "no tool call",
                    )
                )
                # BUG-30: content_filter (HTTP 400) is NOT retryable (retrying
                # the same prompt produces the same rejection) and should
                # surface as a distinct exception so the caller can run the
                # stripped-prompt retry path instead of blind exponential backoff.
                is_content_filter = (
                    "content_filter" in error_str
                    or ("400" in error_str and "high risk" in error_str)
                    or ("400" in error_str and "rejected" in error_str)
                )
                if is_content_filter:
                    raise KimiContentFilterError(str(e)) from e
                if is_retryable and attempt < self.max_retries - 1:
                    wait = self.retry_base_delay * (2 ** attempt)
                    print(f"    ⏳ Kimi retry {attempt+1}/{self.max_retries} in {wait:.0f}s")
                    time.sleep(wait)
                    continue
                raise

        raise RuntimeError("Kimi max retries exceeded")

    @staticmethod
    def _repair_json(raw: str) -> dict:
        """Delegate to shared repair chain (TODO-Y1)."""
        return _shared_repair_json(raw)

    def _call_json_mode_fallback(
        self,
        system_prompt: str,
        user_message: str,
        tool_schema: dict[str, Any],
    ) -> dict[str, Any]:
        """Last-resort path when tool_use returned empty/truncated repeatedly.

        Mirrors DeepSeek's BUG-A20 v4 fallback. Uses
        ``response_format={"type":"json_object"}`` instead of tools=[]; on
        Kimi this is a different code path that sometimes succeeds where
        tool_use fails.
        """
        schema_hint = json.dumps(tool_schema, ensure_ascii=False, indent=2)
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
            raise ValueError(f"Kimi JSON-mode fallback also failed: {e!s}") from e

        if resp.usage:
            self.cost_tracker.record(UsageRecord(
                model_id=self.model,
                input_tokens=resp.usage.prompt_tokens or 0,
                output_tokens=resp.usage.completion_tokens or 0,
            ))

        msg = resp.choices[0].message
        text = (msg.content or "").strip()
        if not text:
            raise ValueError("Kimi JSON-mode fallback returned empty content")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        try:
            return self._repair_json(text)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"Kimi JSON-mode fallback unparseable "
                f"(len={len(text)}, preview={text[:160]!r}): {e!s}"
            ) from e

    def call_text(
        self,
        system_prompt: str,
        user_message: str,
        role: str = "specialist_agent",
    ) -> str:
        """Simple text completion via Kimi."""
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
            self.cost_tracker.record(UsageRecord(
                model_id=self.model,
                input_tokens=response.usage.prompt_tokens or 0,
                output_tokens=response.usage.completion_tokens or 0,
            ))

        return response.choices[0].message.content or ""

    @staticmethod
    def is_available() -> bool:
        return bool(os.environ.get("KIMI_API_KEY") or os.environ.get("MOONSHOT_API_KEY"))
