"""IncidentLens telemetry persistence and query layer.

Public API:
  - TelemetryRepository  — main entry point for recording and querying
  - create_engine        — create a SQLAlchemy engine with schema initialised
"""

from incidentlens_telemetry.database import create_engine
from incidentlens_telemetry.repository import TelemetryRepository

__all__ = ["TelemetryRepository", "create_engine"]
