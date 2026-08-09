"""Tests for summary generation and circuit breaker.

Verifies:
- Complete session memory skips summary model
- Three summary failures open circuit
"""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from incidentlens_control_plane.compaction.domain import CompactionException, SummaryResult
from incidentlens_control_plane.compaction.session import SessionMemorySnapshot
from incidentlens_control_plane.compaction.summary import (
    SummaryCircuitBreaker,
    _extract_response_content,
    _format_messages_for_summary,
    _parse_and_validate_summary,
    summarize_history,
)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_session_memory() -> SessionMemorySnapshot:
    """Sample session memory for testing."""
    return SessionMemorySnapshot(
        incident_id="inc-test",
        session_id="session-test",
        objective="Investigate high latency in order-service",
        current_phase="agent_loop",
        current_round=3,
        max_rounds=8,
        evidence_ids=["ev-1", "ev-2", "ev-3"],
        verified_facts=["DB connection pool at 95%", "Connection timeout errors"],
        loaded_skills=["downstream-timeout"],
        next_action="Continue investigation",
    )


@pytest.fixture
def sample_messages() -> list[dict[str, Any]]:
    """Sample messages for summarization."""
    return [
        {"role": "system", "content": "You are an incident investigator."},
        {"role": "user", "content": "Investigate high latency in order-service"},
        {"role": "assistant", "content": "I will investigate the latency issue."},
        {"role": "tool", "content": "DB connection pool at 95%", "tool_call_id": "tc-1"},
        {"role": "assistant", "content": "Found DB pool issue. Checking logs."},
        {"role": "tool", "content": "Connection timeout errors", "tool_call_id": "tc-2"},
    ]


@pytest.fixture
def mock_summary_model() -> AsyncMock:
    """Mock model that returns a valid summary."""
    model = AsyncMock()
    response = MagicMock()
    response.content = json.dumps({
        "objective": "Investigate high latency in order-service",
        "evidence_ids": ["ev-1", "ev-2"],
        "verified_facts": ["DB connection pool at 95%", "Connection timeout errors"],
        "rejected_directions": ["CPU spike - not related"],
        "completed_work": ["Checked DB metrics", "Analyzed logs"],
        "next_action": "Check deployment history",
    })
    model.ainvoke.return_value = response
    return model


@pytest.fixture
def circuit_breaker() -> SummaryCircuitBreaker:
    """Fresh circuit breaker for testing."""
    return SummaryCircuitBreaker(max_failures=3)


# ---------------------------------------------------------------------------
# Circuit breaker tests
# ---------------------------------------------------------------------------


class TestSummaryCircuitBreaker:
    """Tests for SummaryCircuitBreaker."""

    def test_initial_state(self, circuit_breaker: SummaryCircuitBreaker) -> None:
        """Circuit starts closed."""
        assert circuit_breaker.is_open is False
        assert circuit_breaker.failure_count == 0

    def test_record_success_resets_count(self, circuit_breaker: SummaryCircuitBreaker) -> None:
        """Success resets failure count."""
        circuit_breaker.record_failure("error 1")
        circuit_breaker.record_failure("error 2")
        assert circuit_breaker.failure_count == 2

        circuit_breaker.record_success()
        assert circuit_breaker.failure_count == 0
        assert circuit_breaker.is_open is False

    def test_three_failures_open_circuit(self, circuit_breaker: SummaryCircuitBreaker) -> None:
        """Three failures open the circuit."""
        circuit_breaker.record_failure("error 1")
        assert circuit_breaker.is_open is False

        circuit_breaker.record_failure("error 2")
        assert circuit_breaker.is_open is False

        circuit_breaker.record_failure("error 3")
        assert circuit_breaker.is_open is True
        assert circuit_breaker.failure_count == 3

    def test_manual_reset(self, circuit_breaker: SummaryCircuitBreaker) -> None:
        """Manual reset closes the circuit."""
        circuit_breaker.record_failure("error 1")
        circuit_breaker.record_failure("error 2")
        circuit_breaker.record_failure("error 3")
        assert circuit_breaker.is_open is True

        circuit_breaker.reset()
        assert circuit_breaker.is_open is False
        assert circuit_breaker.failure_count == 0

    def test_custom_threshold(self) -> None:
        """Custom max_failures threshold works."""
        cb = SummaryCircuitBreaker(max_failures=5)
        for i in range(4):
            cb.record_failure(f"error {i}")
        assert cb.is_open is False

        cb.record_failure("error 4")
        assert cb.is_open is True


# ---------------------------------------------------------------------------
# Summary generation tests
# ---------------------------------------------------------------------------


class TestSummarizeHistory:
    """Tests for summarize_history function."""

    @pytest.mark.asyncio
    async def test_complete_session_memory_skips_model(
        self,
        sample_messages: list[dict[str, Any]],
        sample_session_memory: SessionMemorySnapshot,
    ) -> None:
        """When session memory is complete, summary is still generated.

        This test verifies the function works correctly with complete data.
        """
        mock_model = AsyncMock()
        response = MagicMock()
        response.content = json.dumps({
            "objective": sample_session_memory.objective,
            "evidence_ids": sample_session_memory.evidence_ids,
            "verified_facts": sample_session_memory.verified_facts,
            "rejected_directions": [],
            "completed_work": ["Checked DB metrics"],
            "next_action": "Continue investigation",
        })
        mock_model.ainvoke.return_value = response

        result = await summarize_history(
            sample_messages, sample_session_memory, mock_model
        )

        assert isinstance(result, SummaryResult)
        assert result.objective == sample_session_memory.objective
        assert result.evidence_ids == sample_session_memory.evidence_ids
        mock_model.ainvoke.assert_called_once()

    @pytest.mark.asyncio
    async def test_model_failure_raises_error(
        self,
        sample_messages: list[dict[str, Any]],
        sample_session_memory: SessionMemorySnapshot,
    ) -> None:
        """Model failure raises CompactionException."""
        mock_model = AsyncMock()
        mock_model.ainvoke.side_effect = RuntimeError("Model unavailable")

        with pytest.raises(CompactionException) as exc_info:
            await summarize_history(
                sample_messages, sample_session_memory, mock_model
            )

        assert exc_info.value.code == "summary_failed"
        assert "Model unavailable" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_invalid_json_raises_error(
        self,
        sample_messages: list[dict[str, Any]],
        sample_session_memory: SessionMemorySnapshot,
    ) -> None:
        """Invalid JSON response raises CompactionException."""
        mock_model = AsyncMock()
        response = MagicMock()
        response.content = "This is not JSON"
        mock_model.ainvoke.return_value = response

        with pytest.raises(CompactionException) as exc_info:
            await summarize_history(
                sample_messages, sample_session_memory, mock_model
            )

        assert exc_info.value.code == "summary_failed"
        assert "Failed to parse" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_missing_objective_raises_error(
        self,
        sample_messages: list[dict[str, Any]],
        sample_session_memory: SessionMemorySnapshot,
    ) -> None:
        """Missing objective in summary raises CompactionException."""
        mock_model = AsyncMock()
        response = MagicMock()
        response.content = json.dumps({
            "objective": "",
            "evidence_ids": ["ev-1"],
            "verified_facts": [],
            "rejected_directions": [],
            "completed_work": [],
            "next_action": "",
        })
        mock_model.ainvoke.return_value = response

        with pytest.raises(CompactionException) as exc_info:
            await summarize_history(
                sample_messages, sample_session_memory, mock_model
            )

        assert exc_info.value.code == "summary_failed"
        assert "missing objective" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestHelperFunctions:
    """Tests for helper functions in summary module."""

    def test_format_messages_for_summary(self) -> None:
        """Messages are formatted correctly."""
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ]
        result = _format_messages_for_summary(messages)
        assert "[user]: Hello" in result
        assert "[assistant]: Hi there" in result

    def test_format_messages_truncates_long_content(self) -> None:
        """Long content is truncated."""
        long_content = "x" * 3000
        messages = [{"role": "user", "content": long_content}]
        result = _format_messages_for_summary(messages)
        assert "[truncated]" in result
        assert len(result) < 3000

    def test_extract_response_content_string(self) -> None:
        """String content is extracted."""
        response = MagicMock()
        response.content = "test content"
        assert _extract_response_content(response) == "test content"

    def test_extract_response_content_list(self) -> None:
        """List content with text blocks is extracted."""
        response = MagicMock()
        response.content = [
            {"type": "text", "text": "block 1"},
            {"type": "text", "text": "block 2"},
        ]
        result = _extract_response_content(response)
        assert "block 1" in result
        assert "block 2" in result

    def test_parse_and_validate_summary_valid(self, sample_session_memory: SessionMemorySnapshot) -> None:
        """Valid JSON summary parses correctly."""
        content = json.dumps({
            "objective": "Test objective",
            "evidence_ids": ["ev-1", "ev-2"],
            "verified_facts": ["fact 1"],
            "rejected_directions": [],
            "completed_work": ["step 1"],
            "next_action": "next step",
        })
        result = _parse_and_validate_summary(content, sample_session_memory)
        assert result.objective == "Test objective"
        assert result.evidence_ids == ["ev-1", "ev-2"]

    def test_parse_and_validate_summary_with_markdown_fencing(
        self, sample_session_memory: SessionMemorySnapshot
    ) -> None:
        """Summary with markdown fencing is parsed correctly."""
        content = '```json\n{"objective": "Test", "evidence_ids": [], "verified_facts": [], "rejected_directions": [], "completed_work": [], "next_action": ""}\n```'
        result = _parse_and_validate_summary(content, sample_session_memory)
        assert result.objective == "Test"
