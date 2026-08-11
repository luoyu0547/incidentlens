"""Safety boundary for remote infrastructure investigation and changes.

The agent selects typed operations from this package.  It never receives a
general-purpose shell or SSH tool.
"""

from incidentlens_control_plane.remote_ops.asyncssh_adapter import AsyncSshTransportFactory
from incidentlens_control_plane.remote_ops.policy import RemoteOperationPolicy
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
    "CommandResult",
    "ContainerScope",
    "ContainerSession",
    "DockerActionKind",
    "DockerActionRequest",
    "FileEditRequest",
    "FileMetadata",
    "FileMutationRequest",
    "FileOperationKind",
    "FileOperationRequest",
    "FileWriteRequest",
    "HostScope",
    "HostSession",
    "OperationKind",
    "OperationRisk",
    "RemoteAction",
    "RemoteConnectionError",
    "RemoteError",
    "RemoteOperationPolicy",
    "RemotePathError",
    "RemoteProcess",
    "RemoteScope",
    "RemoteTimeoutError",
    "RemoteTransport",
    "RemoteTransportFactory",
    "RuntimeKind",
    "ScopeKind",
    "SessionManager",
    "ShellRequest",
    "TargetProfile",
    "TextReplacement",
]
