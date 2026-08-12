"""Pure parsing of raw log text into structured log lines."""

from __future__ import annotations

import json
import re
from datetime import datetime

from incidentlens_control_plane.logs.types import LogSeverity, ParsedLogLine

_JSON_SEVERITY_KEYS = ("severity", "level", "log.level", "lvl")

_SEVERITY_ALIASES = {
    "trace": LogSeverity.TRACE,
    "debug": LogSeverity.DEBUG,
    "info": LogSeverity.INFO,
    "notice": LogSeverity.NOTICE,
    "warn": LogSeverity.WARN,
    "warning": LogSeverity.WARN,
    "error": LogSeverity.ERROR,
    "err": LogSeverity.ERROR,
    "critical": LogSeverity.CRITICAL,
    "crit": LogSeverity.CRITICAL,
    "fatal": LogSeverity.CRITICAL,
}

# Longest tokens first so longer alternatives win at a given position
# (e.g. "error" before "err", "critical" before "crit").
_TEXT_SEVERITY_RE = re.compile(
    r"\b(error|critical|fatal|warning|warn|notice|info|debug|trace|crit|err)\b"
)

_TS_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2}(?:\.\d+)?)(Z|[+-]\d{2}:?\d{2})?"
)


def _parse_time(text: str) -> datetime | None:
    match = _TS_RE.search(text)
    if match is None:
        return None
    date_part, time_part, tz_part = match.groups()
    iso = f"{date_part}T{time_part}"
    if tz_part:
        iso += tz_part
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None


def _severity_from_token(token: str) -> LogSeverity:
    return _SEVERITY_ALIASES.get(token.strip().lower(), LogSeverity.UNKNOWN)


def _severity_from_text(text: str) -> LogSeverity:
    found = LogSeverity.UNKNOWN
    for match in _TEXT_SEVERITY_RE.finditer(text.lower()):
        found = _SEVERITY_ALIASES.get(match.group(1), LogSeverity.UNKNOWN)
    return found


def parse_log_line(text: str) -> ParsedLogLine:
    fields: dict[str, object] = {}
    message = text
    event_time: datetime | None = None
    severity = LogSeverity.UNKNOWN
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = None

    if isinstance(value, dict):
        fields = dict(value)
        message_value = value.get("message") or value.get("msg") or text
        message = str(message_value)
        for key in ("timestamp", "time", "@timestamp", "ts"):
            if key in value:
                event_time = _parse_time(str(value[key]))
                break
        for key in _JSON_SEVERITY_KEYS:
            if key in value:
                severity = _severity_from_token(str(value[key]))
                break

    if severity is LogSeverity.UNKNOWN:
        severity = _severity_from_text(text)
    if event_time is None:
        event_time = _parse_time(text)
    return ParsedLogLine(
        event_time=event_time,
        severity=severity,
        fields=fields,
        message=message,
    )
