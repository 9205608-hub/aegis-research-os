"""Thesis Version Manager — Section 17.

Lifecycle: DRAFT → UNDER_REVIEW → PUBLISHED → ACTIVE →
           UPDATED (v2, v3...) → EXPIRED / KILLED / CONFIRMED

Principles:
1. Every thesis change must have a version record.
2. Thesis diff must be auto-generated.
3. Full rerun and incremental update both require new run_manifest.
4. Expired/killed thesis are never deleted — only status-changed.
5. Version chain must be complete for any-time-point replay.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from aegis.data_contracts.thesis_version_schema import (
    ThesisFieldChange,
    ThesisVersionRecord,
)


@dataclass
class ThesisSnapshot:
    """In-memory representation of a thesis at a specific version."""

    thesis_id: str
    version: int
    run_id: str
    status: str  # publishing_status
    created_date: date
    data: dict[str, Any] = field(default_factory=dict)


class ThesisVersionManager:
    """Manages thesis lifecycle and version evolution.

    Section 17.2: supports full_rerun, incremental_update, status_change_only.
    """

    def __init__(self) -> None:
        self._theses: dict[str, list[ThesisSnapshot]] = {}  # thesis_id -> version chain
        self._version_records: list[ThesisVersionRecord] = []

    def create_thesis(
        self,
        thesis_id: str,
        run_id: str,
        data: dict[str, Any],
        created_date: date | None = None,
    ) -> ThesisSnapshot:
        """Create a new thesis (version 1, status DRAFT)."""
        snapshot = ThesisSnapshot(
            thesis_id=thesis_id,
            version=1,
            run_id=run_id,
            status="draft",
            created_date=created_date or date.today(),
            data=data,
        )
        self._theses[thesis_id] = [snapshot]
        return snapshot

    def get_current(self, thesis_id: str) -> ThesisSnapshot | None:
        """Get the latest version of a thesis."""
        chain = self._theses.get(thesis_id, [])
        return chain[-1] if chain else None

    def get_version(self, thesis_id: str, version: int) -> ThesisSnapshot | None:
        """Get a specific version of a thesis."""
        chain = self._theses.get(thesis_id, [])
        for snap in chain:
            if snap.version == version:
                return snap
        return None

    def get_version_chain(self, thesis_id: str) -> list[ThesisSnapshot]:
        """Get the full version chain for a thesis."""
        return list(self._theses.get(thesis_id, []))

    def update_status(
        self,
        thesis_id: str,
        new_status: str,
        run_id: str,
        reason: str,
    ) -> ThesisVersionRecord | None:
        """Status-only change (patch version). Section 17.2."""
        current = self.get_current(thesis_id)
        if not current:
            return None

        old_status = current.status
        new_version = current.version  # Patch — same major version

        # Create new snapshot with updated status
        new_snap = ThesisSnapshot(
            thesis_id=thesis_id,
            version=new_version,
            run_id=run_id,
            status=new_status,
            created_date=date.today(),
            data={**current.data, "publishing_status": new_status},
        )
        self._theses[thesis_id].append(new_snap)

        record = ThesisVersionRecord(
            version_record_id=f"vr_{thesis_id}_{current.version}_{new_version}",
            thesis_id=thesis_id,
            from_version=current.version,
            to_version=new_version,
            version_date=date.today(),
            trigger=reason,
            change_type="status_change_only",
            changes=[ThesisFieldChange(
                field="publishing_status",
                old_value=old_status,
                new_value=new_status,
                reason=reason,
            )],
            unchanged_core_thesis=True,
            new_run_id=run_id,
            old_run_id=current.run_id,
        )
        self._version_records.append(record)
        return record

    def full_rerun(
        self,
        thesis_id: str,
        new_run_id: str,
        new_data: dict[str, Any],
        trigger: str,
    ) -> ThesisVersionRecord | None:
        """Full rerun — major version bump. Section 17.2."""
        current = self.get_current(thesis_id)
        if not current:
            return None

        new_version = current.version + 1
        changes = self._compute_diff(current.data, new_data)

        new_snap = ThesisSnapshot(
            thesis_id=thesis_id,
            version=new_version,
            run_id=new_run_id,
            status="under_review",
            created_date=date.today(),
            data=new_data,
        )
        self._theses[thesis_id].append(new_snap)

        unchanged_core = not any(c.field == "core_thesis" for c in changes)

        record = ThesisVersionRecord(
            version_record_id=f"vr_{thesis_id}_{current.version}_{new_version}",
            thesis_id=thesis_id,
            from_version=current.version,
            to_version=new_version,
            version_date=date.today(),
            trigger=trigger,
            change_type="full_rerun",
            changes=changes if changes else [ThesisFieldChange(
                field="full_rerun",
                old_value="v" + str(current.version),
                new_value="v" + str(new_version),
                reason=trigger,
            )],
            unchanged_core_thesis=unchanged_core,
            new_run_id=new_run_id,
            old_run_id=current.run_id,
        )
        self._version_records.append(record)
        return record

    def incremental_update(
        self,
        thesis_id: str,
        new_run_id: str,
        updated_fields: dict[str, Any],
        trigger: str,
    ) -> ThesisVersionRecord | None:
        """Incremental update — minor version bump. Section 17.2."""
        current = self.get_current(thesis_id)
        if not current:
            return None

        new_version = current.version + 1
        new_data = {**current.data, **updated_fields}
        changes = self._compute_diff(current.data, new_data)

        new_snap = ThesisSnapshot(
            thesis_id=thesis_id,
            version=new_version,
            run_id=new_run_id,
            status=current.status,
            created_date=date.today(),
            data=new_data,
        )
        self._theses[thesis_id].append(new_snap)

        unchanged_core = not any(c.field == "core_thesis" for c in changes)

        record = ThesisVersionRecord(
            version_record_id=f"vr_{thesis_id}_{current.version}_{new_version}",
            thesis_id=thesis_id,
            from_version=current.version,
            to_version=new_version,
            version_date=date.today(),
            trigger=trigger,
            change_type="incremental_update",
            changes=changes if changes else [ThesisFieldChange(
                field="incremental_update",
                old_value="v" + str(current.version),
                new_value="v" + str(new_version),
                reason=trigger,
            )],
            unchanged_core_thesis=unchanged_core,
            new_run_id=new_run_id,
            old_run_id=current.run_id,
        )
        self._version_records.append(record)
        return record

    def get_version_records(self, thesis_id: str) -> list[ThesisVersionRecord]:
        """Get all version records for a thesis."""
        return [r for r in self._version_records if r.thesis_id == thesis_id]

    def _compute_diff(
        self, old_data: dict, new_data: dict
    ) -> list[ThesisFieldChange]:
        """Auto-generate structured diff between two thesis versions."""
        changes: list[ThesisFieldChange] = []
        all_keys = set(old_data) | set(new_data)
        for key in sorted(all_keys):
            old_val = old_data.get(key)
            new_val = new_data.get(key)
            if old_val != new_val:
                changes.append(ThesisFieldChange(
                    field=key,
                    old_value=str(old_val) if old_val is not None else "",
                    new_value=str(new_val) if new_val is not None else "",
                    reason="auto-detected change",
                ))
        return changes
