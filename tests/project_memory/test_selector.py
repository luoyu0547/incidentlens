"""Tests for project_memory selector."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from incidentlens_control_plane.project_memory.domain import (
    MemoryCatalogEntry,
    MemoryQuery,
    MemorySelection,
    MemoryType,
)
from incidentlens_control_plane.project_memory.selector import (
    _compute_keyword_scores,
    _normalize_unicode,
    _tokenize,
    select_memories,
)


# ---------------------------------------------------------------------------
# Helper classes
# ---------------------------------------------------------------------------


class MockModelResponse:
    """Mock model response for testing."""

    def __init__(self, content: str) -> None:
        self.content = content


# ---------------------------------------------------------------------------
# Unicode normalization
# ---------------------------------------------------------------------------


def test_normalize_unicode_basic() -> None:
    assert _normalize_unicode("Hello World") == "hello world"


def test_normalize_unicode_accents() -> None:
    assert _normalize_unicode("café") == "cafe"


def test_normalize_unicode_cjk() -> None:
    # CJK characters should be preserved
    assert _normalize_unicode("测试") == "测试"


# ---------------------------------------------------------------------------
# Tokenization
# ---------------------------------------------------------------------------


def test_tokenize_basic() -> None:
    tokens = _tokenize("Hello World")
    assert tokens == ["hello", "world"]


def test_tokenize_with_punctuation() -> None:
    tokens = _tokenize("deploy-v2, test!")
    assert tokens == ["deploy", "v2", "test"]


def test_tokenize_empty() -> None:
    tokens = _tokenize("")
    assert tokens == []


def test_tokenize_unicode() -> None:
    tokens = _tokenize("café résumé")
    assert tokens == ["cafe", "resume"]


# ---------------------------------------------------------------------------
# Keyword scoring
# ---------------------------------------------------------------------------


def test_compute_keyword_scores_basic() -> None:
    catalog = [
        MemoryCatalogEntry(name="deploy-steps", type=MemoryType.PROCEDURE, description="deploy runbook"),
        MemoryCatalogEntry(name="api-conventions", type=MemoryType.PROJECT, description="API design conventions"),
    ]
    query_tokens = ["deploy", "steps"]
    scores = _compute_keyword_scores(query_tokens, catalog)
    assert len(scores) == 1
    assert scores[0][0] == "deploy-steps"
    assert scores[0][1] > 0


def test_compute_keyword_scores_no_match() -> None:
    catalog = [
        MemoryCatalogEntry(name="deploy-steps", type=MemoryType.PROCEDURE, description="deploy runbook"),
    ]
    query_tokens = ["unrelated", "tokens"]
    scores = _compute_keyword_scores(query_tokens, catalog)
    assert len(scores) == 0


def test_compute_keyword_scores_multiple_matches() -> None:
    catalog = [
        MemoryCatalogEntry(name="deploy-steps", type=MemoryType.PROCEDURE, description="deploy runbook"),
        MemoryCatalogEntry(name="deploy-checklist", type=MemoryType.PROCEDURE, description="pre-deploy checklist"),
    ]
    query_tokens = ["deploy"]
    scores = _compute_keyword_scores(query_tokens, catalog)
    assert len(scores) == 2
    # Both should match
    names = [s[0] for s in scores]
    assert "deploy-steps" in names
    assert "deploy-checklist" in names


# ---------------------------------------------------------------------------
# select_memories
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_select_memories_empty_catalog() -> None:
    query = MemoryQuery(alert_summary="test alert", recent_text="test text")
    selection = await select_memories(query, [], model=None)
    assert selection.mode == "empty"
    assert selection.filenames == []
    assert "no memories" in selection.reason.lower()


@pytest.mark.asyncio
async def test_select_memories_model_selection() -> None:
    catalog = [
        MemoryCatalogEntry(name="deploy-steps", type=MemoryType.PROCEDURE, description="deploy runbook"),
        MemoryCatalogEntry(name="api-conventions", type=MemoryType.PROJECT, description="API design conventions"),
    ]
    query = MemoryQuery(alert_summary="deploy failed", recent_text="trying to deploy")

    # Mock model
    model = AsyncMock()
    model.ainvoke.return_value = MockModelResponse(
        content='{"selected_memories": ["deploy-steps"], "reason": "deploy related"}'
    )

    selection = await select_memories(query, catalog, model, limit=5)
    assert selection.mode == "model"
    assert "deploy-steps" in selection.filenames
    assert selection.reason == "deploy related"


@pytest.mark.asyncio
async def test_select_memories_model_with_markdown_json() -> None:
    catalog = [
        MemoryCatalogEntry(name="deploy-steps", type=MemoryType.PROCEDURE, description="deploy runbook"),
    ]
    query = MemoryQuery(alert_summary="deploy failed", recent_text="")

    # Mock model with JSON in markdown code fence
    model = AsyncMock()
    model.ainvoke.return_value = MockModelResponse(
        content='```json\n{"selected_memories": ["deploy-steps"], "reason": "test"}\n```'
    )

    selection = await select_memories(query, catalog, model, limit=5)
    assert selection.mode == "model"
    assert "deploy-steps" in selection.filenames


@pytest.mark.asyncio
async def test_select_memories_model_validates_against_catalog() -> None:
    catalog = [
        MemoryCatalogEntry(name="deploy-steps", type=MemoryType.PROCEDURE, description="deploy runbook"),
    ]
    query = MemoryQuery(alert_summary="deploy failed", recent_text="")

    # Mock model returning invalid memory name
    model = AsyncMock()
    model.ainvoke.return_value = MockModelResponse(
        content='{"selected_memories": ["nonexistent-memory"], "reason": "test"}'
    )

    selection = await select_memories(query, catalog, model, limit=5)
    # Should fall back to keyword or empty
    assert selection.mode != "model"
    assert "nonexistent-memory" not in selection.filenames


@pytest.mark.asyncio
async def test_select_memories_model_deduplicates() -> None:
    catalog = [
        MemoryCatalogEntry(name="deploy-steps", type=MemoryType.PROCEDURE, description="deploy runbook"),
    ]
    query = MemoryQuery(alert_summary="deploy failed", recent_text="")

    # Mock model returning duplicates
    model = AsyncMock()
    model.ainvoke.return_value = MockModelResponse(
        content='{"selected_memories": ["deploy-steps", "deploy-steps"], "reason": "test"}'
    )

    selection = await select_memories(query, catalog, model, limit=5)
    assert selection.mode == "model"
    assert selection.filenames.count("deploy-steps") == 1


@pytest.mark.asyncio
async def test_select_memories_model_limits_results() -> None:
    catalog = [
        MemoryCatalogEntry(name=f"memory-{i}", type=MemoryType.PROJECT, description=f"memory {i}")
        for i in range(10)
    ]
    query = MemoryQuery(alert_summary="test", recent_text="")

    # Mock model returning too many
    model = AsyncMock()
    model.ainvoke.return_value = MockModelResponse(
        content='{"selected_memories": ["memory-0", "memory-1", "memory-2", "memory-3", "memory-4", "memory-5"], "reason": "test"}'
    )

    selection = await select_memories(query, catalog, model, limit=3)
    assert selection.mode == "model"
    assert len(selection.filenames) == 3


@pytest.mark.asyncio
async def test_select_memories_fallback_to_keyword() -> None:
    catalog = [
        MemoryCatalogEntry(name="deploy-steps", type=MemoryType.PROCEDURE, description="deploy runbook"),
        MemoryCatalogEntry(name="api-conventions", type=MemoryType.PROJECT, description="API design conventions"),
    ]
    query = MemoryQuery(alert_summary="deploy failed", recent_text="trying to deploy")

    # Mock model that raises exception
    model = AsyncMock()
    model.ainvoke.side_effect = Exception("model error")

    selection = await select_memories(query, catalog, model, limit=5)
    assert selection.mode == "keyword"
    assert "deploy-steps" in selection.filenames
    assert "keyword" in selection.reason.lower()


@pytest.mark.asyncio
async def test_select_memories_keyword_fallback_empty_tokens() -> None:
    catalog = [
        MemoryCatalogEntry(name="deploy-steps", type=MemoryType.PROCEDURE, description="deploy runbook"),
    ]
    query = MemoryQuery(alert_summary="!!!", recent_text="???")

    # Mock model that raises exception
    model = AsyncMock()
    model.ainvoke.side_effect = Exception("model error")

    selection = await select_memories(query, catalog, model, limit=5)
    assert selection.mode == "empty"
    assert "no tokens" in selection.reason.lower()


@pytest.mark.asyncio
async def test_select_memories_limit_respected() -> None:
    catalog = [
        MemoryCatalogEntry(name=f"memory-{i}", type=MemoryType.PROJECT, description=f"memory {i}")
        for i in range(10)
    ]
    query = MemoryQuery(alert_summary="test", recent_text="test")

    # Mock model that raises exception to force keyword fallback
    model = AsyncMock()
    model.ainvoke.side_effect = Exception("model error")

    selection = await select_memories(query, catalog, model, limit=3)
    assert len(selection.filenames) <= 3
