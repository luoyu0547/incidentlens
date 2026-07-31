"""Tests for investigation export API — TDD RED phase.

Covers:
  - GET /api/investigations/{incident_id}/export returns versioned,
    redacted, downloadable JSON
  - Missing investigation returns 404
"""

from __future__ import annotations

import json


async def test_export_is_versioned_redacted_and_downloadable(
    export_client,
) -> None:
    """Export must include schema_version, redact secrets, and set download headers."""
    response = await export_client.get("/api/investigations/inc-api/export")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert "attachment;" in response.headers["content-disposition"]
    body = response.json()
    assert body["schema_version"] == "incidentlens.investigation-export.v1"
    payload = json.dumps(body)
    assert "super-secret" not in payload
    assert "Authorization" not in payload


async def test_export_missing_investigation_is_404(export_client) -> None:
    """Export for a non-existent investigation returns 404."""
    response = await export_client.get("/api/investigations/missing/export")
    assert response.status_code == 404
