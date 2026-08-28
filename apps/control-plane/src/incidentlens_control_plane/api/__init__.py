"""Versioned product API surface mounted at ``/api/v1``.

This package owns the stable wire contract of the product API: per-request ID
propagation, the unified ``ApiErrorResponse`` envelope, the strict-query
validation mechanism, and the version-info endpoint.  Legacy ``/api/*`` routes
are intentionally untouched; every error handler registered here delegates non
-``/api/v1`` requests to FastAPI's default handling so legacy bodies stay
byte-for-byte unchanged.
"""

from incidentlens_control_plane.api.errors import ApiProblem
from incidentlens_control_plane.api.models import (
    ApiError,
    ApiErrorResponse,
    ApiVersionView,
    JsonValue,
)

__all__ = [
    "ApiError",
    "ApiErrorResponse",
    "ApiVersionView",
    "ApiProblem",
    "JsonValue",
]
