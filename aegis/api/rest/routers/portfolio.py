"""Portfolio API Router — signals, predictions, calibration."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()


class SignalResponse(BaseModel):
    entity_id: str
    run_id: str
    direction: str
    conviction: str
    sizing_tier: str | None = None
    data_quality_tier: str | None = None


@router.get("/signals")
def list_signals():
    """List all portfolio signals (from in-memory research runs)."""
    from aegis.api.rest.routers.research import _research_runs

    signals = []
    for run_id, run in _research_runs.items():
        result = run.get("result")
        if result and hasattr(result, "signal"):
            sig = result.signal
            signals.append({
                "entity_id": result.entity_id,
                "run_id": result.run_id,
                "direction": getattr(sig, "direction", "no_signal"),
                "conviction": getattr(sig, "conviction", "very_low"),
                "sizing_tier": getattr(sig, "sizing_tier", None),
                "data_quality_tier": getattr(sig, "data_quality_tier", None),
                "thesis_horizon": getattr(sig, "thesis_horizon", None),
                "expected_value": getattr(sig, "expected_value_per_share", None),
            })
    return signals


@router.get("/signals/{entity_id}")
def get_signal(entity_id: str):
    """Get signal for a specific entity."""
    from aegis.api.rest.routers.research import _research_runs

    for run in _research_runs.values():
        result = run.get("result")
        if result and result.entity_id == entity_id and hasattr(result, "signal"):
            sig = result.signal
            return {
                "entity_id": entity_id,
                "run_id": result.run_id,
                "direction": getattr(sig, "direction", "no_signal"),
                "conviction": getattr(sig, "conviction", "very_low"),
                "sizing_tier": getattr(sig, "sizing_tier", None),
                "data_quality_tier": getattr(sig, "data_quality_tier", None),
            }
    raise HTTPException(404, f"No signal for entity: {entity_id}")


@router.get("/stats")
def get_portfolio_stats():
    """Get portfolio-level statistics."""
    try:
        from aegis.core.storage.repository import ResearchRepository
        repo = ResearchRepository()
        return repo.get_stats()
    except Exception:
        from aegis.api.rest.routers.research import _research_runs
        return {
            "total_runs": len(_research_runs),
            "completed_runs": sum(1 for r in _research_runs.values() if r["status"] == "completed"),
        }
