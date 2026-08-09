"""InvestigationExportService — versioned, redacted investigation export.

Builds a JSON export payload for a completed investigation, including
the investigation state and audit trail.
Applies sensitive-data redaction and enforces a maximum payload size.

The export is suitable for download as a self-contained JSON file.
"""

from __future__ import annotations

import json
from typing import Any

from incidentlens_control_plane.agent.middleware import redact_sensitive_payload
from incidentlens_control_plane.agent.state import InvestigationAuditStore, InvestigationState

EXPORT_SCHEMA_VERSION = "incidentlens.investigation-export.v1"
MAX_EXPORT_BYTES = 2_000_000


class InvestigationExportNotFound(Exception):
    """Raised when the investigation does not exist for the given incident_id."""


class InvestigationExportTooLarge(Exception):
    """Raised when the export payload exceeds the maximum allowed size."""

    def __init__(self, size_bytes: int) -> None:
        self.size_bytes = size_bytes
        super().__init__(f"Export payload is {size_bytes} bytes, max is {MAX_EXPORT_BYTES}")


class InvestigationExportService:
    """Builds versioned, redacted JSON export payloads for investigations.

    Dependencies are injected via the constructor — no mutable globals.
    """

    def __init__(
        self,
        *,
        engine: Any,
        audit_store: InvestigationAuditStore,
    ) -> None:
        self._engine = engine
        self._audit_store = audit_store

    async def build_export(self, incident_id: str) -> dict[str, Any]:
        """Build a redacted export payload for the given investigation.

        Raises:
            InvestigationExportNotFound: if the investigation does not exist.
            InvestigationExportTooLarge: if the encoded payload exceeds MAX_EXPORT_BYTES.
        """
        state: InvestigationState | None = await self._engine.load(incident_id)
        if state is None:
            raise InvestigationExportNotFound(incident_id)

        # Gather audit trail
        audit = self._audit_store.list_for_incident(incident_id)

        payload: dict[str, Any] = {
            "schema_version": EXPORT_SCHEMA_VERSION,
            "investigation": state.model_dump(mode="json"),
            "audit": audit,
        }

        safe = redact_sensitive_payload(payload)

        # Enforce size gate
        encoded = json.dumps(safe, separators=(",", ":")).encode()
        if len(encoded) > MAX_EXPORT_BYTES:
            raise InvestigationExportTooLarge(len(encoded))

        return safe
