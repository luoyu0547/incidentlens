"""Types and validation for project-scoped, evidence-backed Project Memory.

Project Memory records only verified, evidence-backed outcomes extracted after
a completed investigation.  Every record is immutable (``frozen=True``) and
rejects unknown fields so a model can never smuggle raw tool output, unverified
hypotheses or secrets into a persisted record.  Records stay bounded: ``fact``
carries an explicit ceiling and the service enforces a tighter operational bound
before anything is persisted, and ``source_investigation_id`` may be blank so
the service can reject empty provenance with its own stable message instead of
a schema error.

This module also carries the shared ``ProjectMemoryRejected`` exception so the
store, service and callers all reject Project Memory the same way.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ProjectMemoryKind(StrEnum):
    """Vocabulary of persisted Project Memory outcomes.

    ``UNVERIFIED_HYPOTHESIS`` is part of the extraction vocabulary so a stray
    hypothesis can be recognized and rejected by the service; it is never
    persisted as a record.
    """

    FACT = "verified_fact"
    RELATIONSHIP = "service_relationship"
    FAILURE_MODE = "failure_mode"
    REPAIR = "repair"
    ROLLBACK_LESSON = "rollback_lesson"
    UNVERIFIED_HYPOTHESIS = "unverified_hypothesis"


ACCEPTED_PROJECT_MEMORY_KINDS = frozenset(
    kind
    for kind in ProjectMemoryKind
    if kind is not ProjectMemoryKind.UNVERIFIED_HYPOTHESIS
)


class ProjectMemoryStatus(StrEnum):
    """Lifecycle status of one persisted Project Memory record.

    Supersession is a status transition on the existing row: the historical
    record and its provenance are preserved, never destructively overwritten.
    """

    ACTIVE = "active"
    SUPERSEDED = "superseded"


def _validate_unique_citations(
    cls: type[object], value: tuple[str, ...]
) -> tuple[str, ...]:
    """Reject empty-string or duplicate evidence citations."""
    if any(not citation.strip() for citation in value):
        raise ValueError("evidence_ids must not contain empty strings")
    if len(value) != len(set(value)):
        raise ValueError("evidence_ids must be unique")
    return value


class ProjectMemoryEntry(BaseModel):
    """One immutable, bounded, provenance-bearing Project Memory record.

    ``service_names`` may be a non-empty or empty tuple: a project-level fact
    with no specific service is valid, but the service normalizes (lowercases,
    strips and deduplicates) the names before anything is persisted.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    memory_id: str = Field(min_length=1, max_length=120)
    project_id: str = Field(min_length=1, max_length=80)
    service_names: tuple[str, ...] = Field(default=(), max_length=32)
    fact: str = Field(min_length=1, max_length=8_000)
    kind: ProjectMemoryKind
    source_investigation_id: str = Field(max_length=120)
    evidence_ids: tuple[str, ...] = Field(default=(), max_length=24)
    status: ProjectMemoryStatus = ProjectMemoryStatus.ACTIVE
    created_at: datetime
    last_confirmed_at: datetime

    _validate_evidence_ids = field_validator("evidence_ids")(
        _validate_unique_citations
    )

    @field_validator("created_at", "last_confirmed_at")
    @classmethod
    def _timestamps_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("project memory timestamps must be timezone-aware")
        return value.astimezone(UTC)


class ProjectMemoryRejected(Exception):
    """Raised when an extracted Project Memory entry fails a safety rule.

    Instances carry a stable, specific message naming the violated rule so
    callers and tests can distinguish rejections deterministically.
    """
