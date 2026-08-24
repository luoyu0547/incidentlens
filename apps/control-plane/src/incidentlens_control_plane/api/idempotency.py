"""Idempotency helper and stable errors for v1 mutation routes.

The single entry point future mutation routes call is :func:`execute_idempotent`.
It validates the ``Idempotency-Key`` header against the stable grammar,
atomically reserves the key in the SQLite store (scoped per principal and per
endpoint), runs the caller's ``action`` exactly once for a fresh reservation,
persists a completed 2xx for exact replay, and otherwise raises the stable v1
conflict / in-progress envelopes.

The route layer is responsible for:

- computing ``request_sha256`` (:func:`idempotency_request_sha256`) from the
  method, stable route key, path parameters, and canonical JSON body only --
  never auth, cookies, CSRF tokens, or request IDs;
- setting ``Idempotency-Replayed: true`` when the returned flag is ``True``.

The ``idempotency_in_progress`` envelope with ``Retry-After: 1`` is produced by
an app-level exception handler (:func:`idempotency_in_progress_handler`)
registered in :func:`incidentlens_control_plane.api.errors.install_error_handlers`,
so every future mutation route gets the header for free without a manual catch.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import ClassVar

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from incidentlens_control_plane.api.errors import ApiProblem
from incidentlens_control_plane.api.models import ApiError, ApiErrorResponse
from incidentlens_control_plane.auth.types import Principal
from incidentlens_control_plane.idempotency.service import IdempotencyService
from incidentlens_control_plane.idempotency.types import ReservationStatus

#: Stable grammar for a user-supplied idempotency key (1-200 of ``[A-Za-z0-9._:-]``).
IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")


class IdempotencyKeyRequiredError(ApiProblem):
    """Missing or malformed ``Idempotency-Key`` header."""

    status_code: ClassVar[int] = 422
    code: ClassVar[str] = "idempotency_key_required"
    message: ClassVar[str] = (
        "Idempotency-Key header is required and must match "
        r"^[A-Za-z0-9._:-]{1,200}$"
    )


class IdempotencyConflictError(ApiProblem):
    """The key was already completed with a different request hash."""

    status_code: ClassVar[int] = 409
    code: ClassVar[str] = "idempotency_conflict"
    message: ClassVar[str] = "idempotency key was already used with a different request"


class IdempotencyInProgressError(ApiProblem):
    """An unexpired reservation is still executing."""

    status_code: ClassVar[int] = 409
    code: ClassVar[str] = "idempotency_in_progress"
    message: ClassVar[str] = "a request with this idempotency key is already in progress"


def require_idempotency_key(raw: str | None) -> str:
    """Return *raw* when it is a well-formed idempotency key, else raise.

    A missing header and any value that does not match the stable grammar are
    the same failure: ``idempotency_key_required`` (422).  It is a request
    validation failure, so 422 matches the v1 convention that validation
    problems return 422 ``request_validation_failed``.
    """
    if raw is not None and IDEMPOTENCY_KEY_PATTERN.fullmatch(raw):
        return raw
    raise IdempotencyKeyRequiredError()


def idempotency_request_sha256(
    *,
    method: str,
    route_key: str,
    path_params: Mapping[str, str] | None,
    canonical_body: str,
) -> str:
    """Deterministic SHA-256 of the identity a mutation cares about.

    The digest covers only the stable operation identity -- HTTP method, the
    stable route key, path parameters, and the canonical JSON body.  It
    deliberately excludes authentication, cookies, CSRF tokens, request IDs,
    and anything that varies per transport so a network-fault retry hashes
    identically.
    """
    params_json = json.dumps(
        sorted((path_params or {}).items()),
        sort_keys=True,
        separators=(",", ":"),
    )
    material = f"{method}|{route_key}|{params_json}|{canonical_body}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def in_progress_response(request: Request) -> JSONResponse:
    """Build the stable ``idempotency_in_progress`` envelope + ``Retry-After``.

    The generic :class:`ApiProblem` handler serializes without extra headers, so
    the in-progress case -- which must carry ``Retry-After: 1`` -- is emitted
    here through a response builder that mirrors the same envelope shape.  It is
    wired app-wide via :func:`idempotency_in_progress_handler`.
    """
    request_id = getattr(request.state, "request_id", None) or ""
    headers = {"Retry-After": "1"}
    if request_id:
        headers["X-Request-ID"] = request_id
    return JSONResponse(
        status_code=IdempotencyInProgressError.status_code,
        content=jsonable_encoder(
            ApiErrorResponse(
                error=ApiError(
                    code=IdempotencyInProgressError.code,
                    message=IdempotencyInProgressError.message,
                    details={},
                    request_id=request_id,
                )
            )
        ),
        headers=headers,
    )


async def idempotency_in_progress_handler(
    request: Request, exc: IdempotencyInProgressError
) -> JSONResponse:
    """App-level handler: 409 ``idempotency_in_progress`` with ``Retry-After: 1``.

    Registered in ``install_error_handlers`` so any route that lets
    :class:`IdempotencyInProgressError` propagate gets the correct envelope and
    header without route-specific code.
    """
    return in_progress_response(request)


async def execute_idempotent[T: BaseModel](
    *,
    service: IdempotencyService,
    principal: Principal,
    method: str,
    route_key: str,
    idempotency_key: str,
    request_sha256: str,
    response_type: type[T],
    action: Callable[[], Awaitable[tuple[int, T]]],
) -> tuple[int, T, bool]:
    """Run *action* exactly once under *idempotency_key* and persist the result.

    Only a 2xx outcome is pinned as a replayable completed result: anything else
    (3xx/4xx as well as 5xx) re-arms the short lease so a later same-key retry
    can re-run instead of replaying a non-2xx.  Returns ``(status_code, body,
    replayed)`` where ``replayed`` is ``True`` when a stored 2xx was served
    rather than by running *action*.  Raises :class:`IdempotencyConflictError`
    (409) for a same-key different-request collision and
    :class:`IdempotencyInProgressError` (409) for a still-active reservation.
    """
    validated_key = require_idempotency_key(idempotency_key)
    reservation = service.reserve(
        principal_id=principal.principal_id,
        method=method,
        route_key=route_key,
        idempotency_key=validated_key,
        request_sha256=request_sha256,
        now=datetime.now(UTC),
    )
    if reservation.status == ReservationStatus.REPLAY:
        if reservation.response_json is None:
            raise RuntimeError(
                "stored idempotency record is completed but has no response body"
            )
        replayed_body = response_type.model_validate_json(reservation.response_json)
        status_code = reservation.status_code if reservation.status_code is not None else 200
        return status_code, replayed_body, True
    if reservation.status == ReservationStatus.CONFLICT:
        raise IdempotencyConflictError()
    if reservation.status == ReservationStatus.IN_PROGRESS:
        raise IdempotencyInProgressError()

    try:
        status_code, body = await action()
    except BaseException:
        _rearm_after_failure(service, principal, method, route_key, validated_key)
        raise

    if not 200 <= status_code < 300:
        _rearm_after_failure(service, principal, method, route_key, validated_key)
        return status_code, body, False

    service.complete(
        principal_id=principal.principal_id,
        method=method,
        route_key=route_key,
        idempotency_key=validated_key,
        status_code=status_code,
        response_json=json.dumps(
            jsonable_encoder(body),
            sort_keys=True,
            separators=(",", ":"),
        ),
        now=datetime.now(UTC),
    )
    return status_code, body, False


def _rearm_after_failure(
    service: IdempotencyService,
    principal: Principal,
    method: str,
    route_key: str,
    validated_key: str,
) -> None:
    """Keep an uncompleted reservation reclaimable after ~60s.

    A non-2xx (or raised) outcome is never persisted as a success; the row stays
    ``in_progress`` with a fresh short lease so a same-key retry reclaims it.
    """
    try:
        service.keep_alive_after_failure(
            principal_id=principal.principal_id,
            method=method,
            route_key=route_key,
            idempotency_key=validated_key,
            now=datetime.now(UTC),
        )
    except Exception:
        # The original failure is the interesting one; never mask it with a
        # secondary store write error.
        pass
