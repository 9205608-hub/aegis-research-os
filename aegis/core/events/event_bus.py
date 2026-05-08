"""Event Bus / Trigger Service — Section 24.

Categories:
- Category A: Filing Events → full re-run
- Category B: Market Events → partial update
- Category C: Monitoring Events → monitorable check
- Category D: Macro Events → macro layer refresh
- Category E: Edge Decay Events → edge reassessment

Registered objects:
- Thesis monitorables → monitoring check
- Kill criteria → thesis review_required
- Edge decay triggers → edge reassessment
- Entity relationship changes → cascade impact check

Principles:
1. Every monitorable, kill criterion, edge decay trigger must be registered.
2. Event-triggered updates must generate new run_manifest.
3. Full audit log.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Callable
from uuid import uuid4


@dataclass
class RegisteredMonitorable:
    """A monitorable registered for ongoing checking."""

    monitorable_id: str
    thesis_id: str
    entity_id: str
    description: str
    check_frequency: str  # "daily", "weekly", "monthly", "quarterly"
    source_agent: str
    registered_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    active: bool = True


@dataclass
class RegisteredKillCriterion:
    """A kill criterion registered for monitoring."""

    kill_id: str
    thesis_id: str
    entity_id: str
    description: str
    threshold: str
    check_frequency: str
    registered_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    triggered: bool = False


@dataclass
class RegisteredEdgeDecay:
    """An edge decay trigger registered for monitoring."""

    decay_id: str
    thesis_id: str
    entity_id: str
    edge_type: str
    decay_trigger: str
    registered_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    decayed: bool = False


@dataclass
class RegisteredCatalyst:
    """A catalyst event registered for tracking.

    Catalysts are time-bound events that could close the variant gap.
    Category F events in the bus.
    """

    catalyst_id: str
    thesis_id: str
    entity_id: str
    description: str
    expected_date: date | None = None
    catalyst_type: str = "other"  # "earnings", "regulatory", "product_launch", "macro", "management", "other"
    registered_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    materialized: bool = False


@dataclass
class EventRecord:
    """An event processed by the bus."""

    event_id: str
    category: str  # "A" through "F"
    entity_id: str
    description: str
    timestamp: datetime
    triggered_actions: list[str] = field(default_factory=list)
    affected_thesis_ids: list[str] = field(default_factory=list)


class EventBus:
    """Central event bus for the research system.

    Section 24.3: every monitorable, kill criterion, edge decay trigger
    must be registered. Full audit log maintained.
    """

    def __init__(self) -> None:
        self._monitorables: dict[str, RegisteredMonitorable] = {}
        self._kill_criteria: dict[str, RegisteredKillCriterion] = {}
        self._edge_decays: dict[str, RegisteredEdgeDecay] = {}
        self._catalysts: dict[str, RegisteredCatalyst] = {}
        self._event_log: list[EventRecord] = []
        self._handlers: dict[str, list[Callable]] = {}

    # --- Registration ---

    def register_monitorable(
        self, thesis_id: str, entity_id: str, description: str,
        check_frequency: str, source_agent: str,
    ) -> str:
        mid = f"mon_{uuid4().hex[:8]}"
        self._monitorables[mid] = RegisteredMonitorable(
            monitorable_id=mid, thesis_id=thesis_id, entity_id=entity_id,
            description=description, check_frequency=check_frequency,
            source_agent=source_agent,
        )
        return mid

    def register_kill_criterion(
        self, thesis_id: str, entity_id: str, description: str,
        threshold: str, check_frequency: str,
    ) -> str:
        kid = f"kill_{uuid4().hex[:8]}"
        self._kill_criteria[kid] = RegisteredKillCriterion(
            kill_id=kid, thesis_id=thesis_id, entity_id=entity_id,
            description=description, threshold=threshold,
            check_frequency=check_frequency,
        )
        return kid

    def register_edge_decay(
        self, thesis_id: str, entity_id: str, edge_type: str,
        decay_trigger: str,
    ) -> str:
        did = f"decay_{uuid4().hex[:8]}"
        self._edge_decays[did] = RegisteredEdgeDecay(
            decay_id=did, thesis_id=thesis_id, entity_id=entity_id,
            edge_type=edge_type, decay_trigger=decay_trigger,
        )
        return did

    def register_catalyst(
        self, thesis_id: str, entity_id: str, description: str,
        catalyst_type: str = "other", expected_date: date | None = None,
    ) -> str:
        """Register a catalyst event for tracking."""
        cid = f"cat_{uuid4().hex[:8]}"
        self._catalysts[cid] = RegisteredCatalyst(
            catalyst_id=cid, thesis_id=thesis_id, entity_id=entity_id,
            description=description, catalyst_type=catalyst_type,
            expected_date=expected_date,
        )
        return cid

    # --- Event Processing ---

    def emit_event(
        self, category: str, entity_id: str, description: str,
        affected_thesis_ids: list[str] | None = None,
    ) -> EventRecord:
        """Emit an event and determine triggered actions."""
        event_id = f"evt_{uuid4().hex[:8]}"
        actions: list[str] = []
        affected = affected_thesis_ids or []

        if category == "A":
            # Filing event → full re-run for affected theses
            actions.append("full_rerun")
            for m in self._monitorables.values():
                if m.entity_id == entity_id and m.active:
                    affected.append(m.thesis_id)
        elif category == "B":
            # Market event → incremental update
            actions.append("incremental_update")
        elif category == "C":
            # Monitoring event → check monitorables
            actions.append("monitorable_check")
            for m in self._monitorables.values():
                if m.entity_id == entity_id and m.active:
                    affected.append(m.thesis_id)
        elif category == "D":
            actions.append("macro_refresh")
        elif category == "E":
            # Edge decay event
            actions.append("edge_reassessment")
            for d in self._edge_decays.values():
                if d.entity_id == entity_id and not d.decayed:
                    affected.append(d.thesis_id)

        record = EventRecord(
            event_id=event_id, category=category, entity_id=entity_id,
            description=description, timestamp=datetime.now(timezone.utc),
            triggered_actions=actions, affected_thesis_ids=list(set(affected)),
        )
        self._event_log.append(record)

        # Fire handlers
        for handler in self._handlers.get(category, []):
            handler(record)

        return record

    def on_event(self, category: str, handler: Callable) -> None:
        """Register a handler for a specific event category."""
        self._handlers.setdefault(category, []).append(handler)

    # --- Trigger Checking ---

    def check_kill_criteria(self, entity_id: str, current_data: dict) -> list[str]:
        """Check if any kill criteria are triggered. Returns triggered kill IDs."""
        triggered = []
        for kid, kc in self._kill_criteria.items():
            if kc.entity_id == entity_id and not kc.triggered:
                # In production, this would evaluate the threshold against current_data
                # For now, check if any key matches the description
                triggered_flag = current_data.get(f"kill:{kc.description}", False)
                if triggered_flag:
                    kc.triggered = True
                    triggered.append(kid)
        return triggered

    def mark_edge_decayed(self, decay_id: str) -> None:
        """Mark an edge as decayed."""
        if decay_id in self._edge_decays:
            self._edge_decays[decay_id].decayed = True

    def deactivate_monitorable(self, monitorable_id: str) -> None:
        """Deactivate a monitorable (e.g., thesis killed)."""
        if monitorable_id in self._monitorables:
            self._monitorables[monitorable_id].active = False

    # --- Query ---

    def get_active_monitorables(self, entity_id: str | None = None) -> list[RegisteredMonitorable]:
        results = [m for m in self._monitorables.values() if m.active]
        if entity_id:
            results = [m for m in results if m.entity_id == entity_id]
        return results

    def get_active_kill_criteria(self, thesis_id: str | None = None) -> list[RegisteredKillCriterion]:
        results = [k for k in self._kill_criteria.values() if not k.triggered]
        if thesis_id:
            results = [k for k in results if k.thesis_id == thesis_id]
        return results

    def get_event_log(self, entity_id: str | None = None) -> list[EventRecord]:
        if entity_id:
            return [e for e in self._event_log if e.entity_id == entity_id]
        return list(self._event_log)

    def get_active_catalysts(self, entity_id: str | None = None) -> list[RegisteredCatalyst]:
        results = [c for c in self._catalysts.values() if not c.materialized]
        if entity_id:
            results = [c for c in results if c.entity_id == entity_id]
        return results

    def get_registrations_for_thesis(self, thesis_id: str) -> dict[str, int]:
        return {
            "monitorables": sum(1 for m in self._monitorables.values() if m.thesis_id == thesis_id),
            "kill_criteria": sum(1 for k in self._kill_criteria.values() if k.thesis_id == thesis_id),
            "edge_decays": sum(1 for d in self._edge_decays.values() if d.thesis_id == thesis_id),
            "catalysts": sum(1 for c in self._catalysts.values() if c.thesis_id == thesis_id),
        }
