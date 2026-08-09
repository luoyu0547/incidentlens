"""Compaction domain contracts.

Defines configuration, outcomes, and error types for session memory compaction.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

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


class CompactionResult(BaseModel):
    """Outcome of a compaction operation."""

    model_config = ConfigDict(extra="forbid")

    outcome: CompactionOutcome
    messages_removed: int = Field(ge=0)
    messages_remaining: int = Field(ge=0)
    snapshot_path: str | None = None
    error: str | None = None
    details: dict[str, str | int | bool] = Field(default_factory=dict)


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
