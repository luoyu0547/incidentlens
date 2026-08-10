"""Safety boundary for remote infrastructure investigation and changes.

The agent selects typed operations from this package.  It never receives a
general-purpose shell or SSH tool.
"""

from incidentlens_control_plane.remote_ops.policy import RemoteOperationPolicy
from incidentlens_control_plane.remote_ops.types import (
    ChangeControls,
    OperationKind,
    RemoteAction,
    TargetProfile,
)

__all__ = [
    "ChangeControls",
    "OperationKind",
    "RemoteAction",
    "RemoteOperationPolicy",
    "TargetProfile",
]
