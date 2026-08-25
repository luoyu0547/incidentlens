from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class UnregisteredLogContainer(Exception):
    """Raised when a docker/container-scope query names an unregistered container."""


class InvalidSubscription(Exception):
    """Raised when a log subscription cannot be created for the requested source."""


class InvalidSubscriptionTransition(Exception):
    """Raised when a log subscription state transition is not permitted."""


class TargetNotFound(Exception):
    """Raised when a query names a target that is not registered for the project."""


class ServiceNotFound(Exception):
    """Raised when a query names a service that is not registered for the project."""


class LogSeverity(StrEnum):
    TRACE = "trace"
    DEBUG = "debug"
    INFO = "info"
    NOTICE = "notice"
    WARN = "warn"
    ERROR = "error"
    CRITICAL = "critical"
    UNKNOWN = "unknown"


class LogSourceKind(StrEnum):
    FILE = "file"
    DOCKER = "docker"


class LogScope(StrEnum):
    HOST = "host"
    CONTAINER = "container"


class RawLogLine(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source_ref: str = Field(min_length=1, max_length=500)
    cursor: str = Field(min_length=1, max_length=1000)
    observed_at: datetime
    text: str


class ParsedLogLine(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    event_time: datetime | None
    severity: LogSeverity
    fields: dict[str, object]
    message: str
    # True when the line parsed as JSON but had no ``message``/``msg`` field,
    # so ``message`` is the raw JSON text.  Persisting that text must go
    # through explicit redaction (callers redact the raw text rather than
    # treating ``message`` as a safe message).
    message_is_raw: bool = False


class TruncationInfo(BaseModel):
    """Record of a truncation applied after redaction."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    original_length: int = Field(ge=0)
    kept_length: int = Field(ge=0)
    truncated: bool


class RedactionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    message_redacted: str
    summary: dict[str, int]
    truncated: bool = False
    truncation: TruncationInfo | None = None


class ProcessedLogLine(BaseModel):
    """A parsed and redacted pipeline result for a single raw log line."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    raw: RawLogLine
    parsed: ParsedLogLine
    message_redacted: str
    redaction_summary: dict[str, int]
    normal_signal: str | None = None
    correlation_key: str | None = None


class LogQueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    project_id: str = Field(min_length=1, max_length=80)
    target_id: str = Field(min_length=1, max_length=80)
    service_name: str = Field(min_length=1, max_length=120)
    source_kind: LogSourceKind
    scope: LogScope
    source_ref: str = Field(min_length=1, max_length=500)
    tail_lines: int = Field(default=100, ge=1, le=1000)
    persist: bool = False
    create_evidence: bool = False
    incident_id: str | None = Field(default=None, max_length=120)


class LogRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    log_id: str = Field(min_length=1, max_length=120)
    subscription_id: str | None = Field(default=None, max_length=120)
    project_id: str = Field(min_length=1, max_length=80)
    target_id: str = Field(min_length=1, max_length=80)
    service_name: str = Field(min_length=1, max_length=120)
    source_kind: LogSourceKind
    scope: LogScope
    source_ref: str = Field(min_length=1, max_length=500)
    cursor: str = Field(min_length=1, max_length=1000)
    dedupe_key: str = Field(min_length=1, max_length=128)
    observed_at: datetime
    event_time: datetime | None
    severity: LogSeverity
    message_redacted: str = Field(max_length=16 * 1024)
    redaction_summary: dict[str, int]
    normal_signal: str | None = None
    correlation_key: str | None = None
    evidence_ref_id: str | None = None
    stream_sequence: int = 0
    created_at: datetime


class LogSubscriptionStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    ERROR = "error"
    DELETED = "deleted"


class LogSubscription(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    subscription_id: str = Field(min_length=1, max_length=120)
    project_id: str = Field(min_length=1, max_length=80)
    target_id: str = Field(min_length=1, max_length=80)
    service_name: str = Field(min_length=1, max_length=120)
    source_kind: LogSourceKind
    scope: LogScope
    source_ref: str = Field(min_length=1, max_length=500)
    opt_in_streaming: bool
    status: LogSubscriptionStatus
    created_by: str = Field(min_length=1, max_length=80)
    last_error: str | None = None
    last_error_redacted: str | None = None
    created_at: datetime
    updated_at: datetime


class LogCursor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    subscription_id: str = Field(min_length=1, max_length=120)
    cursor: str = Field(min_length=1, max_length=1000)
    generation: str | None = None
    observed_at: datetime | None = None
    updated_at: datetime
