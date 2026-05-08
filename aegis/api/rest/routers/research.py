"""Research API Router — run research, check status, get results.

Connects the FastAPI endpoints to AutoResearchOrchestrator and ResearchRepository.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Any

from aegis.data_contracts.common import ResearchMode

router = APIRouter()

# In-memory fallback store (used when DB is not configured)
_research_runs: dict[str, dict] = {}


class ResearchRunRequest(BaseModel):
    """Request to start a single-entity auto research run."""

    ticker: str = Field(min_length=1, max_length=10)
    period: str = "FY2024"
    current_price: float | None = None
    market_cap: float | None = None
    sector_pack_id: str | None = None
    wacc: float = 0.095
    terminal_growth_rate: float = 0.03
    generate_html: bool = True


class MultiEntityRequest(BaseModel):
    """Request to start a multi-entity research run."""

    entity_ids: list[str] = Field(min_length=1)
    research_mode: str = "single_entity"
    theme: str = ""
    sector_pack_id: str = ""


class ResearchRunResponse(BaseModel):
    """Response from a completed research run."""

    run_id: str
    ticker: str
    entity_id: str
    status: str
    dcf_per_share: float | None = None
    scenarios: dict[str, float] | None = None
    implied_growth: float | None = None
    publishing_status: str | None = None
    confidence_bucket: str | None = None
    direction: str | None = None
    conviction: str | None = None
    metrics_count: int = 0
    html_report_path: str | None = None
    pipeline_log: list[str] = []
    warnings: list[str] = []


class ResearchListItem(BaseModel):
    run_id: str
    ticker: str
    status: str
    dcf_per_share: float | None = None
    direction: str | None = None
    confidence_bucket: str | None = None


@router.post("/auto", response_model=ResearchRunResponse)
def run_auto_research(req: ResearchRunRequest):
    """Run fully automated research on a single ticker.

    Executes the full pipeline: EDGAR → adapt → metrics → DCF → agents → critics → report.
    """
    from aegis.core.orchestrator.auto_research import AutoResearchOrchestrator, ResearchConfig

    config = ResearchConfig(
        ticker=req.ticker.upper(),
        period=req.period,
        current_price=req.current_price,
        market_cap=req.market_cap,
        sector_pack_id=req.sector_pack_id,
        wacc=req.wacc,
        terminal_growth_rate=req.terminal_growth_rate,
        generate_html=req.generate_html,
    )

    try:
        orchestrator = AutoResearchOrchestrator()
        result = orchestrator.run(config)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(502, str(e))

    # Store in memory
    _research_runs[result.run_id] = {
        "run_id": result.run_id,
        "ticker": result.ticker,
        "entity_id": result.entity_id,
        "status": "completed",
        "result": result,
    }

    # Try to persist to DB (non-blocking)
    try:
        from aegis.core.storage.repository import ResearchRepository
        repo = ResearchRepository()
        repo.save_research_run(result)
        if result.signal:
            repo.save_signal(result.signal, result.entity_id, result.run_id)
    except Exception:
        pass  # DB is optional

    return ResearchRunResponse(
        run_id=result.run_id,
        ticker=result.ticker,
        entity_id=result.entity_id,
        status="completed",
        dcf_per_share=result.dcf_per_share,
        scenarios=result.scenarios,
        implied_growth=result.implied_growth,
        publishing_status=getattr(result.decision, "publishing_status", None),
        confidence_bucket=getattr(result.decision, "confidence_bucket", None),
        direction=getattr(result.signal, "direction", None),
        conviction=getattr(result.signal, "conviction", None),
        metrics_count=len(result.computed_metrics),
        html_report_path=result.html_path,
        pipeline_log=result.pipeline_log,
        warnings=result.bridge_warnings,
    )


@router.post("/run")
def start_research_legacy(req: MultiEntityRequest):
    """Start a research run (legacy endpoint — plans phases).

    Kept for backward compatibility. Use /auto for single-entity auto research.
    """
    return plan_research(req)


@router.post("/plan")
def plan_research(req: MultiEntityRequest):
    """Plan a research run (creates phases but doesn't execute)."""
    from aegis.core.planner import ResearchModeRouter, ResearchRequest as PlanRequest

    try:
        mode = ResearchMode(req.research_mode)
    except ValueError:
        raise HTTPException(400, f"Invalid research mode: {req.research_mode}")

    router_inst = ResearchModeRouter()
    plan = router_inst.route(PlanRequest(
        research_mode=mode,
        entity_ids=req.entity_ids,
        theme=req.theme,
    ))

    result = {
        "run_id": plan.run_id,
        "status": "planned",
        "research_mode": req.research_mode,
        "entity_ids": plan.entity_ids,
        "phases": [
            {"name": p.phase_name, "entities": p.entity_ids, "agents": p.agents_to_run}
            for p in plan.phases
        ],
    }

    # Store for status lookup
    _research_runs[plan.run_id] = {
        "run_id": plan.run_id,
        "ticker": req.entity_ids[0] if req.entity_ids else "",
        "status": "planned",
        "result": None,
    }

    return result


@router.get("/run/{run_id}")
def get_research_status(run_id: str):
    """Get status of a research run."""
    run = _research_runs.get(run_id)
    if run:
        result = run.get("result")
        return {
            "run_id": run_id,
            "ticker": run.get("ticker"),
            "status": run["status"],
            "dcf_per_share": result.dcf_per_share if result else None,
            "scenarios": result.scenarios if result else None,
            "direction": getattr(result.signal, "direction", None) if result else None,
        }

    # Try DB
    try:
        from aegis.core.storage.repository import ResearchRepository
        repo = ResearchRepository()
        db_run = repo.get_run(run_id)
        if db_run:
            return {
                "run_id": db_run.run_id,
                "ticker": db_run.ticker,
                "status": db_run.status,
                "dcf_per_share": db_run.dcf_per_share,
                "scenarios": db_run.scenarios_json,
                "direction": db_run.direction,
            }
    except Exception:
        pass

    raise HTTPException(404, "Run not found")


@router.get("/runs", response_model=list[ResearchListItem])
def list_runs():
    """List all research runs."""
    items = []
    for k, v in _research_runs.items():
        result = v.get("result")
        items.append(ResearchListItem(
            run_id=k,
            ticker=v.get("ticker", ""),
            status=v["status"],
            dcf_per_share=result.dcf_per_share if result else None,
            direction=getattr(result.signal, "direction", None) if result else None,
            confidence_bucket=getattr(result.decision, "confidence_bucket", None) if result else None,
        ))
    return items


@router.get("/run/{run_id}/metrics")
def get_run_metrics(run_id: str):
    """Get computed metrics for a research run."""
    run = _research_runs.get(run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    result = run.get("result")
    if not result:
        raise HTTPException(404, "No results available")
    return {
        "run_id": run_id,
        "computed_metrics": result.computed_metrics,
        "meta_facts_count": len(result.meta_facts),
    }


@router.get("/run/{run_id}/facts")
def get_run_facts(run_id: str):
    """Get meta_facts for a research run."""
    run = _research_runs.get(run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    result = run.get("result")
    if not result:
        raise HTTPException(404, "No results available")
    return {"run_id": run_id, "meta_facts": result.meta_facts}
