"""Types for the append-only Evidence Store."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from incidentlens_control_plane.logs.types import (
    LogScope,
    LogSeverity,
    LogSourceKind,
)


class EvidenceKind(StrEnum):
    LOG_RECORD = "log_record"


class EvidenceRef(BaseModel):
    """An immutable evidence reference over redacted log content."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    evidence_ref_id: str
    incident_id: str
    evidence_kind: EvidenceKind
    project_id: str
    target_id: str
    service_name: str
    source_kind: LogSourceKind
    scope: LogScope
    source_ref: str
    cursor: str
    content_redacted: str
    content_sha256: str
    redaction_summary: dict[str, int]
    severity: LogSeverity
    event_time: datetime | None
    normal_signal: str | None
    correlation_key: str | None
    created_at: datetime
    created_by: str
