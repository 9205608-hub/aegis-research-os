"""Aegis pipeline timeouts and budget knobs.

Single source of truth for the magic numbers that govern how long the
pipeline waits before giving up. Previously hard-coded across four files,
which made tuning a guessing game and let stale values diverge.

Each constant can be overridden via env var without code changes — useful
for one-off long-running A-share runs or cost-capped CI smoke tests.

Conventions: all `*_TIMEOUT_S` are seconds, `*_BUDGET_USD` are dollars.
"""

from __future__ import annotations

import os


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    return int(_env_float(name, default))


# ── LLM subprocess client (claude CLI calls) ────────────────────────────
# Single CLI invocation. A-share Sonnet runs with full Chinese prompt +
# numeric-consistency constraint commonly take 8-15 min, but TBEA v11 hit
# valuation_analyst at exactly 30 min on the first attempt → silent mock
# fallback (timeout is not classified as transient so no retry). Bumped
# 1800 → 3600 (60 min) on 2026-04-25 to give outlier-slow variants room.
SUBPROCESS_CALL_TIMEOUT_S: int = _env_int("AEGIS_SUBPROCESS_TIMEOUT_S", 3600)

# Lighter-weight `call_text` path (short prompts: rate-this, classify, single
# numeric extraction). 180s was the original hard-coded value; under thinking
# models this can be tight. 600s gives slow runs headroom while keeping the
# fast path from blocking the pipeline if the CLI hangs entirely.
# (BUG-24 follow-up 2026-05-05 — eliminate the last hard-coded `timeout=180`.)
SUBPROCESS_TEXT_CALL_TIMEOUT_S: int = _env_int("AEGIS_SUBPROCESS_TEXT_TIMEOUT_S", 600)

# Per-call CLI cost ceiling (Claude Max billing-side guard).
SUBPROCESS_MAX_BUDGET_USD: float = _env_float("AEGIS_LLM_MAX_BUDGET_USD", 5.0)

# ── Agent batches & watchdogs ───────────────────────────────────────────
# Wall-clock for a parallel batch. With AGENT_MAX_PARALLEL=2 and 4 DEEP
# agents in batch 1, the 4 agents run as two pairs sequentially; each pair
# can hit ~25 min, so the batch can take up to ~50 min. Bumped 2400 → 4800
# (80 min) on 2026-04-24 after v10 lost valuation_analyst + risk_analyst
# to this exact mismatch (cap=2 pairing × old 40-min budget = 2 timeouts,
# 2 silent rule-based fallbacks). If you raise AGENT_MAX_PARALLEL back to
# 4, you can drop this back to 2400.
AGENT_BATCH_TIMEOUT_S: int = _env_int("AEGIS_AGENT_BATCH_TIMEOUT_S", 4800)

# Per-agent watchdog (sector_context, synthesizer, report_editor, etc).
# Bumped 900 → 1800 on 2026-04-25 after v11 lost sector_context_agent at
# the 15-min watchdog (its single LLM call ran past the limit, fell to
# mock template). 30 min matches SUBPROCESS_CALL_TIMEOUT_S/2 so the two
# don't fight each other on outlier-slow A-share Sonnet calls.
AGENT_WATCHDOG_TIMEOUT_S: int = _env_int("AEGIS_AGENT_WATCHDOG_S", 1800)

# ── Concurrency caps ────────────────────────────────────────────────────
# Cap on simultaneous claude CLI subprocesses. Anthropic Sonnet rate is
# 50 req/min; each agent fires 2-3 calls back-to-back so 4-way concurrency
# routinely tripped 429. Default 2 is the empirically safe ceiling.
AGENT_MAX_PARALLEL: int = _env_int("AEGIS_AGENT_MAX_PARALLEL", 2)
