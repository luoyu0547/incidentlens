"""Synchronized asciinema, structured trace and plain-text recording."""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path

from incidentlens_control_plane.events.types import RuntimeEvent
from incidentlens_control_plane.logs.redaction import redact_message

_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_IP_RE = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
_PRIVATE_PATH_RE = re.compile(r"/(?:Users|home)/[^\s\"']+")


class SessionRecorder:
    """Write every input/event to all formats immediately and flush each record."""

    def __init__(self, cast_path: Path) -> None:
        self.cast_path = cast_path
        stem = cast_path.with_suffix("")
        self.trace_path = stem.with_suffix(".trace.jsonl")
        self.text_path = stem.with_suffix(".txt")
        for path in (self.cast_path, self.trace_path, self.text_path):
            path.parent.mkdir(parents=True, exist_ok=True)
        self._cast = self.cast_path.open("w", encoding="utf-8")
        self._trace = self.trace_path.open("w", encoding="utf-8")
        self._text = self.text_path.open("w", encoding="utf-8")
        self._started = time.monotonic()
        self._sequence = 0
        self._write_cast(
            {
                "version": 2,
                "width": 120,
                "height": 40,
                "timestamp": int(datetime.now(UTC).timestamp()),
                "env": {"TERM": "xterm-256color"},
            }
        )

    def record_event(self, event: RuntimeEvent) -> None:
        payload = self._redact(event.payload)
        record = {
            "sequence": event.sequence,
            "occurred_at": event.occurred_at.isoformat(),
            "event_type": event.event_type.value,
            "payload": payload,
        }
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        self._trace.write(line + "\n")
        self._trace.flush()
        rendered = self._clean(f"{event.sequence} {event.event_type.value} {payload}")
        self._write_output(rendered + "\n")

    def record_input(self, value: str) -> None:
        self._sequence += 1
        clean = self._clean(value)
        record = {
            "sequence": f"input-{self._sequence}",
            "occurred_at": datetime.now(UTC).isoformat(),
            "event_type": "session.input",
            "payload": {"command": clean},
        }
        self._trace.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._trace.flush()
        self._write_output(f"> {clean}\n")

    def interrupted(self) -> None:
        self._sequence += 1
        record = {
            "sequence": f"session-{self._sequence}",
            "occurred_at": datetime.now(UTC).isoformat(),
            "event_type": "session.interrupted",
            "payload": {},
        }
        self._trace.write(json.dumps(record) + "\n")
        self._trace.flush()
        self._write_output("session interrupted\n")

    def close(self) -> None:
        for stream in (self._cast, self._trace, self._text):
            stream.flush()
            stream.close()

    def _write_output(self, text: str) -> None:
        clean = self._clean(text)
        self._write_cast([round(time.monotonic() - self._started, 6), "o", clean])
        self._text.write(_ANSI_RE.sub("", clean))
        self._text.flush()

    def _write_cast(self, value: object) -> None:
        self._cast.write(json.dumps(value, ensure_ascii=False) + "\n")
        self._cast.flush()

    @classmethod
    def _clean(cls, value: str) -> str:
        value = _ANSI_RE.sub("", value)
        value = _IP_RE.sub("[REDACTED_IP]", value)
        value = _PRIVATE_PATH_RE.sub("[REDACTED_PATH]", value)
        return redact_message(value, max_length=20_000).message_redacted

    @classmethod
    def _redact(cls, value):
        if isinstance(value, dict):
            return {key: cls._redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [cls._redact(item) for item in value]
        if isinstance(value, str):
            return cls._clean(value)
        return value


__all__ = ["SessionRecorder"]
