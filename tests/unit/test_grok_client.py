"""GrokClient (xAI, OpenAI-compatible) integration-surface tests.

Grok is the alternate API backend (wired 2026-07-10, replacing the retired
Moonshot backend). GrokClient is a thin wrapper over DeepSeekClient, so what
needs pinning is:

  - endpoint / key / model resolution (GROK_BASE_URL, GROK_API_KEY →
    XAI_API_KEY fallback, GROK_MODEL env override with grok-4 default,
    and NO DeepSeek alias-map rewriting of xAI model ids)
  - the inherited BUG-A20 recovery chain is actually reachable on Grok:
    empty tool args → retries → JSON-mode fallback; irreparable truncation
    → budget grows (32768 → 65536) → JSON-mode fallback
  - happy-path call_structured returns parsed tool args

Fakes/monkeypatch style mirrors the retired Moonshot-backend recovery tests.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import aegis.core.llm.deepseek_client as ds_mod
from aegis.core.llm.deepseek_client import DeepSeekContentFilterError
from aegis.core.llm.grok_client import (
    GROK_BASE_URL,
    GROK_DEFAULT_MODEL,
    GrokClient,
    default_grok_model,
)


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------

def _fake_response(arguments=None, content=None, tool_name="output", finish_reason="stop"):
    """Build an OpenAI-chat-completions-shaped response object."""
    tool_calls = None
    if arguments is not None:
        tool_calls = [
            SimpleNamespace(function=SimpleNamespace(name=tool_name, arguments=arguments))
        ]
    msg = SimpleNamespace(tool_calls=tool_calls, content=content)
    choice = SimpleNamespace(message=msg, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], usage=None)


class _FakeCompletions:
    def __init__(self, responses):
        self._responses = responses
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        idx = min(len(self.calls) - 1, len(self._responses) - 1)
        return self._responses[idx]


def _make_client(monkeypatch, responses):
    """GrokClient with a fake transport; returns (client, fake_completions)."""
    monkeypatch.setenv("GROK_API_KEY", "xai-test-key")
    monkeypatch.delenv("GROK_MODEL", raising=False)
    # time.sleep lives in the deepseek module (shared implementation)
    monkeypatch.setattr(ds_mod.time, "sleep", lambda *_: None)
    client = GrokClient()
    fake = _FakeCompletions(responses)
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=fake))
    return client, fake


def _capture_fallback(monkeypatch, client, sentinel):
    calls = []

    def _fallback(system_prompt, user_message, tool_schema):
        calls.append((system_prompt, user_message, tool_schema))
        return sentinel

    monkeypatch.setattr(client, "_call_json_mode_fallback", _fallback)
    return calls


# --------------------------------------------------------------------------
# endpoint / key / model resolution
# --------------------------------------------------------------------------

def test_client_init_targets_xai_base_url(monkeypatch):
    monkeypatch.setenv("GROK_API_KEY", "xai-test-key")
    monkeypatch.delenv("GROK_MODEL", raising=False)
    seen = {}

    import openai

    class _SpyOpenAI:
        def __init__(self, api_key=None, base_url=None, **kw):
            seen["api_key"] = api_key
            seen["base_url"] = base_url

    monkeypatch.setattr(openai, "OpenAI", _SpyOpenAI)
    client = GrokClient()

    assert seen["base_url"] == GROK_BASE_URL
    assert seen["base_url"] == "https://api.x.ai/v1"
    assert seen["api_key"] == "xai-test-key"
    assert client.model == GROK_DEFAULT_MODEL == "grok-4"


def test_xai_api_key_fallback(monkeypatch):
    monkeypatch.delenv("GROK_API_KEY", raising=False)
    monkeypatch.setenv("XAI_API_KEY", "xai-alt-key")
    monkeypatch.delenv("GROK_MODEL", raising=False)
    client = GrokClient()
    assert client._api_key == "xai-alt-key"


def test_missing_key_raises_with_grok_message(monkeypatch):
    monkeypatch.delenv("GROK_API_KEY", raising=False)
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GROK_API_KEY"):
        GrokClient()


def test_grok_model_env_override(monkeypatch):
    monkeypatch.setenv("GROK_API_KEY", "xai-test-key")
    monkeypatch.setenv("GROK_MODEL", "grok-4-fast")
    assert default_grok_model() == "grok-4-fast"
    assert GrokClient().model == "grok-4-fast"
    # Explicit constructor arg beats the env var.
    assert GrokClient(model="grok-3-mini").model == "grok-3-mini"


def test_model_ids_bypass_deepseek_alias_map(monkeypatch):
    # "latest" is a DeepSeek alias (→ deepseek-v4-pro); on Grok it must
    # pass through verbatim, never rewritten to a DeepSeek id.
    monkeypatch.setenv("GROK_API_KEY", "xai-test-key")
    assert GrokClient(model="latest").model == "latest"


def test_is_available_checks_both_env_vars(monkeypatch):
    monkeypatch.delenv("GROK_API_KEY", raising=False)
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    assert not GrokClient.is_available()
    monkeypatch.setenv("XAI_API_KEY", "xai-x")
    assert GrokClient.is_available()
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.setenv("GROK_API_KEY", "xai-y")
    assert GrokClient.is_available()


# --------------------------------------------------------------------------
# happy path
# --------------------------------------------------------------------------

def test_good_tool_args_returned_first_try(monkeypatch):
    payload = {"thesis": "买入", "confidence": 0.8}
    client, fake = _make_client(
        monkeypatch, [_fake_response(arguments=json.dumps(payload))]
    )

    result = client.call_structured("sys", "user", {"type": "object"})

    assert result == payload
    assert len(fake.calls) == 1
    assert fake.calls[0]["model"] == "grok-4"


def test_max_tokens_hint_honored(monkeypatch):
    client, fake = _make_client(monkeypatch, [_fake_response(arguments='{"ok": 1}')])
    client.call_structured("sys", "user", {"type": "object"}, max_tokens_hint=8192)
    assert fake.calls[0]["max_tokens"] == 8192


# --------------------------------------------------------------------------
# inherited BUG-A20 recovery chain must be live on Grok
# --------------------------------------------------------------------------

def test_empty_tool_args_retries_then_json_mode_fallback(monkeypatch):
    client, fake = _make_client(
        monkeypatch, [_fake_response(arguments="", finish_reason="stop")]
    )
    sentinel = {"thesis": "ok-from-fallback"}
    fb_calls = _capture_fallback(monkeypatch, client, sentinel)

    result = client.call_structured(
        system_prompt="sys",
        user_message="user",
        tool_schema={"type": "object"},
        tool_name="output",
    )

    assert result == sentinel
    # empty-args ladder: cold start + one shrink retry, then fallback
    assert len(fake.calls) == 2
    assert [c["max_tokens"] for c in fake.calls] == [32768, 16384]
    assert len(fb_calls) == 1


def test_truncated_args_grow_budget_then_json_mode_fallback(monkeypatch):
    # >500 chars, irreparable: single value string cut mid-string — no safe
    # drop point, defeats the whole repair chain, so the grow path fires.
    truncated = '{"analysis": "' + "x" * 600
    assert len(truncated) > 500
    client, fake = _make_client(
        monkeypatch, [_fake_response(arguments=truncated, finish_reason="length")]
    )
    sentinel = {"thesis": "salvaged"}
    fb_calls = _capture_fallback(monkeypatch, client, sentinel)

    result = client.call_structured("sys", "user", {"type": "object"})

    assert result == sentinel
    # 2 main-path calls: 32768 cold start, then grown 65536; then fallback.
    assert len(fake.calls) == 2
    assert [c["max_tokens"] for c in fake.calls] == [32768, 65536]
    assert len(fb_calls) == 1


def test_no_tool_call_value_error_is_retried(monkeypatch):
    # No tool_calls, garbage content → "No tool call or parseable JSON"
    # ValueError, retried to exhaustion via the shared retryable-keyword set.
    client, fake = _make_client(
        monkeypatch, [_fake_response(arguments=None, content="not json at all")]
    )

    with pytest.raises(ValueError, match="No tool call or parseable JSON"):
        client.call_structured("sys", "user", {"type": "object"})

    assert len(fake.calls) == client.max_retries


def test_content_filter_raises_typed_error(monkeypatch):
    class _Boom:
        def create(self, **kwargs):
            raise RuntimeError("Error code: 400 - content_filter: high risk")

    client, _ = _make_client(monkeypatch, [])
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=_Boom()))

    with pytest.raises(DeepSeekContentFilterError):
        client.call_structured("sys", "user", {"type": "object"})
