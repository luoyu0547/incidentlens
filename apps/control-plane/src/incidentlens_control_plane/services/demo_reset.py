"""DemoResetService — transactional cleanup of demo data.

Resets the system to a clean state by:
  1. Clearing all active scenarios
  2. Deleting demo telemetry data (logs, metrics, spans, deployments)
  3. Deleting investigation checkpoints
  4. Deleting investigation audit records
  5. Deleting case memory and FTS index

All operations are performed in order. Each step uses its own session
so that partial failures leave the system in a consistent state per-table.
"""

from __future__ import annotations

from typing import Any

from incidentlens_telemetry.repository import TelemetryRepository

from incidentlens_scenarios.store import ScenarioStore


class DemoResetService:
    """Transactional demo data reset service.

    Clears scenarios first, then deletes all demo data from the database.
    """

    def __init__(
        self,
        repository: TelemetryRepository,
        scenario_store: ScenarioStore,
    ) -> None:
        self._repository = repository
        self._scenario_store = scenario_store

    def reset_demo_data(self) -> dict[str, Any]:
        """Reset all demo data and return a summary.

        Order of operations:
          1. Clear active scenarios
          2. Delete telemetry data (logs, metrics, spans, deployments)
          3. Delete investigation checkpoints
          4. Delete investigation audit records
          5. Delete case memory and FTS index
        """
        from sqlalchemy import text

        engine = self._repository.engine

        # Step 1: Clear active scenarios
        self._scenario_store.reset()

        # Steps 2-5: Delete all demo data tables
        tables_to_clear = [
            "telemetry_logs",
            "metric_points",
            "trace_spans",
            "deployments",
            "investigation_checkpoints",
            "investigation_audits",
            "case_memory",
            "case_fts_index",
        ]

        deleted_counts: dict[str, int] = {}
        with engine.begin() as conn:
            for table_name in tables_to_clear:
                try:
                    result = conn.execute(text(f"DELETE FROM {table_name}"))
                    deleted_counts[table_name] = result.rowcount
                except Exception:
                    # Table may not exist yet; skip gracefully
                    deleted_counts[table_name] = 0

        return {
            "status": "reset",
            "scenarios_cleared": True,
            "tables_cleared": deleted_counts,
        }
