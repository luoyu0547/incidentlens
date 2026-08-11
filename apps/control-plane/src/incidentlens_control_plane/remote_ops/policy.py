"""Policy gate that must run before an adapter can contact a remote target."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePosixPath

from incidentlens_control_plane.project_registry.types import ServiceRegistration
from incidentlens_control_plane.remote_ops.transport import FileMetadata, RemoteTransport
from incidentlens_control_plane.remote_ops.types import (
    READ_ONLY_OPERATIONS,
    ContainerScope,
    HostScope,
    OperationKind,
    PolicyDecision,
    RemoteAction,
    RemoteScope,
    TargetProfile,
)


class RemotePathDenied(Exception):
    """Raised when a remote path is outside the allowed scope."""


class RemotePathPolicy:
    """Authorize individual remote file paths against a service's allowed roots.

    Uses lexical containment checks (``PurePosixPath.is_relative_to``) rather
    than string-prefix matching to prevent prefix-collision and parent-traversal
    attacks.
    """

    def __init__(self, registration: ServiceRegistration) -> None:
        self._host_roots = registration.allowed_host_paths
        self._container_roots = registration.allowed_container_paths
        self._container_names = frozenset(registration.container_names)

    async def authorize(
        self,
        scope: RemoteScope,
        path: PurePosixPath,
        *,
        write: bool,  # noqa: ARG002
        transport: RemoteTransport | None = None,
    ) -> PurePosixPath:
        """Return the *canonical* (resolved) path if it falls within an allowed root.

        Raises ``RemotePathDenied`` for any of:
        - Non-absolute path
        - Path containing ``..`` components
        - Path not contained in an allowed root (lexical check)
        - Symlink detected (Phase 2 -- closes replacement races)
        - Container scope referencing an unknown container name

        Parameters
        ----------
        scope:
            The execution scope (host or container).
        path:
            The requested file path.
        write:
            Whether this is a write operation.  Currently unused but reserved
            for future parent-directory validation on new-file writes.
        transport:
            When provided, the canonical path is resolved via ``realpath()``
            and checked for symlinks.  When ``None`` only lexical checks are
            performed (container scope typically omits the transport).
        """
        # --- lexical checks (always performed) ---

        if not path.is_absolute():
            raise RemotePathDenied(f"path is not absolute: {path}")

        if ".." in path.parts:
            raise RemotePathDenied(f"path contains '..': {path}")

        # Determine the allowed roots based on scope kind.
        if isinstance(scope, ContainerScope):
            if scope.container not in self._container_names:
                raise RemotePathDenied(
                    f"unknown container: {scope.container}"
                )
            allowed_roots = self._container_roots
        elif isinstance(scope, HostScope):
            allowed_roots = self._host_roots
        else:
            raise RemotePathDenied(f"unsupported scope kind: {scope.kind}")

        # Lexical containment -- ``is_relative_to`` prevents prefix collisions.
        root = self._find_root(path, allowed_roots)
        if root is None:
            raise RemotePathDenied(
                f"path {path} is outside allowed roots {allowed_roots}"
            )

        # --- canonical checks (only when a transport is available) ---

        if transport is not None:
            canonical = await transport.realpath(path)

            if not canonical.is_relative_to(root):
                raise RemotePathDenied(
                    f"resolved path {canonical} escapes allowed root {root}"
                )

            meta: FileMetadata = await transport.lstat(canonical)
            if meta.is_symlink:
                raise RemotePathDenied(
                    f"symlink detected at {canonical}"
                )

            return canonical

        return path

    # --- helpers ---

    @staticmethod
    def _find_root(
        path: PurePosixPath,
        roots: tuple[PurePosixPath, ...],
    ) -> PurePosixPath | None:
        """Return the first root that lexically contains *path*, or ``None``."""
        for root in roots:
            if path.is_relative_to(root):
                return root
        return None


class RemoteOperationPolicy:
    """Allow investigation by default; require explicit controls for writes.

    This is intentionally independent of LangChain/LangGraph.  The same gate
    protects actions triggered by the dashboard, an API client, or an agent.
    """

    def __init__(self, targets: Mapping[str, TargetProfile]) -> None:
        self._targets = dict(targets)

    def evaluate(self, action: RemoteAction) -> PolicyDecision:
        target = self._targets.get(action.target_id)
        if target is None:
            return PolicyDecision(allowed=False, reason="target is not registered")
        if action.service not in target.allowed_services:
            return PolicyDecision(
                allowed=False,
                reason="service is not allowlisted for this target",
            )

        if action.operation in READ_ONLY_OPERATIONS:
            return PolicyDecision(allowed=True, reason="allowlisted read-only operation")

        if action.operation is OperationKind.PROPOSE_CHANGE:
            return PolicyDecision(
                allowed=True,
                reason="proposal may be generated but cannot modify the target",
                required_gates=("human_review",),
            )

        if action.operation is OperationKind.APPLY_CHANGE:
            if action.change_controls is None:
                return PolicyDecision(
                    allowed=False,
                    reason="a change needs backup, approval, verification, and rollback controls",
                    required_gates=(
                        "backup",
                        "human_approval",
                        "verification_plan",
                        "rollback_plan",
                    ),
                )
            return PolicyDecision(
                allowed=True,
                reason="approved reversible change; adapter must verify after execution",
                required_gates=("post_change_verification",),
            )

        return PolicyDecision(allowed=False, reason="operation is not supported")
