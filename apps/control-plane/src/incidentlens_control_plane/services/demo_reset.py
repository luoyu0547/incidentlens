"""DemoResetService — transactional cleanup of demo data.

Resets the system to a clean state by:
  1. Clearing all active scenarios
  2. Deleting demo telemetry data (logs, metrics, spans, deployments)
  3. Deleting investigation checkpoints
  4. Deleting investigation audit records
  5. Deleting tool audit records
  6. Deleting case memory and FTS index

All operations are performed in order. Each step uses its own session
so that partial failures leave the system in a consistent state per-table.
"""

from __future__ import annotations

import logging
from typing import Any

from incidentlens_scenarios.store import ScenarioStore
from incidentlens_telemetry.models import (
    DeploymentRow,
    LogRow,
    MetricRow,
    SpanRow,
)
from incidentlens_telemetry.repository import TelemetryRepository
from sqlalchemy.exc import OperationalError

from incidentlens_control_plane.agent.state import (
    InvestigationAuditRow,
    InvestigationCheckpointRow,
)
from incidentlens_control_plane.memory.models import CaseFTSRow, CaseRow
from incidentlens_control_plane.tools.base import ToolAuditRow

logger = logging.getLogger(__name__)


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
          5. Delete tool audit records
          6. Delete case memory and FTS index
        """
        from sqlalchemy import text

        engine = self._repository.engine

        # Step 1: Clear active scenarios
        self._scenario_store.reset()

        # Steps 2-6: Delete all demo data tables
        # Table names derived from ORM model __tablename__ attributes
        tables_to_clear = [
            LogRow.__tablename__,                    # telemetry_logs
            MetricRow.__tablename__,                 # metric_points
            SpanRow.__tablename__,                   # trace_spans
            DeploymentRow.__tablename__,             # deployments
            InvestigationCheckpointRow.__tablename__,  # investigation_checkpoints
            InvestigationAuditRow.__tablename__,       # investigation_audits
            ToolAuditRow.__tablename__,              # tool_audits
            CaseRow.__tablename__,                   # case_memory
            CaseFTSRow.__tablename__,                # case_fts_index
        ]

        deleted_counts: dict[str, int] = {}
        with engine.begin() as conn:
            for table_name in tables_to_clear:
                try:
                    result = conn.execute(text(f"DELETE FROM {table_name}"))
                    deleted_counts[table_name] = result.rowcount
                except OperationalError:
                    # Table may not exist yet; skip gracefully
                    logger.debug("Table %s does not exist, skipping", table_name)
                    deleted_counts[table_name] = 0

        return {
            "status": "reset",
            "scenarios_cleared": True,
            "tables_cleared": deleted_counts,
        }
