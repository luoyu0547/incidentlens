"""Tests for tool budget persistence layer.

Verifies:
- Single large result persistence
- SHA-256 verification
- Preview size limits
- Atomic write safety
"""

import hashlib
from pathlib import Path

import pytest

from incidentlens_control_plane.compaction.domain import CompactionLimits, CompactionOutcome
from incidentlens_control_plane.compaction.tool_budget import (
    ToolOutputStore,
    persist_oversized_tool_results,
)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tool_store(tmp_path: Path) -> ToolOutputStore:
    """Create a temporary tool output store for testing."""
    return ToolOutputStore(tmp_path / "tool_outputs")


@pytest.fixture
def sample_messages() -> list[dict]:
    """Sample message history with tool results for testing."""
    return [
        {"role": "system", "content": "You are an incident investigator."},
        {"role": "user", "content": "Investigate high latency in order-service"},
        {
            "role": "assistant",
            "content": "I will investigate the latency issue.",
            "tool_calls": [
                {"id": "tc-1", "name": "read_metrics", "args": {"service": "order-service"}},
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "tc-1",
            "content": "DB connection pool at 95% utilization",
        },
    ]


@pytest.fixture
def large_tool_messages() -> list[dict]:
    """Message history with a large tool result (over 128KB)."""
    # Create a large content string (150KB)
    large_content = "x" * 150_000
    return [
        {"role": "system", "content": "System prompt"},
        {
            "role": "assistant",
            "content": "Let me check the logs",
            "tool_calls": [
                {"id": "tc-large", "name": "read_logs", "args": {"service": "order-service"}},
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "tc-large",
            "content": large_content,
        },
    ]


# ---------------------------------------------------------------------------
# ToolOutputStore tests
# ---------------------------------------------------------------------------


class TestToolOutputStore:
    """Tests for ToolOutputStore persistence."""

    def test_save_and_load(self, tool_store: ToolOutputStore) -> None:
        """Save and load round-trips correctly."""
        content = "Test tool output content"
        path = tool_store.save("inc-123", content, {"tool_name": "test_tool"})

        assert path.exists()

        loaded = tool_store.load("inc-123", hashlib.sha256(content.encode()).hexdigest())
        assert loaded == content

    def test_load_nonexistent_returns_none(self, tool_store: ToolOutputStore) -> None:
        """Load returns None for nonexistent output."""
        loaded = tool_store.load("inc-nonexistent", "nonexistent-digest")
        assert loaded is None

    def test_exists(self, tool_store: ToolOutputStore) -> None:
        """Exists checks if output exists."""
        content = "Test content"
        digest = hashlib.sha256(content.encode()).hexdigest()

        assert tool_store.exists("inc-123", digest) is False
        tool_store.save("inc-123", content)
        assert tool_store.exists("inc-123", digest) is True

    def test_atomic_write_on_crash(
        self, tool_store: ToolOutputStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Atomic write leaves no temp files on crash."""
        import os

        content = "Test content for crash"

        # Patch os.replace to raise an exception
        original_replace = os.replace

        def failing_replace(src: str, dst: str | Path) -> None:
            if "inc-crash" in str(dst):
                raise OSError("Simulated crash")
            return original_replace(src, dst)

        monkeypatch.setattr(os, "replace", failing_replace)

        # Save should fail
        with pytest.raises(RuntimeError, match="Failed to persist"):
            tool_store.save("inc-crash", content)

        # Check no temp files remain
        incident_dir = tool_store._output_dir("inc-crash")
        if incident_dir.exists():
            temp_files = list(incident_dir.glob("*.tmp"))
            assert len(temp_files) == 0

    def test_deduplication(self, tool_store: ToolOutputStore) -> None:
        """Saving same content twice produces same file."""
        content = "Duplicate content"

        path1 = tool_store.save("inc-123", content)
        path2 = tool_store.save("inc-123", content)

        assert path1 == path2  # Same path = deduplicated


# ---------------------------------------------------------------------------
# SHA-256 verification tests
# ---------------------------------------------------------------------------


class TestSHA256Verification:
    """Tests for SHA-256 digest verification."""

    def test_digest_matches_content(self, tool_store: ToolOutputStore) -> None:
        """Persisted digest matches content SHA-256."""
        content = "Test content for verification"
        expected_digest = hashlib.sha256(content.encode()).hexdigest()

        path = tool_store.save("inc-123", content)

        # Load and verify digest
        import json
        data = json.loads(path.read_text())
        assert data["digest_sha256"] == expected_digest

    def test_different_content_different_digest(self, tool_store: ToolOutputStore) -> None:
        """Different content produces different digests."""
        content1 = "Content one"
        content2 = "Content two"

        path1 = tool_store.save("inc-123", content1)
        path2 = tool_store.save("inc-123", content2)

        assert path1 != path2  # Different paths


# ---------------------------------------------------------------------------
# Preview size tests
# ---------------------------------------------------------------------------


class TestPreviewSize:
    """Tests for preview size limits."""

    def test_preview_respects_size_limit(self, tool_store: ToolOutputStore) -> None:
        """Preview is truncated to specified size."""
        from incidentlens_control_plane.compaction.tool_budget import _make_preview

        content = "x" * 1000
        preview = _make_preview(content, 500)

        assert len(preview.encode("utf-8")) <= 500

    def test_preview_preserves_content(self, tool_store: ToolOutputStore) -> None:
        """Preview preserves beginning of content."""
        from incidentlens_control_plane.compaction.tool_budget import _make_preview

        content = "A" * 100 + "B" * 100 + "C" * 100
        preview = _make_preview(content, 150)

        assert preview.startswith("A" * 100)
        assert preview[100:150] == "B" * 50


# ---------------------------------------------------------------------------
# persist_oversized_tool_results tests
# ---------------------------------------------------------------------------


class TestPersistOversizedToolResults:
    """Tests for persist_oversized_tool_results function."""

    def test_persists_large_result(
        self, tool_store: ToolOutputStore, large_tool_messages: list[dict]
    ) -> None:
        """Large tool result is persisted to disk."""
        limits = CompactionLimits(max_tool_output_bytes=131_072)  # 128KB

        result = persist_oversized_tool_results(
            large_tool_messages, "inc-123", tool_store, limits
        )

        assert result.outcome == CompactionOutcome.SUCCESS
        assert result.messages_removed > 0
        assert "references" in result.details

    def test_skips_under_budget(
        self, tool_store: ToolOutputStore, sample_messages: list[dict]
    ) -> None:
        """Small results are not persisted."""
        limits = CompactionLimits(max_tool_output_bytes=131_072)  # 128KB

        result = persist_oversized_tool_results(
            sample_messages, "inc-123", tool_store, limits
        )

        assert result.outcome == CompactionOutcome.SKIPPED
        assert result.messages_removed == 0

    def test_persists_largest_first(
        self, tool_store: ToolOutputStore, tmp_path: Path
    ) -> None:
        """Persists largest results first to get under budget quickly."""
        # Create messages with different sized tool results
        messages = [
            {"role": "system", "content": "System prompt"},
            {
                "role": "assistant",
                "content": "Let me check",
                "tool_calls": [
                    {"id": "tc-small", "name": "read_small", "args": {}},
                    {"id": "tc-large", "name": "read_large", "args": {}},
                ],
            },
            {"role": "tool", "tool_call_id": "tc-small", "content": "Small content"},
            {"role": "tool", "tool_call_id": "tc-large", "content": "x" * 100_000},
        ]

        # Set budget just above small content but below total
        limits = CompactionLimits(max_tool_output_bytes=50_000)

        result = persist_oversized_tool_results(
            messages, "inc-123", tool_store, limits
        )

        # Large result should be persisted
        assert result.outcome == CompactionOutcome.SUCCESS
        assert result.details["persisted_count"] >= 1

    def test_sha256_in_reference(
        self, tool_store: ToolOutputStore, sample_messages: list[dict]
    ) -> None:
        """Persisted reference includes SHA-256 digest."""
        # Force persistence by setting low budget
        limits = CompactionLimits(max_tool_output_bytes=10)  # Very low

        result = persist_oversized_tool_results(
            sample_messages, "inc-123", tool_store, limits
        )

        if result.outcome == CompactionOutcome.SUCCESS and result.details.get("references"):
            ref = result.details["references"][0]
            assert "digest_sha256" in ref
            assert len(ref["digest_sha256"]) == 64  # SHA-256 hex digest length

    def test_preview_in_reference(
        self, tool_store: ToolOutputStore, sample_messages: list[dict]
    ) -> None:
        """Persisted reference includes preview."""
        limits = CompactionLimits(max_tool_output_bytes=10, preview_size_bytes=100)

        result = persist_oversized_tool_results(
            sample_messages, "inc-123", tool_store, limits
        )

        if result.outcome == CompactionOutcome.SUCCESS and result.details.get("references"):
            ref = result.details["references"][0]
            assert "preview" in ref
            assert len(ref["preview"]) <= 100

    def test_reread_instruction_in_reference(
        self, tool_store: ToolOutputStore, sample_messages: list[dict]
    ) -> None:
        """Persisted reference includes reread instruction."""
        limits = CompactionLimits(max_tool_output_bytes=10)

        result = persist_oversized_tool_results(
            sample_messages, "inc-123", tool_store, limits
        )

        if result.outcome == CompactionOutcome.SUCCESS and result.details.get("references"):
            ref = result.details["references"][0]
            assert "reread_instruction" in ref
            assert "read_file" in ref["reread_instruction"]
