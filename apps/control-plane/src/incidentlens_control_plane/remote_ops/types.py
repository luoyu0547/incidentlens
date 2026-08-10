"""Typed, provider-neutral remote-operation contracts.

These contracts deliberately contain no ``command`` or credential field.
Adapters turn an approved operation into a fixed command template and resolve
credentials from a secret manager at execution time.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class RuntimeKind(StrEnum):
    DOCKER_COMPOSE = "docker_compose"
    KUBERNETES = "kubernetes"


class OperationKind(StrEnum):
    COLLECT_LOGS = "collect_logs"
    INSPECT_CONFIG = "inspect_config"
    INSPECT_CONTAINER = "inspect_container"
    INSPECT_SOURCE = "inspect_source"
    LOOKUP_SOURCE_ARCHIVE = "lookup_source_archive"
    PROPOSE_CHANGE = "propose_change"
    APPLY_CHANGE = "apply_change"


READ_ONLY_OPERATIONS = frozenset(
    {
        OperationKind.COLLECT_LOGS,
        OperationKind.INSPECT_CONFIG,
        OperationKind.INSPECT_CONTAINER,
        OperationKind.INSPECT_SOURCE,
        OperationKind.LOOKUP_SOURCE_ARCHIVE,
    }
)


class TargetProfile(BaseModel):
    """An administrator-created remote target; credentials live elsewhere."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_id: str = Field(min_length=1, max_length=80)
    host: str = Field(min_length=1, max_length=255)
    ssh_user: str = Field(min_length=1, max_length=80)
    runtime: RuntimeKind
    allowed_services: frozenset[str] = Field(min_length=1)
    credential_ref: str = Field(min_length=1, max_length=255)


class ChangeControls(BaseModel):
    """Evidence that a proposed write is reversible and approved."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    change_ticket: str = Field(min_length=1, max_length=120)
    backup_ref: str = Field(min_length=1, max_length=255)
    approval_id: str = Field(min_length=1, max_length=120)
    verification_plan: str = Field(min_length=1, max_length=4_000)
    rollback_plan: str = Field(min_length=1, max_length=4_000)


class RemoteAction(BaseModel):
    """A requested operation, constrained to an approved target and service."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_id: str = Field(min_length=1, max_length=80)
    operation: OperationKind
    service: str = Field(min_length=1, max_length=120)
    incident_id: str = Field(min_length=1, max_length=120)
    log_query: str | None = Field(default=None, max_length=500)
    time_range_minutes: int = Field(default=30, ge=1, le=1_440)
    change_controls: ChangeControls | None = None


class PolicyDecision(BaseModel):
    """A machine- and UI-readable decision, suitable for immutable auditing."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: bool
    reason: str
    required_gates: tuple[str, ...] = ()
