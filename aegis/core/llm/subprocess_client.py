"""Subprocess LLM Client — uses `claude -p` CLI for LLM calls.

Leverages the user's existing Claude Max subscription via Claude Code CLI.
No API key needed — uses the local claude binary with --json-schema
for structured output.

Requires: claude CLI installed and authenticated (Claude Code / Claude Max).
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any

from aegis.core.llm.config import CostTracker, UsageRecord
from aegis.core.llm._recovery import repair_json as _shared_repair_json
from aegis.core.config import (
    SUBPROCESS_CALL_TIMEOUT_S,
    SUBPROCESS_MAX_BUDGET_USD,
    SUBPROCESS_TEXT_CALL_TIMEOUT_S,
)


class SubprocessLLMClient:
    """LLM client that calls `claude -p` as a subprocess.

    Uses Claude Code CLI's non-interactive mode with --json-schema
    for structured output. Runs on the user's existing Claude subscription.
    """

    def __init__(
        self,
        model: str = "opus",
        max_budget_usd: float | None = None,
        claude_path: str | None = None,
    ) -> None:
        self.model = model
        self.max_budget_usd = (
            max_budget_usd if max_budget_usd is not None else SUBPROCESS_MAX_BUDGET_USD
        )
        self.claude_path = claude_path or self._find_claude()
        self.cost_tracker = CostTracker()

    def call_structured(
        self,
        system_prompt: str,
        user_message: str,
        tool_schema: dict[str, Any],
        tool_name: str = "output",
        role: str = "specialist_agent",
        **kwargs,
    ) -> dict[str, Any]:
        """Call Claude CLI with --json-schema for structured output.

        Returns parsed dict matching tool_schema.
        """
        # Combine system + user into a single prompt
        prompt = f"{system_prompt}\n\n---\n\n{user_message}\n\nRespond with structured JSON matching the schema."

        # Build command
        cmd = [
            self.claude_path,
            "-p", prompt,
            "--model", self.model,
            "--output-format", "json",
            "--max-budget-usd", str(self.max_budget_usd),
            "--no-session-persistence",
            "--json-schema", json.dumps(tool_schema),
        ]

        # Run subprocess with CLAUDECODE unset to avoid nested session error
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

        # BUG-33 (2026-04-23): Add retry-on-transient. Previously a single
        # transient hiccup (rate-limit 429, network blip, claude-cli crash with
        # empty stderr) immediately fell through to mock — destroying agent
        # quality with zero retry. Pipeline ran 4 CLI calls in parallel and
        # routinely tripped Anthropic's 50 req/min Sonnet rate limit, leaving
        # 5-of-6 specialist agents on mock. Retry with exponential backoff so
        # the pipeline is robust to bursty failures, not silently degraded.
        import time as _time
        last_error: str = ""
        last_kind: str = "unknown"
        t_start = _time.time()
        for attempt, delay in enumerate([0, 15, 45]):  # 0s, 15s, 45s = ~60s max wait
            if delay:
                _time.sleep(delay)
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=SUBPROCESS_CALL_TIMEOUT_S,
                    env=env,
                )
            except subprocess.TimeoutExpired:
                # Treat the first timeout as transient — Anthropic Sonnet load
                # varies and a single hung call doesn't mean the next one will.
                # Only the 3rd attempt raises (skip the third retry; one retry
                # already costs another full SUBPROCESS_CALL_TIMEOUT_S).
                last_error = f"subprocess timed out at {SUBPROCESS_CALL_TIMEOUT_S}s"
                last_kind = "timeout"
                if attempt < 1:  # only allow ONE retry on timeout (else 90+ min)
                    continue
                elapsed = _time.time() - t_start
                raise RuntimeError(
                    f"Claude CLI failed (kind=timeout, attempts={attempt + 1}/3, "
                    f"elapsed={elapsed:.0f}s): {last_error}"
                )

            if result.returncode != 0:
                # Capture BOTH stdout and stderr — claude CLI sometimes writes
                # the actual error (rate limit JSON, etc.) to stdout while
                # exiting non-zero with empty stderr.
                err_blob = (
                    f"rc={result.returncode} "
                    f"stderr={result.stderr[:300]!r} "
                    f"stdout={result.stdout[:300]!r}"
                )
                last_error = err_blob
                last_kind = "subprocess_rc" if not self._is_transient(err_blob) else "transient_rc"
                if attempt < 2 and self._is_transient(err_blob):
                    continue
                elapsed = _time.time() - t_start
                raise RuntimeError(
                    f"Claude CLI failed (kind={last_kind}, attempts={attempt + 1}/3, "
                    f"elapsed={elapsed:.0f}s): {err_blob[:200]}"
                )

            # Parse the JSON envelope
            try:
                envelope = json.loads(result.stdout)
            except json.JSONDecodeError as e:
                last_error = f"{e}; stdout={result.stdout[:300]!r}"
                last_kind = "json_decode"
                if attempt < 2:
                    continue
                elapsed = _time.time() - t_start
                raise RuntimeError(
                    f"Claude CLI failed (kind=json_decode, attempts={attempt + 1}/3, "
                    f"elapsed={elapsed:.0f}s): {last_error[:200]}"
                )

            if envelope.get("is_error"):
                err_msg = str(envelope.get("result", ""))
                last_error = err_msg[:300]
                last_kind = "envelope_transient" if self._is_transient(err_msg) else "envelope_error"
                if attempt < 2 and self._is_transient(err_msg):
                    continue
                elapsed = _time.time() - t_start
                raise RuntimeError(
                    f"Claude CLI failed (kind={last_kind}, attempts={attempt + 1}/3, "
                    f"elapsed={elapsed:.0f}s): {last_error[:200]}"
                )

            # Success — track usage and return.
            usage_data = envelope.get("usage", {})
            usage = UsageRecord(
                model_id=envelope.get("modelUsage", {}).keys().__iter__().__next__()
                    if envelope.get("modelUsage") else f"claude-{self.model}",
                input_tokens=usage_data.get("input_tokens", 0),
                output_tokens=usage_data.get("output_tokens", 0),
                cache_read_tokens=usage_data.get("cache_read_input_tokens", 0),
                cache_creation_tokens=usage_data.get("cache_creation_input_tokens", 0),
            )
            self.cost_tracker.record(usage)

            structured = envelope.get("structured_output")
            if structured:
                return structured
            result_text = envelope.get("result", "")
            try:
                return json.loads(result_text)
            except json.JSONDecodeError:
                # TODO-Y1: try the shared repair chain (control-char strip,
                # missing-comma fix, depth-0 search, array-truncation salvage)
                # before degrading to {"raw_text": ...}. Subprocess CLI
                # output occasionally carries trailing prose that breaks a
                # naive json.loads.
                try:
                    return _shared_repair_json(result_text)
                except json.JSONDecodeError:
                    return {"raw_text": result_text}

        # All retries exhausted (we shouldn't actually reach here — the inner
        # loop raises on the third attempt — but be defensive).
        elapsed = _time.time() - t_start
        raise RuntimeError(
            f"Claude CLI failed (kind={last_kind}, attempts=3/3, "
            f"elapsed={elapsed:.0f}s): {last_error[:200] or 'no detail captured'}"
        )

    @staticmethod
    def _is_transient(err: str) -> bool:
        """Heuristic: should we retry this error?

        Returns True for: rate limits (429 / "rate" / "overloaded"), network
        blips (connection / timeout / EOF), and claude-cli's empty-stderr
        non-zero exits (observed on overloaded Sonnet endpoints — typically
        clears after 15-45 s).
        """
        e = err.lower()
        for needle in (
            "429", "rate", "overloaded", "overload", "throttle",
            "connection", "timeout", "eof", "broken pipe",
            "service unavailable", "503", "502", "504",
            "internal server error", "500",
            "temporarily unavailable",
        ):
            if needle in e:
                return True
        # Empty-stderr rc!=0 case: stderr=''
        if "stderr=''" in err and "rc=" in err and "rc=0" not in err:
            return True
        return False

    def call_text(
        self,
        system_prompt: str,
        user_message: str,
        role: str = "specialist_agent",
    ) -> str:
        """Simple text completion via Claude CLI."""
        prompt = f"{system_prompt}\n\n---\n\n{user_message}"

        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

        cmd = [
            self.claude_path,
            "-p", prompt,
            "--model", self.model,
            "--output-format", "json",
            "--max-budget-usd", str(self.max_budget_usd),
            "--no-session-persistence",
        ]

        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=SUBPROCESS_TEXT_CALL_TIMEOUT_S, env=env,
        )

        if result.returncode != 0:
            raise RuntimeError(f"Claude CLI failed: {result.stderr[:500]}")

        envelope = json.loads(result.stdout)
        usage_data = envelope.get("usage", {})
        self.cost_tracker.record(UsageRecord(
            model_id=f"claude-{self.model}",
            input_tokens=usage_data.get("input_tokens", 0),
            output_tokens=usage_data.get("output_tokens", 0),
        ))

        return envelope.get("result", "")

    @staticmethod
    def _find_claude() -> str:
        """Find the claude CLI binary."""
        # Check common locations
        candidates = [
            os.path.expanduser("~/.local/bin/claude"),
            "/usr/local/bin/claude",
            "claude",  # Rely on PATH
        ]
        for path in candidates:
            if os.path.isfile(path) and os.access(path, os.X_OK):
                return path
        # Fallback to PATH lookup
        return "claude"

    @staticmethod
    def is_available() -> bool:
        """Check if Claude CLI is installed and accessible."""
        try:
            env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
            result = subprocess.run(
                ["claude", "--version"],
                capture_output=True, text=True, timeout=5, env=env,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
