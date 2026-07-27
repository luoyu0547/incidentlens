"""Investigation state machine and checkpoint models.

State machine phases:
  parse_alert -> scope_incident -> retrieve_memory -> generate_hypotheses ->
  choose_next_action -> execute_tool -> record_evidence -> update_hypotheses ->
  verify_root_cause -> generate_report

Default max rounds: 8
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from incidentlens_contracts.models import (
    Evidence,
    Hypothesis,
    InvestigationStatus,
)
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

# ---------------------------------------------------------------------------
# Custom JSON encoder for datetime serialization
# ---------------------------------------------------------------------------


class _DateTimeEncoder(json.JSONEncoder):
    """JSON encoder that handles datetime objects."""

    def default(self, obj: Any) -> Any:
        if isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)


def _json_dumps(obj: Any) -> str:
    """JSON dumps with datetime support."""
    return json.dumps(obj, cls=_DateTimeEncoder)


# ---------------------------------------------------------------------------
# ORM Base for investigation tables
# ---------------------------------------------------------------------------


class InvestigationBase(DeclarativeBase):
    """Base class for investigation ORM models."""
    pass


# ---------------------------------------------------------------------------
# ORM model for investigation checkpoints
# ---------------------------------------------------------------------------


class InvestigationCheckpointRow(InvestigationBase):
    __tablename__ = "investigation_checkpoints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    incident_id: Mapped[str] = mapped_column(String(255), index=True)
    status: Mapped[str] = mapped_column(String(64))
    current_round: Mapped[int] = mapped_column(Integer, default=0)
    max_rounds: Mapped[int] = mapped_column(Integer, default=8)
    alert_json: Mapped[str] = mapped_column(Text, default="{}")
    hypotheses_json: Mapped[str] = mapped_column(Text, default="[]")
    evidence_json: Mapped[str] = mapped_column(Text, default="[]")
    report_json: Mapped[str] = mapped_column(Text, default="null")
    retrieved_cases_json: Mapped[str] = mapped_column(Text, default="[]")
    phase: Mapped[str] = mapped_column(String(64), default="parse_alert")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(tz=timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(tz=timezone.utc),
        onupdate=lambda: datetime.now(tz=timezone.utc),
    )


# ---------------------------------------------------------------------------
# Pydantic models for investigation state
# ---------------------------------------------------------------------------


class InvestigationState(BaseModel):
    """Current state of an investigation, used for checkpointing and resume."""

    incident_id: str
    status: InvestigationStatus = InvestigationStatus.SCOPING
    current_round: int = 0
    max_rounds: int = 8
    alert: dict[str, Any] = Field(default_factory=dict)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    report: dict[str, Any] | None = None
    phase: str = "parse_alert"
    retrieved_cases: list[dict[str, Any]] = Field(default_factory=list)

    model_config = {"use_enum_values": False}


# ---------------------------------------------------------------------------
# CheckpointStore — persist and load investigation state
# ---------------------------------------------------------------------------


class CheckpointStore:
    """Manages investigation state persistence via SQLAlchemy."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Ensure the investigation checkpoint table exists with the correct schema.

        If the table exists but is missing columns, drop and recreate it.
        This handles the case where the schema has evolved.
        """
        from sqlalchemy import inspect

        inspector = inspect(self._engine)
        if inspector.has_table(InvestigationCheckpointRow.__tablename__):
            existing_columns = {col["name"] for col in inspector.get_columns(
                InvestigationCheckpointRow.__tablename__
            )}
            expected_columns = {
                c.name for c in InvestigationCheckpointRow.__table__.columns
            }
            if not expected_columns.issubset(existing_columns):
                # Schema mismatch: drop and recreate
                InvestigationCheckpointRow.__table__.drop(self._engine)
        InvestigationBase.metadata.create_all(self._engine)

    def save(self, state: InvestigationState) -> None:
        """Persist investigation state to the database."""
        with Session(self._engine) as session:
            # Check if a checkpoint already exists for this incident
            from sqlalchemy import select

            stmt = (
                select(InvestigationCheckpointRow)
                .where(InvestigationCheckpointRow.incident_id == state.incident_id)
                .order_by(InvestigationCheckpointRow.id.desc())
                .limit(1)
            )
            existing = session.scalars(stmt).first()

            data = {
                "incident_id": state.incident_id,
                "status": (
                    state.status.value
                    if isinstance(state.status, InvestigationStatus)
                    else state.status
                ),
                "current_round": state.current_round,
                "max_rounds": state.max_rounds,
                "alert_json": _json_dumps(state.alert),
                "hypotheses_json": _json_dumps([h.model_dump() for h in state.hypotheses]),
                "evidence_json": _json_dumps([e.model_dump() for e in state.evidence]),
                "report_json": _json_dumps(state.report),
                "retrieved_cases_json": _json_dumps(state.retrieved_cases),
                "phase": state.phase,
            }

            if existing:
                for key, value in data.items():
                    setattr(existing, key, value)
                session.commit()
            else:
                row = InvestigationCheckpointRow(**data)
                session.add(row)
                session.commit()

    def load(self, incident_id: str) -> InvestigationState | None:
        """Load investigation state from the database."""
        from sqlalchemy import select

        with Session(self._engine) as session:
            stmt = (
                select(InvestigationCheckpointRow)
                .where(InvestigationCheckpointRow.incident_id == incident_id)
                .order_by(InvestigationCheckpointRow.id.desc())
                .limit(1)
            )
            row = session.scalars(stmt).first()
            if row is None:
                return None

            hypotheses = [
                Hypothesis(**h) for h in json.loads(row.hypotheses_json)
            ]
            evidence = [
                Evidence(**e) for e in json.loads(row.evidence_json)
            ]
            report = json.loads(row.report_json)
            retrieved_cases = json.loads(row.retrieved_cases_json)

            return InvestigationState(
                incident_id=row.incident_id,
                status=InvestigationStatus(row.status),
                current_round=row.current_round,
                max_rounds=row.max_rounds,
                alert=json.loads(row.alert_json),
                hypotheses=hypotheses,
                evidence=evidence,
                report=report,
                phase=row.phase,
                retrieved_cases=retrieved_cases,
            )


# ---------------------------------------------------------------------------
# State machine phase definitions
# ---------------------------------------------------------------------------

PHASES = [
    "parse_alert",
    "scope_incident",
    "retrieve_memory",
    "generate_hypotheses",
    "choose_next_action",
    "execute_tool",
    "record_evidence",
    "update_hypotheses",
    "verify_root_cause",
    "generate_report",
]

# Phases that constitute one "round" of investigation
ROUND_PHASES = [
    "choose_next_action",
    "execute_tool",
    "record_evidence",
    "update_hypotheses",
]
