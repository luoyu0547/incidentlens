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
# ORM model for investigation audit records
# ---------------------------------------------------------------------------


class InvestigationAuditRow(InvestigationBase):
    __tablename__ = "investigation_audits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    incident_id: Mapped[str] = mapped_column(String(255), index=True)
    action: Mapped[str] = mapped_column(String(64))
    details_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(tz=timezone.utc),
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

    # LangGraph agent telemetry fields
    loaded_skill_names: list[str] = Field(default_factory=list)
    model_profile: str = ""
    model_call_count: int = 0
    tool_call_count: int = 0
    fallback_used: bool = False
    last_error_code: str | None = None
    last_checkpoint_id: str | None = None

    # Conclusion phase fields
    conclusion_phase: bool = False
    eligible_cause_codes: list[str] = Field(default_factory=list)
    eligible_evidence_ids: list[str] = Field(default_factory=list)
    conclusion_status: str = "not_ready"
    conclusion_attempt_count: int = 0
    last_report_rejection_reason: str | None = None

    model_config = {"use_enum_values": False}

    @property
    def alert_json(self) -> str:
        """Serialize alert dict to JSON using model_dump(mode='json')."""
        return json.dumps(self.alert, default=self._json_default)

    @property
    def hypotheses_json(self) -> str:
        """Serialize hypotheses to JSON using model_dump(mode='json')."""
        return json.dumps(
            [h.model_dump(mode="json") for h in self.hypotheses],
            default=self._json_default,
        )

    @property
    def evidence_json(self) -> str:
        """Serialize evidence to JSON using model_dump(mode='json')."""
        return json.dumps(
            [e.model_dump(mode="json") for e in self.evidence],
            default=self._json_default,
        )

    @property
    def report_json(self) -> str:
        """Serialize report to JSON using model_dump(mode='json')."""
        return json.dumps(self.report, default=self._json_default)

    @property
    def retrieved_cases_json(self) -> str:
        """Serialize retrieved_cases to JSON."""
        return json.dumps(self.retrieved_cases, default=self._json_default)

    @staticmethod
    def _json_default(obj: Any) -> Any:
        """Fallback JSON serializer for non-standard types like datetime."""
        if isinstance(obj, datetime):
            return obj.isoformat()
        raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


# ---------------------------------------------------------------------------
# CheckpointStore — persist and load investigation state
# ---------------------------------------------------------------------------


class CheckpointStore:
    """Deterministic-baseline compatibility checkpoint store.

    Persists and loads investigation state via SQLAlchemy.
    For LLM agent mode, prefer LangGraph's ``AsyncSqliteSaver`` instead.
    """

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
                table: Any = InvestigationCheckpointRow.__table__
                table.drop(self._engine)
        InvestigationBase.metadata.create_all(self._engine)

    def save(self, state: InvestigationState) -> None:
        """Persist investigation state to the database (append-only).

        Always inserts a new row so that checkpoint history is preserved.
        Use load() to retrieve the latest checkpoint for a given incident.
        """
        with Session(self._engine) as session:
            data = {
                "incident_id": state.incident_id,
                "status": (
                    state.status.value
                    if isinstance(state.status, InvestigationStatus)
                    else state.status
                ),
                "current_round": state.current_round,
                "max_rounds": state.max_rounds,
                "alert_json": state.alert_json,
                "hypotheses_json": state.hypotheses_json,
                "evidence_json": state.evidence_json,
                "report_json": state.report_json,
                "retrieved_cases_json": state.retrieved_cases_json,
                "phase": state.phase,
            }

            row = InvestigationCheckpointRow(**data)
            session.add(row)
            session.commit()

    def load(self, incident_id: str) -> InvestigationState | None:
        """Load the latest investigation checkpoint for *incident_id*.

        Returns ``None`` if no checkpoint exists.
        """
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
                incident_id=incident_id,
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
# InvestigationAuditStore — record audit trail for investigations
# ---------------------------------------------------------------------------


class InvestigationAuditStore:
    """Records audit entries for investigation phase transitions and tool calls."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Ensure the audit table exists."""
        from sqlalchemy import inspect

        inspector = inspect(self._engine)
        if inspector.has_table(InvestigationAuditRow.__tablename__):
            existing_columns = {col["name"] for col in inspector.get_columns(
                InvestigationAuditRow.__tablename__
            )}
            expected_columns = {
                c.name for c in InvestigationAuditRow.__table__.columns
            }
            if not expected_columns.issubset(existing_columns):
                table: Any = InvestigationAuditRow.__table__
                table.drop(self._engine)
        InvestigationBase.metadata.create_all(self._engine)

    def record(self, incident_id: str, action: str, details: dict[str, Any] | None = None) -> None:
        """Record an audit entry for an investigation.

        Args:
            incident_id: The investigation this audit belongs to.
            action: The action type, e.g. "phase_transition", "tool_call".
            details: Optional dict of additional details (stored as JSON).
        """
        with Session(self._engine) as session:
            row = InvestigationAuditRow(
                incident_id=incident_id,
                action=action,
                details_json=json.dumps(details or {}, default=InvestigationState._json_default),
            )
            session.add(row)
            session.commit()

    def list_for_incident(
        self, incident_id: str, action: str | None = None
    ) -> list[dict[str, Any]]:
        """Return audit entries for an incident, optionally filtered by action.

        Returns a list of dicts with keys: id, incident_id, action, details.
        """
        with Session(self._engine) as session:
            query = session.query(InvestigationAuditRow).filter_by(
                incident_id=incident_id
            )
            if action is not None:
                query = query.filter_by(action=action)
            rows = query.order_by(InvestigationAuditRow.id).all()
            return [
                {
                    "id": row.id,
                    "incident_id": row.incident_id,
                    "action": row.action,
                    "details": json.loads(row.details_json) if row.details_json else {},
                }
                for row in rows
            ]


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
