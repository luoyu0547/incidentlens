"""ScenarioStore — persistent scenario state via SQLAlchemy.

Public interface:
  - enable(name, params)  — activate a fault scenario with parameters
  - disable(name)         — deactivate a specific fault scenario
  - reset()               — clear all active faults
  - runtime_for(service)  — return active faults for a given service (safe projection)

Key design:
  - Uses the same SQLAlchemy engine as TelemetryRepository
  - runtime_for() returns only safe parameters (never root_cause_label)
  - State persists across process restarts via SQLite
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import Engine, String, Text, delete, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from incidentlens_scenarios.models import SCENARIOS

# ---------------------------------------------------------------------------
# ORM Base for scenario tables
# ---------------------------------------------------------------------------


class ScenarioBase(DeclarativeBase):
    """Base class for scenario ORM models."""
    pass


# ---------------------------------------------------------------------------
# ORM model for active scenarios
# ---------------------------------------------------------------------------


class ActiveScenarioRow(ScenarioBase):
    __tablename__ = "active_scenarios"

    name: Mapped[str] = mapped_column(String(255), primary_key=True)
    target_service: Mapped[str] = mapped_column(String(255), index=True)
    parameters_json: Mapped[str] = mapped_column(Text, default="{}")


# ---------------------------------------------------------------------------
# Parameter validation
# ---------------------------------------------------------------------------


def _validate_params(name: str, params: dict[str, Any]) -> None:
    """Validate scenario parameters against known constraints.

    Raises ValueError if any parameter is out of range.
    """
    if name == "payment_error_rate":
        error_rate = params.get("error_rate")
        if error_rate is not None and not (0.0 <= error_rate <= 1.0):
            raise ValueError(
                f"error_rate must be between 0 and 1, got {error_rate}"
            )

    if name == "payment_delay":
        delay_ms = params.get("delay_ms")
        if delay_ms is not None and delay_ms <= 0:
            raise ValueError(
                f"delay_ms must be positive, got {delay_ms}"
            )

    if name == "db_pool_exhaustion":
        pool_size = params.get("pool_size")
        if pool_size is not None and pool_size < 1:
            raise ValueError(
                f"pool_size must be a positive integer, got {pool_size}"
            )


# ---------------------------------------------------------------------------
# ScenarioStore
# ---------------------------------------------------------------------------


class ScenarioStore:
    """Persistent scenario state backed by SQLAlchemy.

    Uses the same engine as TelemetryRepository so that scenario state
    and telemetry data share the same SQLite database.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Ensure the active_scenarios table exists."""
        ScenarioBase.metadata.create_all(self._engine)

    def enable(self, name: str, params: dict[str, Any] | None = None) -> None:
        """Activate a fault scenario with the given parameters.

        Merges user params with default params from the scenario definition.
        Raises ValueError if the scenario name is unknown or params are invalid.
        """
        if name not in SCENARIOS:
            raise ValueError(f"unknown scenario: {name}")

        scenario_def = SCENARIOS[name]
        merged = dict(scenario_def["default_params"])
        if params:
            merged.update(params)

        # Validate the merged parameters
        _validate_params(name, merged)

        with Session(self._engine) as session:
            # Upsert: if the scenario is already active, update its params
            stmt = select(ActiveScenarioRow).where(
                ActiveScenarioRow.name == name
            )
            row = session.scalars(stmt).first()
            if row is not None:
                row.parameters_json = json.dumps(merged)
                row.target_service = scenario_def["target_service"]
            else:
                row = ActiveScenarioRow(
                    name=name,
                    target_service=scenario_def["target_service"],
                    parameters_json=json.dumps(merged),
                )
                session.add(row)
            session.commit()

    def disable(self, name: str) -> None:
        """Deactivate a specific fault scenario.

        No-op if the scenario is not currently active.
        """
        with Session(self._engine) as session:
            stmt = select(ActiveScenarioRow).where(
                ActiveScenarioRow.name == name
            )
            row = session.scalars(stmt).first()
            if row is not None:
                session.delete(row)
                session.commit()

    def reset(self) -> None:
        """Clear all active faults."""
        with Session(self._engine) as session:
            session.execute(delete(ActiveScenarioRow))
            session.commit()

    def runtime_for(self, service: str) -> dict[str, dict[str, Any]]:
        """Return active faults for a given service.

        The returned dict maps scenario_name -> params.
        Root cause labels are NOT included in the output (defense-in-depth).
        """
        with Session(self._engine) as session:
            stmt = select(ActiveScenarioRow).where(
                ActiveScenarioRow.target_service == service
            )
            rows = session.scalars(stmt)
            result: dict[str, dict[str, Any]] = {}
            for row in rows:
                params = json.loads(row.parameters_json)
                # Explicitly exclude root_cause_label from output
                safe_params = {
                    k: v for k, v in params.items() if k != "root_cause_label"
                }
                result[row.name] = safe_params
            return result
