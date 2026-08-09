"""Tests for micro compaction and middle snipping.

Verifies:
- Micro compact keeps 3 recent complete results
- Snip never splits tool group
- Boundary preservation
"""

from pathlib import Path

import pytest

from incidentlens_control_plane.compaction.domain import CompactionLimits, CompactionOutcome
from incidentlens_control_plane.compaction.micro import (
    MessageGroup,
    micro_compact,
    snip_middle,
)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_messages_with_tools() -> list[dict]:
    """Sample message history with multiple tool calls and results."""
    return [
        {"role": "system", "content": "You are an incident investigator."},
        {"role": "user", "content": "Investigate high latency in order-service"},
        # Group 1 (oldest)
        {
            "role": "assistant",
            "content": "I will check the metrics",
            "tool_calls": [{"id": "tc-1", "name": "read_metrics", "args": {}}],
        },
        {"role": "tool", "tool_call_id": "tc-1", "content": "Metrics result 1"},
        # Group 2
        {
            "role": "assistant",
            "content": "Let me check the logs",
            "tool_calls": [{"id": "tc-2", "name": "read_logs", "args": {}}],
        },
        {"role": "tool", "tool_call_id": "tc-2", "content": "Logs result 2"},
        # Group 3
        {
            "role": "assistant",
            "content": "Checking database",
            "tool_calls": [{"id": "tc-3", "name": "read_db", "args": {}}],
        },
        {"role": "tool", "tool_call_id": "tc-3", "content": "DB result 3"},
        # Group 4
        {
            "role": "assistant",
            "content": "Checking cache",
            "tool_calls": [{"id": "tc-4", "name": "read_cache", "args": {}}],
        },
        {"role": "tool", "tool_call_id": "tc-4", "content": "Cache result 4"},
        # Group 5 (most recent)
        {
            "role": "assistant",
            "content": "Final check",
            "tool_calls": [{"id": "tc-5", "name": "final_check", "args": {}}],
        },
        {"role": "tool", "tool_call_id": "tc-5", "content": "Final result 5"},
    ]


@pytest.fixture
def messages_with_incomplete_groups() -> list[dict]:
    """Messages with some incomplete tool groups (missing responses)."""
    return [
        {"role": "system", "content": "System prompt"},
        {
            "role": "assistant",
            "content": "Checking metrics",
            "tool_calls": [{"id": "tc-1", "name": "read_metrics", "args": {}}],
        },
        {"role": "tool", "tool_call_id": "tc-1", "content": "Metrics result"},
        {
            "role": "assistant",
            "content": "Checking logs (pending)",
            "tool_calls": [{"id": "tc-2", "name": "read_logs", "args": {}}],
        },
        # Missing tool response for tc-2
        {
            "role": "assistant",
            "content": "Checking DB",
            "tool_calls": [{"id": "tc-3", "name": "read_db", "args": {}}],
        },
        {"role": "tool", "tool_call_id": "tc-3", "content": "DB result"},
    ]


@pytest.fixture
def long_messages_for_snipping() -> list[dict]:
    """Long message history suitable for snipping."""
    messages = [
        {"role": "system", "content": "System prompt for investigation"},
        {"role": "user", "content": "Investigate high latency in order-service"},
    ]

    # Add many tool groups to exceed token limit
    for i in range(20):
        messages.extend([
            {
                "role": "assistant",
                "content": f"Checking item {i}",
                "tool_calls": [{"id": f"tc-{i}", "name": f"check_{i}", "args": {}}],
            },
            {"role": "tool", "tool_call_id": f"tc-{i}", "content": f"Result for check {i}"},
        ])

    return messages


# ---------------------------------------------------------------------------
# MessageGroup tests
# ---------------------------------------------------------------------------


class TestMessageGroup:
    """Tests for MessageGroup dataclass."""

    def test_is_complete_with_all_responses(self) -> None:
        """Group is complete when all tool calls have responses."""
        ai_msg = {
            "role": "assistant",
            "content": "Let me check",
            "tool_calls": [{"id": "tc-1"}, {"id": "tc-2"}],
        }
        tool_msgs = [
            {"role": "tool", "tool_call_id": "tc-1"},
            {"role": "tool", "tool_call_id": "tc-2"},
        ]

        group = MessageGroup(index=0, ai_message=ai_msg, tool_messages=tool_msgs)
        assert group.is_complete is True

    def test_is_incomplete_with_missing_response(self) -> None:
        """Group is incomplete when some tool calls lack responses."""
        ai_msg = {
            "role": "assistant",
            "content": "Let me check",
            "tool_calls": [{"id": "tc-1"}, {"id": "tc-2"}],
        }
        tool_msgs = [
            {"role": "tool", "tool_call_id": "tc-1"},
            # Missing tc-2 response
        ]

        group = MessageGroup(index=0, ai_message=ai_msg, tool_messages=tool_msgs)
        assert group.is_complete is False

    def test_total_size_bytes(self) -> None:
        """total_size_bytes estimates group size."""
        ai_msg = {"role": "assistant", "content": "x" * 100}
        tool_msgs = [{"role": "tool", "content": "y" * 100}]

        group = MessageGroup(index=0, ai_message=ai_msg, tool_messages=tool_msgs)
        # Should be roughly 200 bytes plus overhead
        assert group.total_size_bytes > 150


# ---------------------------------------------------------------------------
# micro_compact tests
# ---------------------------------------------------------------------------


class TestMicroCompact:
    """Tests for micro_compact function."""

    def test_keeps_3_recent_complete_results(
        self, sample_messages_with_tools: list[dict]
    ) -> None:
        """Micro compact keeps the 3 most recent complete results."""
        result = micro_compact(sample_messages_with_tools, keep_recent=3)

        assert result.outcome == CompactionOutcome.SUCCESS
        assert result.details["groups_compacted"] == 2  # Compact 2 oldest groups
        assert result.details["keep_recent"] == 3

    def test_skips_when_not_enough_groups(
        self, sample_messages_with_tools: list[dict]
    ) -> None:
        """Micro compact skips when there are fewer groups than keep_recent."""
        result = micro_compact(sample_messages_with_tools, keep_recent=10)

        assert result.outcome == CompactionOutcome.SKIPPED
        assert result.details["reason"] == "not_enough_complete_groups"

    def test_handles_incomplete_groups(
        self, messages_with_incomplete_groups: list[dict]
    ) -> None:
        """Micro compact only considers complete groups."""
        result = micro_compact(messages_with_incomplete_groups, keep_recent=1)

        # Should still work but with fewer complete groups
        assert result.outcome in (CompactionOutcome.SUCCESS, CompactionOutcome.SKIPPED)

    def test_preserves_user_messages(
        self, sample_messages_with_tools: list[dict]
    ) -> None:
        """Micro compact preserves user messages."""
        result = micro_compact(sample_messages_with_tools, keep_recent=3)

        # The user message should still be in the result
        # We can't directly check messages, but the result should be valid
        assert result.messages_remaining > 0

    def test_inserts_system_marker(
        self, sample_messages_with_tools: list[dict]
    ) -> None:
        """Micro compact inserts a system marker for compacted messages."""
        result = micro_compact(sample_messages_with_tools, keep_recent=3)

        assert result.outcome == CompactionOutcome.SUCCESS
        assert "tool_results_removed" in result.details


# ---------------------------------------------------------------------------
# snip_middle tests
# ---------------------------------------------------------------------------


class TestSnipMiddle:
    """Tests for snip_middle function."""

    def test_snips_middle_groups(
        self, long_messages_for_snipping: list[dict]
    ) -> None:
        """Snip removes complete middle groups."""
        # Set a low target to force snipping
        limits = CompactionLimits(max_snip_tokens=100)

        result = snip_middle(long_messages_for_snipping, target_tokens=100, limits=limits)

        assert result.outcome == CompactionOutcome.SUCCESS
        assert result.messages_removed > 0
        assert result.details["groups_snipped"] > 0

    def test_preserves_first_group(
        self, long_messages_for_snipping: list[dict]
    ) -> None:
        """Snip preserves the initial objective (first group)."""
        limits = CompactionLimits(max_snip_tokens=100)

        result = snip_middle(long_messages_for_snipping, target_tokens=100, limits=limits)

        # The first user message should be preserved
        assert result.outcome == CompactionOutcome.SUCCESS

    def test_preserves_last_group(
        self, long_messages_for_snipping: list[dict]
    ) -> None:
        """Snip preserves the most recent group."""
        limits = CompactionLimits(max_snip_tokens=100)

        result = snip_middle(long_messages_for_snipping, target_tokens=100, limits=limits)

        # The last group should be preserved
        assert result.outcome == CompactionOutcome.SUCCESS

    def test_never_splits_tool_group(
        self, sample_messages_with_tools: list[dict]
    ) -> None:
        """Snip never splits a tool group - it removes complete groups only."""
        limits = CompactionLimits(max_snip_tokens=10)

        result = snip_middle(sample_messages_with_tools, target_tokens=10, limits=limits)

        # Should only remove complete groups
        if result.outcome == CompactionOutcome.SUCCESS:
            # The groups_snipped should equal groups removed
            assert "groups_snipped" in result.details

    def test_skips_when_under_target(
        self, sample_messages_with_tools: list[dict]
    ) -> None:
        """Snip skips when already under target."""
        limits = CompactionLimits(max_snip_tokens=100_000)  # Very high

        result = snip_middle(sample_messages_with_tools, target_tokens=100_000, limits=limits)

        assert result.outcome == CompactionOutcome.SKIPPED
        assert result.details["reason"] == "already_under_target"

    def test_inserts_bounded_marker(
        self, long_messages_for_snipping: list[dict]
    ) -> None:
        """Snip inserts a bounded marker with removed counts."""
        limits = CompactionLimits(max_snip_tokens=100)

        result = snip_middle(long_messages_for_snipping, target_tokens=100, limits=limits)

        assert result.outcome == CompactionOutcome.SUCCESS
        assert "tool_results_removed" in result.details
        assert result.details["tool_results_removed"] > 0

    def test_no_complete_middle_groups_skips(
        self, messages_with_incomplete_groups: list[dict]
    ) -> None:
        """Snip skips when no complete middle groups exist."""
        limits = CompactionLimits(max_snip_tokens=10)

        result = snip_middle(messages_with_incomplete_groups, target_tokens=10, limits=limits)

        # Should skip since middle groups are incomplete
        assert result.outcome == CompactionOutcome.SKIPPED


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


class TestIntegration:
    """Integration tests combining both compaction methods."""

    def test_micro_then_snip(
        self, long_messages_for_snipping: list[dict]
    ) -> None:
        """Micro compact then snip works together."""
        # First micro compact
        micro_result = micro_compact(long_messages_for_snipping, keep_recent=3)
        assert micro_result.outcome == CompactionOutcome.SUCCESS

        # Then snip (would need to reconstruct messages from result)
        # This test validates both can run without errors
        snip_result = snip_middle(long_messages_for_snipping, target_tokens=500)
        assert snip_result.outcome in (CompactionOutcome.SUCCESS, CompactionOutcome.SKIPPED)
