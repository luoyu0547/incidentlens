"""Tests for the log query and search HTTP APIs."""

from __future__ import annotations

from datetime import UTC, datetime

from web.conftest import make_web_log_record


def test_logs_query_rejects_unexpected_connection_fields(client, registered_project) -> None:
    response = client.post(
        "/api/logs/query",
        json={
            "project_id": "payments",
            "target_id": "dev-a",
            "service_name": "payment-api",
            "source_kind": "docker",
            "scope": "container",
            "source_ref": "payments-api-1",
            "host": "attacker.example.test",
            "ssh_user": "root",
        },
    )

    assert response.status_code == 422


def test_logs_query_rejects_unregistered_container(client, registered_project) -> None:
    response = client.post(
        "/api/logs/query",
        json={
            "project_id": "payments",
            "target_id": "dev-a",
            "service_name": "payment-api",
            "source_kind": "docker",
            "scope": "container",
            "source_ref": "not-registered",
            "tail_lines": 50,
            "persist": False,
            "create_evidence": False,
        },
    )

    assert response.status_code == 409


def test_logs_search_returns_persisted_redacted_records(
    client, runtime, registered_project
) -> None:
    now = datetime(2026, 8, 12, tzinfo=UTC)
    runtime.log_store.append_batch((make_web_log_record("ERROR token=[REDACTED_TOKEN]", now=now),))

    response = client.get(
        "/api/logs/search",
        params={"project_id": "payments", "text": "ERROR", "limit": 10},
    )

    assert response.status_code == 200
    assert response.json()[0]["message_redacted"] == "ERROR token=[REDACTED_TOKEN]"
    assert "abc123" not in response.text
