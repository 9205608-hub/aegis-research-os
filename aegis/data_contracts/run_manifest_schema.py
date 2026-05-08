"""Section 4 — Governance / Reproducibility Layer: Run Manifest."""

from datetime import datetime

from pydantic import Field

from .common import (
    EntityId,
    MarketId,
    ResearchMode,
    RunId,
    Sha256Hash,
    StrictModel,
)


class RunManifest(StrictModel):
    """Immutable record of all metadata for a single research run.

    Every output must trace back to exactly one RunManifest.
    No RunManifest = not publishable.
    """

    run_id: RunId
    run_mode: ResearchMode
    entity_ids: list[EntityId] = Field(min_length=1)
    question_id: str = Field(min_length=1)
    research_timestamp: datetime
    price_timestamp: datetime
    filing_cutoff_timestamp: datetime
    consensus_snapshot_timestamp: datetime
    macro_snapshot_timestamp: datetime
    model_profile_id: str = Field(min_length=1)
    prompt_bundle_version: str = Field(min_length=1)
    parser_version: str = Field(min_length=1)
    formula_registry_version: str = Field(min_length=1)
    metric_registry_version: str = Field(min_length=1)
    evidence_extractor_version: str = Field(min_length=1)
    critic_policy_version: str = Field(min_length=1)
    sector_pack_versions: dict[str, str] = Field(default_factory=dict)
    scenario_model_version: str = Field(min_length=1)
    market_adapter_id: MarketId
    data_source_versions: dict[str, str] = Field(default_factory=dict)
    artifact_hash: Sha256Hash
    parent_run_id: RunId | None = None
    thesis_version: int | None = None
