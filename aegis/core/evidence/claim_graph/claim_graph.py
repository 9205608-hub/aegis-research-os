"""Claim Graph — links claims to evidence for traceability.

Section 12.5: Core conclusions without evidence_id cannot enter thesis.
Every claim must be backed by one or more evidence packets.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4

from aegis.data_contracts.common import SourceTier


@dataclass(frozen=True)
class Claim:
    """A research claim that must be backed by evidence."""

    claim_id: str
    entity_id: str
    claim_text: str
    claim_type: str  # "core_thesis", "supporting", "counter", "open_question"
    evidence_ids: list[str]
    judgment_id: str | None = None
    strength: str = "moderate"  # "weak", "moderate", "strong"
    min_required_tier: int = 2  # Minimum source tier for core claims


@dataclass
class ClaimGraph:
    """Manages the mapping between claims and evidence.

    Enforces:
    - Core claims must have evidence_ids (Section 12.5)
    - Tier 3/4 evidence cannot be sole support for core claims
    - All evidence links are traceable
    """

    _claims: dict[str, Claim] = field(default_factory=dict)
    _evidence_tiers: dict[str, int] = field(default_factory=dict)

    def register_evidence_tier(self, evidence_id: str, tier: int) -> None:
        """Record the source tier of an evidence packet."""
        self._evidence_tiers[evidence_id] = tier

    def add_claim(self, claim: Claim) -> list[str]:
        """Add a claim and validate its evidence backing.

        Returns list of validation warnings (empty = valid).
        """
        warnings = []

        # Core claims must have evidence
        if claim.claim_type == "core_thesis" and not claim.evidence_ids:
            warnings.append(
                f"BLOCK: Core claim '{claim.claim_id}' has no evidence_ids. "
                f"Cannot enter thesis without evidence binding."
            )

        # Check tier sufficiency for core claims
        if claim.claim_type in ("core_thesis", "supporting"):
            tier_ok = False
            for eid in claim.evidence_ids:
                tier = self._evidence_tiers.get(eid, 4)
                if tier <= claim.min_required_tier:
                    tier_ok = True
                    break
            if claim.evidence_ids and not tier_ok:
                warnings.append(
                    f"WARN: Claim '{claim.claim_id}' is only backed by "
                    f"Tier 3/4 evidence. Core claims require at least one "
                    f"Tier 1-2 evidence source."
                )

        self._claims[claim.claim_id] = claim
        return warnings

    def get_claim(self, claim_id: str) -> Claim | None:
        return self._claims.get(claim_id)

    def get_claims_for_entity(self, entity_id: str) -> list[Claim]:
        return [c for c in self._claims.values() if c.entity_id == entity_id]

    def get_evidence_for_claim(self, claim_id: str) -> list[str]:
        claim = self._claims.get(claim_id)
        return claim.evidence_ids if claim else []

    def get_claims_for_evidence(self, evidence_id: str) -> list[Claim]:
        return [
            c for c in self._claims.values()
            if evidence_id in c.evidence_ids
        ]

    def validate_all(self) -> list[str]:
        """Validate the entire claim graph. Returns list of issues."""
        issues = []
        for claim in self._claims.values():
            if claim.claim_type == "core_thesis" and not claim.evidence_ids:
                issues.append(
                    f"BLOCK: Core claim '{claim.claim_id}' has no evidence."
                )
            # Check for orphaned evidence references
            for eid in claim.evidence_ids:
                if eid not in self._evidence_tiers:
                    issues.append(
                        f"WARN: Evidence '{eid}' referenced by claim "
                        f"'{claim.claim_id}' has no registered tier."
                    )
        return issues

    @staticmethod
    def generate_claim_id(entity_id: str) -> str:
        return f"claim_{entity_id}_{uuid4().hex[:8]}"
