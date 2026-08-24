"""Route registration for the versioned product API (``/api/v1``)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from incidentlens_control_plane.api.dependencies import (
    NoQueryParams,
    strict_query,
)
from incidentlens_control_plane.api.models import ApiVersionView

router = APIRouter(prefix="/api/v1")


@router.get(
    "/version",
    operation_id="getApiVersion",
    response_model=ApiVersionView,
)
async def get_api_version(
    query: Annotated[NoQueryParams, Depends(strict_query(NoQueryParams))],
) -> ApiVersionView:
    """Return the versioned API contract without touching any target host."""
    return ApiVersionView()
