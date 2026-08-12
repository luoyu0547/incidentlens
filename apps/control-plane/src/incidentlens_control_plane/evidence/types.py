"""Types for the append-only Evidence Store.

``EvidenceRef`` is the unified, immutable reference over *redacted* content.
Log-specific identity fields (``source_kind``, ``scope``, ``cursor``,
``severity``, ``event_time``, ``normal_signal``, ``correlation_key``) are only
populated for ``LOG_RECORD`` refs; every other kind is described by the generic
source identity (``project_id`` / ``target_id`` / ``service_name`` /
``source_ref``) plus ``agent_run_id`` and kind-typed ``metadata``.  Content
hashes are computed exclusively over the already-redacted, truncated content so
raw content never reaches the store, events or the API.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from incidentlens_control_plane.logs.types import (
    LogScope,
    LogSeverity,
    LogSourceKind,
    TruncationInfo,
)

_MAX_METADATA_KEY_LENGTH = 120
_MAX_METADATA_VALUE_LENGTH = 2_000


class EvidenceKind(StrEnum):
    LOG_RECORD = "log_record"
    COMMAND_OUTPUT = "command_output"
    FILE_SNAPSHOT = "file_snapshot"
    DIFF = "diff"
    VALIDATION_RESULT = "validation_result"
    CHILD_REPORT = "child_report"
    REGISTRY_DISCOVERY = "registry_discovery"
    APPROVAL_DECISION = "approval_decision"
    UNCERTAIN_STATE = "uncertain_state"


class EvidenceRef(BaseModel):
    """An immutable evidence reference over redacted, bounded content."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_ref_id: str = Field(min_length=1, max_length=120)
    incident_id: str = Field(min_length=1, max_length=120)
    evidence_kind: EvidenceKind
    agent_run_id: str | None = Field(default=None, min_length=1, max_length=120)
    project_id: str = Field(min_length=1, max_length=80)
    target_id: str = Field(min_length=1, max_length=80)
    service_name: str = Field(min_length=1, max_length=120)
    source_ref: str | None = Field(default=None, min_length=1, max_length=500)

    # Log-record identity: populated only for LOG_RECORD evidence.
    source_kind: LogSourceKind | None = None
    scope: LogScope | None = None
    cursor: str | None = Field(default=None, min_length=1, max_length=1000)
    severity: LogSeverity | None = None
    event_time: datetime | None = None
    # No length caps: these mirror the upstream ``LogRecord`` fields, which are
    # unbounded (``correlation_key`` can be derived from arbitrarily long field
    # values), so a stored log record must always be able to become evidence.
    normal_signal: str | None = None
    correlation_key: str | None = None

    # Redacted, truncated content and its provenance.
    content_redacted: str
    content_sha256: str = Field(min_length=64, max_length=64)
    redaction_summary: dict[str, int]
    truncation: TruncationInfo | None = None
    metadata: dict[str, str] = Field(default_factory=dict)

    created_at: datetime
    created_by: str = Field(min_length=1, max_length=80)

    @field_validator("metadata")
    @classmethod
    def _bound_metadata(cls, value: dict[str, str]) -> dict[str, str]:
        for key, val in value.items():
            if not (1 <= len(key) <= _MAX_METADATA_KEY_LENGTH):
                raise ValueError("metadata keys must be 1..120 characters")
            if len(val) > _MAX_METADATA_VALUE_LENGTH:
                raise ValueError("metadata values must be <= 2000 characters")
        return value
