"""AUDIT-B1 regression tests: KimiClient BUG-A20 recovery chain must be live.

Before the fix, the empty-args / truncation `continue`s inside
`call_structured` bound to the inner `for tc in msg.tool_calls` loop, so
Kimi made exactly ONE API call and raised ValueError on the first
empty/truncated response — budget grow and `_call_json_mode_fallback`
were dead code (while DeepSeek recovered via its retryable-keyword set).

These tests monkeypatch a fake OpenAI-shaped client and assert:
  - empty tool args → >1 API call, ends in _call_json_mode_fallback
  - irreparable truncation → max_tokens grows (16384 → 32768), ends in
    _call_json_mode_fallback
  - fallthrough "No tool call or parseable JSON" ValueError is retried
    (keyword set aligned with deepseek_client) instead of raised on
    the first attempt
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import aegis.core.llm.kimi_client as kimi_mod
from aegis.core.llm.kimi_client import (
    KIMI_BASE_URL,
    KimiClient,
    resolve_kimi_endpoint,
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
    """KimiClient with a fake transport; returns (client, fake_completions)."""
    monkeypatch.setenv("KIMI_API_KEY", "sk-kimi-test-key")
    monkeypatch.setattr(kimi_mod.time, "sleep", lambda *_: None)
    client = KimiClient(model="k2.6")
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
# B1: empty tool args → retries then JSON-mode fallback
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
    # Dead-code bug: exactly 1 call then ValueError. Fixed: full retry budget.
    assert len(fake.calls) > 1
    assert len(fake.calls) == client.max_retries
    assert len(fb_calls) == 1


def test_none_tool_args_treated_as_empty(monkeypatch):
    # tc.function.arguments can be None; must take the empty-retry branch,
    # not fall through to "No tool call" on a matched tool call.
    msg = SimpleNamespace(
        tool_calls=[SimpleNamespace(function=SimpleNamespace(name="output", arguments=None))],
        content=None,
    )
    resp = SimpleNamespace(
        choices=[SimpleNamespace(message=msg, finish_reason="stop")], usage=None
    )
    client, fake = _make_client(monkeypatch, [resp])
    sentinel = {"thesis": "recovered"}
    fb_calls = _capture_fallback(monkeypatch, client, sentinel)

    result = client.call_structured("sys", "user", {"type": "object"})

    assert result == sentinel
    assert len(fake.calls) == client.max_retries
    assert len(fb_calls) == 1


# --------------------------------------------------------------------------
# B1: irreparable truncation → budget grows, then JSON-mode fallback
# --------------------------------------------------------------------------

def test_truncated_args_grow_budget_then_json_mode_fallback(monkeypatch):
    # >500 chars, irreparable: single value string cut mid-string — no safe
    # drop point, no closed nested object, defeats the whole repair chain
    # (a mid-array cut with complete elements WOULD be salvaged by
    # repair_truncated_array and never reach the grow path).
    truncated = '{"analysis": "' + "x" * 600
    assert len(truncated) > 500
    client, fake = _make_client(
        monkeypatch, [_fake_response(arguments=truncated, finish_reason="length")]
    )
    sentinel = {"thesis": "salvaged"}
    fb_calls = _capture_fallback(monkeypatch, client, sentinel)

    result = client.call_structured("sys", "user", {"type": "object"})

    assert result == sentinel
    # 2 main-path calls: 16384 cold start, then grown 32768; then fallback.
    assert len(fake.calls) == 2
    assert [c["max_tokens"] for c in fake.calls] == [16384, 32768]
    assert len(fb_calls) == 1


# --------------------------------------------------------------------------
# B1: fallthrough ValueError is now retryable (keyword set == DeepSeek's)
# --------------------------------------------------------------------------

def test_no_tool_call_value_error_is_retried(monkeypatch):
    # No tool_calls, garbage content → "No tool call or parseable JSON"
    # ValueError. Pre-fix: raised after 1 call. Post-fix: retried to
    # exhaustion like deepseek_client (keywords "json"/"no tool call").
    client, fake = _make_client(
        monkeypatch, [_fake_response(arguments=None, content="not json at all")]
    )

    with pytest.raises(ValueError, match="No tool call or parseable JSON"):
        client.call_structured("sys", "user", {"type": "object"})

    assert len(fake.calls) == client.max_retries


def test_good_tool_args_returned_first_try(monkeypatch):
    # Sanity: happy path unchanged by the restructure.
    payload = {"thesis": "买入", "confidence": 0.8}
    client, fake = _make_client(
        monkeypatch, [_fake_response(arguments=json.dumps(payload))]
    )

    result = client.call_structured("sys", "user", {"type": "object"})

    assert result == payload
    assert len(fake.calls) == 1


# --------------------------------------------------------------------------
# AUDIT-B3 prep: shared endpoint-derivation helper
# --------------------------------------------------------------------------

def test_resolve_kimi_endpoint_kimi_code_key():
    base_url, headers = resolve_kimi_endpoint("sk-kimi-abc123")
    assert base_url == KIMI_BASE_URL
    assert base_url == "https://api.kimi.com/coding/v1"
    assert headers["User-Agent"] == "claude-code/1.0"


def test_resolve_kimi_endpoint_moonshot_key():
    base_url, headers = resolve_kimi_endpoint("sk-moonshot-xyz")
    assert base_url == "https://api.moonshot.ai/v1"
    assert headers["User-Agent"] == "claude-code/1.0"


def test_client_init_uses_shared_endpoint_derivation(monkeypatch):
    # KimiClient must route through the same helper the health probe uses.
    monkeypatch.setenv("KIMI_API_KEY", "sk-kimi-test-key")
    seen = {}

    import openai

    class _SpyOpenAI:
        def __init__(self, api_key=None, base_url=None, default_headers=None, **kw):
            seen["base_url"] = base_url
            seen["default_headers"] = default_headers

    monkeypatch.setattr(openai, "OpenAI", _SpyOpenAI)
    KimiClient(model="k2.6")

    assert seen["base_url"] == KIMI_BASE_URL
    assert seen["default_headers"] == {"User-Agent": "claude-code/1.0"}
