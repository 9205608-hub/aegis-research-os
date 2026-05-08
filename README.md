# Aegis Research OS

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
- `KIMI_API_KEY`
- `FMP_API_KEY`
- `FRED_API_KEY`

See [.env.example](/Users/spensir/Desktop/智能投研助手/.env.example).
