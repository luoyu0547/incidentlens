"""Case memory ORM models.

Governed schema with:
  - Only `human_verified` cases are indexed for FTS search.
  - Historical cases can only generate candidate hypotheses.
  - Append-only review actions, feedback, usage events, and embeddings.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class CaseBase(DeclarativeBase):
    """Base class for case memory ORM models."""

    pass


class CaseRow(CaseBase):
    """Main case memory table with governed columns."""

    __tablename__ = "case_memory"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    incident_id: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    source_reference: Mapped[str] = mapped_column(String(512), default="")
    status: Mapped[str] = mapped_column(String(64), default="draft")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    symptom: Mapped[str] = mapped_column(Text, default="")
    affected_services_json: Mapped[str] = mapped_column(Text, default="[]")
    root_cause_category: Mapped[str] = mapped_column(String(255), default="")
    root_cause_description: Mapped[str] = mapped_column(Text, default="")
    key_evidence_json: Mapped[str] = mapped_column(Text, default="[]")
    investigation_path_json: Mapped[str] = mapped_column(Text, default="[]")
    invalid_hypotheses_json: Mapped[str] = mapped_column(Text, default="[]")
    resolution: Mapped[str] = mapped_column(Text, default="")
    remediation_advice_json: Mapped[str] = mapped_column(Text, default="[]")
    applicability_conditions_json: Mapped[str] = mapped_column(Text, default="[]")
    inapplicability_conditions_json: Mapped[str] = mapped_column(Text, default="[]")
    environment: Mapped[str] = mapped_column(String(255), default="")
    service_version_exact: Mapped[str] = mapped_column(String(255), default="")
    service_version_min: Mapped[str] = mapped_column(String(255), default="")
    service_version_max: Mapped[str] = mapped_column(String(255), default="")
    source_report_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(tz=timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(tz=timezone.utc),
        onupdate=lambda: datetime.now(tz=timezone.utc),
    )


class CaseReviewActionRow(CaseBase):
    """Append-only audit log for case review actions."""

    __tablename__ = "case_review_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[int] = mapped_column(Integer, index=True)
    action: Mapped[str] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(255), default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    previous_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    new_status: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(tz=timezone.utc),
    )


class CaseFeedbackRow(CaseBase):
    """Feedback on case search results with idempotency key."""

    __tablename__ = "case_feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[int] = mapped_column(Integer, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True)
    rating: Mapped[str] = mapped_column(String(64))
    incident_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    actor: Mapped[str] = mapped_column(String(255), default="")
    comment: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(tz=timezone.utc),
    )


class CaseUsageEventRow(CaseBase):
    """Tracks how a case was used in an investigation."""

    __tablename__ = "case_usage_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[int] = mapped_column(Integer, index=True)
    hypothesis_id: Mapped[str] = mapped_column(String(255), default="")
    event_type: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True)
    investigation_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    details_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(tz=timezone.utc),
    )


class CaseEmbeddingRow(CaseBase):
    """Vector embeddings for semantic search."""

    __tablename__ = "case_embeddings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[int] = mapped_column(Integer, index=True)
    embedding_json: Mapped[str] = mapped_column(Text, default="[]")
    dimension: Mapped[int] = mapped_column(Integer, default=0)
    model_name: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(tz=timezone.utc),
    )


class CaseSchemaVersionRow(CaseBase):
    """Schema version tracking."""

    __tablename__ = "incidentlens_schema_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    component: Mapped[str] = mapped_column(String(128))
    version: Mapped[int] = mapped_column(Integer)
    applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(tz=timezone.utc),
    )
