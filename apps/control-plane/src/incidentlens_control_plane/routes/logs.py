"""Log query and search HTTP API routes.

Only redacted content is ever returned: the pipeline stores redacted
``LogRecord`` rows and the API serializes those rows, never raw log text.
Query bodies never accept connection parameters such as ``host`` or
``ssh_user`` (they are resolved only from the project registry), and error
details are fixed safe strings that never echo credentials or raw log text.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from incidentlens_control_plane.logs.sources import LogSourceUnavailable
from incidentlens_control_plane.logs.store import LogSearchFilters
from incidentlens_control_plane.logs.types import (
    LogQueryRequest,
    LogScope,
    LogSeverity,
    LogSourceKind,
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
