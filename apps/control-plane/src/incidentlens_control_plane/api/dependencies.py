"""Reusable request-validation dependencies for the v1 API."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, ValidationError


class StrictQueryModel(BaseModel):
    """Base class for v1 query parcels: unknown query parameters are rejected.

    FastAPI silently ignores undeclared query parameters, which would let a
    caller pass a parameter we never validated and never notice.  Every v1
    query parcel inherits ``extra="forbid"`` and is parsed through
    :func:`strict_query` so unknown keys surface as a stable
    ``request_validation_failed`` 422.
    """

    model_config = ConfigDict(extra="forbid")


class NoQueryParams(StrictQueryModel):
    """Marker query model for endpoints that accept no query parameters."""


def strict_query(model: type[StrictQueryModel]) -> Callable[[Request], StrictQueryModel]:
    """Build a FastAPI dependency that strictly validates ``request.query_params``.

    The returned dependency casts the raw query string into *model* through
    Pydantic and converts any ``ValidationError`` (notably
    ``extra_forbidden``) into the framework's ``RequestValidationError`` so the
    v1 error normalization turns it into a stable 422 envelope.
    """

    def _validate(request: Request) -> StrictQueryModel:
        try:
            return model.model_validate(dict(request.query_params.items()))
        except ValidationError as exc:
            raise RequestValidationError(
                _sanitized_errors(exc.errors()),
                body=dict(request.query_params.items()),
            ) from exc

    return _validate


def _sanitized_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop echoed request input and Pydantic context from validation errors."""
    return [
        {key: value for key, value in error.items() if key not in ("input", "ctx")}
        for error in errors
    ]
