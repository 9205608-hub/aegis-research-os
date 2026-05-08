"""Multi-Entity Research Mode Router — Section 16.1 + 18.

Routes research requests to the appropriate orchestration pipeline
based on research mode.

Section 16.4 principles:
1. Each entity in multi-entity research must pass all publish gates.
2. Cross-entity comparison uses same definition_id and period alignment.
3. Cross-market comparison must go through accounting bridge + currency engine.
4. Screening criteria must be recorded in run_manifest.
5. Pair trade entities must be analyzed in same run, sharing macro context.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from aegis.data_contracts.common import ResearchMode


@dataclass
class ResearchRequest:
    """A request to initiate research."""

    research_mode: ResearchMode
    entity_ids: list[str]
    theme: str = ""
    event_description: str = ""
    screening_filters: list[dict] = field(default_factory=list)
    macro_context_id: str = ""
    sector_pack_ids: list[str] = field(default_factory=list)
    run_id: str = ""
    # Segment analysis configuration
    segment_analysis: bool = False  # Whether to run segment-level DCF
    segment_definitions: dict[str, list[dict]] = field(default_factory=dict)
    # e.g., {"meta": [{"segment_id": "foa", "name": "Family of Apps"},
    #                  {"segment_id": "rl", "name": "Reality Labs"}]}

    def __post_init__(self):
        if not self.run_id:
            self.run_id = f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:6]}"


@dataclass
class ResearchPlan:
    """Output of the router — a structured execution plan."""

    run_id: str
    research_mode: ResearchMode
    entity_ids: list[str]
    phases: list[ResearchPhase]
    shared_context: dict[str, Any] = field(default_factory=dict)


@dataclass
class ResearchPhase:
    """One phase of a multi-phase research plan."""

    phase_name: str
    entity_ids: list[str]
    agents_to_run: list[str]
    cross_entity_agents: list[str] = field(default_factory=list)
    critics_to_run: list[str] = field(default_factory=list)
    depends_on_phase: str | None = None
    segment_ids: list[str] = field(default_factory=list)  # Segments to analyze in this phase


class ResearchModeRouter:
    """Routes research requests to the appropriate execution plan.

    Section 16.1: supports 6 research modes with different agent orchestrations.
    """

    # Standard single-entity agent set
    SINGLE_ENTITY_AGENTS = [
        "accounting_analyst", "business_analyst", "sector_context_agent",
        "management_analyst", "valuation_analyst", "variant_analyst", "risk_analyst",
    ]

    STANDARD_CRITICS = [
        "logic_critic", "accounting_critic", "evidence_critic",
        "sector_critic", "cognitive_bias_critic",
        "macro_consistency_critic", "market_critic",
    ]

    def route(self, request: ResearchRequest) -> ResearchPlan:
        """Create an execution plan based on research mode."""
        mode = request.research_mode

        if mode == ResearchMode.SINGLE_ENTITY:
            return self._plan_single_entity(request)
        elif mode == ResearchMode.MULTI_ENTITY:
            return self._plan_multi_entity(request)
        elif mode == ResearchMode.THEMATIC:
            return self._plan_thematic(request)
        elif mode == ResearchMode.EVENT_IMPACT:
            return self._plan_event_impact(request)
        elif mode == ResearchMode.SUPPLY_CHAIN:
            return self._plan_supply_chain(request)
        elif mode == ResearchMode.PAIR_TRADE:
            return self._plan_pair_trade(request)
        else:
            raise ValueError(f"Unsupported research mode: {mode}")

    def _plan_single_entity(self, req: ResearchRequest) -> ResearchPlan:
        phases = [
            ResearchPhase(
                phase_name="full_analysis",
                entity_ids=req.entity_ids[:1],
                agents_to_run=self.SINGLE_ENTITY_AGENTS,
                critics_to_run=self.STANDARD_CRITICS,
            ),
        ]

        # Add segment analysis phase if requested and segments are defined
        entity_id = req.entity_ids[0] if req.entity_ids else ""
        entity_segments = req.segment_definitions.get(entity_id, [])
        if req.segment_analysis and entity_segments:
            seg_ids = [s.get("segment_id", "") for s in entity_segments if s.get("segment_id")]
            phases.append(
                ResearchPhase(
                    phase_name="segment_analysis",
                    entity_ids=req.entity_ids[:1],
                    agents_to_run=["business_analyst", "valuation_analyst"],
                    critics_to_run=["logic_critic", "accounting_critic"],
                    depends_on_phase="full_analysis",
                    segment_ids=seg_ids,
                ),
            )

        return ResearchPlan(
            run_id=req.run_id,
            research_mode=ResearchMode.SINGLE_ENTITY,
            entity_ids=req.entity_ids[:1],
            phases=phases,
            shared_context={"segment_definitions": req.segment_definitions} if req.segment_analysis else {},
        )

    def _plan_multi_entity(self, req: ResearchRequest) -> ResearchPlan:
        return ResearchPlan(
            run_id=req.run_id,
            research_mode=ResearchMode.MULTI_ENTITY,
            entity_ids=req.entity_ids,
            phases=[
                ResearchPhase(
                    phase_name="per_entity_analysis",
                    entity_ids=req.entity_ids,
                    agents_to_run=["business_analyst", "sector_context_agent", "valuation_analyst"],
                    critics_to_run=["logic_critic", "evidence_critic"],
                ),
                ResearchPhase(
                    phase_name="comparative_analysis",
                    entity_ids=req.entity_ids,
                    agents_to_run=[],
                    cross_entity_agents=["comparative_analyst"],
                    critics_to_run=["cross_entity_critic"],
                    depends_on_phase="per_entity_analysis",
                ),
            ],
        )

    def _plan_thematic(self, req: ResearchRequest) -> ResearchPlan:
        return ResearchPlan(
            run_id=req.run_id,
            research_mode=ResearchMode.THEMATIC,
            entity_ids=req.entity_ids,
            shared_context={"theme": req.theme, "filters": req.screening_filters},
            phases=[
                ResearchPhase(
                    phase_name="screening",
                    entity_ids=req.entity_ids,
                    agents_to_run=["business_analyst", "sector_context_agent"],
                ),
                ResearchPhase(
                    phase_name="comparative_ranking",
                    entity_ids=req.entity_ids,
                    agents_to_run=["valuation_analyst"],
                    cross_entity_agents=["comparative_analyst"],
                    critics_to_run=["cross_entity_critic"],
                    depends_on_phase="screening",
                ),
                ResearchPhase(
                    phase_name="deep_dive_top_picks",
                    entity_ids=[],  # Filled at runtime from top picks
                    agents_to_run=self.SINGLE_ENTITY_AGENTS,
                    critics_to_run=self.STANDARD_CRITICS,
                    depends_on_phase="comparative_ranking",
                ),
            ],
        )

    def _plan_event_impact(self, req: ResearchRequest) -> ResearchPlan:
        return ResearchPlan(
            run_id=req.run_id,
            research_mode=ResearchMode.EVENT_IMPACT,
            entity_ids=req.entity_ids,
            shared_context={"event": req.event_description},
            phases=[
                ResearchPhase(
                    phase_name="impact_assessment",
                    entity_ids=req.entity_ids,
                    agents_to_run=["risk_analyst", "business_analyst"],
                    cross_entity_agents=["comparative_analyst"],
                    critics_to_run=["cross_entity_critic"],
                ),
            ],
        )

    def _plan_supply_chain(self, req: ResearchRequest) -> ResearchPlan:
        return ResearchPlan(
            run_id=req.run_id,
            research_mode=ResearchMode.SUPPLY_CHAIN,
            entity_ids=req.entity_ids,
            phases=[
                ResearchPhase(
                    phase_name="chain_analysis",
                    entity_ids=req.entity_ids,
                    agents_to_run=["business_analyst", "risk_analyst"],
                    cross_entity_agents=["comparative_analyst"],
                    critics_to_run=["cross_entity_critic"],
                ),
            ],
        )

    def _plan_pair_trade(self, req: ResearchRequest) -> ResearchPlan:
        if len(req.entity_ids) != 2:
            raise ValueError("Pair trade requires exactly 2 entities")
        return ResearchPlan(
            run_id=req.run_id,
            research_mode=ResearchMode.PAIR_TRADE,
            entity_ids=req.entity_ids,
            phases=[
                ResearchPhase(
                    phase_name="per_entity_full",
                    entity_ids=req.entity_ids,
                    agents_to_run=self.SINGLE_ENTITY_AGENTS,
                    critics_to_run=self.STANDARD_CRITICS,
                ),
                ResearchPhase(
                    phase_name="pair_comparison",
                    entity_ids=req.entity_ids,
                    agents_to_run=[],
                    cross_entity_agents=["comparative_analyst"],
                    critics_to_run=["cross_entity_critic"],
                    depends_on_phase="per_entity_full",
                ),
            ],
        )
