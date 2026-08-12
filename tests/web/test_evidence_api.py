"""Tests for the evidence HTTP APIs."""

from __future__ import annotations

from datetime import UTC, datetime

from web.conftest import make_web_log_record


def test_create_evidence_from_log_records(client, runtime, registered_project) -> None:
    record = make_web_log_record(
        "ERROR token=[REDACTED_TOKEN]", now=datetime(2026, 8, 12, tzinfo=UTC)
    )
    runtime.log_store.append_batch((record,))

    response = client.post(
        "/api/evidence/from-log-records",
        json={
            "incident_id": "inc-1",
            "log_ids": ["log-web-1"],
            "created_by": "alice",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body[0]["incident_id"] == "inc-1"
    assert "abc123" not in response.text


def test_get_incident_evidence_lists_redacted_refs(client, runtime, registered_project) -> None:
    record = make_web_log_record(
        "WARN password=[REDACTED_PASSWORD]", now=datetime(2026, 8, 12, tzinfo=UTC)
    )
    evidence = runtime.evidence.create_from_log_record(
        record,
        incident_id="inc-1",
        created_by="alice",
        now=datetime(2026, 8, 12, tzinfo=UTC),
    )

    response = client.get("/api/incidents/inc-1/evidence?limit=10")

    assert response.status_code == 200
    assert response.json()[0]["evidence_ref_id"] == evidence.evidence_ref_id
    assert "hunter2" not in response.text
