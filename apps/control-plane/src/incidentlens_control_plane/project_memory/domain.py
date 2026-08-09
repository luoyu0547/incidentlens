"""Project memory domain types.

Defines the validated types for the file-backed project memory system:
four memory categories, bounded limits, name validation, and result models.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class MemoryType(StrEnum):
    """Categories of project memory."""

    PROJECT = "project"
    PROCEDURE = "procedure"
    FEEDBACK = "feedback"
    REFERENCE = "reference"


class MemoryLimits(BaseModel):
    """Immutable hard defaults for memory constraints.

    These are not configurable — they guard against runaway writes and
    oversized memory banks.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_name_length: int = Field(default=62, gt=0)
    max_description_length: int = Field(default=500, gt=0)
    max_body_bytes: int = Field(default=65_536, gt=0)  # 64 KiB
    max_total_entries: int = Field(default=256, gt=0)
    max_frontmatter_bytes: int = Field(default=4_096, gt=0)


_MEMORY_NAME_PATTERN = r"^[a-z][a-z0-9\-]{1,62}$"


class MemoryCandidate(BaseModel):
    """Validated input for writing a new memory entry."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=2, max_length=63, pattern=_MEMORY_NAME_PATTERN)
    description: str = Field(min_length=1, max_length=500)
    type: MemoryType
    body: str = Field(min_length=1, max_length=65_536)


class MemoryRecord(BaseModel):
    """A memory file discovered on disk."""

    model_config = ConfigDict(extra="forbid")

    name: str
    type: MemoryType
    description: str
    path: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(tz=timezone.utc))
    size_bytes: int = Field(ge=0)


class MemoryCatalogEntry(BaseModel):
    """A single row in the MEMORY.md index."""

    model_config = ConfigDict(extra="forbid")

    name: str
    type: MemoryType
    description: str


class MemoryWriteResult(BaseModel):
    """Outcome of a write operation."""

    model_config = ConfigDict(extra="forbid")

    name: str
    action: Literal["created", "updated"]
    path: str
    record: MemoryRecord


class LoadedMemories(BaseModel):
    """Bounded collection of loaded memory content."""

    model_config = ConfigDict(extra="forbid")

    entries: list[MemoryRecord]
    total_bytes: int = Field(ge=0)
    truncated: bool = False


class MemoryQuery(BaseModel):
    """Query for selecting relevant memories."""

    model_config = ConfigDict(extra="forbid")

    alert_summary: str = Field(min_length=1, max_length=10_000)
    recent_text: str = Field(min_length=0, max_length=10_000, default="")


class MemorySelection(BaseModel):
    """Result of memory selection."""

    model_config = ConfigDict(extra="forbid")

    filenames: list[str] = Field(default_factory=list)
    mode: Literal["model", "keyword", "empty"] = "empty"
    reason: str = ""
