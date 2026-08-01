# Aegis Research OS

[![tests](https://github.com/9205608-hub/aegis-research-os/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/9205608-hub/aegis-research-os/actions/workflows/tests.yml)

Python-based investment research system with:

- automated research orchestration
- FastAPI REST and local web entrypoints
- valuation, evidence, critic, and reporting pipelines
- unit, integration, and multi-entity test suites

## Quick Start

Run the full test suite:

```bash
pytest -q
```

Run local web server:

```bash
./run_server.sh
```

Run a research report:

```bash
./run_research.sh META
```

## Environment

Configure secrets through environment variables. Do not commit live keys.

- `SEC_USER_AGENT`
- `DEEPSEEK_API_KEY` (default LLM backend)
- `GROK_API_KEY` / `XAI_API_KEY` (alternate LLM backend; model via `GROK_MODEL`, default `grok-4`)
- `FMP_API_KEY`
- `FRED_API_KEY`

See [.env.example](/Users/spensir/Desktop/智能投研助手/.env.example).
