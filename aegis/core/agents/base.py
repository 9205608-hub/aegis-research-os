"""Agent Base Framework — Section 19.1.

All specialist agents share these constraints:
1. NEVER compute financial values directly (use Formula/Scenario Engine)
2. NEVER introduce numbers not in input
3. Separate observation from inference
4. Include counterarguments
5. Output JudgmentContract schema objects
6. Complete cognitive bias self-check
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from aegis.data_contracts.judgment_schema import (
    CognitiveBiasSelfCheck,
    Counterargument,
    DisconfirmingTrigger,
    Inference,
    JudgmentContract,
    Observation,
)


@dataclass
class AgentInput:
    """Standard input bundle for any specialist agent."""

    entity_id: str
    run_id: str
    question_id: str
    facts: dict[str, Any] = field(default_factory=dict)
    evidence_packets: list[dict] = field(default_factory=list)
    metric_results: dict[str, Any] = field(default_factory=dict)
    macro_context: dict[str, Any] | None = None
    sector_pack: dict[str, Any] | None = None
    entity_relationships: list[dict] = field(default_factory=list)
    prior_judgments: list[JudgmentContract] = field(default_factory=list)

    # Segment-level data — enables per-segment analysis for any entity
    segment_data: dict[str, dict[str, Any]] = field(default_factory=dict)
    # e.g., {"foa": {"revenue": 160B, "operating_margin": 0.54, "metrics": {...}},
    #         "rl":  {"revenue": 3.9B, "operating_margin": -4.56, "metrics": {...}}}
    segment_ids: list[str] = field(default_factory=list)
    # Which segments to analyze; empty = company-level only

    # Rich segment detail from XBRL instance (product, geographic, business_segment)
    segment_detail: dict[str, Any] = field(default_factory=dict)
    # e.g., {"product": {"iphone": {"revenue": 201.2B}, ...},
    #         "geographic": {"americas": {"revenue": 167B}, ...}}

    # Findings from previously-run agents (enables inter-agent information flow)
    previous_agent_findings: list[dict[str, Any]] = field(default_factory=list)
    # e.g., [{"agent": "accounting_analyst", "key_finding": "...", "red_flag": True, "confidence": "high"}, ...]

    # Supplemental data injected to answer follow-up questions from a prior run
    supplemental_data: dict[str, Any] = field(default_factory=dict)
    # e.g., {"gross_margin_by_segment": {"iphone": 0.54, "services": 0.72}}

    # Peer fundamentals — list of dicts, one per peer, with PE / EV_EBITDA /
    # margins / growth rates. Rendered in the user prompt so valuation_analyst
    # and variant_analyst don't ask for "peer median PE" as a follow-up.
    peer_fundamentals: list[dict[str, Any]] = field(default_factory=list)

    # Historical valuation — {"dates": [...], "pe": [...], "ev_ebitda": [...],
    # "pe_stats": {"p25": ..., "p50": ..., "p75": ..., "current_percentile": ...}}
    # Rendered as a compact range summary in the user prompt.
    historical_valuation: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentOutput:
    """Wrapper around JudgmentContract with validation metadata."""

    judgment: JudgmentContract
    validation_passed: bool
    validation_errors: list[str] = field(default_factory=list)

    # Deep-mode narrative: free-form analysis beyond the structured JudgmentContract
    # Only populated when agent runs in "deep" mode — Synthesizer can reference this
    narrative_supplement: str = ""

    # Refactor 5 (2026-05-04): is_llm_fallback signals that the underlying
    # LLM call failed (timeout / content_filter / parse error / retries
    # exhausted) and the judgment was synthesized from a rule-based mock
    # template. Renderers should mark such agent cards as "fallback /
    # n/a" rather than treat the mock text as real analysis. Replaces
    # the previous string-prefix detection in html_report_v2.py — the
    # orchestrator now stamps the flag at the source.
    is_llm_fallback: bool = False
    llm_fallback_reason: str = ""


class AgentBase(ABC):
    """Abstract base class for all specialist agents.

    Enforces Section 19.1 constraints at the framework level.
    """

    AGENT_NAME: str = "base_agent"
    AGENT_VERSION: str = "0.1.0"

    def run(self, agent_input: AgentInput) -> AgentOutput:
        """Execute the agent pipeline: analyze → validate → output."""
        # 1. Agent-specific analysis
        observations = self._extract_observations(agent_input)
        inferences = self._derive_inferences(observations, agent_input)
        counterarguments = self._generate_counterarguments(inferences, agent_input)
        disconfirming = self._identify_disconfirming_triggers(inferences, agent_input)
        bias_check = self._cognitive_bias_self_check(agent_input)

        # 2. Collect traceability IDs
        used_metric_ids = self._collect_metric_ids(agent_input)
        used_evidence_ids = self._collect_evidence_ids(agent_input)
        used_relationship_ids = self._collect_relationship_ids(agent_input)

        # 3. Build judgment
        judgment = JudgmentContract(
            judgment_id=f"j_{self.AGENT_NAME}_{uuid4().hex[:8]}",
            agent_name=self.AGENT_NAME,
            agent_version=self.AGENT_VERSION,
            question_id=agent_input.question_id,
            run_id=agent_input.run_id,
            depends_on_judgment_ids=[
                j.judgment_id for j in agent_input.prior_judgments
            ],
            observations=observations,
            inferences=inferences,
            counterarguments=counterarguments,
            disconfirming_triggers=disconfirming,
            used_metric_ids=used_metric_ids,
            used_evidence_ids=used_evidence_ids,
            used_relationship_ids=used_relationship_ids,
            self_reported_uncertainties=self._report_uncertainties(agent_input),
            cognitive_bias_self_check=bias_check,
            sector_context_applied=self._sector_context_label(agent_input),
            judgment_status="complete",
        )

        # 4. Validate constraints
        errors = self._validate_constraints(judgment, agent_input)
        return AgentOutput(
            judgment=judgment,
            validation_passed=len(errors) == 0,
            validation_errors=errors,
        )

    # --- Abstract methods each agent must implement ---

    @abstractmethod
    def _extract_observations(self, inp: AgentInput) -> list[Observation]:
        """Extract factual observations from input data.

        Observations must be grounded in facts/evidence — no inference here.
        """

    @abstractmethod
    def _derive_inferences(
        self, observations: list[Observation], inp: AgentInput
    ) -> list[Inference]:
        """Derive inferences from observations.

        Each inference must reference observation indices.
        """

    @abstractmethod
    def _generate_counterarguments(
        self, inferences: list[Inference], inp: AgentInput
    ) -> list[Counterargument]:
        """Generate counterarguments for inferences.

        Cannot be empty — at least one counterargument required.
        """

    @abstractmethod
    def _identify_disconfirming_triggers(
        self, inferences: list[Inference], inp: AgentInput
    ) -> list[DisconfirmingTrigger]:
        """Identify observable events that would disconfirm the analysis."""

    @abstractmethod
    def _cognitive_bias_self_check(
        self, inp: AgentInput
    ) -> CognitiveBiasSelfCheck:
        """Self-assess bias risks in this analysis."""

    @abstractmethod
    def _report_uncertainties(self, inp: AgentInput) -> list[str]:
        """Report key uncertainties in the analysis."""

    # --- Default implementations (overridable) ---

    def _collect_metric_ids(self, inp: AgentInput) -> list[str]:
        """Collect metric IDs that this agent actually uses.

        If the agent defines FOCUS_METRICS, only include those.
        Also handles SBC/dilution mutual exclusion to prevent double-counting blocks.
        """
        if hasattr(self, "FOCUS_METRICS") and self.FOCUS_METRICS:
            metrics = [m for m in inp.metric_results if m in self.FOCUS_METRICS]
        else:
            metrics = list(inp.metric_results.keys())

        # SBC/dilution mutual exclusion: never claim both simultaneously
        # unless this agent specifically handles the double-counting logic
        has_sbc = "sbc_to_revenue" in metrics
        has_dilution = "dilution_rate" in metrics
        if has_sbc and has_dilution:
            # Prefer sbc_to_revenue (more conservative), drop dilution_rate
            metrics = [m for m in metrics if m != "dilution_rate"]

        return metrics

    def _collect_evidence_ids(self, inp: AgentInput) -> list[str]:
        return [ep.get("evidence_id", "") for ep in inp.evidence_packets if ep.get("evidence_id")]

    def _collect_relationship_ids(self, inp: AgentInput) -> list[str]:
        return [r.get("relationship_id", "") for r in inp.entity_relationships if r.get("relationship_id")]

    def _sector_context_label(self, inp: AgentInput) -> str | None:
        if inp.sector_pack:
            return inp.sector_pack.get("sector_pack_id")
        return None

    # --- Constraint validation (Section 19.1 enforcement) ---

    def _validate_constraints(
        self, judgment: JudgmentContract, inp: AgentInput
    ) -> list[str]:
        """Validate Section 19.1 hard constraints."""
        errors: list[str] = []

        # 1. Must have observations
        if not judgment.observations:
            errors.append("CONSTRAINT_VIOLATION: No observations — agent must ground analysis in data")

        # 2. Each inference must reference valid observation indices
        obs_count = len(judgment.observations)
        for i, inf in enumerate(judgment.inferences):
            for idx in inf.based_on_observation_indices:
                if idx < 0 or idx >= obs_count:
                    errors.append(
                        f"CONSTRAINT_VIOLATION: Inference[{i}] references observation index {idx} "
                        f"but only {obs_count} observations exist"
                    )

        # 3. Must have at least one counterargument
        if not judgment.counterarguments:
            errors.append("CONSTRAINT_VIOLATION: No counterarguments — agent must not omit opposing view")

        # 4. Observations must have source_ids
        for i, obs in enumerate(judgment.observations):
            if not obs.source_ids:
                errors.append(f"CONSTRAINT_VIOLATION: Observation[{i}] has no source_ids")

        # 5. Cognitive bias self-check must be present (enforced by schema, but double-check)
        if judgment.cognitive_bias_self_check is None:
            errors.append("CONSTRAINT_VIOLATION: Missing cognitive bias self-check")

        return errors
