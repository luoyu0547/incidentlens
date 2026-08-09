"""Compaction domain contracts.

Defines configuration, outcomes, and error types for session memory compaction.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class CompactionOutcome(StrEnum):
    """Outcome of a compaction operation."""

    SUCCESS = "success"
    PARTIAL = "partial"
    SKIPPED = "skipped"
    FAILED = "failed"


class CompactionConfig(BaseModel):
    """Immutable configuration for compaction operations.

    These are hard limits to prevent runaway compaction and ensure
    predictable behavior.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_messages_before_compact: int = Field(default=200, gt=0)
    max_messages_after_compact: int = Field(default=50, gt=0)
    min_messages_to_compact: int = Field(default=100, gt=0)
    max_snapshot_bytes: int = Field(default=65_536, gt=0)  # 64 KiB
    max_evidence_references: int = Field(default=500, gt=0)
    require_all_evidence_ids: bool = Field(default=True)


class CompactionLimits(BaseModel):
    """Limits for tool budget and micro compaction operations."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # Tool budget limits
    max_tool_output_bytes: int = Field(default=131_072, gt=0)  # 128 KB
    preview_size_bytes: int = Field(default=500, gt=0)  # 500 bytes

    # Micro compaction limits
    keep_recent_results: int = Field(default=3, gt=0)  # Keep 3 most recent
    max_snip_tokens: int = Field(default=10_000, gt=0)  # Target for snipping

    # Atomic write settings
    atomic_write_buffer_size: int = Field(default=8_192, gt=0)  # 8 KB buffer


class CompactionResult(BaseModel):
    """Outcome of a compaction operation."""

    model_config = ConfigDict(extra="forbid")

    outcome: CompactionOutcome
    messages_removed: int = Field(ge=0)
    messages_remaining: int = Field(ge=0)
    snapshot_path: str | None = None
    error: str | None = None
    details: dict[str, str | int | bool | list[Any]] = Field(default_factory=dict)


class ToolOutputReference(BaseModel):
    """Reference to a persisted tool output on disk."""

    model_config = ConfigDict(extra="forbid")

    path: str  # Absolute path to persisted file
    size_bytes: int = Field(ge=0)
    digest_sha256: str  # SHA-256 hex digest
    preview: str  # First preview_size_bytes of the content
    reread_instruction: str  # Instruction for model to reread if needed


class CompactionError(BaseModel):
    """Error types for compaction operations."""

    model_config = ConfigDict(extra="forbid")

    code: Literal[
        "missing_evidence",
        "snapshot_too_large",
        "invalid_state",
        "persistence_failed",
        "validation_failed",
    ]
    message: str
    details: dict[str, str | int | bool] = Field(default_factory=dict)
