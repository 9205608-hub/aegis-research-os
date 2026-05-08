"""Admin API Router — audit log, cost monitoring, system status."""

from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import dataclass, field, asdict
from fastapi import APIRouter

router = APIRouter()


@dataclass
class AuditEntry:
    timestamp: str
    action: str
    user: str
    details: str


@dataclass
class CostRecord:
    run_id: str
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0


# In-memory stores for MVP
_audit_log: list[AuditEntry] = []
_cost_records: dict[str, CostRecord] = {}


@router.get("/audit-log")
def get_audit_log(limit: int = 50):
    """Get recent audit log entries."""
    return [asdict(e) for e in _audit_log[-limit:]]


@router.post("/audit-log")
def add_audit_entry(action: str, user: str = "system", details: str = ""):
    """Add an audit log entry."""
    entry = AuditEntry(
        timestamp=datetime.now(timezone.utc).isoformat(),
        action=action, user=user, details=details,
    )
    _audit_log.append(entry)
    return asdict(entry)


@router.get("/costs")
def get_cost_summary():
    """Get LLM cost summary across runs."""
    total = sum(c.estimated_cost_usd for c in _cost_records.values())
    return {
        "total_runs": len(_cost_records),
        "total_cost_usd": total,
        "runs": [asdict(c) for c in _cost_records.values()],
    }


@router.post("/costs/{run_id}")
def record_cost(run_id: str, llm_calls: int = 0, input_tokens: int = 0,
                output_tokens: int = 0, estimated_cost_usd: float = 0.0):
    """Record LLM cost for a run."""
    record = CostRecord(
        run_id=run_id, llm_calls=llm_calls,
        input_tokens=input_tokens, output_tokens=output_tokens,
        estimated_cost_usd=estimated_cost_usd,
    )
    _cost_records[run_id] = record
    return asdict(record)


@router.get("/status")
def system_status():
    """System health status."""
    return {
        "status": "operational",
        "version": "0.1.0",
        "audit_entries": len(_audit_log),
        "cost_records": len(_cost_records),
    }
