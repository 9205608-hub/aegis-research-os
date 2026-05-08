"""Thesis API Router — CRUD for theses, version history, post-mortems."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Any

router = APIRouter()

# In-memory store for MVP
_theses: dict[str, dict] = {}


class ThesisCreate(BaseModel):
    thesis_id: str
    entity_id: str
    run_id: str
    core_thesis: str
    data: dict[str, Any] = {}


@router.post("/create")
def create_thesis(req: ThesisCreate):
    """Create a new thesis."""
    from aegis.core.thesis_manager import ThesisVersionManager

    tvm = ThesisVersionManager()
    snap = tvm.create_thesis(req.thesis_id, req.run_id, {
        "entity_id": req.entity_id,
        "core_thesis": req.core_thesis,
        **req.data,
    })
    _theses[req.thesis_id] = {
        "thesis_id": req.thesis_id,
        "version": snap.version,
        "status": snap.status,
        "data": snap.data,
    }
    return _theses[req.thesis_id]


@router.get("/{thesis_id}")
def get_thesis(thesis_id: str):
    """Get a thesis by ID."""
    thesis = _theses.get(thesis_id)
    if not thesis:
        raise HTTPException(404, "Thesis not found")
    return thesis


@router.get("/")
def list_theses():
    """List all theses."""
    return list(_theses.values())


@router.post("/{thesis_id}/status")
def update_status(thesis_id: str, new_status: str, reason: str = "manual update"):
    """Update thesis publishing status."""
    thesis = _theses.get(thesis_id)
    if not thesis:
        raise HTTPException(404, "Thesis not found")
    thesis["status"] = new_status
    return thesis
