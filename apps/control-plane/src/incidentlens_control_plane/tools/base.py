"""Base classes for audited read-only tools.

Provides:
  - ReadOnlyTool: abstract base for all read-only tools with timeout, retry,
    audit trail, and unified ToolResult return.
  - AuditStore: records and queries tool audit records.
  - ToolAuditRow: SQLAlchemy model for the tool_audits table.
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

from incidentlens_contracts.models import ToolResult
from pydantic import BaseModel
from sqlalchemy import DateTime, Engine, Float, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

# ---------------------------------------------------------------------------
# ORM model for tool_audits table
# ---------------------------------------------------------------------------


class AuditBase(DeclarativeBase):
    """Base class for audit ORM models."""
    pass


class ToolAuditRow(AuditBase):
    __tablename__ = "tool_audits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tool_name: Mapped[str] = mapped_column(String(255))
    parameters: Mapped[str] = mapped_column(Text)
    result_summary: Mapped[str] = mapped_column(Text, default="")
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0)
    retries: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(tz=timezone.utc)
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tool_name": self.tool_name,
            "parameters": self.parameters,
            "result_summary": self.result_summary,
            "duration_ms": self.duration_ms,
            "retries": self.retries,
            "error": self.error,
            "occurred_at": self.occurred_at,
        }


# ---------------------------------------------------------------------------
# AuditStore — write and query audit records
# ---------------------------------------------------------------------------


class AuditStore:
    """Manages tool audit records in the tool_audits table."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        # Ensure the audit table exists
        AuditBase.metadata.create_all(engine)

    def record(
        self,
        *,
        tool_name: str,
        parameters: str,
        result_summary: str = "",
        duration_ms: float = 0.0,
        retries: int = 0,
        error: str | None = None,
    ) -> ToolAuditRow:
        """Record a tool invocation audit entry."""
        with Session(self._engine) as session:
            row = ToolAuditRow(
                tool_name=tool_name,
                parameters=parameters,
                result_summary=result_summary,
                duration_ms=duration_ms,
                retries=retries,
                error=error,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    def latest(self) -> ToolAuditRow:
        """Return the most recent audit record."""
        from sqlalchemy import select

        with Session(self._engine) as session:
            stmt = (
                select(ToolAuditRow)
                .order_by(ToolAuditRow.id.desc())
                .limit(1)
            )
            row = session.scalars(stmt).first()
            if row is None:
                raise ValueError("No audit records found")
            return row


# ---------------------------------------------------------------------------
# ReadOnlyTool — abstract base
# ---------------------------------------------------------------------------


class ReadOnlyTool(ABC):
    """Abstract base class for all read-only tools.

    Enforces:
      - permission = "read_only"
      - timeout_seconds = 3
      - max_retries = 1
      - All invocations are audited
      - Returns ToolResult[Any] (never throws unhandled exceptions)
    """

    _permission: str = "read_only"
    _timeout_seconds: int = 3
    _max_retries: int = 1

    def __init__(self, audit_store: AuditStore) -> None:
        self._audit_store = audit_store

    @abstractmethod
    def _tool_name(self) -> str:
        """Return the name of this tool for audit purposes."""
        ...

    @abstractmethod
    async def _execute(self, args: BaseModel) -> ToolResult[Any]:
        """Execute the tool logic. Must return ToolResult[Any]."""
        ...

    async def invoke(self, args: BaseModel) -> ToolResult[Any]:
        """Invoke the tool with audit trail, timeout, and retry.

        Every invocation is recorded in tool_audits with parameter/result
        summary, duration, retries, and errors. Empty results and timeouts
        return ToolResult, never throw unhandled exceptions.
        """
        start = time.monotonic()
        retries = 0
        error_msg: str | None = None
        result: ToolResult[Any] | None = None

        for attempt in range(self._max_retries + 1):
            try:
                result = await asyncio.wait_for(
                    self._execute(args),
                    timeout=self._timeout_seconds,
                )
                break
            except asyncio.TimeoutError:
                error_msg = f"Tool timed out after {self._timeout_seconds}s"
                retries = attempt
                result = ToolResult(
                    ok=False,
                    error=error_msg,
                    metadata={"timeout": True, "retries": retries},
                )
                if attempt < self._max_retries:
                    continue
            except Exception as exc:
                error_msg = str(exc)
                retries = attempt
                if attempt < self._max_retries:
                    continue
                result = ToolResult(
                    ok=False,
                    error=error_msg,
                    metadata={"retries": retries},
                )

        duration_ms = (time.monotonic() - start) * 1000

        # Record audit
        params_str = (
            args.model_dump_json() if hasattr(args, "model_dump_json") else str(args)
        )
        result_summary = ""
        if result is not None:
            if result.ok:
                data = result.data
                if isinstance(data, list):
                    result_summary = f"returned {len(data)} items"
                elif data is not None:
                    result_summary = "returned data"
                else:
                    result_summary = "returned empty"
            else:
                result_summary = f"error: {result.error}"

        self._audit_store.record(
            tool_name=self._tool_name(),
            parameters=params_str,
            result_summary=result_summary,
            duration_ms=duration_ms,
            retries=retries,
            error=error_msg,
        )

        return result or ToolResult(ok=False, error="no result produced")
