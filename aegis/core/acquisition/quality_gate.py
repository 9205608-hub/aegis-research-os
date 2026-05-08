"""Ingestion Quality Gate — Section 5.4.

Every piece of data must pass all checks before entering Truth Layer.
This is a deterministic gate — no model inference allowed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum

from .models import RawDataPacket


class QualityCheckCode(str, Enum):
    """All quality gate checks from Section 5.4."""

    SCHEMA_VALID = "schema_valid"
    NO_DUPLICATE = "no_duplicate"
    SOURCE_IDENTIFIED = "source_identified"
    TIMESTAMP_VALID = "timestamp_valid"
    CURRENCY_IDENTIFIED = "currency_identified"
    UNIT_IDENTIFIED = "unit_identified"
    PIT_ASSIGNED = "pit_assigned"
    HASH_COMPUTED = "hash_computed"
    MARKET_ADAPTED = "market_adapted"


@dataclass(frozen=True)
class QualityCheckResult:
    """Result of a single quality check."""

    code: QualityCheckCode
    passed: bool
    message: str = ""


@dataclass
class QualityGateResult:
    """Aggregate result of all quality gate checks."""

    checks: list[QualityCheckResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def failed_checks(self) -> list[QualityCheckResult]:
        return [c for c in self.checks if not c.passed]

    def add(self, check: QualityCheckResult) -> None:
        self.checks.append(check)


class QualityGate:
    """Deterministic quality gate for data ingestion.

    Data that fails any check is rejected from entering Truth Layer.
    """

    def __init__(self, known_hashes: set[str] | None = None) -> None:
        self._known_hashes: set[str] = known_hashes or set()

    def check(
        self,
        packet: RawDataPacket,
        *,
        pit_timestamp: datetime | None = None,
        currency: str | None = None,
        unit: str | None = None,
        market_adapted: bool = False,
    ) -> QualityGateResult:
        """Run all quality gate checks on a raw data packet."""
        result = QualityGateResult()

        # 1. schema_valid — basic structural check
        result.add(self._check_schema(packet))

        # 2. no_duplicate
        result.add(self._check_no_duplicate(packet))

        # 3. source_identified
        result.add(self._check_source_identified(packet))

        # 4. timestamp_valid
        result.add(self._check_timestamp_valid(packet))

        # 5. currency_identified
        result.add(self._check_currency_identified(currency))

        # 6. unit_identified
        result.add(self._check_unit_identified(unit))

        # 7. pit_assigned
        result.add(self._check_pit_assigned(pit_timestamp))

        # 8. hash_computed
        result.add(self._check_hash_computed(packet))

        # 9. market_adapted
        result.add(self._check_market_adapted(market_adapted))

        # If passed, register hash to prevent future duplicates
        if result.passed:
            self._known_hashes.add(packet.content_hash)

        return result

    def _check_schema(self, packet: RawDataPacket) -> QualityCheckResult:
        if packet.raw_content is None:
            return QualityCheckResult(
                code=QualityCheckCode.SCHEMA_VALID,
                passed=False,
                message="Raw content is None",
            )
        return QualityCheckResult(code=QualityCheckCode.SCHEMA_VALID, passed=True)

    def _check_no_duplicate(self, packet: RawDataPacket) -> QualityCheckResult:
        if packet.content_hash in self._known_hashes:
            return QualityCheckResult(
                code=QualityCheckCode.NO_DUPLICATE,
                passed=False,
                message=f"Duplicate content hash: {packet.content_hash}",
            )
        return QualityCheckResult(code=QualityCheckCode.NO_DUPLICATE, passed=True)

    def _check_source_identified(self, packet: RawDataPacket) -> QualityCheckResult:
        if not packet.source_id:
            return QualityCheckResult(
                code=QualityCheckCode.SOURCE_IDENTIFIED,
                passed=False,
                message="source_id is empty",
            )
        return QualityCheckResult(code=QualityCheckCode.SOURCE_IDENTIFIED, passed=True)

    def _check_timestamp_valid(self, packet: RawDataPacket) -> QualityCheckResult:
        now = datetime.now(timezone.utc)
        # Reject timestamps in the future or more than 10 years old
        if packet.fetched_at > now + timedelta(hours=1):
            return QualityCheckResult(
                code=QualityCheckCode.TIMESTAMP_VALID,
                passed=False,
                message=f"Fetch timestamp is in the future: {packet.fetched_at}",
            )
        if packet.fetched_at < now - timedelta(days=3650):
            return QualityCheckResult(
                code=QualityCheckCode.TIMESTAMP_VALID,
                passed=False,
                message=f"Fetch timestamp is more than 10 years old: {packet.fetched_at}",
            )
        return QualityCheckResult(code=QualityCheckCode.TIMESTAMP_VALID, passed=True)

    def _check_currency_identified(self, currency: str | None) -> QualityCheckResult:
        if not currency or len(currency) != 3:
            return QualityCheckResult(
                code=QualityCheckCode.CURRENCY_IDENTIFIED,
                passed=False,
                message=f"Currency not identified or invalid: '{currency}'",
            )
        return QualityCheckResult(code=QualityCheckCode.CURRENCY_IDENTIFIED, passed=True)

    def _check_unit_identified(self, unit: str | None) -> QualityCheckResult:
        if not unit:
            return QualityCheckResult(
                code=QualityCheckCode.UNIT_IDENTIFIED,
                passed=False,
                message="Unit not identified",
            )
        return QualityCheckResult(code=QualityCheckCode.UNIT_IDENTIFIED, passed=True)

    def _check_pit_assigned(self, pit_timestamp: datetime | None) -> QualityCheckResult:
        if pit_timestamp is None:
            return QualityCheckResult(
                code=QualityCheckCode.PIT_ASSIGNED,
                passed=False,
                message="Point-in-time timestamp not assigned",
            )
        return QualityCheckResult(code=QualityCheckCode.PIT_ASSIGNED, passed=True)

    def _check_hash_computed(self, packet: RawDataPacket) -> QualityCheckResult:
        if not packet.content_hash:
            return QualityCheckResult(
                code=QualityCheckCode.HASH_COMPUTED,
                passed=False,
                message="Content hash not computed",
            )
        return QualityCheckResult(code=QualityCheckCode.HASH_COMPUTED, passed=True)

    def _check_market_adapted(self, adapted: bool) -> QualityCheckResult:
        if not adapted:
            return QualityCheckResult(
                code=QualityCheckCode.MARKET_ADAPTED,
                passed=False,
                message="Data has not been processed by market adapter",
            )
        return QualityCheckResult(code=QualityCheckCode.MARKET_ADAPTED, passed=True)
