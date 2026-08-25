"""Product read models for log history.

A :class:`LogRecordView` exposes only the redacted public surface of a log
record -- never the raw source text or un-redacted structured fields.  The
product cursor is an opaque token on top of the durable ``stream_sequence``;
clients page forward with ``next_cursor`` and may pin an upper bound with
``snapshot_cursor`` so a long-running read stays consistent as new records
arrive.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from incidentlens_control_plane.api.models import JsonValue
from incidentlens_control_plane.logs.cursors import encode_log_cursor
from incidentlens_control_plane.logs.store import LogStore
from incidentlens_control_plane.logs.types import LogRecord, LogSeverity


class LogRecordView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    log_id: str
    cursor: str
    occurred_at: datetime
    severity: LogSeverity
    message: str
    fields: dict[str, Any] = Field(default_factory=dict)


class LogStreamEnvelope(BaseModel):
    """Versioned frame emitted by the cursor-based log WebSocket."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    event_type: str
    occurred_at: datetime
    cursor: str | None = None
    payload: dict[str, JsonValue] | None = None


class LogPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[LogRecordView, ...]
    next_cursor: str | None
    previous_cursor: str | None
    has_more: bool
    snapshot_cursor: str | None


def list_log_page(
    store: LogStore,
    *,
    service_name: str,
    before_sequence: int | None = None,
    after_sequence: int | None = None,
    limit: int = 200,
    severity: LogSeverity | None = None,
    source_ref: str | None = None,
    snapshot_sequence: int | None = None,
    allowed_target_ids: frozenset[str] | None = None,
) -> LogPage:
    """Return one bounded, service-scoped page of product log records.

    ``after_sequence`` pages forward; ``before_sequence`` is the bounded upper
    edge captured so newly inserted records do not shift the window.
    """
    if before_sequence is not None and after_sequence is not None:
        raise ValueError("before and after cursors are mutually exclusive")
    snapshot = snapshot_sequence
    if snapshot is None:
        snapshot = before_sequence or store.latest_product_sequence()
    records, has_more = store.list_product_page(
        service_name=service_name,
        before_sequence=before_sequence,
        after_sequence=after_sequence,
        snapshot_sequence=snapshot,
        limit=limit,
        severity=severity.value if severity is not None else None,
        source_ref=source_ref,
        allowed_target_ids=allowed_target_ids,
    )
    items = tuple(_to_view(record) for record in records)
    if not records:
        return LogPage(
            items=items,
            next_cursor=None,
            previous_cursor=(
                encode_log_cursor(before_sequence)
                if before_sequence is not None
                else None
            ),
            has_more=False,
            snapshot_cursor=encode_log_cursor(before_sequence or after_sequence or 0),
        )
    last_sequence = records[-1].stream_sequence
    return LogPage(
        items=items,
        next_cursor=encode_log_cursor(last_sequence) if has_more else None,
        previous_cursor=(
            encode_log_cursor(before_sequence) if before_sequence is not None else None
        ),
        has_more=has_more,
        snapshot_cursor=encode_log_cursor(last_sequence),
    )


def _to_view(record: LogRecord) -> LogRecordView:
    return LogRecordView(
        log_id=record.log_id,
        cursor=record.cursor,
        occurred_at=record.observed_at,
        severity=record.severity,
        message=record.message_redacted,
        fields={},
    )


__all__ = ["LogPage", "LogRecordView", "LogStreamEnvelope", "list_log_page"]
