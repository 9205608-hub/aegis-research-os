"""Ingestion Pipeline — Section 5.3.

External Source -> Source Connector -> Schema Validation -> Deduplication
-> Freshness Check -> Market Adapter -> PIT Assignment -> Quality Gate
-> Truth Layer Staging -> Truth Layer Commit.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from aegis.data_contracts.common import MarketId, SourceTier

from .models import DataQuery, RawDataPacket, SchemaValidationResult, SourceConnector
from .quality_gate import QualityGate, QualityGateResult


@dataclass
class IngestionRecord:
    """Record of a single data ingestion attempt with full audit trail."""

    ingestion_id: str
    batch_id: str
    source_id: str
    source_tier: SourceTier
    market_id: str
    entity_id: str
    query: DataQuery
    fetched_at: datetime
    content_hash: str
    pit_timestamp: datetime
    schema_validation: SchemaValidationResult
    quality_gate_result: QualityGateResult
    status: str  # "staged", "committed", "rejected"
    rejection_reason: str = ""
    staged_facts: list[dict[str, Any]] = field(default_factory=list)


class IngestionPipeline:
    """Orchestrates the full data ingestion flow.

    No data enters Truth Layer without going through this pipeline.
    Every step is recorded for auditability.
    """

    def __init__(
        self,
        quality_gate: QualityGate | None = None,
    ) -> None:
        self._quality_gate = quality_gate or QualityGate()
        self._connectors: dict[str, SourceConnector] = {}
        self._batch_counter = 0
        self._staged: list[IngestionRecord] = []
        self._committed: list[IngestionRecord] = []

    def register_connector(self, connector: SourceConnector) -> None:
        """Register a data source connector."""
        self._connectors[connector.source_id] = connector

    def ingest(
        self,
        *,
        source_id: str,
        query: DataQuery,
        currency: str,
        unit: str = "units",
        market_adapted: bool = False,
    ) -> IngestionRecord:
        """Run the full ingestion pipeline for a single query.

        Steps (Section 5.3):
        1. Source Connector fetch + rate limit
        2. Schema Validation
        3. Deduplication (handled by QualityGate)
        4. Freshness Check (logged but non-blocking in v1)
        5. Market Adapter (flagged via market_adapted param)
        6. PIT Timestamp Assignment
        7. Quality Gate
        8. Staging
        """
        if source_id not in self._connectors:
            raise KeyError(f"Connector '{source_id}' not registered. Register before ingestion.")

        connector = self._connectors[source_id]
        batch_id = self._next_batch_id(source_id)

        # Step 1: Fetch
        packet = connector.fetch(query)

        # Step 2: Schema validation
        schema_result = connector.validate_schema(packet)

        # Step 6: PIT timestamp assignment
        pit_timestamp = datetime.now(timezone.utc)

        # Steps 3-5, 7-8: Quality Gate (covers dedup, hash, market_adapted, etc.)
        gate_result = self._quality_gate.check(
            packet,
            pit_timestamp=pit_timestamp,
            currency=currency,
            unit=unit,
            market_adapted=market_adapted,
        )

        # Determine status
        if not schema_result.valid:
            status = "rejected"
            reason = f"Schema validation failed: {schema_result.errors}"
        elif not gate_result.passed:
            status = "rejected"
            failed = [f"{c.code.value}: {c.message}" for c in gate_result.failed_checks]
            reason = f"Quality gate failed: {failed}"
        else:
            status = "staged"
            reason = ""

        record = IngestionRecord(
            ingestion_id=f"ing_{batch_id}_{query.entity_id}",
            batch_id=batch_id,
            source_id=source_id,
            source_tier=connector.source_tier,
            market_id=connector.market_id,
            entity_id=query.entity_id,
            query=query,
            fetched_at=packet.fetched_at,
            content_hash=packet.content_hash,
            pit_timestamp=pit_timestamp,
            schema_validation=schema_result,
            quality_gate_result=gate_result,
            status=status,
            rejection_reason=reason,
        )

        if status == "staged":
            self._staged.append(record)
        return record

    def commit_staged(self) -> list[IngestionRecord]:
        """Commit all staged records to Truth Layer.

        In a real implementation, this writes to the database.
        Returns the list of committed records.
        """
        committed = []
        for record in self._staged:
            record.status = "committed"
            committed.append(record)
        self._committed.extend(committed)
        self._staged.clear()
        return committed

    def get_staged(self) -> list[IngestionRecord]:
        """Get all currently staged records."""
        return list(self._staged)

    def get_committed(self) -> list[IngestionRecord]:
        """Get all committed records."""
        return list(self._committed)

    def _next_batch_id(self, source_id: str) -> str:
        self._batch_counter += 1
        ts = datetime.now(timezone.utc).strftime("%Y%m%d")
        return f"batch_{ts}_{source_id}_{self._batch_counter:03d}"


def compute_content_hash(content: Any) -> str:
    """Compute SHA-256 hash of arbitrary content for deduplication."""
    if isinstance(content, (dict, list)):
        canonical = json.dumps(content, sort_keys=True, default=str, ensure_ascii=False)
    elif isinstance(content, bytes):
        canonical = content.decode("utf-8", errors="replace")
    else:
        canonical = str(content)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"
