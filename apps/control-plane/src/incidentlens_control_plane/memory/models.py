"""Case memory ORM models.

Only `human_verified` cases are indexed for FTS search.
Historical cases can only generate candidate hypotheses.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class CaseBase(DeclarativeBase):
    """Base class for case memory ORM models."""
    pass


class CaseRow(CaseBase):
    __tablename__ = "case_memory"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    status: Mapped[str] = mapped_column(String(64), default="pending_review")
    symptom: Mapped[str] = mapped_column(Text, default="")
    service: Mapped[str] = mapped_column(String(255), default="")
    root_cause: Mapped[str] = mapped_column(Text, default="")
    resolution: Mapped[str] = mapped_column(Text, default="")
    evidence_summary: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(tz=timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(tz=timezone.utc),
        onupdate=lambda: datetime.now(tz=timezone.utc),
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "symptom": self.symptom,
            "service": self.service,
            "root_cause": self.root_cause,
            "resolution": self.resolution,
            "evidence_summary": self.evidence_summary,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class CaseFTSRow(CaseBase):
    """FTS index table for human_verified cases only.

    Uses a simple keyword-based search approach compatible with SQLite.
    For production, this would be replaced with a proper FTS5 virtual table.
    """
    __tablename__ = "case_fts_index"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[int] = mapped_column(Integer, index=True)
    keyword: Mapped[str] = mapped_column(String(255), index=True)
    service: Mapped[str] = mapped_column(String(255), index=True)
