"""Safety boundary for remote infrastructure investigation and changes.

The agent selects typed operations from this package.  It never receives a
general-purpose shell or SSH tool.
"""

from incidentlens_control_plane.remote_ops.policy import RemoteOperationPolicy
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
    "ChangeControls",
    "ChangeSetRequest",
    "ContainerScope",
    "DockerActionKind",
    "DockerActionRequest",
    "FileEditRequest",
    "FileMutationRequest",
    "FileOperationKind",
    "FileOperationRequest",
    "FileWriteRequest",
    "HostScope",
    "OperationKind",
    "OperationRisk",
    "RemoteAction",
    "RemoteOperationPolicy",
    "RemoteScope",
    "RuntimeKind",
    "ScopeKind",
    "ShellRequest",
    "TargetProfile",
    "TextReplacement",
]
