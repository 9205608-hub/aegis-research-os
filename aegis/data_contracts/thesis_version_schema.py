"""Section 9.11 — Thesis Version Record."""

from datetime import date

from pydantic import Field

from .common import EventId, RunId, StrictModel, ThesisId


class ThesisFieldChange(StrictModel):
    """A single field change between thesis versions."""

    field: str = Field(min_length=1)
    old_value: str
    new_value: str
    reason: str = Field(min_length=1)


class ThesisVersionRecord(StrictModel):
    """Records the evolution from one thesis version to the next."""

    version_record_id: str = Field(min_length=1)
    thesis_id: ThesisId
    from_version: int = Field(ge=1)
    to_version: int = Field(ge=1)
    version_date: date
    trigger: str = Field(min_length=1)
    trigger_event_id: EventId | None = None
    change_type: str = Field(
        pattern=r"^(full_rerun|incremental_update|status_change_only)$"
    )
    changes: list[ThesisFieldChange] = Field(min_length=1)
    unchanged_core_thesis: bool
    new_run_id: RunId
    old_run_id: RunId
