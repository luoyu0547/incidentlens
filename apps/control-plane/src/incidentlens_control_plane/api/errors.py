"""Stable error vocabulary and normalization for the versioned product API.

The codes in :data:`STABLE_ERROR_CODES` are the canonical machine-readable
vocabulary for the ``/api/v1`` surface.  The handlers in this module translate
framework and application exceptions into the stable :class:`ApiErrorResponse`
envelope.  Handlers are registered app-wide (so middleware order is simple) but
every non-``/api/v1`` request is delegated to FastAPI's default handling, which
keeps legacy ``/api/*`` error bodies untouched.
"""

from __future__ import annotations

import logging
from http import HTTPStatus
from typing import Any, ClassVar

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import (
    http_exception_handler as _default_http_exception_handler,
)
from fastapi.exception_handlers import (
    request_validation_exception_handler as _default_request_validation_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import PlainTextResponse, Response

from incidentlens_control_plane.api.models import ApiError, ApiErrorResponse, JsonValue

logger = logging.getLogger(__name__)

V1_PREFIX = "/api/v1"

#: The stable machine-readable codes introduced with the versioned API.  Every
#: v1 endpoint should map its failures to one of these.
STABLE_ERROR_CODES: frozenset[str] = frozenset(
    {
        "request_validation_failed",
        "authentication_required",
        "permission_denied",
        "resource_not_found",
        "resource_conflict",
        "idempotency_key_required",
        "idempotency_conflict",
        "idempotency_in_progress",
        "target_unreachable",
        "host_key_verification_failed",
        "operation_not_cancellable",
        "cursor_invalid",
        "approval_expired",
        "approval_already_decided",
        "approval_already_consumed",
        "downstream_processing_failed",
        "internal_error",
    }
)

#: Common HTTP status -> stable code hints.  Anything not mapped falls back to
#: ``http_<status>``.
_HTTP_STATUS_CODES: dict[int, str] = {
    400: "request_validation_failed",
    401: "authentication_required",
    403: "permission_denied",
    404: "resource_not_found",
    409: "resource_conflict",
    422: "request_validation_failed",
    500: "internal_error",
    502: "downstream_processing_failed",
    503: "target_unreachable",
    504: "target_unreachable",
}


class ApiProblem(Exception):
    """Marker exception raised by v1 routes to emit a stable error envelope.

    Subclasses may declare ``status_code`` / ``code`` / ``message`` /
    ``details`` as class attributes; instance overrides win for one-off
    responses.
    """

    status_code: ClassVar[int] = 500
    code: ClassVar[str] = "internal_error"
    message: ClassVar[str] = "Internal server error"
    details: ClassVar[dict[str, JsonValue]] = {}

    def __init__(
        self,
        *,
        status_code: int | None = None,
        code: str | None = None,
        message: str | None = None,
        details: dict[str, JsonValue] | None = None,
    ) -> None:
        self.status_code = status_code or type(self).status_code
        self.code = code or type(self).code
        self.message = message or type(self).message
        self.details = dict(details) if details is not None else dict(type(self).details)
        super().__init__(self.message)


# ---------------------------------------------------------------------------
# Envelope construction
# ---------------------------------------------------------------------------


def _is_v1_request(request: Request) -> bool:
    return request.url.path.startswith(V1_PREFIX)


def _envelope_for(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    details: dict[str, JsonValue] | None = None,
) -> JSONResponse:
    """Serialize a stable error envelope, echoing the request ID."""
    request_id = getattr(request.state, "request_id", None) or ""
    body = ApiErrorResponse(
        error=ApiError(
            code=code,
            message=message,
            details=dict(details or {}),
            request_id=request_id,
        )
    )
    return JSONResponse(
        status_code=status_code,
        content=jsonable_encoder(body),
        headers={"X-Request-ID": request_id} if request_id else None,
    )


def _sanitized_errors(errors: list[Any]) -> list[dict[str, Any]]:
    """Strip echo-able input/context before placing validation errors on wire."""
    cleaned: list[dict[str, Any]] = []
    for error in errors:
        if not isinstance(error, dict):
            continue
        cleaned.append(
            {key: value for key, value in error.items() if key not in ("input", "ctx")}
        )
    return cleaned


# ---------------------------------------------------------------------------
# Exception handlers (scoped to /api/v1; legacy delegated to FastAPI defaults)
# ---------------------------------------------------------------------------


async def api_problem_handler(request: Request, exc: ApiProblem) -> JSONResponse:
    """Normalize :class:`ApiProblem` (v1 marker) into the stable envelope."""
    return _envelope_for(
        request,
        status_code=exc.status_code,
        code=exc.code,
        message=exc.message,
        details=exc.details,
    )


async def http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse | Response:
    """Normalize ``HTTPException`` raised inside v1 routes.

    Legacy paths are delegated to FastAPI's default ``{"detail": ...}`` body.
    """
    if not _is_v1_request(request):
        return await _default_http_exception_handler(request, exc)
    status_code = exc.status_code
    detail = exc.detail
    if isinstance(detail, str) and detail:
        message: str = detail
    else:
        try:
            message = HTTPStatus(status_code).phrase
        except ValueError:
            message = f"HTTP {status_code}"
    code = _HTTP_STATUS_CODES.get(status_code, f"http_{status_code}")
    return _envelope_for(request, status_code=status_code, code=code, message=message)


async def request_validation_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Normalize validation failures into a stable 422 envelope."""
    if not _is_v1_request(request):
        return await _default_request_validation_handler(request, exc)
    return _envelope_for(
        request,
        status_code=422,
        code="request_validation_failed",
        message="Request validation failed",
        details={"errors": _sanitized_errors(list(exc.errors()))},
    )


async def unhandled_exception_handler(
    request: Request, exc: Exception
) -> JSONResponse | PlainTextResponse:
    """Turn uncaught exceptions into redacted 500s; never leak exception text.

    Starlette routes the ``Exception`` handler through ``ServerErrorMiddleware``,
    which still re-raises after the response is sent so the server logs the
    underlying failure -- the client only ever sees the stable envelope (v1) or
    the exact legacy plain-text 500 (non-v1).
    """
    if not _is_v1_request(request):
        return PlainTextResponse("Internal Server Error", status_code=500)
    logger.error(
        "unhandled '%s' while serving %s",
        type(exc).__name__,
        request.url.path,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    return _envelope_for(
        request,
        status_code=500,
        code="internal_error",
        message="Internal server error",
    )


def install_error_handlers(app: FastAPI) -> None:
    """Register v1-scoped error normalization on *app*.

    Handlers are registered app-wide; each one delegates non-``/api/v1``
    requests to FastAPI's default handler so legacy bodies are unchanged.

    The :class:`~incidentlens_control_plane.api.idempotency.IdempotencyInProgressError`
    handler is registered here too (imported lazily to avoid a module cycle
    with ``api/idempotency.py``) so every app gets the ``Retry-After: 1``
    in-progress envelope without slicing it into each idempotent route.
    """
    from incidentlens_control_plane.api.idempotency import (
        IdempotencyInProgressError,
        idempotency_in_progress_handler,
    )

    app.add_exception_handler(IdempotencyInProgressError, idempotency_in_progress_handler)
    app.add_exception_handler(ApiProblem, api_problem_handler)
    app.add_exception_handler(RequestValidationError, request_validation_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
