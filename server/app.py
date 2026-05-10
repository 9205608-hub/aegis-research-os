"""FastAPI app — local web entry for Aegis Research OS.

Routes:

    GET  /                       → redirect to /search
    GET  /search                 → serve web/search.html (with API-driven data)
    GET  /progress               → serve web/progress.html
    GET  /report/{slug}          → serve a completed report from demos/
    GET  /web/*                  → static assets (report.jsx, future CSS, etc.)

    GET  /api/universe           → ticker universe (list of {tck, ex, name, sector})
    GET  /api/recent             → recent reports scanned from demos/
    GET  /api/runs               → active runs
    POST /api/run                → {ticker: "NVDA"} → spawn pipeline, returns run state
    GET  /api/runs/{id}          → poll run state
    GET  /api/progress/{id}      → SSE stream tailing logs/run_{id}.log

Designed for single-user local dev (`uvicorn server.app:app --reload`).
Not hardened for untrusted input.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, AsyncGenerator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .runner import RunnerRegistry
from .scanner import read_report_html, scan_demos
from .universe import get_universe

# ─────────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = PROJECT_ROOT / "web"
DEMOS_DIR = PROJECT_ROOT / "demos"

app = FastAPI(title="Aegis Research OS", version="0.2.0")

# Mount the v1 REST API (research/thesis/events/portfolio/admin) at /api/v1/*.
# These routers were previously only reachable via the standalone
# aegis/api/rest/app.py — never started by run_server.sh — so the routes
# existed but no production caller could hit them. Including them here
# keeps a single uvicorn process while exposing the v1 surface alongside
# the existing local-dev /api/* endpoints. No prefix collision: server
# uses /api/<noun>, the v1 routers use /api/v1/<noun>.
from aegis.api.rest.routers.research import router as _research_router
from aegis.api.rest.routers.thesis import router as _thesis_router
from aegis.api.rest.routers.events import router as _events_router
from aegis.api.rest.routers.portfolio import router as _portfolio_router
from aegis.api.rest.routers.admin import router as _admin_router

app.include_router(_research_router, prefix="/api/v1/research", tags=["research"])
app.include_router(_thesis_router,   prefix="/api/v1/thesis",   tags=["thesis"])
app.include_router(_events_router,   prefix="/api/v1/events",   tags=["events"])
app.include_router(_portfolio_router, prefix="/api/v1/portfolio", tags=["portfolio"])
app.include_router(_admin_router,    prefix="/api/v1/admin",    tags=["admin"])


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    """Liveness probe used by load balancers / monitoring."""
    return {"status": "ok", "version": app.version}


@app.get("/dashboard", response_class=HTMLResponse, tags=["meta"])
def dashboard() -> HTMLResponse:
    """Serve the static Aegis dashboard (aegis/dashboard/index.html)."""
    html_path = PROJECT_ROOT / "aegis" / "dashboard" / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Dashboard not found</h1>", status_code=404)


_runner = RunnerRegistry(PROJECT_ROOT)


# ─────────────────────────────────────────────────────────────────
# Static / page routes
# ─────────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/search")


@app.get("/search", response_class=HTMLResponse, include_in_schema=False)
def search_page() -> FileResponse:
    return FileResponse(WEB_DIR / "search.html", media_type="text/html")


@app.get("/progress", response_class=HTMLResponse, include_in_schema=False)
def progress_page() -> FileResponse:
    return FileResponse(WEB_DIR / "progress.html", media_type="text/html")


@app.get("/report/{slug}", response_class=HTMLResponse, include_in_schema=False)
def report_page(slug: str) -> HTMLResponse:
    """Serve a rendered report by filename slug (no extension).

    Slug format: `{ticker}_{period}_auto_report` e.g. `301358_fy2024_auto_report`.
    """
    # Reject traversal attempts before touching the filesystem.
    if "/" in slug or "\\" in slug or ".." in slug:
        raise HTTPException(status_code=400, detail="invalid slug")
    html = read_report_html(DEMOS_DIR, slug)
    if html is None:
        raise HTTPException(status_code=404, detail="report not found")
    return HTMLResponse(content=html)


# Mount the raw web/ for developer access (e.g. open report.jsx directly
# during debugging). Report pages themselves are self-contained — no
# client-side load from /web/report.jsx needed.
app.mount("/web", StaticFiles(directory=str(WEB_DIR)), name="web")


# ─────────────────────────────────────────────────────────────────
# API routes
# ─────────────────────────────────────────────────────────────────

@app.get("/api/universe")
def api_universe() -> list[dict[str, Any]]:
    return get_universe(PROJECT_ROOT)


@app.get("/api/recent")
def api_recent(limit: int = 12) -> list[dict[str, Any]]:
    limit = max(1, min(50, limit))
    return scan_demos(DEMOS_DIR, limit=limit)


class RunRequest(BaseModel):
    ticker: str


@app.post("/api/run")
def api_run(req: RunRequest) -> dict[str, Any]:
    ticker = req.ticker.strip().upper()
    if not ticker or len(ticker) > 12 or not all(c.isalnum() or c in "._-" for c in ticker):
        raise HTTPException(status_code=400, detail="invalid ticker")
    state = _runner.start_run(ticker)
    return _runner.as_dict(state)


@app.get("/api/runs")
def api_runs() -> list[dict[str, Any]]:
    return [_runner.as_dict(s) for s in _runner.list_active()]


@app.get("/api/runs/{run_id}")
def api_run_state(run_id: str) -> dict[str, Any]:
    state = _runner.poll(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail="run not found")
    return _runner.as_dict(state)


@app.get("/api/progress/{run_id}")
async def api_progress(run_id: str, request: Request) -> StreamingResponse:
    """Server-sent events stream of the run's live log + terminal state.

    Event payloads are JSON with a `type` discriminator:
      - {"type": "log",   "line": "...", "seq": N}
      - {"type": "state", "status": "running|finished|failed", "report": "/report/..." | null}
      - {"type": "hb"}   # heartbeat every ~10s to keep the connection alive

    Client should subscribe with `new EventSource("/api/progress/{id}")`
    and close after receiving a terminal "state" message.
    """
    state = _runner.poll(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail="run not found")

    log_path = Path(state.log_path)

    async def gen() -> AsyncGenerator[bytes, None]:
        # Initial state snapshot so the client can render immediately.
        yield _sse({"type": "state", **_state_payload(state)})

        seq = 0
        pos = 0
        last_hb = asyncio.get_event_loop().time()

        while True:
            if await request.is_disconnected():
                return

            # Tail new bytes from the log file if it exists.
            try:
                with log_path.open("r", encoding="utf-8", errors="replace") as fh:
                    fh.seek(pos)
                    chunk = fh.read()
                    pos = fh.tell()
            except FileNotFoundError:
                chunk = ""

            if chunk:
                for line in chunk.splitlines():
                    if not line.strip():
                        continue
                    seq += 1
                    yield _sse({"type": "log", "line": line, "seq": seq})

            # Re-poll for terminal state.
            s = _runner.poll(run_id)
            if s and s.status != "running":
                # Drain any remaining bytes the subprocess flushed before exit.
                try:
                    with log_path.open("r", encoding="utf-8", errors="replace") as fh:
                        fh.seek(pos)
                        tail = fh.read()
                except FileNotFoundError:
                    tail = ""
                for line in tail.splitlines():
                    if not line.strip():
                        continue
                    seq += 1
                    yield _sse({"type": "log", "line": line, "seq": seq})
                yield _sse({"type": "state", **_state_payload(s)})
                return

            now = asyncio.get_event_loop().time()
            if now - last_hb > 10:
                last_hb = now
                yield _sse({"type": "hb"})

            await asyncio.sleep(0.5)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",  # in case a proxy is in front
        },
    )


def _sse(payload: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


def _state_payload(state: Any) -> dict[str, Any]:
    return {
        "status": state.status,
        "report": state.report_path,
        "exit_code": state.exit_code,
        "ticker": state.ticker,
        "run_id": state.run_id,
    }
