"""Transparent LLM call cache wrapper.

TODO-3 (2026-05-05): on a 25-min pipeline, you change one prompt or one
fallback heuristic and have to wait for all 7 specialists, the Director,
the Synthesizer and the Editor to re-run from scratch. The vast majority
of those calls had identical inputs the first time around. By hashing
(model, system_prompt, user_message, tool_schema) and caching the parsed
JSON result on disk, a re-run with one upstream tweak only re-pays for
the calls whose inputs actually changed.

Cache key components (in order, NUL-separated):
  role · model · system_prompt · user_message · tool_name · tool_schema

This is a pure wrapper — it forwards `cost_tracker` to the inner client so
cost accounting is uniform whether or not we hit. On hit we DO NOT charge
the cost tracker (no API call happened); on miss we let the inner client's
own usage bookkeeping run.

Opt-in: set env var `AEGIS_LLM_CACHE=1`. Cache root defaults to
`.cache/llm_calls/<ticker>/` so different tickers keep disjoint caches.

Bust the cache by deleting the directory or bumping any input — there is
no TTL because LLM "freshness" is not the relevant axis (we want
determinism for the same prompt, not new randomness).
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any


# TODO-Y2 (2026-05-06): cache key version. Bump this constant when the cache
# format itself changes (e.g. key composition reordered). Env override
# AEGIS_LLM_CACHE_VERSION lets users force-invalidate without touching code,
# e.g. when they tweak a prompt-shaping helper that *isn't* one of the hashed
# inputs (currently: role, model, system_prompt, user_message, tool_name,
# tool_schema). Schema field changes — including `minItems`, `description`,
# nested property additions — already invalidate naturally because the full
# tool_schema is hashed via json.dumps(sort_keys=True).
_CACHE_KEY_VERSION = os.environ.get("AEGIS_LLM_CACHE_VERSION", "v1")


class CachedLLMClient:
    """Wrap any LLM client with on-disk JSON cache for `call_structured` /
    `call_text`. Cache hits skip the inner API call entirely.
    """

    def __init__(self, inner: Any, cache_dir: str | Path) -> None:
        self.inner = inner
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        # Forward cost tracker so the orchestrator's per-run summary still
        # works exactly the same way. Hits don't add to it (no call was
        # made); misses pass through to inner.call_structured which records.
        self.cost_tracker = getattr(inner, "cost_tracker", None)
        self._hits = 0
        self._misses = 0
        # TODO-Y3 (2026-05-06): batch 1 runs 4 agents through one cached
        # client concurrently; `self._hits += 1` is not atomic in CPython
        # under thread contention so the displayed hit-rate can drift. Lock
        # is cheap (held for one int increment).
        self._lock = threading.Lock()

    @property
    def model(self) -> str:
        return getattr(self.inner, "model", "")

    def call_structured(
        self,
        system_prompt: str,
        user_message: str,
        tool_schema: dict[str, Any],
        tool_name: str = "output",
        role: str = "agent",
        **kwargs: Any,
    ) -> dict[str, Any]:
        key = self._make_key(system_prompt, user_message, tool_schema, tool_name, role)
        cache_path = self.cache_dir / f"{key}.json"
        if cache_path.exists():
            with self._lock:
                self._hits += 1
            try:
                return json.loads(cache_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                # Corrupt cache file — fall through to fresh call. Removing
                # the bad file lets the next miss rewrite it cleanly.
                try:
                    cache_path.unlink()
                except OSError:
                    pass
        result = self.inner.call_structured(
            system_prompt=system_prompt,
            user_message=user_message,
            tool_schema=tool_schema,
            tool_name=tool_name,
            role=role,
            **kwargs,
        )
        with self._lock:
            self._misses += 1
        try:
            cache_path.write_text(
                json.dumps(result, ensure_ascii=False), encoding="utf-8",
            )
        except (TypeError, OSError):
            # Result wasn't JSON-serialisable (e.g. contains a Pydantic
            # model that the inner client forgot to dump). Don't crash the
            # pipeline over a cache write — just skip caching this call.
            pass
        return result

    def call_text(
        self,
        system_prompt: str,
        user_message: str,
        role: str = "agent",
    ) -> str:
        key = self._make_key(system_prompt, user_message, None, "__text__", role)
        cache_path = self.cache_dir / f"{key}.json"
        if cache_path.exists():
            with self._lock:
                self._hits += 1
            try:
                obj = json.loads(cache_path.read_text(encoding="utf-8"))
                if isinstance(obj, dict) and "text" in obj:
                    return obj["text"]
            except (json.JSONDecodeError, OSError):
                try:
                    cache_path.unlink()
                except OSError:
                    pass
        result = self.inner.call_text(
            system_prompt=system_prompt, user_message=user_message, role=role,
        )
        with self._lock:
            self._misses += 1
        try:
            cache_path.write_text(
                json.dumps({"text": result}, ensure_ascii=False), encoding="utf-8",
            )
        except OSError:
            pass
        return result

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"hits": self._hits, "misses": self._misses}

    def _make_key(
        self,
        system_prompt: str,
        user_message: str,
        tool_schema: dict[str, Any] | None,
        tool_name: str,
        role: str,
    ) -> str:
        h = hashlib.sha256()
        # Order matters — never reorder these without bumping the version
        # tag below, or all existing caches become silently mismatched.
        for part in (
            _CACHE_KEY_VERSION, role, self.model, system_prompt, user_message, tool_name,
        ):
            h.update(part.encode("utf-8"))
            h.update(b"\0")
        if tool_schema is not None:
            h.update(json.dumps(tool_schema, sort_keys=True).encode("utf-8"))
        # 24 hex chars = 96 bits, collision-free for any pipeline this side
        # of millions of distinct prompts; keeps filenames readable.
        return h.hexdigest()[:24]


def maybe_wrap_with_cache(client: Any, ticker: str) -> Any:
    """Wrap `client` in a `CachedLLMClient` if env opt-in is set.

    Env knobs:
      AEGIS_LLM_CACHE=1                     enable the cache
      AEGIS_LLM_CACHE_DIR=<path>            override default cache root
                                             (default `.cache/llm_calls`)

    No-op when disabled or when the inner client is already cached.
    """
    if os.environ.get("AEGIS_LLM_CACHE", "").strip() in ("", "0", "false", "False"):
        return client
    if isinstance(client, CachedLLMClient):
        return client
    root = os.environ.get("AEGIS_LLM_CACHE_DIR", ".cache/llm_calls")
    cache_dir = Path(root) / (ticker.lower() if ticker else "_default")
    return CachedLLMClient(client, cache_dir)
