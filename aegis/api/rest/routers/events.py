"""Events API Router — emit events, query event log, manage monitorables."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from aegis.core.events import EventBus

router = APIRouter()

# Shared event bus instance for MVP
_event_bus = EventBus()


class EventEmit(BaseModel):
    category: str  # "A" through "E"
    entity_id: str
    description: str
    affected_thesis_ids: list[str] = []


class MonitorableRegister(BaseModel):
    thesis_id: str
    entity_id: str
    description: str
    check_frequency: str = "quarterly"
    source_agent: str = "manual"


@router.post("/emit")
def emit_event(req: EventEmit):
    """Emit an event to the event bus."""
    record = _event_bus.emit_event(
        req.category, req.entity_id, req.description, req.affected_thesis_ids,
    )
    return {
        "event_id": record.event_id,
        "category": record.category,
        "triggered_actions": record.triggered_actions,
        "affected_thesis_ids": record.affected_thesis_ids,
    }


@router.get("/log")
def get_event_log(entity_id: str | None = None):
    """Get the event log."""
    events = _event_bus.get_event_log(entity_id)
    return [
        {"event_id": e.event_id, "category": e.category,
         "entity_id": e.entity_id, "description": e.description}
        for e in events
    ]


@router.post("/monitorables")
def register_monitorable(req: MonitorableRegister):
    """Register a new monitorable."""
    mid = _event_bus.register_monitorable(
        req.thesis_id, req.entity_id, req.description,
        req.check_frequency, req.source_agent,
    )
    return {"monitorable_id": mid}


@router.get("/monitorables")
def list_monitorables(entity_id: str | None = None):
    """List active monitorables."""
    monitorables = _event_bus.get_active_monitorables(entity_id)
    return [
        {"monitorable_id": m.monitorable_id, "thesis_id": m.thesis_id,
         "entity_id": m.entity_id, "description": m.description}
        for m in monitorables
    ]
