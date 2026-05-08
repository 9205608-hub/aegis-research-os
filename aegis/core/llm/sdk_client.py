"""SDK LLM Client — uses Anthropic Python SDK with OAuth token.

Leverages the user's Claude Max subscription via CLAUDE_CODE_OAUTH_TOKEN.
Handles proxy and SSL configuration automatically for China-based users.

No subprocess overhead — direct API calls with proper retry and rate limiting.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from aegis.core.llm.config import CostTracker, LLMConfig, ModelProfile, UsageRecord
from aegis.core.llm._recovery import repair_json as _shared_repair_json


# Model ID mapping: short name → full model ID
MODEL_ID_MAP = {
    "sonnet": "claude-sonnet-4-20250514",
    "opus": "claude-opus-4-20250514",
    "haiku": "claude-haiku-4-5-20251001",
}


class SDKClient:
    """LLM client using Anthropic Python SDK with OAuth token.

    Uses CLAUDE_CODE_OAUTH_TOKEN from the Claude Max subscription.
    Automatically detects and configures proxy settings.
    """

    def __init__(
        self,
        model: str = "sonnet",
        api_key: str | None = None,
        max_retries: int = 5,
        retry_base_delay: float = 2.0,
    ) -> None:
        self.model = model
        self.model_id = MODEL_ID_MAP.get(model, model)
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        self.cost_tracker = CostTracker()

        # Resolve API key
        self._api_key = (
            api_key
            or os.environ.get("ANTHROPIC_API_KEY")
            or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
        )
        if not self._api_key:
            raise RuntimeError(
                "No API key found. Set ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN."
            )

        # Initialize Anthropic client with proxy support
        self._client = self._create_client()

    def _create_client(self):
        """Create Anthropic client with proxy and SSL handling."""
        import anthropic

        # Detect proxy from environment
        proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")

        if proxy_url:
            import httpx
            http_client = httpx.Client(
                proxy=proxy_url,
                verify=False,  # Needed for Clash Verge and similar proxies
                timeout=httpx.Timeout(120.0, connect=30.0),
            )
            return anthropic.Anthropic(
                api_key=self._api_key,
                http_client=http_client,
            )

        return anthropic.Anthropic(api_key=self._api_key)

    def call_structured(
        self,
        system_prompt: str,
        user_message: str,
        tool_schema: dict[str, Any],
        tool_name: str = "output",
        role: str = "specialist_agent",
        **kwargs,
    ) -> dict[str, Any]:
        """Call Claude API with tool_use for structured output.

        Returns parsed dict matching tool_schema.
        """
        tool_def = {
            "name": tool_name,
            "description": f"Output structured {tool_name} data",
            "input_schema": tool_schema,
        }

        # TODO-Y1 (2026-05-06): grow max_tokens on truncation, mirroring
        # DeepSeek's BUG-A20 v3 grow path. Anthropic surfaces truncation as
        # `stop_reason="max_tokens"`, so detection is straightforward (more
        # reliable than DeepSeek's finish_reason which we found unreliable).
        BUDGET_TRUNCATED = [8192, 16384, 32768]
        truncated_attempts = 0
        for attempt in range(self.max_retries):
            try:
                if truncated_attempts > 0:
                    _budget = (
                        BUDGET_TRUNCATED[truncated_attempts]
                        if truncated_attempts < len(BUDGET_TRUNCATED)
                        else 32768
                    )
                else:
                    _budget = 8192
                response = self._client.messages.create(
                    model=self.model_id,
                    max_tokens=_budget,
                    temperature=0.2,
                    system=system_prompt,
                    messages=[{"role": "user", "content": user_message}],
                    tools=[tool_def],
                    tool_choice={"type": "tool", "name": tool_name},
                )

                # Track usage
                usage = UsageRecord(
                    model_id=self.model_id,
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    cache_read_tokens=getattr(response.usage, "cache_read_input_tokens", 0),
                    cache_creation_tokens=getattr(response.usage, "cache_creation_input_tokens", 0),
                )
                self.cost_tracker.record(usage)

                # TODO-Y1: detect truncation via stop_reason and grow budget.
                _stop = getattr(response, "stop_reason", None)
                if _stop == "max_tokens":
                    truncated_attempts += 1
                    if truncated_attempts < len(BUDGET_TRUNCATED):
                        print(
                            f"    ⏳ SDK response truncated (stop=max_tokens, "
                            f"attempt {truncated_attempts}/{len(BUDGET_TRUNCATED)}), "
                            f"retry max_tokens={BUDGET_TRUNCATED[truncated_attempts]}"
                        )
                        time.sleep(1)
                        continue
                    # Exhausted grow budget — try to salvage whatever partial
                    # tool_use input came back rather than raise outright.

                # Extract tool_use response
                for block in response.content:
                    if block.type == "tool_use" and block.name == tool_name:
                        # Anthropic SDK pre-parses tool_use input — usually
                        # we can return it directly. On truncation though
                        # the SDK may return a partial dict that's missing
                        # required fields; the orchestrator's quality gate
                        # handles that downstream.
                        return block.input

                raise ValueError(
                    f"No tool_use block found in response (stop={_stop})"
                )

            except Exception as e:
                error_str = str(e).lower()
                is_retryable = any(
                    k in error_str
                    for k in ("rate_limit", "overloaded", "529", "429", "timeout", "connection")
                )
                if is_retryable and attempt < self.max_retries - 1:
                    wait = self.retry_base_delay * (2 ** attempt)
                    print(f"    ⏳ Retry {attempt+1}/{self.max_retries} in {wait:.0f}s ({type(e).__name__})")
                    time.sleep(wait)
                    continue
                if attempt == self.max_retries - 1:
                    raise
                raise

        raise RuntimeError("Max retries exceeded")

    def call_text(
        self,
        system_prompt: str,
        user_message: str,
        role: str = "specialist_agent",
    ) -> str:
        """Simple text completion."""
        for attempt in range(self.max_retries):
            try:
                response = self._client.messages.create(
                    model=self.model_id,
                    max_tokens=4096,
                    temperature=0.2,
                    system=system_prompt,
                    messages=[{"role": "user", "content": user_message}],
                )

                usage = UsageRecord(
                    model_id=self.model_id,
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                )
                self.cost_tracker.record(usage)

                return response.content[0].text

            except Exception as e:
                error_str = str(e).lower()
                is_retryable = any(
                    k in error_str
                    for k in ("rate_limit", "overloaded", "529", "429", "timeout", "connection")
                )
                if is_retryable and attempt < self.max_retries - 1:
                    wait = self.retry_base_delay * (2 ** attempt)
                    time.sleep(wait)
                    continue
                raise

        raise RuntimeError("Max retries exceeded")

    @staticmethod
    def is_available() -> bool:
        """Check if SDK mode is available (OAuth token exists)."""
        return bool(
            os.environ.get("ANTHROPIC_API_KEY")
            or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
        )
