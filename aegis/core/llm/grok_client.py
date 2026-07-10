"""Grok LLM Client — uses xAI API (OpenAI-compatible).

Thin wrapper over DeepSeekClient's OpenAI-compatible transport so the whole
BUG-A20 recovery chain (empty-retry shrink, truncation grow, JSON-mode
fallback) and the AUDIT-D3 ``max_tokens_hint`` depth tiers apply to Grok
without duplication.

Key resolution: GROK_API_KEY, falling back to XAI_API_KEY (xAI's own
canonical env var name).

Model resolution: constructor arg > GROK_MODEL env var > "grok-4".
NOTE: the "grok-4" default has NOT been validated against a live key (none
available at integration time, 2026-07-10) — if xAI renames the flagship
model, override via ``export GROK_MODEL=...`` without touching code.

Requires proxy access from mainland China (api.x.ai is not directly
reachable) — same Clash Verge setup as the Anthropic SDK path.
"""

from __future__ import annotations

import os

from aegis.core.llm.deepseek_client import DeepSeekClient

GROK_BASE_URL = "https://api.x.ai/v1"
GROK_DEFAULT_MODEL = "grok-4"


def default_grok_model() -> str:
    """Default Grok model id; env-overridable (see module docstring)."""
    return os.environ.get("GROK_MODEL", "") or GROK_DEFAULT_MODEL


class GrokClient(DeepSeekClient):
    """LLM client using xAI's Grok API (OpenAI-compatible).

    Inherits call_structured / call_text — including retry ladders, the
    JSON-mode fallback and cost tracking — from DeepSeekClient; only the
    endpoint, key resolution and model naming differ.
    """

    _provider = "Grok"

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        max_retries: int = 3,
        retry_base_delay: float = 1.0,
    ) -> None:
        super().__init__(
            model=model or default_grok_model(),
            api_key=api_key,
            max_retries=max_retries,
            retry_base_delay=retry_base_delay,
            base_url=GROK_BASE_URL,
        )

    # -- injection hooks --------------------------------------------------

    def _resolve_model(self, model: str) -> str:
        # No alias map: xAI model ids pass through verbatim (and must never
        # hit DEEPSEEK_MODEL_MAP, which would rewrite e.g. "latest").
        return model

    def _resolve_env_api_key(self) -> str:
        return os.environ.get("GROK_API_KEY", "") or os.environ.get("XAI_API_KEY", "")

    def _missing_key_message(self) -> str:
        return "No Grok API key. Set GROK_API_KEY (or XAI_API_KEY) env var."

    @staticmethod
    def is_available() -> bool:
        return bool(os.environ.get("GROK_API_KEY") or os.environ.get("XAI_API_KEY"))
