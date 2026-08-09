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
    SummaryResult,
    ToolOutputReference,
    TranscriptRecord,
)
from incidentlens_control_plane.compaction.middleware import (
    CompactionMiddleware,
    TranscriptStore,
    is_prompt_too_long,
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
from incidentlens_control_plane.compaction.summary import (
    SummaryCircuitBreaker,
    summarize_history,
)
from incidentlens_control_plane.compaction.tool_budget import (
    ToolOutputStore,
    persist_oversized_tool_results,
)

__all__ = [
    "CompactionConfig",
    "CompactionError",
    "CompactionLimits",
    "CompactionMiddleware",
    "CompactionOutcome",
    "CompactionResult",
    "MessageGroup",
    "SessionMemorySnapshot",
    "SessionMemoryStore",
    "SessionMemoryValidation",
    "SummaryCircuitBreaker",
    "SummaryResult",
    "ToolOutputReference",
    "ToolOutputStore",
    "TranscriptRecord",
    "TranscriptStore",
    "is_prompt_too_long",
    "micro_compact",
    "persist_oversized_tool_results",
    "project_session_memory",
    "snip_middle",
    "summarize_history",
    "validate_session_memory",
]
