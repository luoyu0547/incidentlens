"""Stable wire models for the versioned product API.

Every model here is frozen and rejects unknown fields so the on-the-wire
contract is exactly the declared shape: new fields are a deliberate, versioned
change, never an accidental drift.
"""

from __future__ import annotations

from typing import Literal, TypeAliasType

from pydantic import BaseModel, ConfigDict, Field

#: A JSON-compatible value that may appear inside an error ``details`` payload.
#: Expressed as a lazy ``TypeAliasType`` so Pydantic can resolve the recursion.
JsonValue = TypeAliasType(
    "JsonValue",
    "str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]",
)


class ApiError(BaseModel):
    """One stable, machine-readable error."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    message: str
    details: dict[str, JsonValue] = Field(default_factory=dict)
    request_id: str


class ApiErrorResponse(BaseModel):
    """The envelope every v1 failure is serialized into."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    error: ApiError


class ApiVersionView(BaseModel):
    """The version contract served at ``GET /api/v1/version``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    api_version: Literal["v1"] = "v1"
    stream_schema_versions: tuple[Literal[1], ...] = (1,)
    minimum_cli_protocol_version: str = "1.0.0"
    minimum_web_protocol_version: str = "1.0.0"
