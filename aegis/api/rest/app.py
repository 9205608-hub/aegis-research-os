"""REST API — Section 28.3.

FastAPI application with routers for research, thesis, events, portfolio, admin.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from aegis.api.rest.routers.research import router as research_router
from aegis.api.rest.routers.thesis import router as thesis_router
from aegis.api.rest.routers.events import router as events_router
from aegis.api.rest.routers.portfolio import router as portfolio_router
from aegis.api.rest.routers.admin import router as admin_router

app = FastAPI(
    title="Aegis Research OS",
    description="Intelligent Investment Research System API",
    version="0.1.0",
)

# CORS for dashboard
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(research_router, prefix="/api/v1/research", tags=["research"])
app.include_router(thesis_router, prefix="/api/v1/thesis", tags=["thesis"])
app.include_router(events_router, prefix="/api/v1/events", tags=["events"])
app.include_router(portfolio_router, prefix="/api/v1/portfolio", tags=["portfolio"])
app.include_router(admin_router, prefix="/api/v1/admin", tags=["admin"])


@app.get("/health")
def health_check():
    return {"status": "ok", "version": "0.1.0"}


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    """Serve the Aegis Dashboard."""
    html_path = Path(__file__).resolve().parent.parent.parent / "dashboard" / "index.html"
    if html_path.exists():
        return html_path.read_text(encoding="utf-8")
    return HTMLResponse("<h1>Dashboard not found</h1>", status_code=404)
