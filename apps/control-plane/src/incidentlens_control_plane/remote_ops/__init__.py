"""Safety boundary for remote infrastructure investigation and changes.

The agent selects typed operations from this package.  It never receives a
general-purpose shell or SSH tool.
"""

from incidentlens_control_plane.remote_ops.asyncssh_adapter import AsyncSshTransportFactory
from incidentlens_control_plane.remote_ops.files import (
    ContainerFileOperationUnsupported,
    FileReadResult,
    RemoteFileError,
    RemoteFileTools,
    SearchMatch,
)
from incidentlens_control_plane.remote_ops.gateway import (
    CommandForbidden,
    Gateway,
    RemoteToolGateway,
    ShellResult,
)
from incidentlens_control_plane.remote_ops.policy import (
    RemoteOperationPolicy,
    RemotePathDenied,
    RemotePathPolicy,
)
from incidentlens_control_plane.remote_ops.sessions import (
    ContainerSession,
    HostSession,
    SessionManager,
)
from incidentlens_control_plane.remote_ops.transport import (
    CommandResult,
    FileMetadata,
    RemoteConnectionError,
    RemoteError,
    RemotePathError,
    RemoteProcess,
    RemoteTimeoutError,
    RemoteTransport,
    RemoteTransportFactory,
)
from incidentlens_control_plane.remote_ops.types import (
    ChangeControls,
    ChangeSetRequest,
    ContainerScope,
    DockerActionKind,
    DockerActionRequest,
    FileEditRequest,
    FileMutationRequest,
    FileOperationKind,
    FileOperationRequest,
    FileWriteRequest,
    HostScope,
    OperationKind,
    OperationRisk,
    RemoteAction,
    RemoteScope,
    RuntimeKind,
    ScopeKind,
    ShellRequest,
    TargetProfile,
    TextReplacement,
)

__all__ = [
    "AsyncSshTransportFactory",
    "ChangeControls",
    "ChangeSetRequest",
    "CommandForbidden",
    "CommandResult",
    "ContainerFileOperationUnsupported",
    "ContainerScope",
    "ContainerSession",
    "DockerActionKind",
    "DockerActionRequest",
    "FileEditRequest",
    "FileMetadata",
    "FileMutationRequest",
    "FileOperationKind",
    "FileOperationRequest",
    "FileReadResult",
    "FileWriteRequest",
    "Gateway",
    "HostScope",
    "HostSession",
    "OperationKind",
    "OperationRisk",
    "RemoteAction",
    "RemoteConnectionError",
    "RemoteError",
    "RemoteFileError",
    "RemoteFileTools",
    "RemoteOperationPolicy",
    "RemotePathDenied",
    "RemotePathError",
    "RemotePathPolicy",
    "RemoteProcess",
    "RemoteScope",
    "RemoteTimeoutError",
    "RemoteToolGateway",
    "RemoteTransport",
    "RemoteTransportFactory",
    "RuntimeKind",
    "ScopeKind",
    "SearchMatch",
    "SessionManager",
    "ShellRequest",
    "ShellResult",
    "TargetProfile",
    "TextReplacement",
]
