"""Section 26 — Post-Mortem & Calibration Contracts."""

from datetime import date

from pydantic import Field

from .common import ConfidenceBucket, EdgeType, RunId, StrictModel, ThesisId


class PostMortem(StrictModel):
    """Post-mortem evaluation of a thesis after its horizon expires."""

    postmortem_id: str = Field(min_length=1)
    thesis_id: ThesisId
    thesis_version: int = Field(ge=1)
    original_thesis_date: date
    review_date: date
    price_at_thesis: float = Field(gt=0)
    price_at_review: float = Field(gt=0)
    total_return: float
    thesis_survived: bool
    variant_realized: bool
    edge_type: EdgeType
    edge_realized: bool
    kill_criteria_triggered: list[str] = Field(default_factory=list)
    monitorables_triggered: list[str] = Field(default_factory=list)
    bias_warnings_at_publish: list[str] = Field(default_factory=list)
    what_was_right: list[str] = Field(min_length=1)
    what_was_wrong: list[str] = Field(min_length=1)
    error_taxonomy_labels: list[str] = Field(default_factory=list)
    lessons_for_system: list[str] = Field(default_factory=list)
    original_confidence_bucket: ConfidenceBucket
    original_run_id: RunId
