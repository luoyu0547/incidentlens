"""SQLAlchemy ORM models for telemetry persistence.

Tables:
  - telemetry_logs   : log events with level and message
  - metric_points    : numeric metric observations
  - trace_spans      : distributed-tracing spans
  - deployments      : service deployment records
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base class for all telemetry ORM models."""

    pass


class LogRow(Base):
    __tablename__ = "telemetry_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    service: Mapped[str] = mapped_column(String(255), index=True)
    trace_id: Mapped[str] = mapped_column(String(255), index=True)
    level: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "service": self.service,
            "trace_id": self.trace_id,
            "level": self.level,
            "message": self.message,
            "occurred_at": self.occurred_at,
        }


class MetricRow(Base):
    __tablename__ = "metric_points"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    service: Mapped[str] = mapped_column(String(255), index=True)
    trace_id: Mapped[str] = mapped_column(String(255), index=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    value: Mapped[float] = mapped_column(Float)
    occurred_at: Mapped[datetime] = mapped_column(DateTime)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "service": self.service,
            "trace_id": self.trace_id,
            "name": self.name,
            "value": self.value,
            "occurred_at": self.occurred_at,
        }


class SpanRow(Base):
    __tablename__ = "trace_spans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    service: Mapped[str] = mapped_column(String(255))
    trace_id: Mapped[str] = mapped_column(String(255), index=True)
    span_id: Mapped[str] = mapped_column(String(255))
    parent_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    operation: Mapped[str] = mapped_column(String(512))
    occurred_at: Mapped[datetime] = mapped_column(DateTime)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "service": self.service,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "operation": self.operation,
            "occurred_at": self.occurred_at,
        }


class DeploymentRow(Base):
    __tablename__ = "deployments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    service: Mapped[str] = mapped_column(String(255), index=True)
    version: Mapped[str] = mapped_column(String(64))
    occurred_at: Mapped[datetime] = mapped_column(DateTime)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "service": self.service,
            "version": self.version,
            "occurred_at": self.occurred_at,
        }
