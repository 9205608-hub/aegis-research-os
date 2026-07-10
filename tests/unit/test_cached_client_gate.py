"""AUDIT-B2 regression tests: CachedLLMClient quality gate + bypass_cache.

Before the fix, `call_structured` cached ANY dict the inner client
returned — including subprocess_client's `{"raw_text": ...}` degradation
shell and sdk_client's `"__partial"` truncation salvage — permanently
poisoning the cache; and the orchestrator's same-prompt quality-gate
retry was a guaranteed cache hit (structural no-op).

Asserts:
  - degraded shells / empty dicts / __partial / missing-required results
    are never written to disk
  - bypass_cache=True skips the read path and re-calls inner
  - legacy poisoned entries on disk are evicted and healed by a fresh call
"""

from __future__ import annotations

import json

from aegis.core.llm.cached_client import CachedLLMClient


SCHEMA = {
    "type": "object",
    "properties": {"thesis": {"type": "string"}, "rating": {"type": "string"}},
    "required": ["thesis", "rating"],
}

GOOD = {"thesis": "护城河稳固", "rating": "买入"}


class _FakeInner:
    """Inner client returning a fixed sequence of results (last one repeats)."""

    model = "fake-model"

    def __init__(self, results):
        self._results = list(results)
        self.calls = 0

    def call_structured(self, **kwargs):
        self.calls += 1
        idx = min(self.calls - 1, len(self._results) - 1)
        return self._results[idx]


def _cache_files(cache_dir):
    return list(cache_dir.glob("*.json"))


# --------------------------------------------------------------------------
# write gate: degradation products never hit the disk
# --------------------------------------------------------------------------

def test_raw_text_shell_not_cached_and_retry_reaches_inner(tmp_path):
    inner = _FakeInner([{"raw_text": "mangled CLI output"}])
    client = CachedLLMClient(inner, tmp_path)

    r1 = client.call_structured("sys", "user", SCHEMA)
    assert r1 == {"raw_text": "mangled CLI output"}
    assert _cache_files(tmp_path) == []  # shell not persisted

    # Same prompt again — pre-fix this was a poisoned cache hit; post-fix
    # the inner client gets another chance.
    client.call_structured("sys", "user", SCHEMA)
    assert inner.calls == 2


def test_partial_tag_not_cached(tmp_path):
    inner = _FakeInner([{"thesis": "截断", "rating": "买入", "__partial": True}])
    client = CachedLLMClient(inner, tmp_path)

    client.call_structured("sys", "user", SCHEMA)
    assert _cache_files(tmp_path) == []

    client.call_structured("sys", "user", SCHEMA)
    assert inner.calls == 2


def test_empty_dict_not_cached(tmp_path):
    inner = _FakeInner([{}])
    client = CachedLLMClient(inner, tmp_path)

    client.call_structured("sys", "user", SCHEMA)
    assert _cache_files(tmp_path) == []


def test_missing_required_fields_not_cached(tmp_path):
    inner = _FakeInner([{"thesis": "只有一半"}])  # rating (required) missing
    client = CachedLLMClient(inner, tmp_path)

    client.call_structured("sys", "user", SCHEMA)
    assert _cache_files(tmp_path) == []


def test_good_result_is_cached_and_hit(tmp_path):
    inner = _FakeInner([GOOD])
    client = CachedLLMClient(inner, tmp_path)

    r1 = client.call_structured("sys", "user", SCHEMA)
    assert r1 == GOOD
    assert len(_cache_files(tmp_path)) == 1

    r2 = client.call_structured("sys", "user", SCHEMA)
    assert r2 == GOOD
    assert inner.calls == 1  # second call was a hit
    assert client.stats() == {"hits": 1, "misses": 1}


# --------------------------------------------------------------------------
# bypass_cache: quality-gate retry must pierce the cache
# --------------------------------------------------------------------------

def test_bypass_cache_pierces_existing_entry(tmp_path):
    better = {"thesis": "更充实的论点", "rating": "增持"}
    inner = _FakeInner([GOOD, better])
    client = CachedLLMClient(inner, tmp_path)

    r1 = client.call_structured("sys", "user", SCHEMA)
    assert r1 == GOOD and inner.calls == 1

    # Orchestrator quality-gate retry: same prompt, bypass_cache=True →
    # inner is called again despite a valid cache entry.
    r2 = client.call_structured("sys", "user", SCHEMA, bypass_cache=True)
    assert r2 == better
    assert inner.calls == 2

    # The fresh (gate-passing) result replaced the cached entry.
    r3 = client.call_structured("sys", "user", SCHEMA)
    assert r3 == better
    assert inner.calls == 2


def test_bypass_cache_result_still_gated(tmp_path):
    inner = _FakeInner([GOOD, {"raw_text": "glitch"}])
    client = CachedLLMClient(inner, tmp_path)

    client.call_structured("sys", "user", SCHEMA)
    assert len(_cache_files(tmp_path)) == 1
    good_bytes = _cache_files(tmp_path)[0].read_bytes()

    # Bypass retry returns a degraded shell — it must NOT clobber the
    # good cached entry.
    r2 = client.call_structured("sys", "user", SCHEMA, bypass_cache=True)
    assert r2 == {"raw_text": "glitch"}
    assert _cache_files(tmp_path)[0].read_bytes() == good_bytes


# --------------------------------------------------------------------------
# read gate: legacy poisoned entries (written pre-fix) get evicted
# --------------------------------------------------------------------------

def test_legacy_poisoned_entry_evicted_and_healed(tmp_path):
    inner = _FakeInner([GOOD])
    client = CachedLLMClient(inner, tmp_path)

    # Simulate a pre-fix poisoned cache file at the exact key this call maps to.
    key = client._make_key("sys", "user", SCHEMA, "output", "agent")
    poisoned = tmp_path / f"{key}.json"
    poisoned.write_text(
        json.dumps({"raw_text": "poison from last month"}), encoding="utf-8"
    )

    result = client.call_structured("sys", "user", SCHEMA)
    assert result == GOOD  # healthy inner reached, not the poison
    assert inner.calls == 1
    # Entry healed on disk.
    assert json.loads(poisoned.read_text(encoding="utf-8")) == GOOD
