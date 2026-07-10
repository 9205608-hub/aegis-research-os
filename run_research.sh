#!/bin/bash
# Aegis Research OS — One-click research report generator
#
# Usage:
#   ./run_research.sh NVDA                    # auto-fetch latest price and latest 10-K
#   ./run_research.sh NVDA 110                # pin a specific price (for backtests)
#   ./run_research.sh NVDA "" --no-llm        # rule-based mode, auto-price
#   ./run_research.sh NVDA 110 --no-llm       # rule-based mode, pinned price
#   ./run_research.sh --smoke NVDA            # smoke mode (rule-based, <5min)
#   ./run_research.sh --strict-llm NVDA       # fail hard on agent mock fallback
#   FAST_AGENTS=1 ./run_research.sh NVDA      # hybrid flash routing (see below)
#
# Price and period defaults:
#   - If PRICE arg is empty or not passed, yfinance auto-fetches current price
#   - Period defaults to 'latest' which probes EDGAR for the most recent 10-K
#
# Env switches (AUDIT-D2, 2026-07-09):
#   AEGIS_LLM_CACHE   LLM disk cache. Default ON (=1): re-runs of the same
#                     ticker reuse cached LLM calls for free (~1s vs minutes
#                     per unchanged call). Set AEGIS_LLM_CACHE=0 to force
#                     fresh LLM calls (e.g. after a prompt change).
#   FAST_AGENTS       Set =1 to pass --fast-agents: 4 of 7 specialist agents
#                     drop from deepseek-v4-pro to -flash (valuation/variant/
#                     accounting + chief-analyst roles stay on pro). Expected
#                     pipeline time ~40min → ~22min. DEFAULT OFF because the
#                     flash tier's output quality has not yet been validated
#                     on real tickers — flip it on for iteration runs, keep
#                     it off for reports you intend to publish.
#   (--fast, DEEP→standard depth downgrade, remains an explicit CLI opt-in
#   on demos/auto_research_demo.py — it trades away narrative_supplement
#   content, which is not a pure performance knob.)

set -e

# Parse leading flags (--smoke / --strict-llm) before positional args.
SMOKE=""
STRICT_LLM=""
while [[ "${1:-}" == --* ]]; do
    case "$1" in
        --smoke) SMOKE="--smoke"; shift ;;
        --strict-llm) STRICT_LLM="--strict-llm"; shift ;;
        *) echo "Unknown flag: $1"; exit 2 ;;
    esac
done

TICKER="${1:?Usage: ./run_research.sh [--smoke|--strict-llm] TICKER [PRICE] [--no-llm]}"
PRICE="${2:-}"
NO_LLM="${3:-}"

export SEC_USER_AGENT="Aegis Research research@aegis.ai"
# Keep legacy env name working for older SEC client call sites.
export EDGAR_USER_AGENT="${EDGAR_USER_AGENT:-$SEC_USER_AGENT}"
# LLM / data enrichment keys.
# 默认 LLM 后端为 DeepSeek V4（OpenAI 兼容，中国大陆直连，低成本）；
# 备选后端 Grok（xAI，需代理），设 GROK_API_KEY（或 XAI_API_KEY）并
# BACKEND=grok 启用。密钥从环境注入（例如 ~/.zshrc，或本地专用的
# run_research.local.sh，已 gitignore）。不要在这里提交真实 key。
export DEEPSEEK_API_KEY="${DEEPSEEK_API_KEY:-}"
export GROK_API_KEY="${GROK_API_KEY:-}"
export FMP_API_KEY="${FMP_API_KEY:-}"
export FRED_API_KEY="${FRED_API_KEY:-}"

# AUDIT-D2: LLM disk cache defaults ON. The cache layer was implemented
# (cached_client.py) but the documented entrypoint never exported the env
# gate, so every "production" run paid full price even on identical reruns.
# Override with AEGIS_LLM_CACHE=0 for a guaranteed-fresh run.
export AEGIS_LLM_CACHE="${AEGIS_LLM_CACHE:-1}"

# AUDIT-D2: hybrid flash routing opt-in (see header comment for why this
# is not the default yet).
FAST_AGENTS_ARG=""
if [ "${FAST_AGENTS:-0}" = "1" ]; then
    FAST_AGENTS_ARG="--fast-agents"
fi

# Build price arg: only pass --price if user provided one
PRICE_ARG=""
if [ -n "$PRICE" ]; then
    PRICE_ARG="--price $PRICE"
fi

if [ -n "$SMOKE" ]; then
    echo "Running in SMOKE mode (rule-based, <5min plumbing check) — period=latest, auto-price..."
    python demos/auto_research_demo.py "$TICKER" $PRICE_ARG --wacc 0.095 --period latest --smoke
elif [ "$NO_LLM" = "--no-llm" ]; then
    echo "Running in rule-based mode (no LLM) — period=latest, auto-price..."
    python demos/auto_research_demo.py "$TICKER" $PRICE_ARG --wacc 0.095 --period latest
else
    # Default backend: deepseek (V4) — OpenAI-compatible, direct access from
    # China, low cost. Falls back to subprocess (Claude Max via CLI) if
    # DEEPSEEK_API_KEY is unset. Alternate: BACKEND=grok（需 GROK_API_KEY
    # 且能代理访问 api.x.ai；模型用 GROK_MODEL 覆盖，缺省 grok-4）。
    # To override, set BACKEND / MODEL in the caller env.
    BACKEND="${BACKEND:-deepseek}"
    MODEL="${MODEL:-deepseek-v4-pro}"
    echo "Running with LLM agents (backend=$BACKEND model=$MODEL) — period=latest, auto-price..."
    python demos/auto_research_demo.py "$TICKER" $PRICE_ARG --wacc 0.095 --period latest \
        --llm --backend "$BACKEND" --model "$MODEL" $STRICT_LLM $FAST_AGENTS_ARG
fi
