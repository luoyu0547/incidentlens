"""Project registry HTTP API routes."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from incidentlens_control_plane.events.types import RuntimeEvent, RuntimeEventType
from incidentlens_control_plane.project_registry.store import (
    ProjectAlreadyExists,
    ProjectNotFound,
)
from incidentlens_control_plane.project_registry.types import (
    ProjectRegistration,
)
from incidentlens_control_plane.routes import get_runtime

router = APIRouter(prefix="/api/projects", tags=["projects"])


@router.post("", status_code=201)
async def create_project(
    request: Request,
    registration: ProjectRegistration,
) -> dict[str, Any]:
    """Create a new project registration."""
    runtime = get_runtime(request)
    now = datetime.now(UTC)

    try:
        record = runtime.projects.create(registration, now=now)
    except ProjectAlreadyExists:
        raise HTTPException(status_code=409, detail="Project already exists")

    # Append event
    event = RuntimeEvent(
        event_id=uuid.uuid4().hex,
        sequence=0,
        event_type=RuntimeEventType.PROJECT_CREATED,
        occurred_at=now,
        payload={"project_id": record.project_id},
    )
    stored_event = runtime.events.append(event)
    await runtime.broker.publish(stored_event)

    return record.model_dump(mode="json")


@router.get("")
async def list_projects(request: Request) -> list[dict[str, Any]]:
    """List all project registrations."""
    runtime = get_runtime(request)
    records = runtime.projects.list()
    return [record.model_dump(mode="json") for record in records]


@router.get("/{project_id}")
async def get_project(
    request: Request,
    project_id: str,
) -> dict[str, Any]:
    """Get a project registration by ID."""
    runtime = get_runtime(request)

    try:
        record = runtime.projects.get(project_id)
    except ProjectNotFound:
        raise HTTPException(status_code=404, detail="Project not found")

    return record.model_dump(mode="json")


@router.put("/{project_id}")
async def update_project(
    request: Request,
    project_id: str,
    registration: ProjectRegistration,
) -> dict[str, Any]:
    """Update a project registration."""
    if registration.project_id != project_id:
        raise HTTPException(
            status_code=409,
            detail="Project ID in body does not match URL",
        )

    runtime = get_runtime(request)
    now = datetime.now(UTC)

    try:
        record = runtime.projects.replace(registration, now=now)
    except ProjectNotFound:
        raise HTTPException(status_code=404, detail="Project not found")

    # Append event
    event = RuntimeEvent(
        event_id=uuid.uuid4().hex,
        sequence=0,
        event_type=RuntimeEventType.PROJECT_UPDATED,
        occurred_at=now,
        payload={"project_id": record.project_id},
    )
    stored_event = runtime.events.append(event)
    await runtime.broker.publish(stored_event)

    return record.model_dump(mode="json")


@router.delete("/{project_id}", status_code=204)
async def delete_project(
    request: Request,
    project_id: str,
) -> None:
    """Delete a project registration."""
    runtime = get_runtime(request)
    now = datetime.now(UTC)

    try:
        runtime.projects.delete(project_id)
    except ProjectNotFound:
        raise HTTPException(status_code=404, detail="Project not found")

    # Append event
    event = RuntimeEvent(
        event_id=uuid.uuid4().hex,
        sequence=0,
        event_type=RuntimeEventType.PROJECT_DELETED,
        occurred_at=now,
        payload={"project_id": project_id},
    )
    stored_event = runtime.events.append(event)
    await runtime.broker.publish(stored_event)
