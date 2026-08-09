"""Tests for project_memory domain types."""

from __future__ import annotations

import pytest
from incidentlens_control_plane.project_memory.domain import (
    LoadedMemories,
    MemoryCandidate,
    MemoryCatalogEntry,
    MemoryLimits,
    MemoryRecord,
    MemoryType,
    MemoryWriteResult,
)
from pydantic import ValidationError

# ---------------------------------------------------------------------------
# MemoryType
# ---------------------------------------------------------------------------


def test_memory_type_has_expected_values() -> None:
    assert MemoryType.PROJECT.value == "project"
    assert MemoryType.PROCEDURE.value == "procedure"
    assert MemoryType.FEEDBACK.value == "feedback"
    assert MemoryType.REFERENCE.value == "reference"


def test_memory_type_is_str_enum() -> None:
    assert isinstance(MemoryType.PROJECT, str)
    assert MemoryType.PROJECT == "project"


# ---------------------------------------------------------------------------
# MemoryLimits
# ---------------------------------------------------------------------------


def test_memory_limits_defaults() -> None:
    limits = MemoryLimits()
    assert limits.max_name_length == 62
    assert limits.max_description_length == 500
    assert limits.max_body_bytes == 65_536
    assert limits.max_total_entries == 256
    assert limits.max_frontmatter_bytes == 4_096


def test_memory_limits_is_frozen() -> None:
    limits = MemoryLimits()
    with pytest.raises(ValidationError):
        limits.max_body_bytes = 1  # type: ignore[misc]


def test_memory_limits_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        MemoryLimits(unknown_field=True)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# MemoryCandidate
# ---------------------------------------------------------------------------


def test_memory_candidate_valid() -> None:
    c = MemoryCandidate(
        name="api-conventions",
        description="API design conventions",
        type=MemoryType.PROJECT,
        body="# API Conventions\n\nUse REST.",
    )
    assert c.name == "api-conventions"
    assert c.type == MemoryType.PROJECT


@pytest.mark.parametrize(
    "name",
    [
        "A",
        "123",
        "UPPER",
        "has spaces",
        "has_underscore",
        "a" * 64,
        "-leading-dash",
    ],
)
def test_memory_candidate_rejects_invalid_names(name: str) -> None:
    with pytest.raises(ValidationError):
        MemoryCandidate(
            name=name,
            description="desc",
            type=MemoryType.PROJECT,
            body="body",
        )


@pytest.mark.parametrize(
    "name",
    [
        "ab",
        "my-project",
        "deploy-v2",
        "a1b2",
    ],
)
def test_memory_candidate_accepts_valid_names(name: str) -> None:
    c = MemoryCandidate(
        name=name,
        description="desc",
        type=MemoryType.PROJECT,
        body="body",
    )
    assert c.name == name


def test_memory_candidate_rejects_empty_description() -> None:
    with pytest.raises(ValidationError):
        MemoryCandidate(
            name="ok",
            description="",
            type=MemoryType.PROJECT,
            body="body",
        )


def test_memory_candidate_rejects_empty_body() -> None:
    with pytest.raises(ValidationError):
        MemoryCandidate(
            name="ok",
            description="desc",
            type=MemoryType.PROJECT,
            body="",
        )


def test_memory_candidate_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        MemoryCandidate(
            name="ok",
            description="desc",
            type=MemoryType.PROJECT,
            body="body",
            extra="nope",  # type: ignore[call-arg]
        )


# ---------------------------------------------------------------------------
# MemoryRecord
# ---------------------------------------------------------------------------


def test_memory_record_valid() -> None:
    r = MemoryRecord(
        name="test",
        type=MemoryType.REFERENCE,
        description="ref",
        path=".incidentlens/memory/test.md",
        size_bytes=100,
    )
    assert r.size_bytes == 100


def test_memory_record_rejects_negative_size() -> None:
    with pytest.raises(ValidationError):
        MemoryRecord(
            name="test",
            type=MemoryType.REFERENCE,
            description="ref",
            path=".incidentlens/memory/test.md",
            size_bytes=-1,
        )


# ---------------------------------------------------------------------------
# MemoryCatalogEntry
# ---------------------------------------------------------------------------


def test_catalog_entry_valid() -> None:
    e = MemoryCatalogEntry(
        name="runbook",
        type=MemoryType.PROCEDURE,
        description="deploy steps",
    )
    assert e.type == MemoryType.PROCEDURE


# ---------------------------------------------------------------------------
# MemoryWriteResult
# ---------------------------------------------------------------------------


def test_write_result_created() -> None:
    r = MemoryRecord(
        name="x", type=MemoryType.PROJECT, description="d", path="p", size_bytes=0
    )
    result = MemoryWriteResult(name="x", action="created", path="p", record=r)
    assert result.action == "created"


def test_write_result_updated() -> None:
    r = MemoryRecord(
        name="x", type=MemoryType.PROJECT, description="d", path="p", size_bytes=0
    )
    result = MemoryWriteResult(name="x", action="updated", path="p", record=r)
    assert result.action == "updated"


def test_write_result_rejects_invalid_action() -> None:
    r = MemoryRecord(
        name="x", type=MemoryType.PROJECT, description="d", path="p", size_bytes=0
    )
    with pytest.raises(ValidationError):
        MemoryWriteResult(name="x", action="deleted", path="p", record=r)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# LoadedMemories
# ---------------------------------------------------------------------------


def test_loaded_memories_empty() -> None:
    lm = LoadedMemories(entries=[], total_bytes=0)
    assert lm.truncated is False
    assert len(lm.entries) == 0


def test_loaded_memories_rejects_negative_total() -> None:
    with pytest.raises(ValidationError):
        LoadedMemories(entries=[], total_bytes=-1)
