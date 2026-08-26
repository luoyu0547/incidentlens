"""Workspace invalidation stream over durable runtime events.

``GET /events/v1/workspace`` is the browser-visible, read-only invalidation
channel for the future Web observer.  Each SSE frame carries a deliberately
minimal ``data:`` envelope (a :class:`WorkspaceResourceChanged` or a
:class:`WorkspaceStreamGap`) so the observer knows *which* workspace resource
changed and never sees the durable event payload itself.

Safety rules (mirrored from the CLI stream):

* every ``data:`` frame is rebuilt from a handful of safe payload fields; the
  original runtime payload is never forwarded;
* events for targets the principal is not allowed to address are filtered
  before serialization (at the store for replay and in this module for live);
* durable event types without a resource mapping are ignored; ignoring them
  never advances a cursor, so it cannot create a gap by itself;
* a requested cursor that is absent or outside retained history emits a
  ``stream.gap`` with ``action: reload_snapshot`` and ends the stream -- the
  client is never silently resumed at the latest position.

The replay-to-live handoff subscribes to the in-memory broker before capturing
the durable high-water sequence so no live event is lost between the durable
scan and the live transition.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from incidentlens_control_plane.events.broker import RuntimeEventBroker
from incidentlens_control_plane.events.store import RuntimeEventStore
from incidentlens_control_plane.events.types import RuntimeEvent, RuntimeEventType
from pydantic import BaseModel, ConfigDict

#: Durable page size used while replaying history.
REPLAY_PAGE_SIZE = 500

#: Seconds of idle time before the stream emits a ``: heartbeat <UTC>`` comment.
HEARTBEAT_INTERVAL_SECONDS = 15.0

#: SSE line for a heartbeat comment (stable ``event:``/``id:`` are never used).
_HEARTBEAT_PREFIX = "heartbeat "


class WorkspaceResourceKind(StrEnum):
    """The workspace resources the Web observer can invalidate."""

    OVERVIEW = "overview"
    TARGET = "target"
    SERVICE = "service"
    ISSUE = "issue"
    INVESTIGATION = "investigation"
    EVIDENCE = "evidence"


class WorkspaceResourceChanged(BaseModel):
    """Minimal invalidation envelope forwarded as SSE ``data:``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    event_id: str
    event_type: Literal["resource.changed"] = "resource.changed"
    occurred_at: datetime
    resource_kind: WorkspaceResourceKind
    resource_id: str | None = None
    target_id: str | None = None
    service_id: str | None = None


class WorkspaceStreamGap(BaseModel):
    """Gap envelope for a cursor that is absent or outside retained history."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    event_id: str
    event_type: Literal["stream.gap"] = "stream.gap"
    occurred_at: datetime
    reason: str
    action: Literal["reload_snapshot"] = "reload_snapshot"


#: Durable events that change the aggregate workspace overview.
_OVERVIEW_EVENTS = frozenset(
    {
        RuntimeEventType.PROJECT_CREATED,
        RuntimeEventType.PROJECT_UPDATED,
        RuntimeEventType.PROJECT_DELETED,
        RuntimeEventType.APPROVAL_REQUESTED,
        RuntimeEventType.APPROVAL_APPROVED,
        RuntimeEventType.APPROVAL_REJECTED,
        RuntimeEventType.APPROVAL_CONSUMED,
        RuntimeEventType.CHANGESET_CREATED,
        RuntimeEventType.CHANGESET_STATUS_CHANGED,
        RuntimeEventType.CHANGESET_ROLLED_BACK,
        RuntimeEventType.OPERATION_QUEUED,
        RuntimeEventType.OPERATION_RUNNING,
        RuntimeEventType.OPERATION_CANCEL_REQUESTED,
        RuntimeEventType.OPERATION_SUCCEEDED,
        RuntimeEventType.OPERATION_FAILED,
        RuntimeEventType.OPERATION_CANCELLED,
        RuntimeEventType.OPERATION_UNCERTAIN,
        RuntimeEventType.RECOVERY_STARTED,
        RuntimeEventType.RECOVERY_COMPLETED,
    }
)

#: Durable events that change an investigation's public result.
_INVESTIGATION_EVENTS = frozenset(
    {
        RuntimeEventType.INVESTIGATION_CREATED,
        RuntimeEventType.INVESTIGATION_STARTED,
        RuntimeEventType.INVESTIGATION_STATUS_CHANGED,
        RuntimeEventType.INVESTIGATION_COMPLETED,
        RuntimeEventType.INVESTIGATION_CANCELLED,
        RuntimeEventType.INVESTIGATION_FAILED,
        RuntimeEventType.CONCLUSION_CREATED,
        RuntimeEventType.REGISTRY_PROPOSAL_CREATED,
        RuntimeEventType.REGISTRY_PROPOSAL_DECIDED,
    }
)

#: Durable events that change a service's public state (health, log sources).
_SERVICE_EVENTS = frozenset(
    {
        RuntimeEventType.LOG_SUBSCRIPTION_STARTED,
        RuntimeEventType.LOG_SUBSCRIPTION_PAUSED,
        RuntimeEventType.LOG_SUBSCRIPTION_RESUMED,
        RuntimeEventType.LOG_SUBSCRIPTION_DELETED,
        RuntimeEventType.LOG_SUBSCRIPTION_ERROR,
        RuntimeEventType.LOG_SOURCE_ROTATED,
        RuntimeEventType.LOG_BACKPRESSURE,
        RuntimeEventType.DOCKER_ACTION_REQUESTED,
        RuntimeEventType.DOCKER_ACTION_STARTED,
        RuntimeEventType.DOCKER_ACTION_COMPLETED,
        RuntimeEventType.DOCKER_ACTION_FAILED,
    }
)

#: Durable events that change the evidence surface.
_EVIDENCE_EVENTS = frozenset(
    {
        RuntimeEventType.EVIDENCE_APPENDED,
    }
)


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def invalidation_for(event: RuntimeEvent) -> WorkspaceResourceChanged | None:
    """Map one durable event to a minimal resource invalidation (or ``None``).

    Only safe, non-sensitive payload fields are read; the original payload is
    never copied into the returned envelope.  Event types without a mapping
    return ``None`` and are ignored by the stream.
    """
    event_type = event.event_type
    payload = event.payload
    target_id = _string(payload.get("target_id"))
    if event_type in _OVERVIEW_EVENTS:
        return WorkspaceResourceChanged(
            event_id=event.event_id,
            occurred_at=event.occurred_at,
            resource_kind=WorkspaceResourceKind.OVERVIEW,
            target_id=target_id,
        )
    if event_type in _INVESTIGATION_EVENTS:
        return WorkspaceResourceChanged(
            event_id=event.event_id,
            occurred_at=event.occurred_at,
            resource_kind=WorkspaceResourceKind.INVESTIGATION,
            resource_id=_string(payload.get("investigation_id")),
            target_id=target_id,
        )
    if event_type in _SERVICE_EVENTS:
        service_id = _string(payload.get("service_name")) or _string(
            payload.get("service")
        )
        return WorkspaceResourceChanged(
            event_id=event.event_id,
            occurred_at=event.occurred_at,
            resource_kind=WorkspaceResourceKind.SERVICE,
            resource_id=service_id,
            target_id=target_id,
            service_id=service_id,
        )
    if event_type in _EVIDENCE_EVENTS:
        return WorkspaceResourceChanged(
            event_id=event.event_id,
            occurred_at=event.occurred_at,
            resource_kind=WorkspaceResourceKind.EVIDENCE,
            resource_id=_string(payload.get("evidence_ref_id"))
            or _string(payload.get("investigation_id")),
            target_id=target_id,
        )
    return None


def _sse(
    *,
    event: str | None = None,
    event_id: str | None = None,
    data: dict[str, Any] | None = None,
    comment: str | None = None,
) -> str:
    """Build one UTF-8 SSE frame block; compact JSON keeps ``data:`` one line."""
    lines: list[str] = []
    if comment is not None:
        lines.append(f": {comment}")
    if event is not None:
        lines.append(f"event: {event}")
    if event_id is not None:
        lines.append(f"id: {event_id}")
    if data is not None:
        encoded = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        lines.extend(f"data: {line}" for line in encoded.splitlines() or [""])
    return "\n".join(lines) + "\n\n"


def _heartbeat_comment() -> str:
    return f"{_HEARTBEAT_PREFIX}{datetime.now(UTC).isoformat()}"


class WorkspaceEventStream:
    """Replay a workspace's safe invalidations, then follow the broker live."""

    def __init__(
        self,
        *,
        events: RuntimeEventStore,
        broker: RuntimeEventBroker,
        heartbeat_seconds: float = HEARTBEAT_INTERVAL_SECONDS,
    ) -> None:
        self._events = events
        self._broker = broker
        self._heartbeat_seconds = heartbeat_seconds

    @staticmethod
    def _page_filters(
        target_id: str | None,
        allowed_target_ids: frozenset[str] | None,
    ) -> dict[str, object]:
        params: dict[str, object] = {}
        if target_id is not None:
            params["target_id"] = target_id
        if allowed_target_ids is not None:
            params["allowed_target_ids"] = allowed_target_ids
        return params

    @staticmethod
    def _allowed(
        event: RuntimeEvent,
        target_id: str | None,
        allowed_target_ids: frozenset[str] | None,
    ) -> bool:
        """Mirror the store's authorization filtering for live events."""
        item_target = _string(event.payload.get("target_id"))
        if target_id is not None and item_target != target_id:
            return False
        if allowed_target_ids is not None:
            if not allowed_target_ids:
                return False
            if item_target is not None and item_target not in allowed_target_ids:
                return False
        return True

    async def run(
        self,
        *,
        after_event_id: str | None,
        target_id: str | None,
        allowed_target_ids: frozenset[str] | None,
    ) -> AsyncIterator[str]:
        """Resolve the cursor, replay safe invalidations, then go live."""
        filters = self._page_filters(target_id, allowed_target_ids)
        async with self._broker.subscribe() as queue:
            high_water = self._events.list_page(
                after_sequence=0, limit=1, **filters
            ).latest_sequence
            # The event ID cursor is resolved through the Event store: durable
            # pages are scanned in sequence order, the matching event_id fixes
            # the resume position, and delivery continues strictly after it.
            cursor = 0
            searching = after_event_id is not None
            while True:
                page = self._events.list_page(
                    after_sequence=cursor,
                    limit=REPLAY_PAGE_SIZE,
                    **filters,
                )
                for item in page.items:
                    if item.sequence > high_water:
                        break
                    if searching:
                        if item.event_id == after_event_id:
                            searching = False
                        continue
                    invalidation = invalidation_for(item)
                    if invalidation is None:
                        continue
                    yield self._changed_frame(invalidation)
                if not page.has_more or not page.items or page.items[-1].sequence >= high_water:
                    break
                cursor = page.next_after_sequence
            if searching:
                # The requested event is absent from the client's retained,
                # authorized view; never silently resume at the latest.
                yield self._gap_frame(after_event_id)
                return
            while True:
                try:
                    item = await asyncio.wait_for(
                        queue.get(), timeout=self._heartbeat_seconds
                    )
                except asyncio.TimeoutError:
                    if self._broker.dropped_count(queue):
                        return
                    yield _sse(comment=_heartbeat_comment())
                    continue
                if self._broker.dropped_count(queue):
                    # The workspace contract has no slow-consumer frame type;
                    # end the stream rather than silently dropping history.
                    return
                if item.sequence <= high_water:
                    continue
                if not self._allowed(item, target_id, allowed_target_ids):
                    continue
                invalidation = invalidation_for(item)
                if invalidation is None:
                    continue
                yield self._changed_frame(invalidation)
                high_water = item.sequence

    @staticmethod
    def _changed_frame(invalidation: WorkspaceResourceChanged) -> str:
        return _sse(
            event="resource.changed",
            event_id=invalidation.event_id,
            data=invalidation.model_dump(mode="json"),
        )

    @staticmethod
    def _gap_frame(after_event_id: str) -> str:
        gap = WorkspaceStreamGap(
            schema_version=1,
            event_id=after_event_id,
            occurred_at=datetime.now(UTC),
            reason="event cursor not found in retained history",
            action="reload_snapshot",
        )
        return _sse(
            event="stream.gap",
            event_id=gap.event_id,
            data=gap.model_dump(mode="json"),
        )


__all__ = [
    "HEARTBEAT_INTERVAL_SECONDS",
    "REPLAY_PAGE_SIZE",
    "WorkspaceEventStream",
    "WorkspaceResourceChanged",
    "WorkspaceResourceKind",
    "WorkspaceStreamGap",
    "invalidation_for",
]
