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
