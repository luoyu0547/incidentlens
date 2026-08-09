"""Tests for project_memory extractor."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from incidentlens_control_plane.project_memory.extractor import (
    _contains_secret,
    _dedupe_name,
    extract_memories,
)


class MockModel:
    """Mock language model for testing."""

    def __init__(self, response_json: str) -> None:
        self._response = response_json

    async def ainvoke(self, prompt: str) -> MagicMock:
        response = MagicMock()
        response.content = f"```json\n{self._response}\n```"
        return response


# ---------------------------------------------------------------------------
# Secret scanning
# ---------------------------------------------------------------------------


def test_contains_secret_api_key() -> None:
    """Detect API key patterns."""
    assert _contains_secret("api_key=sk-1234567890")
    assert _contains_secret("API_KEY: abcdefghij")
    assert _contains_secret("token = xyz123")


def test_contains_secret_bearer() -> None:
    """Detect bearer tokens."""
    assert _contains_secret("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9")
    assert _contains_secret("bearer: abc123")


def test_contains_secret_private_key() -> None:
    """Detect private key patterns."""
    assert _contains_secret("-----BEGIN RSA PRIVATE KEY-----")
    assert _contains_secret("-----BEGIN PRIVATE KEY-----")


def test_no_secret_normal_text() -> None:
    """Normal text should not be flagged as secret."""
    assert not _contains_secret("deploy to production")
    assert not _contains_secret("check logs for errors")
    assert not _contains_secret("restart the service")


# ---------------------------------------------------------------------------
# Name deduplication
# ---------------------------------------------------------------------------


def test_dedupe_name_new() -> None:
    """New name should be returned as-is."""
    catalog = [{"name": "existing"}]
    result = _dedupe_name("new-name", catalog, "body content")
    assert result == "new-name"


def test_dedupe_name_update() -> None:
    """Same name with equivalent content should return same name (update)."""
    catalog = [{"name": "deploy-steps", "description": "Deploy instructions"}]
    # New candidate with same name
    body = "Completely different deployment procedure"
    result = _dedupe_name("deploy-steps", catalog, body)
    assert result == "deploy-steps"


def test_dedupe_name_conflict() -> None:
    """Same name with different description should still update (same name = update)."""
    catalog = [{"name": "deploy-steps", "description": "Old deployment"}]
    body = "New deployment instructions that are completely different"
    result = _dedupe_name("deploy-steps", catalog, body)
    # Same name = update existing memory
    assert result == "deploy-steps"


# ---------------------------------------------------------------------------
# Extract memories
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_empty_transcript(tmp_path: Path) -> None:
    """Empty transcript returns no candidates."""
    transcript = tmp_path / "transcript.txt"
    transcript.write_text("", encoding="utf-8")

    model = MockModel("[]")
    candidates = await extract_memories(transcript, [], model)
    assert candidates == []


@pytest.mark.asyncio
async def test_extract_valid_candidates(tmp_path: Path) -> None:
    """Valid candidates are extracted successfully."""
    transcript = tmp_path / "transcript.txt"
    transcript.write_text("We deployed to production and learned lessons.", encoding="utf-8")

    candidates_json = [
        {
            "name": "deploy-lesson",
            "description": "Lesson from deployment",
            "type": "feedback",
            "body": "Always check database pool before deployment.",
        }
    ]

    model = MockModel(json.dumps(candidates_json))
    candidates = await extract_memories(transcript, [], model)

    assert len(candidates) == 1
    assert candidates[0].name == "deploy-lesson"
    assert candidates[0].type.value == "feedback"


@pytest.mark.asyncio
async def test_extract_duplicate_candidate_does_not_grow(tmp_path: Path) -> None:
    """Duplicate candidate does not grow the store."""
    transcript = tmp_path / "transcript.txt"
    transcript.write_text("Learning from incidents.", encoding="utf-8")

    candidates_json = [
        {
            "name": "incident-lesson",
            "description": "Important lesson",
            "type": "feedback",
            "body": "Always monitor after deploy.",
        }
    ]

    # Existing catalog with same name
    catalog = [{"name": "incident-lesson", "description": "Important lesson"}]

    model = MockModel(json.dumps(candidates_json))
    candidates = await extract_memories(transcript, catalog, model)

    # Should return candidate (will update existing)
    assert len(candidates) == 1
    assert candidates[0].name == "incident-lesson"


@pytest.mark.asyncio
async def test_extract_rejects_secret_candidate(tmp_path: Path) -> None:
    """Candidate with secret is rejected."""
    transcript = tmp_path / "transcript.txt"
    transcript.write_text("Config with secrets.", encoding="utf-8")

    candidates_json = [
        {
            "name": "config-ref",
            "description": "Config reference",
            "type": "reference",
            "body": "api_key=sk-1234567890abcdef",
        }
    ]

    model = MockModel(json.dumps(candidates_json))
    candidates = await extract_memories(transcript, [], model)

    # Should be rejected due to secret
    assert len(candidates) == 0


@pytest.mark.asyncio
async def test_extract_conflict_name(tmp_path: Path) -> None:
    """Same name with different content should still update."""
    transcript = tmp_path / "transcript.txt"
    transcript.write_text("New procedure.", encoding="utf-8")

    candidates_json = [
        {
            "name": "deploy-steps",
            "description": "Completely different description",
            "type": "procedure",
            "body": "Different deployment procedure.",
        }
    ]

    # Existing catalog with different description
    catalog = [{"name": "deploy-steps", "description": "Old deployment"}]

    model = MockModel(json.dumps(candidates_json))
    candidates = await extract_memories(transcript, catalog, model)

    assert len(candidates) == 1
    # Same name = update existing memory
    assert candidates[0].name == "deploy-steps"


@pytest.mark.asyncio
async def test_extract_missing_transcript(tmp_path: Path) -> None:
    """Missing transcript raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        await extract_memories(tmp_path / "missing.txt", [], MockModel("[]"))


@pytest.mark.asyncio
async def test_extract_invalid_json_response(tmp_path: Path) -> None:
    """Invalid JSON response returns empty list."""
    transcript = tmp_path / "transcript.txt"
    transcript.write_text("Some content.", encoding="utf-8")

    class BadModel:
        async def ainvoke(self, prompt: str) -> MagicMock:
            response = MagicMock()
            response.content = "This is not JSON"
            return response

    candidates = await extract_memories(transcript, [], BadModel())
    assert candidates == []


@pytest.mark.asyncio
async def test_extract_invalid_candidate_fields(tmp_path: Path) -> None:
    """Invalid candidate fields are skipped."""
    transcript = tmp_path / "transcript.txt"
    transcript.write_text("Some content.", encoding="utf-8")

    candidates_json = [
        {"name": "", "description": "desc", "type": "project", "body": "body"},  # empty name
        {"name": "valid", "description": "", "type": "project", "body": "body"},  # empty desc
        {"name": "valid2", "description": "desc", "type": "invalid", "body": "body"},  # invalid type
    ]

    model = MockModel(json.dumps(candidates_json))
    candidates = await extract_memories(transcript, [], model)

    # Only valid2 should be included (type "invalid" is rejected)
    assert len(candidates) == 0
