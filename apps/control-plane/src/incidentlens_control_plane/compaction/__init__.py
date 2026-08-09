"""Session memory compaction and persistence.

Deterministic session memory projection and persistence for incident investigations.
No model calls required — pure projection from state and messages.
"""

from incidentlens_control_plane.compaction.domain import (
    CompactionConfig,
    CompactionError,
    CompactionLimits,
    CompactionOutcome,
    CompactionResult,
    ToolOutputReference,
)
from incidentlens_control_plane.compaction.micro import (
    MessageGroup,
    micro_compact,
    snip_middle,
)
from incidentlens_control_plane.compaction.session import (
    SessionMemorySnapshot,
    SessionMemoryStore,
    SessionMemoryValidation,
    project_session_memory,
    validate_session_memory,
)
from incidentlens_control_plane.compaction.tool_budget import (
    ToolOutputStore,
    persist_oversized_tool_results,
)

__all__ = [
    "CompactionConfig",
    "CompactionError",
    "CompactionLimits",
    "CompactionOutcome",
    "CompactionResult",
    "MessageGroup",
    "SessionMemorySnapshot",
    "SessionMemoryStore",
    "SessionMemoryValidation",
    "ToolOutputReference",
    "ToolOutputStore",
    "micro_compact",
    "persist_oversized_tool_results",
    "project_session_memory",
    "snip_middle",
    "validate_session_memory",
]
