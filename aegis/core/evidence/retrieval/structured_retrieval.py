"""Structured Evidence Retrieval — Section 12.4.

Supports exact matching by entity_id + period + assertion_type.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class EvidenceStore:
    """In-memory evidence store with structured query support.

    In production, this would be backed by PostgreSQL.
    """

    _packets: list[dict] = field(default_factory=list)
    _index_by_entity: dict[str, list[int]] = field(default_factory=dict)
    _index_by_type: dict[str, list[int]] = field(default_factory=dict)

    def add(self, packet: dict) -> None:
        """Add an evidence packet to the store."""
        idx = len(self._packets)
        self._packets.append(packet)

        entity_id = packet.get("entity_id", "")
        self._index_by_entity.setdefault(entity_id, []).append(idx)

        assertion_type = packet.get("assertion_type", "")
        self._index_by_type.setdefault(assertion_type, []).append(idx)

    def query(
        self,
        *,
        entity_id: str | None = None,
        period: str | None = None,
        assertion_type: str | None = None,
        stance: str | None = None,
        source_tier_max: int | None = None,
    ) -> list[dict]:
        """Query evidence packets with structured filters."""
        results = list(self._packets)

        if entity_id:
            indices = set(self._index_by_entity.get(entity_id, []))
            results = [self._packets[i] for i in indices]

        if period:
            results = [p for p in results if p.get("period_relevance") == period]

        if assertion_type:
            results = [p for p in results if p.get("assertion_type") == assertion_type]

        if stance:
            results = [p for p in results if p.get("stance") == stance]

        if source_tier_max is not None:
            results = [
                p for p in results
                if p.get("source_tier", 4) <= source_tier_max
            ]

        return results

    def count(self) -> int:
        return len(self._packets)

    def get_by_id(self, evidence_id: str) -> dict | None:
        for p in self._packets:
            if p.get("evidence_id") == evidence_id:
                return p
        return None
