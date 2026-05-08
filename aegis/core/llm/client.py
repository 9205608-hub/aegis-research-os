"""LLM Client — Claude API wrapper with tool_use structured output.

Section 28.2: structured output via tool_use, fallback to json_mode,
validation via pydantic_v2.
"""

from __future__ import annotations

import json
import time
from typing import Any

from aegis.core.llm.config import CostTracker, LLMConfig, LLMMode, ModelProfile, UsageRecord


class LLMClient:
    """Wrapper around Anthropic Claude API.

    Features:
    - tool_use for structured output (primary method)
    - Automatic retry on rate limits
    - Token usage tracking
    - Prompt caching support
    """

    def __init__(self, config: LLMConfig | None = None) -> None:
        self.config = config or LLMConfig.from_env()
        self.cost_tracker = CostTracker()
        self._client = None

        if self.config.mode == LLMMode.LIVE:
            try:
                import anthropic
                self._client = anthropic.Anthropic(api_key=self.config.api_key)
            except ImportError:
                raise ImportError("anthropic SDK not installed. Run: pip install anthropic")
            except Exception as e:
                raise RuntimeError(f"Failed to initialize Anthropic client: {e}")

    def call_structured(
        self,
        system_prompt: str,
        user_message: str,
        tool_schema: dict[str, Any],
        tool_name: str = "output",
        role: str = "specialist_agent",
        max_retries: int = 3,
    ) -> dict[str, Any]:
        """Call Claude with tool_use to get structured JSON output.

        Args:
            system_prompt: System-level instructions.
            user_message: The user/analysis prompt.
            tool_schema: JSON Schema for the tool output.
            tool_name: Name of the tool (default "output").
            role: Agent role for model selection.
            max_retries: Retry count for transient failures.

        Returns:
            Parsed dict matching tool_schema.
        """
        if self.config.mode == LLMMode.MOCK:
            raise RuntimeError("call_structured() called in MOCK mode. Use MockLLMClient instead.")

        profile = self.config.get_profile(role)

        tool_def = {
            "name": tool_name,
            "description": f"Output structured {tool_name} data",
            "input_schema": tool_schema,
        }

        for attempt in range(max_retries):
            try:
                response = self._client.messages.create(
                    model=profile.model_id,
                    max_tokens=profile.max_tokens,
                    temperature=profile.temperature,
                    system=system_prompt,
                    messages=[{"role": "user", "content": user_message}],
                    tools=[tool_def],
                    tool_choice={"type": "tool", "name": tool_name},
                )

                # Track usage
                usage = UsageRecord(
                    model_id=profile.model_id,
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    cache_read_tokens=getattr(response.usage, "cache_read_input_tokens", 0),
                    cache_creation_tokens=getattr(response.usage, "cache_creation_input_tokens", 0),
                )
                self.cost_tracker.record(usage)

                # Extract tool_use response
                for block in response.content:
                    if block.type == "tool_use" and block.name == tool_name:
                        return block.input

                raise ValueError("No tool_use block found in response")

            except Exception as e:
                error_str = str(e)
                if "rate_limit" in error_str.lower() and attempt < max_retries - 1:
                    wait = 2 ** attempt
                    time.sleep(wait)
                    continue
                if attempt == max_retries - 1:
                    raise

        raise RuntimeError("Max retries exceeded")

    def call_text(
        self,
        system_prompt: str,
        user_message: str,
        role: str = "specialist_agent",
    ) -> str:
        """Simple text completion (for reports, translations, etc.)."""
        if self.config.mode == LLMMode.MOCK:
            raise RuntimeError("call_text() called in MOCK mode.")

        profile = self.config.get_profile(role)

        response = self._client.messages.create(
            model=profile.model_id,
            max_tokens=profile.max_tokens,
            temperature=profile.temperature,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )

        usage = UsageRecord(
            model_id=profile.model_id,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
        self.cost_tracker.record(usage)

        return response.content[0].text
