"""Tests for the evidence HTTP APIs."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from incidentlens_control_plane.evidence.types import EvidenceKind, EvidenceRef

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


def test_create_evidence_from_log_record_with_long_correlation_key(
    client, runtime, registered_project
) -> None:
    """A derived correlation_key longer than the old 500-char cap must not 500."""
    now = datetime(2026, 8, 12, tzinfo=UTC)
    record = make_web_log_record(
        "ERROR token=[REDACTED_TOKEN]", now=now
    ).model_copy(
        update={
            "log_id": "log-long",
            "correlation_key": "x" * 2000,
            "normal_signal": "y" * 2000,
        }
    )
    runtime.log_store.append_batch((record,))

    response = client.post(
        "/api/evidence/from-log-records",
        json={
            "incident_id": "inc-1",
            "log_ids": ["log-long"],
            "created_by": "alice",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body[0]["correlation_key"] == "x" * 2000
    assert body[0]["normal_signal"] == "y" * 2000


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


def test_list_incident_evidence_filters_by_kind_and_run(
    client, runtime, registered_project
) -> None:
    now = datetime(2026, 8, 12, tzinfo=UTC)
    record = make_web_log_record("ERROR token=[REDACTED_TOKEN]", now=now)
    runtime.evidence.create_from_log_record(
        record,
        incident_id="inc-1",
        created_by="alice",
        now=now,
    )
    content = "restarting token=[REDACTED_TOKEN]"
    runtime.evidence.create(
        EvidenceRef(
            evidence_ref_id="ev-web-cmd",
            incident_id="inc-1",
            evidence_kind=EvidenceKind.COMMAND_OUTPUT,
            agent_run_id="run-1",
            project_id="payments",
            target_id="dev-a",
            service_name="payment-api",
            source_ref="host:dev-a",
            content_redacted=content,
            content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            redaction_summary={"token": 1},
            truncation=None,
            metadata={"command": "restart", "exit_code": "0"},
            created_at=now,
            created_by="alice",
        )
    )

    by_kind = client.get("/api/incidents/inc-1/evidence?kind=command_output")
    assert by_kind.status_code == 200
    body = by_kind.json()
    assert len(body) == 1
    assert body[0]["evidence_ref_id"] == "ev-web-cmd"
    assert body[0]["agent_run_id"] == "run-1"
    assert "abc" not in by_kind.text

    by_run = client.get("/api/incidents/inc-1/evidence?agent_run_id=run-1")
    assert by_run.status_code == 200
    assert len(by_run.json()) == 1
    assert by_run.json()[0]["evidence_ref_id"] == "ev-web-cmd"

    all_evidence = client.get("/api/incidents/inc-1/evidence?limit=10")
    assert all_evidence.status_code == 200
    assert len(all_evidence.json()) == 2

