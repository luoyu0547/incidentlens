"""IncidentLens shared service common code.

Provides:
  - Context propagation (X-Request-ID, X-Trace-ID)
  - Telemetry client for emitting events
"""

from incidentlens_service_common.context import (
    extract_context,
    generate_request_id,
    generate_trace_id,
    propagate_headers,
)
from incidentlens_service_common.telemetry_client import TelemetryClient

__all__ = [
    "TelemetryClient",
    "extract_context",
    "generate_request_id",
    "generate_trace_id",
    "propagate_headers",
]
