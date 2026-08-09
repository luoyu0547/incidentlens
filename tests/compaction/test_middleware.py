"""Tests for compaction middleware.

Verifies:
- Prompt-too-long recovery (2 retries max)
- Evidence and objective preservation
"""

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from incidentlens_control_plane.compaction.domain import (
    CompactionException,
    CompactionLimits,
    CompactionOutcome,
)
from incidentlens_control_plane.compaction.middleware import (
    CompactionMiddleware,
    TranscriptStore,
    is_prompt_too_long,
)
from incidentlens_control_plane.compaction.session import SessionMemoryStore, SessionMemorySnapshot
from incidentlens_control_plane.compaction.summary import SummaryCircuitBreaker
from incidentlens_control_plane.compaction.tool_budget import ToolOutputStore


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_state() -> dict[str, Any]:
    """Sample investigation state."""
    return {
        "incident_id": "inc-test",
        "session_id": "session-test",
        "status": "investigating",
        "phase": "agent_loop",
        "current_round": 3,
        "max_rounds": 8,
        "alert": {"summary": "High latency in order-service"},
        "evidence": [
            {"id": "ev-1", "source_tool": "read_metrics", "content": {"summary": "DB pool at 95%"}},
            {"id": "ev-2", "source_tool": "read_logs", "content": {"summary": "Timeout errors"}},
        ],
        "loaded_skill_names": ["downstream-timeout"],
    }


@pytest.fixture
def sample_messages() -> list[dict[str, Any]]:
    """Sample messages."""
    return [
        {"role": "system", "content": "You are an investigator."},
        {"role": "user", "content": "Investigate latency"},
        {"role": "assistant", "content": "I will investigate."},
        {"role": "tool", "content": "DB pool at 95%", "tool_call_id": "tc-1"},
    ]


@pytest.fixture
def model_profile() -> MagicMock:
    """Mock model profile with context window info."""
    profile = MagicMock()
    profile.context_window_tokens = 128_000
    profile.reserved_output_tokens = 4_096
    return profile


@pytest.fixture
def middleware_dirs(tmp_path: Path) -> dict[str, Path]:
    """Create temporary directories for middleware stores."""
    return {
        "sessions": tmp_path / "sessions",
        "tool_outputs": tmp_path / "tool_outputs",
        "transcripts": tmp_path / "transcripts",
    }


@pytest.fixture
def middleware(
    middleware_dirs: dict[str, Path],
    model_profile: MagicMock,
) -> CompactionMiddleware:
    """Create a CompactionMiddleware instance."""
    session_store = SessionMemoryStore(middleware_dirs["sessions"])
    tool_output_store = ToolOutputStore(middleware_dirs["tool_outputs"])
    transcript_store = TranscriptStore(middleware_dirs["transcripts"])

    return CompactionMiddleware(
        runtime=None,
        limits=CompactionLimits(),
        model_profile=model_profile,
        session_store=session_store,
        tool_output_store=tool_output_store,
        transcript_store=transcript_store,
        summary_circuit_breaker=SummaryCircuitBreaker(max_failures=3),
        max_retries=2,
    )


# ---------------------------------------------------------------------------
# is_prompt_too_long tests
# ---------------------------------------------------------------------------


class TestIsPromptTooLong:
    """Tests for is_prompt_too_long helper."""

    def test_detects_prompt_too_long_error(self) -> None:
        """Detects 'prompt is too long' error."""
        exc = ValueError("prompt is too long for model")
        assert is_prompt_too_long(exc) is True

    def test_detects_context_length_error(self) -> None:
        """Detects 'context length exceeded' error."""
        exc = RuntimeError("context length exceeded")
        assert is_prompt_too_long(exc) is True

    def test_detects_token_limit_error(self) -> None:
        """Detects 'token limit' error."""
        exc = Exception("token limit reached")
        assert is_prompt_too_long(exc) is True

    def test_ignores_unrelated_errors(self) -> None:
        """Ignores errors not related to prompt length."""
        exc = ValueError("invalid argument")
        assert is_prompt_too_long(exc) is False

    def test_detects_error_type_name(self) -> None:
        """Detects by error type name."""

        class PromptTooLongError(Exception):
            pass

        exc = PromptTooLongError("some error")
        assert is_prompt_too_long(exc) is True


# ---------------------------------------------------------------------------
# Prompt-too-long recovery tests
# ---------------------------------------------------------------------------


class TestPromptTooLongRecovery:
    """Tests for reactive recovery on prompt-too-long errors."""

    @pytest.mark.asyncio
    async def test_recovery_retries_max_two_times(
        self,
        middleware: CompactionMiddleware,
        sample_state: dict[str, Any],
        sample_messages: list[dict[str, Any]],
    ) -> None:
        """Recovery retries up to max_retries times."""
        mock_request = MagicMock()
        mock_request.state = sample_state
        mock_request.system_message = MagicMock()
        mock_request.system_message.text = "System prompt"
        mock_request.messages = []
        mock_request.override.return_value = mock_request

        handler = AsyncMock()
        handler.side_effect = ValueError("prompt is too long for model")

        with pytest.raises(CompactionException) as exc_info:
            await middleware.awrap_model_call(mock_request, handler)

        assert exc_info.value.code == "prompt_too_long"
        assert "2 retries" in str(exc_info.value)
        # Initial call + 2 retries = 3 total
        assert handler.call_count == 3

    @pytest.mark.asyncio
    async def test_success_on_first_try_no_retry(
        self,
        middleware: CompactionMiddleware,
        sample_state: dict[str, Any],
    ) -> None:
        """Successful call on first try does not retry."""
        mock_request = MagicMock()
        mock_request.state = sample_state
        mock_request.system_message = MagicMock()
        mock_request.system_message.text = "System prompt"
        mock_request.messages = []
        mock_request.override.return_value = mock_request

        mock_response = MagicMock()
        handler = AsyncMock(return_value=mock_response)

        result = await middleware.awrap_model_call(mock_request, handler)

        assert result == mock_response
        assert handler.call_count == 1

    @pytest.mark.asyncio
    async def test_success_on_retry_after_failure(
        self,
        middleware: CompactionMiddleware,
        sample_state: dict[str, Any],
    ) -> None:
        """Successful call on retry after initial failure."""
        mock_request = MagicMock()
        mock_request.state = sample_state
        mock_request.system_message = MagicMock()
        mock_request.system_message.text = "System prompt"
        mock_request.messages = []
        mock_request.override.return_value = mock_request

        mock_response = MagicMock()
        handler = AsyncMock(side_effect=[
            ValueError("prompt is too long"),
            mock_response,
        ])

        result = await middleware.awrap_model_call(mock_request, handler)

        assert result == mock_response
        assert handler.call_count == 2

    @pytest.mark.asyncio
    async def test_non_prompt_error_not_retried(
        self,
        middleware: CompactionMiddleware,
        sample_state: dict[str, Any],
    ) -> None:
        """Non-prompt-too-long errors are not retried."""
        mock_request = MagicMock()
        mock_request.state = sample_state
        mock_request.system_message = MagicMock()
        mock_request.system_message.text = "System prompt"
        mock_request.messages = []
        mock_request.override.return_value = mock_request

        handler = AsyncMock(side_effect=ValueError("unrelated error"))

        with pytest.raises(ValueError, match="unrelated error"):
            await middleware.awrap_model_call(mock_request, handler)

        assert handler.call_count == 1


# ---------------------------------------------------------------------------
# Evidence and objective preservation tests
# ---------------------------------------------------------------------------


class TestEvidenceAndObjectivePreservation:
    """Tests for evidence and objective preservation during compaction."""

    def test_threshold_computation(self, middleware: CompactionMiddleware) -> None:
        """Threshold is computed correctly: context - reserved - 13000."""
        threshold = middleware._compute_threshold()
        # 128000 - 4096 - 13000 = 110904
        assert threshold == 110_904

    def test_threshold_without_model_profile(self) -> None:
        """Default threshold when no model profile provided."""
        mw = CompactionMiddleware(runtime=None, model_profile=None)
        threshold = mw._compute_threshold()
        assert threshold == 100_000

    def test_estimate_tokens(self, middleware: CompactionMiddleware) -> None:
        """Token estimation uses ~4 chars per token."""
        messages = [
            {"role": "user", "content": "x" * 400},  # ~100 tokens
            {"role": "assistant", "content": "y" * 800},  # ~200 tokens
        ]
        tokens = middleware._estimate_tokens(messages)
        assert tokens == 300  # (400 + 800) / 4

    def test_session_memory_updated_with_evidence(
        self,
        middleware: CompactionMiddleware,
        sample_state: dict[str, Any],
        sample_messages: list[dict[str, Any]],
    ) -> None:
        """Session memory is updated with evidence IDs."""
        snapshot = middleware._update_session_memory(sample_state, sample_messages)

        assert snapshot is not None
        assert snapshot.incident_id == "inc-test"
        assert "ev-1" in snapshot.evidence_ids
        assert "ev-2" in snapshot.evidence_ids
        assert snapshot.objective != ""

    def test_session_memory_preserves_objective(
        self,
        middleware: CompactionMiddleware,
        sample_state: dict[str, Any],
        sample_messages: list[dict[str, Any]],
    ) -> None:
        """Session memory preserves the investigation objective."""
        snapshot = middleware._update_session_memory(sample_state, sample_messages)

        assert "latency" in snapshot.objective.lower() or "order-service" in snapshot.objective.lower()

    def test_transcript_persisted(
        self,
        middleware: CompactionMiddleware,
        sample_state: dict[str, Any],
        sample_messages: list[dict[str, Any]],
    ) -> None:
        """Transcript is persisted to JSONL file."""
        path = middleware._persist_transcript(sample_state, sample_messages)

        assert path is not None
        assert path.exists()
        # Verify file has content
        content = path.read_text()
        assert len(content) > 0
        # Verify it's valid JSONL
        lines = [l for l in content.strip().split("\n") if l]
        assert len(lines) == len(sample_messages)

    def test_compaction_pipeline_preserves_evidence(
        self,
        middleware: CompactionMiddleware,
        sample_state: dict[str, Any],
        sample_messages: list[dict[str, Any]],
    ) -> None:
        """Compaction pipeline preserves evidence references."""
        messages, results = middleware._apply_compaction_pipeline(
            sample_state, sample_messages
        )

        # Messages should still be present (not enough to trigger compaction)
        assert len(messages) > 0

        # Session memory should have evidence IDs
        snapshot = middleware._update_session_memory(sample_state, messages)
        assert "ev-1" in snapshot.evidence_ids


# ---------------------------------------------------------------------------
# TranscriptStore tests
# ---------------------------------------------------------------------------


class TestTranscriptStore:
    """Tests for TranscriptStore."""

    def test_persist_transcript(self, tmp_path: Path) -> None:
        """Transcript is persisted as JSONL."""
        store = TranscriptStore(tmp_path / "transcripts")
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
        ]

        path = store.persist_transcript("inc-1", "session-1", messages)

        assert path.exists()
        lines = path.read_text().strip().split("\n")
        assert len(lines) == 2

        # Verify each line is valid JSON
        for line in lines:
            record = json.loads(line)
            assert "role" in record
            assert "content" in record
            assert "timestamp" in record

    def test_persist_transcript_appends(self, tmp_path: Path) -> None:
        """Multiple persists append to the same file."""
        store = TranscriptStore(tmp_path / "transcripts")
        messages1 = [{"role": "user", "content": "Hello"}]
        messages2 = [{"role": "assistant", "content": "Hi"}]

        store.persist_transcript("inc-1", "session-1", messages1)
        store.persist_transcript("inc-1", "session-1", messages2)

        path = tmp_path / "transcripts" / "inc-1" / "session-1.jsonl"
        lines = path.read_text().strip().split("\n")
        assert len(lines) == 2
