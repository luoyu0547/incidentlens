"""Append-only transcript operations and tool-use/result grouping.

The transcript is the model-visible message history for one agent run.  It is
append-only: ``sequence`` is assigned by the writer and a run can never
overwrite or renumber a message.  ``group_messages`` pairs every assistant
``ToolUseBlock`` with the immediately following ``ToolResultBlock`` message so
compaction, retry and recovery always operate on whole tool exchanges and never
split a tool request from its result.
"""

from __future__ import annotations

from dataclasses import dataclass

from incidentlens_control_plane.investigation.store import InvestigationStore
from incidentlens_control_plane.investigation.types import (
    ToolResultBlock,
    ToolUseBlock,
    TranscriptMessage,
)


class UnpairedToolMessage(Exception):
    """Raised when a tool use has no matching following tool-result message."""


@dataclass(frozen=True, slots=True)
class MessageGroup:
    """One atomic slice of a transcript.

    A group is either a single non-tool message or an assistant tool-use message
    paired with the immediately following tool-result message.
    """

    messages: tuple[TranscriptMessage, ...]


def group_messages(messages: tuple[TranscriptMessage, ...]) -> tuple[MessageGroup, ...]:
    """Group messages so every tool use is paired with its matching result.

    A message that contains ``ToolUseBlock`` values must be followed by a
    message whose ``ToolResultBlock`` ids match exactly; anything else raises
    ``UnpairedToolMessage`` so a corrupt or truncated tail is surfaced instead
    of silently mis-compacted.
    """
    groups: list[MessageGroup] = []
    index = 0
    while index < len(messages):
        current = messages[index]
        tool_ids = {
            block.tool_call_id for block in current.blocks if isinstance(block, ToolUseBlock)
        }
        if tool_ids:
            if index + 1 >= len(messages):
                raise UnpairedToolMessage("tool use has no following result")
            following = messages[index + 1]
            result_ids = {
                block.tool_call_id
                for block in following.blocks
                if isinstance(block, ToolResultBlock)
            }
            if result_ids != tool_ids:
                raise UnpairedToolMessage("tool result ids do not match tool use ids")
            groups.append(MessageGroup((current, following)))
            index += 2
            continue
        groups.append(MessageGroup((current,)))
        index += 1
    return tuple(groups)


class TranscriptService:
    """Persist transcript messages and group them for context materialization."""

    def __init__(self, store: InvestigationStore) -> None:
        self._store = store

    def append_message(self, message: TranscriptMessage) -> TranscriptMessage:
        """Persist one transcript message; raise TranscriptConflict on a duplicate."""
        return self._store.append_transcript_message(message)

    def group_messages(
        self, agent_run_id: str, *, after: int = 0
    ) -> tuple[MessageGroup, ...]:
        """Return message groups for a run whose sequence is greater than ``after``.

        A compact boundary's ``through_sequence`` is the usual ``after`` value so
        a resumed context replays only the messages written since the boundary.
        """
        messages = tuple(
            message
            for message in self._store.list_transcript_messages(agent_run_id)
            if message.sequence > after
        )
        return group_messages(messages)


__all__ = [
    "MessageGroup",
    "TranscriptService",
    "UnpairedToolMessage",
    "group_messages",
]
