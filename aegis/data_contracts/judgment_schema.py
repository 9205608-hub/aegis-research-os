"""Section 9.6 — Judgment Contract."""

from pydantic import Field

from .common import (
    EvidenceId,
    JudgmentId,
    MetricId,
    RunId,
    ScenarioId,
    StrictModel,
)


class CognitiveBiasSelfCheck(StrictModel):
    """Self-reported bias risk assessment by an agent."""

    anchoring_risk: str = Field(pattern=r"^(low|medium|high)$")
    confirmation_bias_risk: str = Field(pattern=r"^(low|medium|high)$")
    recency_bias_risk: str = Field(pattern=r"^(low|medium|high)$")
    narrative_fallacy_risk: str = Field(pattern=r"^(low|medium|high)$")
    mitigation_steps_taken: list[str] = Field(default_factory=list)


class Observation(StrictModel):
    """A factual observation derived from data or evidence."""

    text: str = Field(min_length=1)
    source_ids: list[str] = Field(default_factory=list)  # fact_ids or evidence_ids; critic validates non-empty


class Inference(StrictModel):
    """An inference derived from observations."""

    text: str = Field(min_length=1)
    based_on_observation_indices: list[int] = Field(min_length=1)
    confidence: str = Field(pattern=r"^(low|medium|high)$")


class Counterargument(StrictModel):
    """A counterargument to the agent's inferences."""

    text: str = Field(min_length=1)
    strength: str = Field(pattern=r"^(weak|moderate|strong)$")
    evidence_ids: list[str] = Field(default_factory=list)


class DisconfirmingTrigger(StrictModel):
    """A specific, observable event that would disconfirm the judgment."""

    text: str = Field(min_length=1)
    monitorable: bool = True
    check_frequency: str = Field(default="quarterly")


class FollowUpQuestion(StrictModel):
    """A question the agent needs answered to refine its judgment."""

    question: str = Field(min_length=1)
    data_type: str = Field(pattern=r"^(metric|segment|time_series|fact)$")
    data_key: str = Field(min_length=1)  # e.g., "capex_by_segment", "operating_margin"
    priority: str = Field(default="medium", pattern=r"^(high|medium|low)$")


class JudgmentContract(StrictModel):
    """Structured output from a specialist agent."""

    judgment_id: JudgmentId
    agent_name: str = Field(min_length=1)
    agent_version: str = Field(min_length=1)
    question_id: str = Field(min_length=1)
    run_id: RunId
    depends_on_judgment_ids: list[JudgmentId] = Field(default_factory=list)
    observations: list[Observation] = Field(default_factory=list)
    inferences: list[Inference] = Field(default_factory=list)
    counterarguments: list[Counterargument] = Field(default_factory=list)
    disconfirming_triggers: list[DisconfirmingTrigger] = Field(default_factory=list)
    used_metric_ids: list[MetricId] = Field(default_factory=list)
    used_evidence_ids: list[EvidenceId] = Field(default_factory=list)
    used_macro_context_ids: list[str] = Field(default_factory=list)
    used_scenario_ids: list[ScenarioId] = Field(default_factory=list)
    used_relationship_ids: list[str] = Field(default_factory=list)
    forbidden_leaps_detected: list[str] = Field(default_factory=list)
    self_reported_uncertainties: list[str] = Field(default_factory=list)
    cognitive_bias_self_check: CognitiveBiasSelfCheck
    sector_context_applied: str | None = None
    judgment_status: str = Field(pattern=r"^(complete|partial|blocked)$")
    follow_up_questions: list[FollowUpQuestion] = Field(default_factory=list)
