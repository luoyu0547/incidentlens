"""Gateway for shell operations and scoped file tools with policy integration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath

from incidentlens_control_plane.approvals.service import (
    ApprovalMismatch,
    ApprovalService,
    ApprovalUnavailable,
)
from incidentlens_control_plane.approvals.store import ApprovalNotFound
from incidentlens_control_plane.project_registry.store import (
    ProjectNotFound,
    ProjectRegistryStore,
)
from incidentlens_control_plane.project_registry.types import (
    TargetRegistration,
)
from incidentlens_control_plane.remote_ops.files import (
    ContainerFileOperationUnsupported,
    FileReadResult,
    RemoteFileTools,
    SearchMatch,
)
from incidentlens_control_plane.remote_ops.policy import RemotePathPolicy
from incidentlens_control_plane.remote_ops.sessions import SessionManager
from incidentlens_control_plane.remote_ops.transport import FileMetadata
from incidentlens_control_plane.remote_ops.types import (
    ContainerScope,
    HostScope,
    OperationRisk,
    RemoteScope,
    ShellRequest,
)


@dataclass(frozen=True, slots=True)
class ShellResult:
    """Result of a shell operation."""

    command: str
    approved: bool
    approval_id: str | None = None
    reason: str = ""


class CommandForbidden(Exception):
    """Raised when a command is classified as forbidden."""


class Gateway:
    """Gateway for shell operations with approval integration.

    Implements three-tier command routing:
    - Automatic (AUTO_READ, BACKUP_REQUIRED): Execute immediately without approval.
    - Approval-required (APPROVAL_REQUIRED): Need approval before execution.
    - Forbidden (FORBIDDEN): Never execute, never create approvals.
    """

    def __init__(self, approvals: ApprovalService) -> None:
        self._approvals = approvals

    async def shell(
        self,
        request: ShellRequest,
        approval_id: str | None = None,
    ) -> ShellResult:
        """Execute a shell command with risk-based routing.

        Automatic commands execute immediately.
        Approval-required commands call approvals.request when no ID is provided
        and return that pending record without transport execution.
        With an ID, call approvals.consume immediately before writing to the PTY.
        Forbidden commands never create approvals.
        """
        intent = {
            "kind": "shell",
            "target_id": request.target_id,
            "command": request.command,
            "service": request.service,
        }

        # Tier 3: Forbidden commands never execute or create approvals
        if request.risk == OperationRisk.FORBIDDEN:
            raise CommandForbidden(
                f"Command '{request.command}' is forbidden and cannot be executed"
            )

        # Tier 1: Automatic commands execute immediately
        if request.risk in (OperationRisk.AUTO_READ, OperationRisk.BACKUP_REQUIRED):
            return ShellResult(
                command=request.command,
                approved=True,
                reason="Automatic command executed without approval",
            )

        # Tier 2: Approval-required commands
        now = datetime.now(UTC)

        if approval_id is not None:
            # Consume the approval before executing
            try:
                await self._approvals.consume(approval_id, intent, now=now)
            except ApprovalNotFound as exc:
                return ShellResult(
                    command=request.command,
                    approved=False,
                    approval_id=approval_id,
                    reason=f"Approval not found: {exc}",
                )
            except ApprovalUnavailable as exc:
                return ShellResult(
                    command=request.command,
                    approved=False,
                    approval_id=approval_id,
                    reason=f"Approval unavailable: {exc}",
                )
            except ApprovalMismatch as exc:
                return ShellResult(
                    command=request.command,
                    approved=False,
                    approval_id=approval_id,
                    reason=f"Intent mismatch: {exc}",
                )
            return ShellResult(
                command=request.command,
                approved=True,
                approval_id=approval_id,
                reason="Approved and consumed",
            )

        # No approval ID provided - request one
        record = await self._approvals.request(intent, now=now)
        return ShellResult(
            command=request.command,
            approved=False,
            approval_id=record.approval_id,
            reason="Approval required",
        )


# ---------------------------------------------------------------------------
# Scoped file-tool gateway
# ---------------------------------------------------------------------------


class RemoteToolGateway:
    """Read-only gateway that resolves project/target/service policy and
    delegates to :class:`RemoteFileTools`.

    Host scope is fully executable.  Container scope validates the path
    policy but returns :class:`ContainerFileOperationUnsupported` until
    Task 7 installs the fixed Docker file backend.
    """

    def __init__(
        self,
        projects: ProjectRegistryStore,
        sessions: SessionManager,
        targets: dict[str, TargetRegistration],
    ) -> None:
        self._projects = projects
        self._sessions = sessions
        self._targets = targets

    # --- internal helpers ---

    def _resolve_project_service(
        self, project_id: str, target_id: str, service: str
    ) -> tuple[object, object]:
        """Return ``(project_record, service_registration)`` or raise."""
        from incidentlens_control_plane.project_registry.types import (
            ServiceRegistration,
        )

        record = self._projects.get(project_id)

        target = self._targets.get(target_id)
        if target is None:
            raise ValueError(f"target {target_id!r} is not registered")

        svc: ServiceRegistration | None = None
        for s in record.services:
            if s.compose_service == service:
                svc = s
                break
        if svc is None:
            raise ValueError(
                f"service {service!r} not found in project {project_id!r}"
            )
        return record, svc

    def _make_scope(
        self, scope: dict[str, object] | None
    ) -> RemoteScope:
        if scope is None:
            return HostScope()
        kind = scope.get("kind", "host")
        if kind == "container":
            return ContainerScope(container=str(scope["container"]))
        return HostScope()

    async def _authorize_host_path(
        self, svc_registration: object, path: PurePosixPath, *, write: bool
    ) -> PurePosixPath:
        from incidentlens_control_plane.project_registry.types import (
            ServiceRegistration,
        )

        policy = RemotePathPolicy(svc_registration)  # type: ignore[arg-type]
        return await policy.authorize(HostScope(), path, write=write)

    # --- public API ---

    async def read(
        self,
        *,
        project_id: str,
        target_id: str,
        service: str,
        path: PurePosixPath,
        offset: int = 0,
        limit: int = 1_048_576,
        scope: dict[str, object] | None = None,
    ) -> FileReadResult:
        _, svc = self._resolve_project_service(project_id, target_id, service)
        resolved_scope = self._make_scope(scope)

        if isinstance(resolved_scope, ContainerScope):
            raise ContainerFileOperationUnsupported(
                "container file operations are not supported until Task 7"
            )

        canonical = await self._authorize_host_path(svc, path, write=False)
        session = await self._sessions.connect(self._targets[target_id])
        tools = RemoteFileTools(session.transport)
        return await tools.read(canonical, offset=offset, limit=limit)

    async def list_dir(
        self,
        *,
        project_id: str,
        target_id: str,
        service: str,
        path: PurePosixPath,
        scope: dict[str, object] | None = None,
    ) -> tuple[FileMetadata, ...]:
        _, svc = self._resolve_project_service(project_id, target_id, service)
        resolved_scope = self._make_scope(scope)

        if isinstance(resolved_scope, ContainerScope):
            raise ContainerFileOperationUnsupported(
                "container file operations are not supported until Task 7"
            )

        canonical = await self._authorize_host_path(svc, path, write=False)
        session = await self._sessions.connect(self._targets[target_id])
        tools = RemoteFileTools(session.transport)
        return await tools.list(canonical)

    async def search(
        self,
        *,
        project_id: str,
        target_id: str,
        service: str,
        path: PurePosixPath,
        query: str,
        scope: dict[str, object] | None = None,
    ) -> tuple[SearchMatch, ...]:
        _, svc = self._resolve_project_service(project_id, target_id, service)
        resolved_scope = self._make_scope(scope)

        if isinstance(resolved_scope, ContainerScope):
            raise ContainerFileOperationUnsupported(
                "container file operations are not supported until Task 7"
            )

        canonical = await self._authorize_host_path(svc, path, write=False)
        session = await self._sessions.connect(self._targets[target_id])
        tools = RemoteFileTools(session.transport)
        return await tools.search(canonical, query)

    async def stat(
        self,
        *,
        project_id: str,
        target_id: str,
        service: str,
        path: PurePosixPath,
        scope: dict[str, object] | None = None,
    ) -> FileMetadata:
        _, svc = self._resolve_project_service(project_id, target_id, service)
        resolved_scope = self._make_scope(scope)

        if isinstance(resolved_scope, ContainerScope):
            raise ContainerFileOperationUnsupported(
                "container file operations are not supported until Task 7"
            )

        canonical = await self._authorize_host_path(svc, path, write=False)
        session = await self._sessions.connect(self._targets[target_id])
        tools = RemoteFileTools(session.transport)
        return await tools.stat(canonical)
