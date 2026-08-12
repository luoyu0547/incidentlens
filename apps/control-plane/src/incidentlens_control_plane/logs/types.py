from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


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


class RedactionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    message_redacted: str
    summary: dict[str, int]
    truncated: bool = False


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
    created_at: datetime
    updated_at: datetime


class LogCursor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    subscription_id: str = Field(min_length=1, max_length=120)
    cursor: str = Field(min_length=1, max_length=1000)
    generation: str | None = None
    observed_at: datetime | None = None
    updated_at: datetime
