"""Wire and persistence types for the Target product facade.

The facade is deliberately thin: it exposes a clean product model (``TargetView``)
over the authoritative :class:`~incidentlens_control_plane.project_registry`
data.  ``host`` / ``ssh_user`` / ``ssh_port`` and the service/scope data stay in
the authoritative ``projects.record_json``; the ``target_facade_bindings`` table
carries only product identity, the server-side ``authentication_ref`` and its
display metadata.

The on-the-wire models are frozen and reject unknown fields so the product
contract is exactly the declared shape and actor identity can never be smuggled
through a request body.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

HostKeyPolicyLiteral = Literal["strict", "pinned"]


class TargetCreate(BaseModel):
    """Body for ``POST /api/v1/targets``.

    ``authentication_ref`` is an SSH identity reference (for example an
    ``ssh-agent:<user>@<host>`` URI or the name of a named profile) stored
    server-side and never echoed back to a client.  ``optional_source_path`` is
    product-local display metadata and never participates in path authorization.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=120)
    host: str = Field(min_length=1, max_length=255)
    ssh_user: str = Field(min_length=1, max_length=80)
    ssh_port: int = Field(default=22, ge=1, le=65535)
    authentication_ref: str = Field(min_length=1, max_length=500)
    host_key_policy: HostKeyPolicyLiteral = "strict"
    pinned_host_key_sha256: str | None = None
    optional_source_path: Path | None = None

    @field_validator("optional_source_path")
    @classmethod
    def optional_source_path_must_be_absolute(
        cls, value: Path | None
    ) -> Path | None:
        if value is not None and not value.is_absolute():
            raise ValueError("optional_source_path must be absolute")
        return value

    @model_validator(mode="after")
    def pinned_policy_requires_pin(self) -> "TargetCreate":
        if (
            self.host_key_policy == "pinned"
            and self.pinned_host_key_sha256 is None
        ):
            raise ValueError(
                "pinned_host_key_sha256 is required when host_key_policy is 'pinned'"
            )
        return self


class TargetPatch(BaseModel):
    """Body for ``PATCH /api/v1/targets/{id}`` (optimistic concurrency).

    ``expected_version`` must match the current facade ``version`` or the patch
    fails with a stable ``resource_conflict`` (409).  Only the fields a caller
    sets are applied; everything else (including services and scope on the
    authoritative record) is preserved.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str | None = Field(default=None, min_length=1, max_length=120)
    host: str | None = Field(default=None, min_length=1, max_length=255)
    ssh_user: str | None = Field(default=None, min_length=1, max_length=80)
    ssh_port: int | None = Field(default=None, ge=1, le=65535)
    authentication_ref: str | None = Field(default=None, min_length=1, max_length=500)
    host_key_policy: HostKeyPolicyLiteral | None = None
    pinned_host_key_sha256: str | None = None
    optional_source_path: Path | None = None
    expected_version: int = Field(ge=1)

    @field_validator("optional_source_path")
    @classmethod
    def optional_source_path_must_be_absolute(
        cls, value: Path | None
    ) -> Path | None:
        if value is not None and not value.is_absolute():
            raise ValueError("optional_source_path must be absolute")
        return value

    @model_validator(mode="after")
    def pinned_policy_requires_pin(self) -> "TargetPatch":
        if (
            self.host_key_policy == "pinned"
            and self.pinned_host_key_sha256 is None
        ):
            raise ValueError(
                "pinned_host_key_sha256 is required when host_key_policy is 'pinned'"
            )
        return self


class TargetView(BaseModel):
    """The product-facing representation of a target.

    ``authentication_configured`` and ``authentication_hint`` are the only
    traces of the server-side ``authentication_ref``; the full reference is
    never serialized.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_id: str
    name: str
    host: str
    ssh_user: str
    ssh_port: int
    authentication_configured: bool
    authentication_hint: str
    host_key_policy: HostKeyPolicyLiteral
    pinned_host_key_sha256: str | None
    optional_source_path: Path | None
    version: int
    created_at: datetime
    updated_at: datetime


class TargetServiceView(BaseModel):
    """One service exposed through ``GET /api/v1/targets/{id}/services``.

    Resolved from the authoritative ProjectRegistry record, never duplicated in
    the facade table.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    service: str
    container_names: tuple[str, ...]
    allowed_host_paths: tuple[PurePosixPath, ...]
    protected_remote_paths: tuple[PurePosixPath, ...]


class TargetBinding(BaseModel):
    """A persisted ``target_facade_bindings`` row.

    Holds only product identity, the server-side auth reference, host-key policy
    metadata and the facade version.  ``project_id`` + ``registry_target_id``
    point back at the authoritative record and are unique together so duplicate
    internal target IDs in different projects never collapse into one facade
    target.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_id: str
    project_id: str
    registry_target_id: str
    name: str
    authentication_ref: str
    host_key_policy: HostKeyPolicyLiteral
    pinned_host_key_sha256: str | None
    version: int = Field(default=1, ge=1)
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def timestamps_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must be timezone-aware")
        return value
