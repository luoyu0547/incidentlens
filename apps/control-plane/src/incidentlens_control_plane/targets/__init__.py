"""Target product facade over the authoritative ProjectRegistry."""

from incidentlens_control_plane.targets.service import (
    TargetDeleteBlocked,
    TargetService,
)
from incidentlens_control_plane.targets.store import (
    TargetAlreadyExists,
    TargetNotFound,
    TargetStore,
    TargetVersionConflict,
)
from incidentlens_control_plane.targets.types import (
    TargetBinding,
    TargetCreate,
    TargetPatch,
    TargetServiceView,
    TargetView,
)

__all__ = [
    "TargetAlreadyExists",
    "TargetBinding",
    "TargetCreate",
    "TargetDeleteBlocked",
    "TargetNotFound",
    "TargetPatch",
    "TargetService",
    "TargetServiceView",
    "TargetStore",
    "TargetVersionConflict",
    "TargetView",
]
