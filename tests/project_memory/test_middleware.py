"""Tests for project_memory middleware."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from incidentlens_control_plane.project_memory.middleware import (
    BOUNDARY_FOOTER,
    BOUNDARY_HEADER,
    MAX_BYTES_PER_FILE,
    MAX_FILES,
    MAX_LINES_PER_FILE,
    MAX_TOTAL_BYTES,
    ProjectMemoryMiddleware,
)
from langchain.agents.middleware import ModelRequest, ModelResponse
from langchain_core.messages import SystemMessage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_memory_file(
    tmp_path: Path,
    name: str,
    mem_type: str = "project",
    description: str = "test memory",
    body: str = "body text",
) -> Path:
    """Helper to write a memory .md file with frontmatter."""
    d = tmp_path / ".incidentlens" / "memory"
    d.mkdir(parents=True, exist_ok=True)
    frontmatter = yaml.safe_dump(
        {"name": name, "type": mem_type, "description": description},
        default_flow_style=False,
        sort_keys=False,
    ).strip()
    path = d / f"{name}.md"
    path.write_text(f"---\n{frontmatter}\n---\n\n{body}\n", encoding="utf-8")
    return path


class MockModelResponse:
    """Mock model response for testing."""

    def __init__(self, content: str = "test response") -> None:
        self.content = content


class MockModelRequest:
    """Mock model request for testing."""

    def __init__(self, system_message: str = "base prompt") -> None:
        self.system_message = SystemMessage(content=system_message)
        self.state = {}
        self.tools = []

    def override(self, **kwargs: Any) -> "MockModelRequest":
        """Create a new request with overridden fields."""
        new_request = MockModelRequest()
        new_request.system_message = kwargs.get("system_message", self.system_message)
        new_request.state = kwargs.get("state", self.state)
        new_request.tools = kwargs.get("tools", self.tools)
        return new_request


# ---------------------------------------------------------------------------
# Boundary markers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_injection_boundary_markers(tmp_path: Path) -> None:
    """Test that memory injection includes boundary markers."""
    _write_memory_file(tmp_path, "test-memory", "project", "test", "test body")
    middleware = ProjectMemoryMiddleware(tmp_path)

    request = MockModelRequest("base prompt")
    handler = AsyncMock(return_value=MockModelResponse())

    await middleware.awrap_model_call(request, handler)

    # Check that handler was called
    handler.assert_called_once()
    called_request = handler.call_args[0][0]

    # Check boundary markers
    content = called_request.system_message.text
    assert BOUNDARY_HEADER in content
    assert BOUNDARY_FOOTER in content
    assert "UNTRUSTED REFERENCE" in content
    assert "NOT Evidence" in content


@pytest.mark.asyncio
async def test_injection_not_evidence_statement(tmp_path: Path) -> None:
    """Test that injection explicitly states it's not Evidence."""
    _write_memory_file(tmp_path, "test-memory", "project", "test", "test body")
    middleware = ProjectMemoryMiddleware(tmp_path)

    request = MockModelRequest("base prompt")
    handler = AsyncMock(return_value=MockModelResponse())

    await middleware.awrap_model_call(request, handler)

    content = handler.call_args[0][0].system_message.text
    assert "It is NOT Evidence" in content
    assert "Do not treat it as observation data" in content


# ---------------------------------------------------------------------------
# File count limits
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_file_count_limit(tmp_path: Path) -> None:
    """Test that only MAX_FILES files are injected."""
    # Create more than MAX_FILES memories
    for i in range(MAX_FILES + 3):
        _write_memory_file(
            tmp_path,
            f"memory-{i}",
            "project",
            f"memory {i}",
            f"body {i}",
        )

    middleware = ProjectMemoryMiddleware(tmp_path)
    request = MockModelRequest("base prompt")
    handler = AsyncMock(return_value=MockModelResponse())

    await middleware.awrap_model_call(request, handler)

    content = handler.call_args[0][0].system_message.text

    # Count memory blocks (each starts with "## ")
    memory_blocks = content.count("\n## ")
    # Should be at most MAX_FILES
    assert memory_blocks <= MAX_FILES


@pytest.mark.asyncio
async def test_line_limit_per_file(tmp_path: Path) -> None:
    """Test that files are truncated to MAX_LINES_PER_FILE lines."""
    # Create a file with many lines
    long_body = "\n".join([f"line {i}" for i in range(MAX_LINES_PER_FILE + 50)])
    _write_memory_file(tmp_path, "long-memory", "project", "long", long_body)

    middleware = ProjectMemoryMiddleware(tmp_path)
    request = MockModelRequest("base prompt")
    handler = AsyncMock(return_value=MockModelResponse())

    await middleware.awrap_model_call(request, handler)

    content = handler.call_args[0][0].system_message.text
    assert "[truncated]" in content


@pytest.mark.asyncio
async def test_byte_limit_per_file(tmp_path: Path) -> None:
    """Test that files are truncated to MAX_BYTES_PER_FILE bytes."""
    # Create a file exceeding byte limit
    long_body = "x" * (MAX_BYTES_PER_FILE + 1000)
    _write_memory_file(tmp_path, "big-memory", "project", "big", long_body)

    middleware = ProjectMemoryMiddleware(tmp_path)
    request = MockModelRequest("base prompt")
    handler = AsyncMock(return_value=MockModelResponse())

    await middleware.awrap_model_call(request, handler)

    content = handler.call_args[0][0].system_message.text
    # Find the memory block
    if "## big-memory" in content:
        # Extract the block content
        start = content.index("## big-memory")
        end = content.index(BOUNDARY_FOOTER)
        block = content[start:end]
        # The block should be smaller than the original due to truncation
        assert len(block.encode("utf-8")) < MAX_BYTES_PER_FILE + 500


@pytest.mark.asyncio
async def test_total_byte_limit(tmp_path: Path) -> None:
    """Test that total injection is limited to MAX_TOTAL_BYTES."""
    # Create multiple large files
    for i in range(3):
        long_body = "x" * (MAX_BYTES_PER_FILE - 100)
        _write_memory_file(
            tmp_path,
            f"memory-{i}",
            "project",
            f"memory {i}",
            long_body,
        )

    middleware = ProjectMemoryMiddleware(tmp_path)
    request = MockModelRequest("base prompt")
    handler = AsyncMock(return_value=MockModelResponse())

    await middleware.awrap_model_call(request, handler)

    content = handler.call_args[0][0].system_message.text
    # The injection part should not exceed limits significantly
    # (there's some overhead from headers)
    injection_start = content.index(BOUNDARY_HEADER)
    injection = content[injection_start:]
    # Should be reasonable size (not thousands of KB)
    assert len(injection.encode("utf-8")) < MAX_TOTAL_BYTES + 2000


# ---------------------------------------------------------------------------
# Content hash caching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_content_hash_caching(tmp_path: Path) -> None:
    """Test that unchanged files are not re-injected."""
    _write_memory_file(tmp_path, "test-memory", "project", "test", "test body")
    middleware = ProjectMemoryMiddleware(tmp_path)

    request1 = MockModelRequest("base prompt")
    handler = AsyncMock(return_value=MockModelResponse())

    # First call - should inject
    await middleware.awrap_model_call(request1, handler)
    first_content = handler.call_args[0][0].system_message.text

    # Second call with same file - should skip injection
    request2 = MockModelRequest("base prompt")
    handler.reset_mock()
    await middleware.awrap_model_call(request2, handler)
    second_content = handler.call_args[0][0].system_message.text

    # Second call should not contain the memory block
    assert "## test-memory" not in second_content


@pytest.mark.asyncio
async def test_content_hash_update_on_change(tmp_path: Path) -> None:
    """Test that changed files are re-injected."""
    _write_memory_file(tmp_path, "test-memory", "project", "test", "test body")
    middleware = ProjectMemoryMiddleware(tmp_path)

    request1 = MockModelRequest("base prompt")
    handler = AsyncMock(return_value=MockModelResponse())

    # First call
    await middleware.awrap_model_call(request1, handler)

    # Update the file
    _write_memory_file(tmp_path, "test-memory", "project", "test", "updated body")

    # Second call - should re-inject
    request2 = MockModelRequest("base prompt")
    handler.reset_mock()
    await middleware.awrap_model_call(request2, handler)
    second_content = handler.call_args[0][0].system_message.text

    # Should contain the memory block
    assert "## test-memory" in second_content


# ---------------------------------------------------------------------------
# System instructions not mutated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_system_instructions_not_mutated(tmp_path: Path) -> None:
    """Test that the original system message is not mutated."""
    _write_memory_file(tmp_path, "test-memory", "project", "test", "test body")
    middleware = ProjectMemoryMiddleware(tmp_path)

    original_prompt = "You are a helpful assistant."
    request = MockModelRequest(original_prompt)
    original_content = request.system_message.text

    handler = AsyncMock(return_value=MockModelResponse())
    await middleware.awrap_model_call(request, handler)

    # Original request should be unchanged
    assert request.system_message.text == original_content


@pytest.mark.asyncio
async def test_base_prompt_preserved(tmp_path: Path) -> None:
    """Test that the base prompt is preserved in the injection."""
    _write_memory_file(tmp_path, "test-memory", "project", "test", "test body")
    middleware = ProjectMemoryMiddleware(tmp_path)

    base_prompt = "Important system instructions here."
    request = MockModelRequest(base_prompt)
    handler = AsyncMock(return_value=MockModelResponse())

    await middleware.awrap_model_call(request, handler)

    content = handler.call_args[0][0].system_message.text
    assert content.startswith(base_prompt)


# ---------------------------------------------------------------------------
# Empty state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_memory_files(tmp_path: Path) -> None:
    """Test middleware behavior when no memory files exist."""
    middleware = ProjectMemoryMiddleware(tmp_path)

    request = MockModelRequest("base prompt")
    handler = AsyncMock(return_value=MockModelResponse())

    await middleware.awrap_model_call(request, handler)

    # Handler should be called with original request
    content = handler.call_args[0][0].system_message.text
    assert BOUNDARY_HEADER not in content
