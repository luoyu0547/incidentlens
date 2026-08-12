"""Log query and search HTTP API routes.

Only redacted content is ever returned: the pipeline stores redacted
``LogRecord`` rows and the API serializes those rows, never raw log text.
Query bodies never accept connection parameters such as ``host`` or
``ssh_user`` (they are resolved only from the project registry), and error
details are fixed safe strings that never echo credentials or raw log text.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

from fastapi import (
    APIRouter,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import BaseModel, ConfigDict, Field

from incidentlens_control_plane.logs.sources import LogSourceUnavailable
from incidentlens_control_plane.logs.store import LogSearchFilters
from incidentlens_control_plane.logs.subscriptions import (
    TooManyActiveSubscriptions,
)
from incidentlens_control_plane.logs.types import (
    LogQueryRequest,
    LogRecord,
    LogScope,
    LogSeverity,
    LogSourceKind,
    LogSubscription,
    LogSubscriptionStatus,
    ServiceNotFound,
    TargetNotFound,
    UnregisteredLogContainer,
)
from incidentlens_control_plane.project_registry.store import ProjectNotFound
from incidentlens_control_plane.remote_ops.policy import RemotePathDenied
from incidentlens_control_plane.remote_ops.transport import (
    RemoteConnectionError,
    RemotePathError,
    RemoteTimeoutError,
)
from incidentlens_control_plane.routes import get_runtime
from incidentlens_control_plane.runtime import RuntimeServices

router = APIRouter(prefix="/api/logs", tags=["logs"])


class LogQueryRequestModel(BaseModel):
    """Request body mirroring the ``LogQueryRequest`` domain model.

    ``extra="forbid"`` rejects connection fields (host, ssh_user, credentials)
    that must only ever be resolved from the project registry.
    """

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


@router.post("/query")
async def query_logs(
    request: Request, body: LogQueryRequestModel
) -> list[dict[str, object]]:
    """Run the on-demand log pipeline and return redacted log records."""
    runtime = get_runtime(request)
    try:
        records = await runtime.logs.query(
            LogQueryRequest(**body.model_dump()),
            now=datetime.now(UTC),
        )
    except ProjectNotFound:
        raise HTTPException(status_code=404, detail="Project not found")
    except TargetNotFound:
        raise HTTPException(status_code=404, detail="Target not found")
    except ServiceNotFound:
        raise HTTPException(status_code=404, detail="Service not found")
    except UnregisteredLogContainer:
        raise HTTPException(
            status_code=409, detail="Container is not registered for the service"
        )
    except RemotePathDenied:
        raise HTTPException(status_code=422, detail="Log path is not authorized")
    except LogSourceUnavailable:
        raise HTTPException(status_code=502, detail="Log source unavailable")
    except RemoteTimeoutError:
        raise HTTPException(status_code=504, detail="Log source timed out")
    except (RemoteConnectionError, RemotePathError):
        raise HTTPException(status_code=502, detail="Log source unavailable")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid log query")
    return [record.model_dump(mode="json") for record in records]


@router.get("/search")
async def search_logs(
    request: Request,
    project_id: str | None = None,
    text: str | None = None,
    target_id: str | None = None,
    service_name: str | None = None,
    source_kind: LogSourceKind | None = None,
    scope: LogScope | None = None,
    severity: LogSeverity | None = None,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
) -> list[dict[str, object]]:
    """Search persisted redacted log records by filters and full-text match."""
    runtime = get_runtime(request)
    filters = LogSearchFilters(
        project_id=project_id,
        target_id=target_id,
        service_name=service_name,
        source_kind=source_kind,
        scope=scope,
        severity=severity,
        text=text,
        start_time=start_time,
        end_time=end_time,
    )
    try:
        records = runtime.log_store.search(filters, limit=limit)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid search text")
    return [record.model_dump(mode="json") for record in records]


class CreateLogSubscriptionRequest(BaseModel):
    """Request body for creating a persistent log subscription.

    ``extra="forbid"`` rejects connection fields (host, ssh_user, credentials)
    that must only ever be resolved from the project registry.
    """

    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, max_length=80)
    target_id: str = Field(min_length=1, max_length=80)
    service_name: str = Field(min_length=1, max_length=120)
    source_kind: LogSourceKind
    scope: LogScope
    source_ref: str = Field(min_length=1, max_length=500)
    opt_in_streaming: bool
    created_by: str = Field(min_length=1, max_length=80)


class LogSubscriptionView(BaseModel):
    """API view of a ``LogSubscription``.

    ``status`` is a ``LogSubscriptionStatus`` StrEnum which serializes to its
    plain string value in JSON mode.
    """

    model_config = ConfigDict(extra="forbid")

    subscription_id: str
    project_id: str
    target_id: str
    service_name: str
    source_kind: LogSourceKind
    scope: LogScope
    source_ref: str
    opt_in_streaming: bool
    status: LogSubscriptionStatus
    created_by: str
    last_error: str | None = None
    last_error_redacted: str | None = None
    created_at: datetime
    updated_at: datetime


class LogRecordView(BaseModel):
    """API view of a persisted ``LogRecord`` (always redacted content)."""

    model_config = ConfigDict(extra="forbid")

    log_id: str
    subscription_id: str | None = None
    project_id: str
    target_id: str
    service_name: str
    source_kind: LogSourceKind
    scope: LogScope
    source_ref: str
    cursor: str
    dedupe_key: str
    observed_at: datetime
    event_time: datetime | None
    severity: LogSeverity
    message_redacted: str
    redaction_summary: dict[str, int]
    normal_signal: str | None = None
    correlation_key: str | None = None
    evidence_ref_id: str | None = None
    created_at: datetime


def _subscription_view(subscription: LogSubscription) -> dict[str, object]:
    """Serialize a subscription through the view so status is a plain string."""
    return LogSubscriptionView(**subscription.model_dump()).model_dump(mode="json")


def _record_view(record: LogRecord) -> dict[str, object]:
    """Serialize a record through the view model."""
    return LogRecordView(**record.model_dump()).model_dump(mode="json")


@router.post("/subscriptions", status_code=201)
async def create_subscription(
    request: Request, body: CreateLogSubscriptionRequest
) -> dict[str, object]:
    """Create a persistent opt-in log subscription and start streaming it."""
    runtime = get_runtime(request)
    try:
        subscription = await runtime.subscriptions.create(**body.model_dump())
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="opt_in_streaming=true is required for streaming",
        )
    except TooManyActiveSubscriptions:
        raise HTTPException(
            status_code=429, detail="Active log subscription limit reached"
        )
    return _subscription_view(subscription)


@router.get("/subscriptions")
async def list_subscriptions(request: Request) -> list[dict[str, object]]:
    """List all persistent log subscriptions."""
    runtime = get_runtime(request)
    return [
        _subscription_view(subscription)
        for subscription in runtime.log_store.list_subscriptions()
    ]


@router.get("/subscriptions/{subscription_id}")
async def get_subscription(
    request: Request, subscription_id: str
) -> dict[str, object]:
    """Get a single persistent log subscription."""
    runtime = get_runtime(request)
    subscription = runtime.log_store.get_subscription(subscription_id)
    if subscription is None:
        raise HTTPException(status_code=404, detail="Subscription not found")
    return _subscription_view(subscription)


@router.post("/subscriptions/{subscription_id}/pause")
async def pause_subscription(
    request: Request, subscription_id: str
) -> dict[str, object]:
    """Pause a subscription, preserving its stored cursor."""
    runtime = get_runtime(request)
    _require_transitionable_subscription(runtime, subscription_id)
    try:
        subscription = await runtime.subscriptions.pause(subscription_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Subscription not found")
    return _subscription_view(subscription)


@router.post("/subscriptions/{subscription_id}/resume")
async def resume_subscription(
    request: Request, subscription_id: str
) -> dict[str, object]:
    """Resume a paused subscription from its stored cursor."""
    runtime = get_runtime(request)
    _require_transitionable_subscription(runtime, subscription_id)
    try:
        subscription = await runtime.subscriptions.resume(subscription_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Subscription not found")
    return _subscription_view(subscription)


@router.delete("/subscriptions/{subscription_id}", status_code=204)
async def delete_subscription(
    request: Request, subscription_id: str
) -> None:
    """Delete a subscription and stop its reader/writer tasks."""
    runtime = get_runtime(request)
    try:
        await runtime.subscriptions.delete(subscription_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Subscription not found")


@router.get("/subscriptions/{subscription_id}/records")
async def list_subscription_records(
    request: Request,
    subscription_id: str,
    after_cursor: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
) -> list[dict[str, object]]:
    """List persisted records for a subscription in cursor order."""
    runtime = get_runtime(request)
    if runtime.log_store.get_subscription(subscription_id) is None:
        raise HTTPException(status_code=404, detail="Subscription not found")
    records = runtime.log_store.list_records_for_subscription(
        subscription_id, after_cursor=after_cursor, limit=limit
    )
    return [_record_view(record) for record in records]


def _require_transitionable_subscription(
    runtime: RuntimeServices, subscription_id: str
) -> None:
    """Reject unknown and deleted subscriptions for pause/resume transitions.

    A deleted subscription must never be resurrected by a later pause/resume.
    """
    subscription = runtime.log_store.get_subscription(subscription_id)
    if subscription is None:
        raise HTTPException(status_code=404, detail="Subscription not found")
    if subscription.status == LogSubscriptionStatus.DELETED:
        raise HTTPException(status_code=409, detail="Subscription is deleted")


@router.websocket("/subscriptions/{subscription_id}/ws")
async def subscription_websocket(
    websocket: WebSocket, subscription_id: str
) -> None:
    """WebSocket endpoint for subscription record replay and live streaming.

    Registers for live records BEFORE replaying durable records so no record is
    missed between the two phases, then streams live records, skipping any whose
    ``dedupe_key`` was already sent during replay.  A skipped duplicate is
    acknowledged with a ``{"event": "heartbeat"}`` frame.  On disconnect only
    the socket loop exits; the subscription itself is left untouched.
    """
    await websocket.accept()
    runtime = cast(RuntimeServices, websocket.app.state.runtime)

    if runtime.log_store.get_subscription(subscription_id) is None:
        await websocket.close(code=1008)
        return

    try:
        async with runtime.subscriptions.subscribe_records(subscription_id) as queue:
            # Replay durable records in cursor order, tracking sent dedupe keys.
            seen_dedupe_keys: set[str] = set()
            after_cursor: str | None = None
            while True:
                records = runtime.log_store.list_records_for_subscription(
                    subscription_id, after_cursor=after_cursor, limit=1000
                )
                if not records:
                    break
                for record in records:
                    await websocket.send_json(record.model_dump(mode="json"))
                    seen_dedupe_keys.add(record.dedupe_key)
                after_cursor = records[-1].cursor

            # Stream live records, skipping duplicates already sent.
            while True:
                record = await queue.get()
                if record.dedupe_key in seen_dedupe_keys:
                    await websocket.send_json({"event": "heartbeat"})
                else:
                    await websocket.send_json(record.model_dump(mode="json"))
                    seen_dedupe_keys.add(record.dedupe_key)
    except WebSocketDisconnect:
        pass
