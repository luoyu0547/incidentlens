"""Deterministic detection of normal (non-incident) signals in log lines."""

from __future__ import annotations

import re

from incidentlens_control_plane.logs.types import ParsedLogLine

_HEARTBEAT_RE = re.compile(r"\b(?:heartbeat|keepalive|tick)\b")
_HEALTHCHECK_RE = re.compile(
    r"\b(?:get|head)\s+/health\b[^\n]*\b(?:200|204|304)\b"
)
_REQUEST_OK_RE = re.compile(
    r"\b(?:get|post|put|patch|delete|head|options)\s+/\S+\s+[123]\d\d\b"
)
_STARTUP_RE = re.compile(r"\b(?:started|startup|listening|ready)\b")
_SHUTDOWN_RE = re.compile(r"\bshutdown\b")
_RETRY_RE = re.compile(r"\b(?:retry\w*|reconnect\w*|backoff\w*)\b")


def detect_normal_signal(parsed: ParsedLogLine) -> str | None:
    """Classify a parsed log line as a known-normal signal, or None.

    Order encodes precedence: ``healthcheck_ok`` must win over ``request_ok``
    for ``/health`` lines that are also HTTP 2xx/3xx responses.
    """
    message = parsed.message.lower()
    if _HEARTBEAT_RE.search(message):
        return "heartbeat"
    if _HEALTHCHECK_RE.search(message):
        return "healthcheck_ok"
    if _REQUEST_OK_RE.search(message):
        return "request_ok"
    if _STARTUP_RE.search(message):
        return "startup"
    if _SHUTDOWN_RE.search(message):
        return "shutdown"
    if _RETRY_RE.search(message):
        return "retry"
    return None
