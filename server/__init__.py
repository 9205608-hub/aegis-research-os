"""Aegis Research OS — local web server.

Thin FastAPI wrapper that exposes the search/progress/report pages as a
web app. Core pipeline in `aegis/` is unchanged — the server spawns
`./run_research.sh` as a subprocess when a run is requested.

Run locally:

    uvicorn server.app:app --reload --port 8000

Open http://localhost:8000
"""
