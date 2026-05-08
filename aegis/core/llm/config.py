"""LLM Configuration — Section 28.2.

Model assignments, temperature, cost tracking, prompt caching.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum


class LLMMode(str, Enum):
    MOCK = "mock"
    LIVE = "live"
    SUBPROCESS = "subprocess"  # Use claude CLI
    SDK = "sdk"  # Use Anthropic SDK with OAuth token (Claude Max subscription)
    KIMI = "kimi"  # Use Moonshot/Kimi API (low cost, China-direct) — primary


@dataclass(frozen=True)
class ModelProfile:
    """Configuration for a specific model assignment."""

    model_id: str
    temperature: float
    max_tokens: int = 4096
    top_p: float = 1.0


# Section 28.2 model assignments
AGENT_PROFILES: dict[str, ModelProfile] = {
    "planner": ModelProfile("claude-opus-4-6-20250414", 0.1, 8192),
    "chief_analyst": ModelProfile("claude-opus-4-6-20250414", 0.3, 8192),  # Higher temp for creative synthesis
    "specialist_agent": ModelProfile("claude-opus-4-6-20250414", 0.2, 8192),
    "critic": ModelProfile("claude-opus-4-6-20250414", 0.0, 4096),
    "cognitive_bias_critic": ModelProfile("claude-opus-4-6-20250414", 0.0, 4096),
    "evidence_extraction": ModelProfile("claude-sonnet-4-6-20250414", 0.0, 4096),
    "report_generation": ModelProfile("claude-sonnet-4-6-20250414", 0.3, 8192),
    "report_editor": ModelProfile("claude-opus-4-6-20250414", 0.4, 8192),  # Highest temp for editorial voice
    "translation": ModelProfile("claude-sonnet-4-6-20250414", 0.1, 4096),
}


@dataclass
class LLMConfig:
    """Global LLM configuration."""

    mode: LLMMode = LLMMode.MOCK
    api_key: str = ""
    default_profile: str = "specialist_agent"
    cache_enabled: bool = True
    cache_ttl_hours: int = 24
    monthly_budget_usd: float = 5000.0
    per_run_cost_tracking: bool = True

    @classmethod
    def from_env(cls) -> LLMConfig:
        mode_str = os.environ.get("LLM_MODE", "mock").lower()
        mode_map = {"live": LLMMode.LIVE, "subprocess": LLMMode.SUBPROCESS, "sdk": LLMMode.SDK}
        mode = mode_map.get(mode_str, LLMMode.MOCK)

        # Auto-detect SDK mode: if CLAUDE_CODE_OAUTH_TOKEN is set and no explicit API key
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        oauth_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
        if not api_key and oauth_token and mode == LLMMode.MOCK:
            mode = LLMMode.SDK
            api_key = oauth_token

        return cls(
            mode=mode,
            api_key=api_key or oauth_token,
        )

    def get_profile(self, role: str) -> ModelProfile:
        return AGENT_PROFILES.get(role, AGENT_PROFILES[self.default_profile])


@dataclass
class UsageRecord:
    """Token usage for a single LLM call."""

    model_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    # TODO-X6: reasoning_tokens are a subset of output_tokens for thinking
    # models (DeepSeek V4, OpenAI o1). Stored separately for diagnostics —
    # cost is already correctly billed via output_tokens at the API level
    # (no special rate), but tracking them lets us see how much output
    # budget thinking is eating per call. Default 0 for non-reasoning models.
    reasoning_tokens: int = 0
    # TODO-X5: short snippet of the model's `reasoning_content` (chain of
    # thought) so we can post-mortem failures like "why did this call return
    # 0 inferences?". First ~600 chars only; full text gets dropped to keep
    # cost-tracker memory bounded. Empty string for models that don't expose
    # CoT.
    reasoning_preview: str = ""

    @property
    def estimated_cost_usd(self) -> float:
        """Rough cost estimate per-call. Published rate cards as of
        2026-04-14.
        """
        m = (self.model_id or "").lower()
        # Anthropic Claude family
        if "opus" in m:
            return (self.input_tokens * 15 + self.output_tokens * 75) / 1_000_000
        if "sonnet" in m:
            return (self.input_tokens * 3 + self.output_tokens * 15) / 1_000_000
        if "haiku" in m:
            return (self.input_tokens * 0.8 + self.output_tokens * 4) / 1_000_000
        # Kimi / Moonshot — K2.6 (current) and K2.5 (legacy)
        if "k2.6" in m or "kimi-k2.6" in m:
            # K2.6 Code (2026-04-13): $0.60 / $2.50 per M token
            return (self.input_tokens * 0.60 + self.output_tokens * 2.50) / 1_000_000
        if "kimi" in m or "moonshot" in m or m.startswith("k2"):
            # K2.5 / Moonshot v1 legacy rates
            return (self.input_tokens * 0.15 + self.output_tokens * 2.50) / 1_000_000
        # DeepSeek — V4 (deepseek-v4-pro / deepseek-v4-flash) and legacy
        # V3 IDs. Published rate cards (2026-Q2):
        #   deepseek-v4-pro:     $0.27 in (miss) / $0.07 in (hit) / $1.10 out
        #   deepseek-v4-flash:   ~50% cheaper across the board
        #   deepseek-reasoner:   $0.55 in / $2.19 out
        # Cache hit pricing applies to `cache_read_tokens`; the rest of the
        # input is full-priced. UsageRecord stores both so we differentiate.
        if "deepseek" in m:
            if "reasoner" in m or "r1" in m:
                return (self.input_tokens * 0.55 + self.output_tokens * 2.19) / 1_000_000
            if "flash" in m:
                in_miss_rate, in_hit_rate, out_rate = 0.14, 0.04, 0.55
            else:
                # v4-pro / chat (legacy alias) / default
                in_miss_rate, in_hit_rate, out_rate = 0.27, 0.07, 1.10
            cache_hit = self.cache_read_tokens or 0
            cache_miss = max(self.input_tokens - cache_hit, 0)
            return (
                cache_miss * in_miss_rate
                + cache_hit * in_hit_rate
                + self.output_tokens * out_rate
            ) / 1_000_000
        return 0.0


class CostTracker:
    """Tracks LLM costs across a session."""

    def __init__(self) -> None:
        self._records: list[UsageRecord] = []

    def record(self, usage: UsageRecord) -> None:
        self._records.append(usage)

    @property
    def total_cost_usd(self) -> float:
        return sum(r.estimated_cost_usd for r in self._records)

    @property
    def total_input_tokens(self) -> int:
        return sum(r.input_tokens for r in self._records)

    @property
    def total_output_tokens(self) -> int:
        return sum(r.output_tokens for r in self._records)

    @property
    def total_reasoning_tokens(self) -> int:
        return sum(getattr(r, "reasoning_tokens", 0) for r in self._records)

    @property
    def total_cache_read_tokens(self) -> int:
        return sum(getattr(r, "cache_read_tokens", 0) for r in self._records)

    @property
    def call_count(self) -> int:
        return len(self._records)

    def summary(self) -> dict:
        out = {
            "calls": self.call_count,
            "input_tokens": self.total_input_tokens,
            "output_tokens": self.total_output_tokens,
            "estimated_cost_usd": round(self.total_cost_usd, 4),
        }
        # Only surface reasoning stats if the run actually used a thinking
        # model — otherwise this clutters non-DeepSeek/Kimi summaries.
        rt = self.total_reasoning_tokens
        if rt:
            out["reasoning_tokens"] = rt
            out["reasoning_share_of_output"] = (
                round(rt / self.total_output_tokens, 3)
                if self.total_output_tokens else 0.0
            )
        cr = self.total_cache_read_tokens
        if cr:
            out["cache_read_tokens"] = cr
            out["cache_hit_rate"] = (
                round(cr / self.total_input_tokens, 3)
                if self.total_input_tokens else 0.0
            )
        return out

    def dump_trace(self, path: str) -> None:
        """Write per-call diagnostics (model, tokens, reasoning preview) as
        JSONL. Used for post-mortem of '0 inferences' / 'all paths exhausted'
        failures where the chain-of-thought tells us why a structured tool
        call came back empty.
        """
        import json as _json
        with open(path, "a", encoding="utf-8") as f:
            for r in self._records:
                f.write(_json.dumps({
                    "model": r.model_id,
                    "input_tokens": r.input_tokens,
                    "output_tokens": r.output_tokens,
                    "reasoning_tokens": getattr(r, "reasoning_tokens", 0),
                    "cache_read_tokens": r.cache_read_tokens,
                    "reasoning_preview": getattr(r, "reasoning_preview", ""),
                    "estimated_cost_usd": round(r.estimated_cost_usd, 6),
                }, ensure_ascii=False) + "\n")
