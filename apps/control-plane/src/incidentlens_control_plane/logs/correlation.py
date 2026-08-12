"""Extract a correlation key from a parsed log line.

Precedence: structured ``fields`` first, then text patterns, in the order
trace -> request -> span -> correlation.  Returned keys are namespaced so
callers can group lines across sources without ambiguity.
"""

from __future__ import annotations

import re

from incidentlens_control_plane.logs.types import ParsedLogLine

_FIELD_KEYS = (
    ("trace_id", "trace"),
    ("request_id", "request"),
    ("span_id", "span"),
    ("correlation_id", "correlation"),
)

_TEXT_PATTERNS = (
    (re.compile(r"\btrace[_-]?id\b\s*[=:]\s*([A-Za-z0-9._-]+)"), "trace"),
    (re.compile(r"\brequest[_-]?id\b\s*[=:]\s*([A-Za-z0-9._-]+)"), "request"),
    (re.compile(r"\bspan(?:[_-]?id)?\b\s*[=:]\s*([A-Za-z0-9._-]+)"), "span"),
    (re.compile(r"\bcorrelation[_-]?id\b\s*[=:]\s*([A-Za-z0-9._-]+)"), "correlation"),
)


def extract_correlation_key(parsed: ParsedLogLine) -> str | None:
    for key, prefix in _FIELD_KEYS:
        value = parsed.fields.get(key)
        if value is not None and value != "":
            return f"{prefix}:{value}"
    for pattern, prefix in _TEXT_PATTERNS:
        match = pattern.search(parsed.message)
        if match is not None:
            return f"{prefix}:{match.group(1)}"
    return None
