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
#
# Price and period defaults:
#   - If PRICE arg is empty or not passed, yfinance auto-fetches current price
#   - Period defaults to 'latest' which probes EDGAR for the most recent 10-K

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
# Default LLM is DeepSeek V4 — OpenAI-compatible, direct access from China,
# low cost. Set DEEPSEEK_API_KEY in your environment (e.g., export it from
# ~/.zshrc or use a local-only run_research.local.sh). Do not commit a real
# key here.
export DEEPSEEK_API_KEY="${DEEPSEEK_API_KEY:-}"
export FMP_API_KEY="${FMP_API_KEY:-}"
export FRED_API_KEY="${FRED_API_KEY:-}"

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
    # China, low cost. Switched 2026-05-04 from subprocess after user
    # provided a working DEEPSEEK_API_KEY. Falls back to subprocess (Claude
    # Max via CLI) if DEEPSEEK_API_KEY is unset.
    # To override, set BACKEND / MODEL in the caller env.
    BACKEND="${BACKEND:-deepseek}"
    MODEL="${MODEL:-deepseek-v4-pro}"
    echo "Running with LLM agents (backend=$BACKEND model=$MODEL) — period=latest, auto-price..."
    python demos/auto_research_demo.py "$TICKER" $PRICE_ARG --wacc 0.095 --period latest \
        --llm --backend "$BACKEND" --model "$MODEL" $STRICT_LLM
fi
