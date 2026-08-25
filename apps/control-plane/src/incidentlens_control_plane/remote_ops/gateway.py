"""Gateway for shell operations and scoped file tools with policy integration."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from incidentlens_control_plane.approvals.service import (
    ApprovalMismatch,
    ApprovalService,
    ApprovalUnavailable,
)
from incidentlens_control_plane.approvals.store import ApprovalNotFound
from incidentlens_control_plane.events.broker import RuntimeEventBroker
from incidentlens_control_plane.events.store import RuntimeEventStore
from incidentlens_control_plane.events.types import RuntimeEvent, RuntimeEventType
from incidentlens_control_plane.project_registry.store import (
    ProjectRegistryStore,
)
from incidentlens_control_plane.project_registry.types import (
    TargetRegistration,
)
from incidentlens_control_plane.remote_ops.files import (
    ContainerFileBackend,
    ContainerFileOperationUnsupported,
    FileReadResult,
    RemoteFileTools,
    SearchMatch,
)
from incidentlens_control_plane.remote_ops.policy import RemotePathPolicy
from incidentlens_control_plane.remote_ops.sessions import SessionManager
from incidentlens_control_plane.remote_ops.transport import (
    FileMetadata,
    RemoteTransport,
)
from incidentlens_control_plane.remote_ops.types import (
    ChangeSetRequest,
    ContainerScope,
    DockerActionKind,
    DockerActionRequest,
    FileEditRequest,
    FileWriteRequest,
    HostScope,
    OperationRisk,
    RemoteScope,
    ShellRequest,
    TextReplacement,
)

if TYPE_CHECKING:
    from incidentlens_control_plane.changes.manager import (
        ChangeManager,
        ChangeResult,
    )
    from incidentlens_control_plane.project_registry.types import (
        ServiceRegistration,
    )


@dataclass(frozen=True, slots=True)
class ShellResult:
    """Result of a shell operation."""

    command: str
    approved: bool
    approval_id: str | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class DockerActionResult:
    """Result of a docker_action request."""

    action: DockerActionKind
    approved: bool
    approval_id: str | None = None
    exit_status: int | None = None
    reason: str = ""


class CommandForbidden(Exception):
    """Raised when a command is classified as forbidden."""


class DockerActionError(Exception):
    """Raised when a docker action fails or is rejected."""


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
        record = await self._approvals.request(
            intent,
            now=now,
            project_id=request.project_id,
            target_id=request.target_id,
            service=request.service,
            session_id=request.session_id,
            investigation_id=request.investigation_id,
            agent_run_id=request.agent_run_id,
            tool_call_id=request.tool_call_id,
            risk=request.risk.value,
            preview={
                "preview": "Shell action requires explicit approval.",
                "impact": "Runs a guarded shell action against the target service.",
            },
        )
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
    """Gateway that resolves project/target/service policy and delegates to
    :class:`RemoteFileTools` (host scope) or :class:`ContainerFileBackend`
    (container scope), and to the :class:`ChangeManager` for mutations.
    """

    def __init__(
        self,
        projects: ProjectRegistryStore,
        sessions: SessionManager,
        targets: dict[str, TargetRegistration] | None = None,
        changes: ChangeManager | None = None,
        approvals: ApprovalService | None = None,
        events: RuntimeEventStore | None = None,
        broker: RuntimeEventBroker | None = None,
    ) -> None:
        self._projects = projects
        self._sessions = sessions
        self._targets = targets or {}
        self._changes = changes
        self._approvals = approvals
        self._events = events
        self._broker = broker

    # --- internal helpers ---

    def _resolve_target(
        self, project_id: str, target_id: str
    ) -> TargetRegistration:
        """Return the target registration from the project record, or raise.

        The target is looked up in ``record.targets`` so the production
        runtime (which never pre-registers a ``targets`` dict) resolves every
        operation from the project registry at call time.
        """
        record = self._projects.get(project_id)
        for target in record.targets:
            if target.target_id == target_id:
                return target
        raise ValueError(
            f"target {target_id!r} is not registered for project {project_id!r}"
        )

    def _resolve_project_service(
        self, project_id: str, target_id: str, service: str
    ) -> tuple[object, TargetRegistration, object]:
        """Return ``(project_record, target_registration, service_registration)`` or raise."""
        record = self._projects.get(project_id)
        target = self._resolve_target(project_id, target_id)

        svc = None
        for s in record.services:
            if s.compose_service == service:
                svc = s
                break
        if svc is None:
            raise ValueError(
                f"service {service!r} not found in project {project_id!r}"
            )
        return record, target, svc

    def resolve_service(
        self, project_id: str, target_id: str, service: str
    ) -> ServiceRegistration:
        """Return the service registration, validating target and service names.

        Exposes the same registry resolution the gateway uses for every
        operation so an agent executor can classify shell commands and inspect
        allowed paths against the registered service without duplicating the
        lookup or contacting the remote target.
        """
        record = self._projects.get(project_id)
        self._resolve_target(project_id, target_id)
        for svc in record.services:
            if svc.compose_service == service:
                return svc
        raise ValueError(f"service {service!r} not found in project {project_id!r}")

    def _make_scope(
        self, scope: dict[str, object] | None
    ) -> RemoteScope:
        if scope is None:
            return HostScope()
        kind = scope.get("kind", "host")
        if kind == "container":
            return ContainerScope(container=str(scope["container"]))
        return HostScope()

    async def _authorize_path(
        self,
        svc_registration: object,
        scope: RemoteScope,
        path: PurePosixPath,
        *,
        write: bool,
        transport: RemoteTransport | None = None,
    ) -> PurePosixPath:
        policy = RemotePathPolicy(svc_registration)  # type: ignore[arg-type]
        return await policy.authorize(scope, path, write=write, transport=transport)

    async def _connect(self, target: TargetRegistration) -> Any:
        session = await self._sessions.connect(target)
        return session.transport

    async def _container_backend(
        self, target: TargetRegistration, scope: ContainerScope
    ) -> ContainerFileBackend:
        transport = await self._connect(target)
        return ContainerFileBackend(transport, scope.container)

    def _require_changes(self) -> ChangeManager:
        if self._changes is None:
            raise ContainerFileOperationUnsupported(
                "change management is not configured"
            )
        return self._changes

    # --- public read-only API ---

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
        _, target, svc = self._resolve_project_service(project_id, target_id, service)
        resolved_scope = self._make_scope(scope)

        if isinstance(resolved_scope, ContainerScope):
            canonical = await self._authorize_path(
                svc, resolved_scope, path, write=False
            )
            backend = await self._container_backend(target, resolved_scope)
            raw = await backend.read_bytes(canonical, max_bytes=offset + limit)
            content = raw[offset:]
            meta = await backend.lstat(canonical)
            if meta.is_symlink:
                raise ContainerFileOperationUnsupported(
                    f"symbolic links are not supported: {canonical}"
                )
            truncated = (offset + limit) < meta.size
            return FileReadResult(
                path=canonical,
                content=content,
                sha256=hashlib.sha256(content).hexdigest(),
                metadata=meta,
                truncated=truncated,
            )

        # Host scope: canonicalize through the live transport so intermediate
        # symlink components are resolved and rejected exactly like the write
        # path (ChangeManager).
        transport = await self._connect(target)
        canonical = await self._authorize_path(
            svc, resolved_scope, path, write=False, transport=transport
        )
        return await RemoteFileTools(transport).read(
            canonical, offset=offset, limit=limit
        )

    async def list_dir(
        self,
        *,
        project_id: str,
        target_id: str,
        service: str,
        path: PurePosixPath,
        scope: dict[str, object] | None = None,
    ) -> tuple[FileMetadata, ...]:
        _, target, svc = self._resolve_project_service(project_id, target_id, service)
        resolved_scope = self._make_scope(scope)

        if isinstance(resolved_scope, ContainerScope):
            canonical = await self._authorize_path(svc, resolved_scope, path, write=False)
            backend = await self._container_backend(target, resolved_scope)
            return await backend.list_directory(canonical)

        transport = await self._connect(target)
        canonical = await self._authorize_path(
            svc, resolved_scope, path, write=False, transport=transport
        )
        return await RemoteFileTools(transport).list(canonical)

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
        _, target, svc = self._resolve_project_service(project_id, target_id, service)
        resolved_scope = self._make_scope(scope)

        if isinstance(resolved_scope, ContainerScope):
            canonical = await self._authorize_path(svc, resolved_scope, path, write=False)
            backend = await self._container_backend(target, resolved_scope)
            return await backend.search(canonical, query)

        transport = await self._connect(target)
        canonical = await self._authorize_path(
            svc, resolved_scope, path, write=False, transport=transport
        )
        return await RemoteFileTools(transport).search(canonical, query)

    async def stat(
        self,
        *,
        project_id: str,
        target_id: str,
        service: str,
        path: PurePosixPath,
        scope: dict[str, object] | None = None,
    ) -> FileMetadata:
        _, target, svc = self._resolve_project_service(project_id, target_id, service)
        resolved_scope = self._make_scope(scope)

        if isinstance(resolved_scope, ContainerScope):
            canonical = await self._authorize_path(
                svc, resolved_scope, path, write=False
            )
            backend = await self._container_backend(target, resolved_scope)
            meta = await backend.lstat(canonical)
            if meta.is_symlink:
                raise ContainerFileOperationUnsupported(
                    f"symbolic links are not supported: {canonical}"
                )
            return meta

        transport = await self._connect(target)
        canonical = await self._authorize_path(
            svc, resolved_scope, path, write=False, transport=transport
        )
        return await RemoteFileTools(transport).stat(canonical)

    # --- mutation API ---

    async def edit(
        self,
        *,
        project_id: str,
        target_id: str,
        service: str,
        path: PurePosixPath,
        expected_sha256: str,
        replacements: tuple[TextReplacement, ...],
        scope: dict[str, object] | None = None,
        incident_id: str = "unknown",
        approval_id: str | None = None,
    ) -> ChangeResult:
        """Wrap a single-file edit in a generated ``ChangeSetRequest``."""
        _, _, svc = self._resolve_project_service(project_id, target_id, service)
        resolved_scope = self._make_scope(scope)
        file_req = FileEditRequest(
            operation_id=f"op-{uuid.uuid4().hex[:12]}",
            incident_id=incident_id,
            project_id=project_id,
            target_id=target_id,
            service=service,
            scope=resolved_scope,
            path=path,
            expected_sha256=expected_sha256,
            replacements=replacements,
        )
        request = ChangeSetRequest(
            changeset_id=f"chs-{uuid.uuid4().hex[:12]}",
            files=(file_req,),
            verification_plan="run syntax checks and compare service behavior",
            rollback_plan="restore the verified timestamped backup",
        )
        return await self.apply_changeset(request, approval_id=approval_id)

    async def write(
        self,
        *,
        project_id: str,
        target_id: str,
        service: str,
        path: PurePosixPath,
        content: bytes,
        mode: int | None = None,
        expected_sha256: str | None = None,
        scope: dict[str, object] | None = None,
        incident_id: str = "unknown",
        approval_id: str | None = None,
    ) -> ChangeResult:
        """Wrap a single-file write in a generated ``ChangeSetRequest``."""
        _, _, svc = self._resolve_project_service(project_id, target_id, service)
        resolved_scope = self._make_scope(scope)
        file_req = FileWriteRequest(
            operation_id=f"op-{uuid.uuid4().hex[:12]}",
            incident_id=incident_id,
            project_id=project_id,
            target_id=target_id,
            service=service,
            scope=resolved_scope,
            path=path,
            content=content,
            mode=mode,
            expected_sha256=expected_sha256,
        )
        request = ChangeSetRequest(
            changeset_id=f"chs-{uuid.uuid4().hex[:12]}",
            files=(file_req,),
            verification_plan="run syntax checks and compare service behavior",
            rollback_plan="remove the file if the change fails",
        )
        return await self.apply_changeset(request, approval_id=approval_id)

    async def apply_changeset(
        self,
        request: ChangeSetRequest,
        *,
        approval_id: str | None = None,
    ) -> ChangeResult:
        """Apply an explicit multi-file changeset."""
        changes = self._require_changes()
        return await changes.apply(request, approval_id=approval_id)

    async def restore(
        self,
        *,
        changeset_id: str,
        approval_id: str | None = None,
    ) -> None:
        """Restore an applied changeset from its verified backups."""
        changes = self._require_changes()
        await changes.rollback(changeset_id, approval_id)

    # --- docker action ---

    async def docker_action(
        self,
        request: DockerActionRequest,
        approval_id: str | None = None,
    ) -> DockerActionResult:
        """Execute a fixed, typed docker/compose action with exact approval.

        Container actions use ``docker <stop|restart|kill|rm> -- <container>``.
        Compose actions use ``docker compose --project-directory <dir> [--project-name
        <name>] <stop|restart|down|up -d>``.  None of these values are accepted from
        the model; they are resolved from the registered target/service.
        """
        _, target, svc = self._resolve_project_service(
            request.project_id, request.target_id, request.service
        )
        argv = self._docker_argv(request, svc, target)

        intent = {
            "kind": "docker_action",
            "target_id": request.target_id,
            "service": request.service,
            "action": request.action.value,
            "container": request.container,
        }

        if approval_id is None:
            if self._approvals is None:
                raise DockerActionError("approval service is not configured")
            record = await self._approvals.request(
                intent,
                project_id=request.project_id,
                target_id=request.target_id,
                service=request.service,
                session_id=request.session_id,
                investigation_id=request.investigation_id,
                agent_run_id=request.agent_run_id,
                tool_call_id=request.tool_call_id,
                risk="approval_required",
                preview={
                    "preview": "Container action requires explicit approval.",
                    "impact": (
                        f"Runs docker action {request.action.value} "
                        f"against {request.container or request.service}."
                    ),
                },
            )
            await self._emit_docker_event(
                RuntimeEventType.DOCKER_ACTION_REQUESTED,
                request,
                "pending",
                approval_id=record.approval_id,
            )
            return DockerActionResult(
                action=request.action,
                approved=False,
                approval_id=record.approval_id,
                reason="Approval required",
            )

        if self._approvals is None:
            raise DockerActionError("approval service is not configured")
        try:
            await self._approvals.consume(approval_id, intent)
        except (ApprovalNotFound, ApprovalUnavailable, ApprovalMismatch) as exc:
            return DockerActionResult(
                action=request.action,
                approved=False,
                approval_id=approval_id,
                reason=str(exc),
            )

        await self._emit_docker_event(
            RuntimeEventType.DOCKER_ACTION_STARTED, request, "running"
        )
        transport = await self._connect(target)
        try:
            result = await transport.run_argv(argv, timeout=60.0)
        except Exception as exc:
            await self._emit_docker_event(
                RuntimeEventType.DOCKER_ACTION_FAILED,
                request,
                "failed",
                error=str(exc),
            )
            raise DockerActionError(str(exc)) from exc

        if result.exit_status != 0:
            detail = result.stderr.decode(errors="replace")
            await self._emit_docker_event(
                RuntimeEventType.DOCKER_ACTION_FAILED,
                request,
                "failed",
                error=detail,
            )
            raise DockerActionError(
                f"docker action {request.action.value} failed: {detail}"
            )

        await self._emit_docker_event(
            RuntimeEventType.DOCKER_ACTION_COMPLETED, request, "completed"
        )
        return DockerActionResult(
            action=request.action,
            approved=True,
            approval_id=approval_id,
            exit_status=result.exit_status,
            reason="completed",
        )

    def _docker_argv(
        self,
        request: DockerActionRequest,
        svc: object,
        target: TargetRegistration | None,
    ) -> tuple[str, ...]:
        registration = svc  # type: ignore[assignment]
        container_commands = {
            DockerActionKind.STOP: "stop",
            DockerActionKind.RESTART: "restart",
            DockerActionKind.KILL: "kill",
            DockerActionKind.REMOVE: "rm",
        }
        compose_commands = {
            DockerActionKind.COMPOSE_STOP: ("stop",),
            DockerActionKind.COMPOSE_RESTART: ("restart",),
            DockerActionKind.COMPOSE_DOWN: ("down",),
            DockerActionKind.COMPOSE_UP: ("up", "-d"),
        }

        if request.action in container_commands:
            if request.container not in registration.container_names:
                raise DockerActionError(
                    f"container {request.container!r} is not registered for "
                    f"service {request.service!r}"
                )
            return (
                "docker",
                container_commands[request.action],
                "--",
                request.container,  # type: ignore[arg-type]
            )

        if target is None or target.compose_working_directory is None:
            raise DockerActionError(
                f"target {request.target_id!r} has no compose_working_directory"
            )
        argv = [
            "docker",
            "compose",
            "--project-directory",
            str(target.compose_working_directory),
        ]
        for compose_file in target.compose_files:
            argv += ["--file", str(compose_file)]
        if target.compose_project_name:
            argv += ["--project-name", target.compose_project_name]
        argv += list(compose_commands[request.action])
        return tuple(argv)

    async def _emit_docker_event(
        self,
        event_type: RuntimeEventType,
        request: DockerActionRequest,
        status: str,
        *,
        approval_id: str | None = None,
        error: str | None = None,
    ) -> None:
        if self._events is None or self._broker is None:
            return
        payload: dict[str, Any] = {
            "target_id": request.target_id,
            "service": request.service,
            "action": request.action.value,
            "container": request.container,
            "status": status,
        }
        if approval_id is not None:
            payload["approval_id"] = approval_id
        if error is not None:
            payload["error"] = error[:500]
        event = RuntimeEvent(
            event_id=uuid.uuid4().hex,
            sequence=0,
            event_type=event_type,
            occurred_at=datetime.now(UTC),
            payload=payload,
        )
        stored_event = self._events.append(event)
        await self._broker.publish(stored_event)
