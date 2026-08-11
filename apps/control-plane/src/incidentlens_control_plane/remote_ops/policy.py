"""Policy gate that must run before an adapter can contact a remote target."""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict

from incidentlens_control_plane.project_registry.types import ServiceRegistration
from incidentlens_control_plane.remote_ops.transport import FileMetadata, RemoteTransport
from incidentlens_control_plane.remote_ops.types import (
    READ_ONLY_OPERATIONS,
    ContainerScope,
    HostScope,
    OperationKind,
    OperationRisk,
    PolicyDecision,
    RemoteAction,
    RemoteScope,
    ShellRequest,
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

    def contains(self, path: PurePosixPath, scope: RemoteScope) -> bool:
        """Return whether a (canonicalized) path stays within an allowed root."""
        if isinstance(scope, ContainerScope):
            roots = self._container_roots
        elif isinstance(scope, HostScope):
            roots = self._host_roots
        else:
            return False
        return any(path.is_relative_to(root) for root in roots)

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


# ---------------------------------------------------------------------------
# Shell command policy
# ---------------------------------------------------------------------------


class ShellPolicyDecision(BaseModel):
    """Machine- and UI-readable decision for shell command classification."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    risk: OperationRisk
    reason: str
    approval_can_override: bool
    canonical_operation: str


# Patterns that indicate dangerous command features
_NUL_RE = re.compile(r"\x00")
_NEWLINE_RE = re.compile(r"\n")
_CMD_SUBSTITUTION_RE = re.compile(r"\$\(|`")
_EVAL_RE = re.compile(r"\beval\b")
_XARGS_RM_RE = re.compile(r"\bxargs\b.*\brm\b")
_FIND_DELETE_RE = re.compile(r"\bfind\b.*-delete")


def _parse_command(command: str) -> list[str] | None:
    """Parse a command string into tokens, returning None if dangerous features detected.

    Rejects:
    - NUL bytes
    - Newlines (command injection)
    - Malformed quoting (ValueError from shlex)
    - Command substitution ($() or backticks)
    - eval
    - xargs rm
    - find -delete
    """
    # Check for NUL bytes
    if _NUL_RE.search(command):
        return None

    # Check for newlines (command injection)
    if _NEWLINE_RE.search(command):
        return None

    # Check for command substitution
    if _CMD_SUBSTITUTION_RE.search(command):
        return None

    # Check for eval
    if _EVAL_RE.search(command):
        return None

    # Check for xargs rm
    if _XARGS_RM_RE.search(command):
        return None

    # Check for find -delete
    if _FIND_DELETE_RE.search(command):
        return None

    try:
        tokens = shlex.split(command)
    except ValueError:
        return None

    return tokens


def _strip_prefixes(tokens: list[str]) -> list[str]:
    """Strip leading sudo, env NAME=value, and command prefixes."""
    result = list(tokens)
    while result:
        token = result[0]
        if token == "sudo":
            result = result[1:]
        elif token == "command":
            result = result[1:]
        elif token == "env":
            # Skip env and any NAME=value pairs
            result = result[1:]
            while result and "=" in result[0]:
                result = result[1:]
        else:
            break
    return result


def _has_recursive_force_rm(tokens: list[str]) -> bool:
    """Check if command is a recursive + force rm.

    Scans *every* token so any ordering of paths and flags is caught, e.g.
    ``rm /opt/app -rf`` and ``sudo rm /opt/app --force -r``.  A sequence is
    forbidden when it contains both a recursive (``r``/``R``) and a force
    (``f``) short flag, or the matching long flags, in any positions.
    """
    stripped = _strip_prefixes(tokens)
    if not stripped:
        return False

    cmd = stripped[0]
    if cmd != "rm":
        return False

    has_recursive = False
    has_force = False
    for token in stripped[1:]:
        if token.startswith("--"):
            # Long flags: --recursive, --force (prefix match stays conservative
            # for combined or abbreviated forms).
            if token.startswith("--recursive"):
                has_recursive = True
            elif token.startswith("--force"):
                has_force = True
            continue
        if token.startswith("-") and token != "-":
            short_flags = token[1:]
            if any(f in short_flags for f in ("r", "R")):
                has_recursive = True
            if "f" in short_flags:
                has_force = True

    return has_recursive and has_force


def _is_path_authorized(
    path: str,
    service: ServiceRegistration,
) -> bool:
    """Check if a path is within allowed roots for the service."""
    try:
        pp = PurePosixPath(path)
        if not pp.is_absolute():
            return False
        if ".." in pp.parts:
            return False

        # Check against host roots
        for root in service.allowed_host_paths:
            if pp.is_relative_to(root):
                return True

        # Check against container roots
        for root in service.allowed_container_paths:
            if pp.is_relative_to(root):
                return True

        return False
    except Exception:
        return False


class CommandPolicy:
    """Conservative command classifier for shell execution.

    The policy is deliberately conservative: parsing uncertainty becomes
    APPROVAL_REQUIRED, never automatic execution.
    """

    def evaluate(
        self,
        request: ShellRequest,
        service: ServiceRegistration,
    ) -> ShellPolicyDecision:
        """Classify a shell command and return a policy decision."""
        command = request.command

        # Parse the command
        tokens = _parse_command(command)
        if tokens is None:
            return ShellPolicyDecision(
                risk=OperationRisk.FORBIDDEN,
                reason="command contains dangerous features "
                "(NUL, newlines, command substitution, eval, or malformed syntax)",
                approval_can_override=False,
                canonical_operation=command,
            )

        # Check for recursive + force rm (always forbidden)
        if _has_recursive_force_rm(tokens):
            return ShellPolicyDecision(
                risk=OperationRisk.FORBIDDEN,
                reason="recursive force rm is permanently forbidden",
                approval_can_override=False,
                canonical_operation=command,
            )

        # Strip prefixes to identify the executable
        stripped = _strip_prefixes(tokens)
        if not stripped:
            return ShellPolicyDecision(
                risk=OperationRisk.APPROVAL_REQUIRED,
                reason="empty command after stripping prefixes",
                approval_can_override=True,
                canonical_operation=command,
            )

        executable = stripped[0]

        # --- Automatic reads ---
        if executable == "pwd":
            return ShellPolicyDecision(
                risk=OperationRisk.AUTO_READ,
                reason="pwd is a safe read-only command",
                approval_can_override=True,
                canonical_operation=command,
            )

        if executable == "docker" and len(stripped) > 1:
            docker_subcmd = stripped[1]

            # docker ps is automatic
            if docker_subcmd == "ps":
                return ShellPolicyDecision(
                    risk=OperationRisk.AUTO_READ,
                    reason="docker ps is a safe read-only command",
                    approval_can_override=True,
                    canonical_operation=command,
                )

            # docker inspect is automatic for registered containers
            if docker_subcmd == "inspect" and len(stripped) > 2:
                target_container = stripped[2]
                if target_container in service.container_names:
                    return ShellPolicyDecision(
                        risk=OperationRisk.AUTO_READ,
                        reason=f"docker inspect {target_container} is a registered container",
                        approval_can_override=True,
                        canonical_operation=command,
                    )

            # docker logs is automatic for registered containers
            if docker_subcmd == "logs" and len(stripped) > 2:
                target_container = stripped[2]
                if target_container in service.container_names:
                    return ShellPolicyDecision(
                        risk=OperationRisk.AUTO_READ,
                        reason=f"docker logs {target_container} is a registered container",
                        approval_can_override=True,
                        canonical_operation=command,
                    )

            # docker compose ps/logs/config are automatic for registered services
            if docker_subcmd == "compose" and len(stripped) > 2:
                compose_subcmd = stripped[2]
                if compose_subcmd in ("ps", "logs", "config"):
                    return ShellPolicyDecision(
                        risk=OperationRisk.AUTO_READ,
                        reason=f"docker compose {compose_subcmd} is a safe read-only command",
                        approval_can_override=True,
                        canonical_operation=command,
                    )

            # docker compose up/down need approval
            if docker_subcmd == "compose" and len(stripped) > 2:
                compose_subcmd = stripped[2]
                if compose_subcmd in ("up", "down"):
                    return ShellPolicyDecision(
                        risk=OperationRisk.APPROVAL_REQUIRED,
                        reason=f"docker compose {compose_subcmd} modifies system state",
                        approval_can_override=True,
                        canonical_operation=command,
                    )

            # docker restart/rm need approval
            if docker_subcmd in ("restart", "rm"):
                return ShellPolicyDecision(
                    risk=OperationRisk.APPROVAL_REQUIRED,
                    reason=f"docker {docker_subcmd} modifies container state",
                    approval_can_override=True,
                    canonical_operation=command,
                )

        # --- File read commands (automatic when every path is authorized) ---
        if executable in ("ls", "cat", "stat"):
            # Every non-flag argument must be an authorized absolute path.  A
            # relative argument (e.g. "../secret") cannot be authorized against
            # the registered roots, so the command is not automatic.
            if len(stripped) > 1:
                path_args = [arg for arg in stripped[1:] if not arg.startswith("-")]
                all_paths_valid = all(
                    _is_path_authorized(arg, service) for arg in path_args
                )
                if all_paths_valid:
                    return ShellPolicyDecision(
                        risk=OperationRisk.AUTO_READ,
                        reason=f"{executable} with valid paths",
                        approval_can_override=True,
                        canonical_operation=command,
                    )

        # --- sed -i is forbidden (must use remote_edit) ---
        if executable == "sed" and "-i" in stripped:
            return ShellPolicyDecision(
                risk=OperationRisk.FORBIDDEN,
                reason="use remote_edit so mandatory backups cannot be bypassed",
                approval_can_override=False,
                canonical_operation=command,
            )

        # --- Package managers need approval ---
        if executable in ("apt-get", "apt", "yum", "dnf", "pip", "npm"):
            return ShellPolicyDecision(
                risk=OperationRisk.APPROVAL_REQUIRED,
                reason=f"{executable} modifies system packages",
                approval_can_override=True,
                canonical_operation=command,
            )

        # --- Unclassified commands require approval ---
        return ShellPolicyDecision(
            risk=OperationRisk.APPROVAL_REQUIRED,
            reason=f"unclassified command {executable!r} requires approval",
            approval_can_override=True,
            canonical_operation=command,
        )
