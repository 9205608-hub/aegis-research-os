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

Run a research report (canonical entry):

```bash
./run_research.sh META         # US ticker
./run_research.sh 301358       # A-share (寒武纪)
```

Run local web server (search UI + REST v1 API):

```bash
./run_server.sh                # http://localhost:8000
```

Endpoints:

| Path | Purpose |
|---|---|
| `/search`, `/progress`, `/report/{slug}` | Local-dev UI pages |
| `/api/universe`, `/api/recent`, `/api/run`, `/api/runs/{id}`, `/api/progress/{id}` | Local-dev runner registry + SSE log stream |
| `/api/v1/research/{auto,plan,run,runs}` | REST v1: orchestrate / plan / list research runs |
| `/api/v1/thesis/*` | REST v1: thesis CRUD with versioning |
| `/api/v1/events/{emit,log,monitorables}` | REST v1: event bus + monitorables |
| `/api/v1/portfolio/{signals,stats}` | REST v1: portfolio signals |
| `/api/v1/admin/{status,audit-log,costs}` | REST v1: admin / observability |
| `/health` | Liveness probe |
| `/dashboard` | Static dashboard (aegis/dashboard/index.html) |
| `/docs` | Auto-generated OpenAPI |

Optional Typer CLI (requires `pip install typer`):

```bash
python -m aegis.api.cli.main health
python -m aegis.api.cli.main metrics META
python -m aegis.api.cli.main research META
```

## Environment

Configure secrets through environment variables. Do not commit live keys.

- `SEC_USER_AGENT`
- `KIMI_API_KEY`
- `FMP_API_KEY`
- `FRED_API_KEY`

See [.env.example](/Users/spensir/Desktop/智能投研助手/.env.example).
