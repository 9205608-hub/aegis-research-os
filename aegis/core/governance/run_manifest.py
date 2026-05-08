"""Run Manifest management — the core of the Governance / Reproducibility Layer.

Every research run generates exactly one RunManifest.
Outputs without a RunManifest are unpublishable experimental outputs.
"""

from datetime import datetime, timezone
from uuid import uuid4

from aegis.data_contracts import MarketId, ResearchMode, RunManifest

from .artifact_hashing import compute_artifact_hash


def generate_run_id() -> str:
    """Generate a unique run ID with timestamp prefix for sortability."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    short_uuid = uuid4().hex[:8]
    return f"run_{ts}_{short_uuid}"


def create_run_manifest(
    *,
    entity_ids: list[str],
    question_id: str,
    run_mode: ResearchMode,
    market_adapter_id: MarketId,
    model_profile_id: str = "default_v1",
    prompt_bundle_version: str = "v1.0",
    parser_version: str = "xbrl_parser_v1",
    formula_registry_version: str = "formula_v1",
    metric_registry_version: str = "metric_v1",
    evidence_extractor_version: str = "evidence_v1",
    critic_policy_version: str = "critic_v1",
    scenario_model_version: str = "scenario_engine_v1",
    sector_pack_versions: dict[str, str] | None = None,
    data_source_versions: dict[str, str] | None = None,
    parent_run_id: str | None = None,
    thesis_version: int | None = None,
) -> RunManifest:
    """Create a new RunManifest with all required metadata.

    This freezes the entire research context for reproducibility.
    """
    now = datetime.now(timezone.utc)
    run_id = generate_run_id()

    manifest_data = {
        "run_id": run_id,
        "run_mode": run_mode.value,
        "entity_ids": entity_ids,
        "question_id": question_id,
        "research_timestamp": now.isoformat(),
        "price_timestamp": now.isoformat(),
        "filing_cutoff_timestamp": now.isoformat(),
        "consensus_snapshot_timestamp": now.isoformat(),
        "macro_snapshot_timestamp": now.isoformat(),
        "model_profile_id": model_profile_id,
        "prompt_bundle_version": prompt_bundle_version,
        "parser_version": parser_version,
        "formula_registry_version": formula_registry_version,
        "metric_registry_version": metric_registry_version,
        "evidence_extractor_version": evidence_extractor_version,
        "critic_policy_version": critic_policy_version,
        "sector_pack_versions": sector_pack_versions or {},
        "scenario_model_version": scenario_model_version,
        "market_adapter_id": market_adapter_id.value,
        "data_source_versions": data_source_versions or {},
        "parent_run_id": parent_run_id,
        "thesis_version": thesis_version,
    }

    artifact_hash = compute_artifact_hash(manifest_data)

    return RunManifest(
        run_id=run_id,
        run_mode=run_mode,
        entity_ids=entity_ids,
        question_id=question_id,
        research_timestamp=now,
        price_timestamp=now,
        filing_cutoff_timestamp=now,
        consensus_snapshot_timestamp=now,
        macro_snapshot_timestamp=now,
        model_profile_id=model_profile_id,
        prompt_bundle_version=prompt_bundle_version,
        parser_version=parser_version,
        formula_registry_version=formula_registry_version,
        metric_registry_version=metric_registry_version,
        evidence_extractor_version=evidence_extractor_version,
        critic_policy_version=critic_policy_version,
        sector_pack_versions=sector_pack_versions or {},
        scenario_model_version=scenario_model_version,
        market_adapter_id=market_adapter_id,
        data_source_versions=data_source_versions or {},
        artifact_hash=artifact_hash,
        parent_run_id=parent_run_id,
        thesis_version=thesis_version,
    )
