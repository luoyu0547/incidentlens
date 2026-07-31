"""Evaluation run store — persists evaluation runs and records.

Tables:
  - evaluation_runs: tracks strategy/scenario/status/metrics per run
  - evaluation_run_records: stores individual RunRecord JSON per run

Statuses: running, completed, failed
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
)


class Base(DeclarativeBase):
    """Base class for evaluation ORM models."""

    pass


class EvaluationRunModel(Base):  # type: ignore[valid-type]
    """ORM model for evaluation_runs table."""

    __tablename__ = "evaluation_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy: Mapped[str] = mapped_column(String(64), nullable=False)
    scenario: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    metrics_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    records: Mapped[list[EvaluationRunRecordModel]] = relationship(
        "EvaluationRunRecordModel",
        back_populates="run",
        cascade="all, delete-orphan",
    )


class EvaluationRunRecordModel(Base):  # type: ignore[valid-type]
    """ORM model for evaluation_run_records table."""

    __tablename__ = "evaluation_run_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(Integer, ForeignKey("evaluation_runs.id"), nullable=False)
    scenario: Mapped[str] = mapped_column(String(64), nullable=False)
    record_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    run: Mapped[EvaluationRunModel] = relationship("EvaluationRunModel", back_populates="records")


class EvaluationRunStore:
    """Stores evaluation runs and provides comparison queries.

    Usage:
        store = EvaluationRunStore(engine)
        run_id = store.start("incidentlens_verified", "all")
        store.record(run_id, record_dict)
        store.complete(run_id, metrics_dict)
        comparison = store.latest_comparison(scenario="all")
    """

    def __init__(self, engine: Any) -> None:
        self._engine = engine
        Base.metadata.create_all(engine)

    def start(self, strategy: str, scenario: str) -> int:
        """Start a new evaluation run and return its ID."""
        now = datetime.now(timezone.utc)
        with Session(self._engine) as session:
            run = EvaluationRunModel(
                strategy=strategy,
                scenario=scenario,
                status="running",
                started_at=now,
            )
            session.add(run)
            session.commit()
            session.refresh(run)
            return run.id

    def record(self, run_id: int, record: dict[str, Any]) -> None:
        """Add a RunRecord to an existing run."""
        now = datetime.now(timezone.utc)
        with Session(self._engine) as session:
            run = session.get(EvaluationRunModel, run_id)
            if run is None:
                raise ValueError(f"Run {run_id} not found")
            if run.status != "running":
                raise ValueError(f"Run {run_id} is not running (status={run.status})")
            record_model = EvaluationRunRecordModel(
                run_id=run_id,
                scenario=run.scenario,
                record_json=json.dumps(record),
                created_at=now,
            )
            session.add(record_model)
            session.commit()

    def complete(self, run_id: int, metrics: dict[str, Any]) -> None:
        """Complete a run with aggregated metrics.

        Requires at least one record to have been added.
        Rejects completing an already failed run.
        """
        now = datetime.now(timezone.utc)
        with Session(self._engine) as session:
            run = session.get(EvaluationRunModel, run_id)
            if run is None:
                raise ValueError(f"Run {run_id} not found")
            if run.status == "failed":
                raise ValueError(f"Run {run_id} already failed")
            if run.status == "completed":
                raise ValueError(f"Run {run_id} already completed")

            # Check that at least one record exists
            record_count = (
                session.query(EvaluationRunRecordModel)
                .filter(EvaluationRunRecordModel.run_id == run_id)
                .count()
            )
            if record_count == 0:
                raise ValueError(
                    f"Run {run_id} has no records; need at least one to complete"
                )

            run.status = "completed"
            run.metrics_json = json.dumps(metrics)
            run.completed_at = now
            session.commit()

    def fail(self, run_id: int, error_summary: str) -> None:
        """Mark a run as failed."""
        now = datetime.now(timezone.utc)
        with Session(self._engine) as session:
            run = session.get(EvaluationRunModel, run_id)
            if run is None:
                raise ValueError(f"Run {run_id} not found")
            run.status = "failed"
            run.error_summary = error_summary
            run.completed_at = now
            session.commit()

    def latest_comparison(
        self, scenario: str = "all"
    ) -> list[dict[str, Any]]:
        """Return the latest completed run per strategy.

        Returns a list of dicts with keys: strategy, scenario, metrics, started_at, completed_at.
        """
        with Session(self._engine) as session:
            # Get all completed runs, ordered by completed_at desc
            query = (
                session.query(EvaluationRunModel)
                .filter(EvaluationRunModel.status == "completed")
            )
            if scenario != "all":
                query = query.filter(EvaluationRunModel.scenario == scenario)
            runs = query.order_by(EvaluationRunModel.completed_at.desc()).all()

            # Deduplicate by strategy, keeping the latest
            seen_strategies: set[str] = set()
            result: list[dict[str, Any]] = []
            for run in runs:
                if run.strategy in seen_strategies:
                    continue
                seen_strategies.add(run.strategy)
                metrics = json.loads(run.metrics_json) if run.metrics_json else {}
                result.append({
                    "strategy": run.strategy,
                    "scenario": run.scenario,
                    "metrics": metrics,
                    "started_at": run.started_at.isoformat() if run.started_at else None,
                    "completed_at": run.completed_at.isoformat() if run.completed_at else None,
                })
            return result
