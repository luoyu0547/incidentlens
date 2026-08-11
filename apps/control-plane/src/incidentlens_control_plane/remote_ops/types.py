"""Typed, provider-neutral remote-operation contracts.

These contracts deliberately contain no ``command`` or credential field.
Adapters turn an approved operation into a fixed command template and resolve
credentials from a secret manager at execution time.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ScopeKind(StrEnum):
    HOST = "host"
    CONTAINER = "container"


class HostScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal[ScopeKind.HOST] = ScopeKind.HOST


class ContainerScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal[ScopeKind.CONTAINER] = ScopeKind.CONTAINER
    container: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


RemoteScope = Annotated[HostScope | ContainerScope, Field(discriminator="kind")]


class FileOperationKind(StrEnum):
    READ = "read"
    LIST = "list"
    SEARCH = "search"
    STAT = "stat"
    EDIT = "edit"
    WRITE = "write"
    RESTORE = "restore"


class OperationRisk(StrEnum):
    AUTO_READ = "auto_read"
    BACKUP_REQUIRED = "backup_required"
    APPROVAL_REQUIRED = "approval_required"
    FORBIDDEN = "forbidden"


class OperationContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    operation_id: str = Field(min_length=1, max_length=120)
    incident_id: str = Field(min_length=1, max_length=120)
    project_id: str = Field(min_length=1, max_length=80)
    target_id: str = Field(min_length=1, max_length=80)
    service: str = Field(min_length=1, max_length=120)
    scope: RemoteScope
    session_id: str | None = Field(default=None, min_length=1, max_length=120)


class FileOperationRequest(OperationContext):
    kind: FileOperationKind
    path: PurePosixPath


class TextReplacement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    old_text: str = Field(min_length=1, max_length=1_000_000)
    new_text: str = Field(max_length=1_000_000)
    expected_count: int = Field(default=1, ge=1, le=1_000)


class FileEditRequest(OperationContext):
    kind: Literal[FileOperationKind.EDIT] = FileOperationKind.EDIT
    path: PurePosixPath
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    replacements: tuple[TextReplacement, ...] = Field(min_length=1, max_length=200)


class FileWriteRequest(OperationContext):
    kind: Literal[FileOperationKind.WRITE] = FileOperationKind.WRITE
    path: PurePosixPath
    expected_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    content: bytes = Field(max_length=10_485_760)
    mode: int | None = Field(default=None, ge=0, le=0o7777)


FileMutationRequest = Annotated[
    FileEditRequest | FileWriteRequest,
    Field(discriminator="kind"),
]


class ChangeSetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    changeset_id: str = Field(min_length=1, max_length=120)
    files: tuple[FileMutationRequest, ...] = Field(min_length=1, max_length=100)
    verification_plan: str = Field(min_length=1, max_length=4_000)
    rollback_plan: str = Field(min_length=1, max_length=4_000)

    @model_validator(mode="after")
    def files_share_one_operation_context(self) -> "ChangeSetRequest":
        first = self.files[0]
        context = (
            first.incident_id,
            first.project_id,
            first.target_id,
            first.service,
            first.scope,
        )
        if any(
            (
                item.incident_id,
                item.project_id,
                item.target_id,
                item.service,
                item.scope,
            )
            != context
            for item in self.files[1:]
        ):
            raise ValueError("all files must share one operation context")
        return self


class ShellRequest(OperationContext):
    risk: OperationRisk = OperationRisk.APPROVAL_REQUIRED
    command: str = Field(min_length=1, max_length=8_000)
    reason: str = Field(min_length=1, max_length=1_000)


class DockerActionKind(StrEnum):
    STOP = "stop"
    RESTART = "restart"
    KILL = "kill"
    REMOVE = "remove"
    COMPOSE_STOP = "compose_stop"
    COMPOSE_RESTART = "compose_restart"
    COMPOSE_DOWN = "compose_down"
    COMPOSE_UP = "compose_up"


class DockerActionRequest(OperationContext):
    action: DockerActionKind
    container: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"
    )
    reason: str = Field(min_length=1, max_length=1_000)

    @model_validator(mode="after")
    def container_required_for_container_actions(self) -> "DockerActionRequest":
        container_actions = {
            DockerActionKind.STOP,
            DockerActionKind.RESTART,
            DockerActionKind.KILL,
            DockerActionKind.REMOVE,
        }
        compose_actions = {
            DockerActionKind.COMPOSE_STOP,
            DockerActionKind.COMPOSE_RESTART,
            DockerActionKind.COMPOSE_DOWN,
            DockerActionKind.COMPOSE_UP,
        }
        if self.action in container_actions and self.container is None:
            raise ValueError(f"container is required for {self.action.value} action")
        if self.action in compose_actions and self.container is not None:
            raise ValueError(f"container must not be set for {self.action.value} action")
        return self


class RuntimeKind(StrEnum):
    DOCKER_COMPOSE = "docker_compose"


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
