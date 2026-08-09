"""Micro compaction and middle snipping layers.

Provides message-level compaction without model calls:
- Micro compaction: Replaces old completed tool results, keeping recent ones
- Middle snipping: Removes complete middle groups while preserving boundaries

No model calls required - pure message transformation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field

from incidentlens_control_plane.compaction.domain import (
    CompactionLimits,
    CompactionOutcome,
    CompactionResult,
)


@dataclass
class MessageGroup:
    """A group of messages consisting of an AIMessage with tool_calls
    and their corresponding ToolMessage responses.

    This groups tool invocations together so they can be compacted
    as a unit (never split).
    """

    index: int  # Starting index in the message list
    ai_message: Mapping[str, Any]  # The AIMessage with tool_calls
    tool_messages: list[Mapping[str, Any]] = field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        """Check if all tool calls have received responses."""
        if not self.tool_messages:
            return False
        tool_call_ids = self._get_tool_call_ids()
        response_ids = {msg.get("tool_call_id") for msg in self.tool_messages}
        return tool_call_ids.issubset(response_ids)

    def _get_tool_call_ids(self) -> set[str]:
        """Extract tool_call_ids from the AIMessage."""
        tool_calls = self.ai_message.get("tool_calls", [])
        if not tool_calls:
            return set()
        return {tc.get("id", "") for tc in tool_calls}

    @property
    def total_size_bytes(self) -> int:
        """Estimate total size of the group in bytes."""
        size = len(str(self.ai_message).encode("utf-8"))
        for msg in self.tool_messages:
            size += len(str(msg).encode("utf-8"))
        return size


def _group_messages(messages: Sequence[Mapping[str, Any]]) -> list[MessageGroup]:
    """Group messages into AIMessage + ToolMessage pairs.

    Scans through messages and groups each AIMessage with tool_calls
    with its subsequent ToolMessage responses.

    Args:
        messages: The message history.

    Returns:
        List of MessageGroup objects.
    """
    groups: list[MessageGroup] = []
    current_group: MessageGroup | None = None

    for i, msg in enumerate(messages):
        role = msg.get("role", "")

        if role == "assistant" and msg.get("tool_calls"):
            # Start a new group
            current_group = MessageGroup(index=i, ai_message=msg)
            groups.append(current_group)
        elif role == "tool" and current_group is not None:
            # Add tool response to current group
            current_group.tool_messages.append(msg)
        elif role in ("user", "system"):
            # Non-tool message resets the current group
            current_group = None

    return groups


def micro_compact(
    messages: Sequence[Mapping[str, Any]],
    keep_recent: int = 3,
    limits: CompactionLimits | None = None,
) -> CompactionResult:
    """Compact messages by replacing old completed tool results.

    This function:
    1. Groups messages into AIMessage + ToolMessage pairs
    2. Identifies complete groups (all tool calls have responses)
    3. Keeps the N most recent complete groups
    4. Replaces older complete groups with a summary marker

    Args:
        messages: The message history to compact.
        keep_recent: Number of recent complete groups to keep (default: 3).
        limits: Optional limits configuration. Uses defaults if None.

    Returns:
        CompactionResult with details of the compaction.
    """
    if limits is None:
        limits = CompactionLimits()

    if keep_recent <= 0:
        keep_recent = limits.keep_recent_results

    # Convert to mutable list
    msg_list = list(messages)

    # Group messages
    groups = _group_messages(msg_list)

    if not groups:
        return CompactionResult(
            outcome=CompactionOutcome.SKIPPED,
            messages_removed=0,
            messages_remaining=len(messages),
            details={"reason": "no_groups_found"},
        )

    # Find complete groups
    complete_groups = [g for g in groups if g.is_complete]

    if len(complete_groups) <= keep_recent:
        return CompactionResult(
            outcome=CompactionOutcome.SKIPPED,
            messages_removed=0,
            messages_remaining=len(messages),
            details={
                "reason": "not_enough_complete_groups",
                "complete_groups": len(complete_groups),
                "keep_recent": keep_recent,
            },
        )

    # Identify groups to compact (all but keep_recent most recent)
    groups_to_compact = complete_groups[:-keep_recent] if keep_recent > 0 else complete_groups

    # Build new message list
    new_messages: list[Mapping[str, Any]] = []
    compacted_indices: set[int] = set()

    # Collect indices to compact
    for group in groups_to_compact:
        # Mark the AI message and all tool messages for removal
        compacted_indices.add(group.index)
        for tool_msg in group.tool_messages:
            # Find index of this tool message
            for i, msg in enumerate(msg_list):
                if msg is tool_msg:
                    compacted_indices.add(i)
                    break

    # Build replacement marker
    removed_count = len(compacted_indices)
    total_tool_results_removed = sum(len(g.tool_messages) for g in groups_to_compact)

    # Create compacted message list
    skip_until: int | None = None
    for i, msg in enumerate(msg_list):
        if skip_until is not None and i <= skip_until:
            continue

        if i in compacted_indices:
            # Find the group this message belongs to
            for group in groups_to_compact:
                if group.index == i:
                    # Insert a summary marker
                    marker = {
                        "role": "system",
                        "content": (
                            f"[Compacted: {len(group.tool_messages)} tool result(s) removed. "
                            f"Original tool calls are no longer available.]"
                        ),
                    }
                    new_messages.append(marker)
                    # Skip tool messages in this group
                    if group.tool_messages:
                        last_tool_idx = msg_list.index(group.tool_messages[-1])
                        skip_until = last_tool_idx
                    break
        else:
            new_messages.append(msg)

    return CompactionResult(
        outcome=CompactionOutcome.SUCCESS,
        messages_removed=removed_count,
        messages_remaining=len(new_messages),
        details={
            "groups_compacted": len(groups_to_compact),
            "tool_results_removed": total_tool_results_removed,
            "keep_recent": keep_recent,
        },
    )


def snip_middle(
    messages: Sequence[Mapping[str, Any]],
    target_tokens: int | None = None,
    limits: CompactionLimits | None = None,
) -> CompactionResult:
    """Snip complete middle groups to reduce context size.

    This function:
    1. Groups messages into AIMessage + ToolMessage pairs
    2. Preserves the initial objective (first user message)
    3. Preserves the most recent groups
    4. Removes complete middle groups until under target size

    Args:
        messages: The message history to snip.
        target_tokens: Target token count to snip to. If None, uses limits.
        limits: Optional limits configuration. Uses defaults if None.

    Returns:
        CompactionResult with details of the snipping.
    """
    if limits is None:
        limits = CompactionLimits()

    if target_tokens is None:
        target_tokens = limits.max_snip_tokens

    # Convert to mutable list
    msg_list = list(messages)

    # Calculate current token estimate (rough: 4 chars per token)
    total_chars = sum(len(str(m.get("content", ""))) for m in msg_list)
    current_tokens = total_chars // 4

    if current_tokens <= target_tokens:
        return CompactionResult(
            outcome=CompactionOutcome.SKIPPED,
            messages_removed=0,
            messages_remaining=len(messages),
            details={
                "reason": "already_under_target",
                "current_tokens": current_tokens,
                "target_tokens": target_tokens,
            },
        )

    # Group messages
    groups = _group_messages(msg_list)

    if len(groups) <= 2:
        return CompactionResult(
            outcome=CompactionOutcome.SKIPPED,
            messages_removed=0,
            messages_remaining=len(messages),
            details={
                "reason": "not_enough_groups_to_snip",
                "group_count": len(groups),
            },
        )

    # Preserve first and last groups
    # First group contains the objective
    # Last group is the most recent context
    preserve_first = 1  # Keep first group
    preserve_last = 1   # Keep last group

    # Middle groups are candidates for snipping
    middle_groups = groups[preserve_first:-preserve_last] if preserve_last > 0 else groups[preserve_first:]

    # Only snip complete middle groups
    snip_candidates = [g for g in middle_groups if g.is_complete]

    if not snip_candidates:
        return CompactionResult(
            outcome=CompactionOutcome.SKIPPED,
            messages_removed=0,
            messages_remaining=len(messages),
            details={"reason": "no_complete_middle_groups"},
        )

    # Snip from oldest to newest until under target
    messages_to_remove: set[int] = set()
    tokens_removed = 0

    for group in snip_candidates:
        if current_tokens - tokens_removed <= target_tokens:
            break

        # Mark this group for removal
        messages_to_remove.add(group.index)
        for tool_msg in group.tool_messages:
            for i, msg in enumerate(msg_list):
                if msg is tool_msg:
                    messages_to_remove.add(i)
                    break

        # Estimate tokens in this group
        group_tokens = group.total_size_bytes // 4
        tokens_removed += group_tokens

    if not messages_to_remove:
        return CompactionResult(
            outcome=CompactionOutcome.SKIPPED,
            messages_removed=0,
            messages_remaining=len(messages),
            details={"reason": "no_groups_removed"},
        )

    # Build new message list with marker
    new_messages: list[Mapping[str, Any]] = []
    removed_count = 0
    removed_tool_results = 0

    # Insert marker at the beginning of the snipped region
    marker_inserted = False
    for i, msg in enumerate(msg_list):
        if i in messages_to_remove:
            if not marker_inserted:
                # Count removed tool results
                for group in snip_candidates:
                    if group.index in messages_to_remove:
                        removed_tool_results += len(group.tool_messages)

                # Insert bounded marker
                marker = {
                    "role": "system",
                    "content": (
                        f"[Snipped: {len(messages_to_remove)} messages removed from middle. "
                        f"Removed {removed_tool_results} tool results. "
                        f"Original context is no longer available.]"
                    ),
                }
                new_messages.append(marker)
                marker_inserted = True
            removed_count += 1
        else:
            new_messages.append(msg)

    return CompactionResult(
        outcome=CompactionOutcome.SUCCESS,
        messages_removed=removed_count,
        messages_remaining=len(new_messages),
        details={
            "groups_snipped": len([g for g in snip_candidates if g.index in messages_to_remove]),
            "tool_results_removed": removed_tool_results,
            "tokens_removed": tokens_removed,
            "target_tokens": target_tokens,
        },
    )
